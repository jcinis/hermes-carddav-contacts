# ABOUTME: Prepare/apply orchestration for reviewed, revision-bound contact mutations.
# ABOUTME: Shared by the CLI and hermes_carddav_contacts.api; holds the profile lock while writing.

"""Reviewed, conditional contact mutations shared by the CLI and the public API.

One operation is prepared, then applied. Preparing reads the target fresh from
the server, binds the operation to one profile, one collection, one exact
record identity, and the revision that record carried at review time, and
stores the whole thing — including the before-image — privately under the
profile. Applying consumes that binding by its digest and does nothing else: it
never re-derives the target, never picks up a newer revision on its own, and
never edits a card built from the normalized local snapshot, which is a lossy
projection rather than a writable record.

Safety rules that hold for every verb:

- A create uses a no-overwrite precondition; an update and a delete use the
  reviewed revision as an `If-Match` precondition. A precondition failure is
  final: it needs a fresh review, never an automatic retry against whatever
  the record says now.
- Every applied write is proven by reading the exact record back and comparing
  it against what the operation intended; a delete is proven by an
  authoritative not-found.
- A timeout or a dropped connection is an unknown outcome, not a failure.
  The operation is left needing `reconcile_operation`, which decides what
  actually happened by reading the server, so a retry can never create a second
  contact or delete a record that was recreated in the meantime.
- A successful remote write invalidates the local generation before the
  refresh is attempted. If the refresh fails, the write is still reported as
  applied and the cache is reported stale — never as a failure that invites
  another mutation.

There is no approval platform here, no batching, and no merge policy: those
belong to a consumer.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import re
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

_SCRIPT_DIR = Path(__file__).parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import generations
import ids
import index
import profiles
import records
import schemas
import transport
import vcards

OPERATION_SCHEMA_VERSION = "carddav-operation/1.0"
RECEIPT_SCHEMA_VERSION = "carddav-receipt/1.0"
RESULT_SCHEMA_VERSION = "carddav-result/1.0"
RECORD_SCHEMA_VERSION = "carddav-record/1.0"

OPERATIONS_DIR_NAME = "operations"
RECEIPTS_DIR_NAME = "receipts"
CACHE_INVALID_NAME = profiles.CACHE_INVALID_NAME
# Private previews and receipts are retained for reconciliation, not forever.
MAX_RETAINED_OPERATIONS = 64

VERBS = ("create", "update", "replace", "delete")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class WriteRefused(Exception):
    """Base class for every refusal of a mutation; carries no private detail."""


class InvalidOperationRequest(WriteRefused):
    """The request, its target, or its change document is not acceptable."""


class OperationNotFound(WriteRefused):
    """No prepared operation with this identifier exists for this profile."""


class ContactNotFound(WriteRefused):
    """The addressed contact is not present in the selected collections."""


class RevisionConflict(WriteRefused):
    """The record moved on since it was reviewed; prepare the change again."""


class AlreadyExists(WriteRefused):
    """A no-overwrite create found a different record already in place."""


class VerificationFailed(WriteRefused):
    """The write was accepted but the record does not read back as intended."""


class UnknownOutcome(WriteRefused):
    """The outcome is unknown; reconcile before attempting anything else."""


class ReconciliationRequired(WriteRefused):
    """This operation has an unresolved outcome and must be reconciled first."""


class RemoteUnavailable(WriteRefused):
    """The server could not be reached or answered unusably; nothing was written."""


class ProfileBindingMismatch(WriteRefused):
    """The operation was reviewed against different profile settings than these."""


def _now() -> str:
    return datetime.fromtimestamp(int(time.time()), UTC).strftime(_TIMESTAMP_FORMAT)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8") + b"\n"


@contextmanager
def _locked(profile_name: str) -> Iterator[tuple[Path, dict[str, object], bytes]]:
    """Hold one profile's exclusive lock for a whole prepare, apply, or reconcile."""
    if not isinstance(profile_name, str) or not profiles.IDENTIFIER_PATTERN.fullmatch(
        profile_name
    ):
        raise InvalidOperationRequest
    profile_dir, profile_path, lock_path = profiles.resolve_profile_paths(profile_name)
    with lock_path.open() as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            profile_bytes = profile_path.read_bytes()
            yield profile_dir, profiles.read_profile(profile_path), profile_bytes
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _store(profile_dir: Path, name: str) -> Path:
    return profiles.make_private_dir(profile_dir / name)


def _prune(directory: Path, keep: int) -> None:
    """Keep the retained private set bounded, oldest first."""
    entries = sorted(directory.glob("*.json"), key=lambda path: path.lstat().st_mtime_ns)
    for path in entries[: max(0, len(entries) - keep)]:
        path.unlink(missing_ok=True)


def _write_document(path: Path, document: Mapping[str, object]) -> None:
    profiles.write_private_file(path, _canonical(document))


def _read_document(path: Path, keys: tuple[str, ...]) -> dict[str, object]:
    if not profiles.check_managed_path(path):
        raise OperationNotFound
    raw = path.read_bytes()
    try:
        value = schemas.parse_json(raw)
    except ValueError as exc:
        raise InvalidOperationRequest from exc
    if not isinstance(value, dict) or set(value) != set(keys):
        raise InvalidOperationRequest
    document = cast(dict[str, object], value)
    if raw != _canonical(document):
        raise InvalidOperationRequest
    return document


# The closed write contracts live in `schemas.py`, so the documents this module
# writes and the documents a consumer validates are the same contract.
_OPERATION_KEYS = schemas.OPERATION_KEYS
_RECEIPT_KEYS = schemas.RECEIPT_KEYS


def _operation_id(binding: Mapping[str, object]) -> str:
    return hashlib.sha256(
        _canonical({key: value for key, value in binding.items() if key != "operation_id"})
    ).hexdigest()


def _contact_of(raw: str | None, alias: str) -> dict[str, object] | None:
    """Project one raw card through the shipped reader, for review only."""
    if raw is None:
        return None
    try:
        return index.parse_vcard(raw, alias)
    except index.InvalidContact as exc:
        raise InvalidOperationRequest from exc


def _canonical_contact(raw: str, alias: str) -> bytes:
    """Canonical bytes of one card's projection, for read-back comparison."""
    contact = _contact_of(raw, alias)
    try:
        return schemas.canonical_source(
            {
                "schema_version": index.SOURCE_SCHEMA_VERSION,
                "account_namespace": "verify",
                "collections": [alias],
                "contacts": [contact],
            }
        )
    except ValueError as exc:
        raise VerificationFailed from exc


def _aliases(profile: Mapping[str, object]) -> list[str]:
    allowlist = profile.get("collection_allowlist")
    if not isinstance(allowlist, list):
        raise InvalidOperationRequest
    return [value for value in allowlist if isinstance(value, str)]


def _remote(action: Callable[[], object]) -> object:
    """Translate one record-transport outcome into this module's vocabulary."""
    try:
        return action()
    except records.RevisionMismatch as exc:
        raise RevisionConflict from exc
    except records.RecordAlreadyExists:
        raise
    except records.RecordNotFound as exc:
        raise ContactNotFound from exc
    except records.UnknownOutcome:
        raise
    except (records.UnsafeRemoteTarget, records.RecordTransportFailed) as exc:
        raise RemoteUnavailable from exc
    except transport.InvalidCredentials:
        raise


def _fresh_record(profile: Mapping[str, object], contact_id: object) -> records.Record:
    if not isinstance(contact_id, str) or profiles.CONTACT_ID_PATTERN.fullmatch(
        contact_id
    ) is None:
        raise InvalidOperationRequest
    return cast(
        records.Record,
        _remote(lambda: records.fetch_record(profile, _aliases(profile), contact_id)),
    )


def _prepared(
    profile_dir: Path,
    profile: Mapping[str, object],
    profile_bytes: bytes,
    profile_name: str,
    verb: str,
    alias: str,
    contact_id: str,
    href: str | None,
    base_revision: str | None,
    before_vcard: str | None,
    after_vcard: str | None,
) -> dict[str, object]:
    """Bind, store, and return one reviewable operation."""
    binding: dict[str, object] = {
        "operation_schema_version": OPERATION_SCHEMA_VERSION,
        "operation_id": "",
        "operation": verb,
        "profile": profile_name,
        "account_namespace": profile["account_namespace"],
        "collection_alias": alias,
        "contact_id": contact_id,
        "href": href,
        "base_revision": base_revision,
        "before_vcard": before_vcard,
        "after_vcard": after_vcard,
        "before_contact": _contact_of(before_vcard, alias),
        "after_contact": _contact_of(after_vcard, alias),
        "profile_generation_sha256": hashlib.sha256(profile_bytes).hexdigest(),
        "prepared_at": _now(),
    }
    binding["operation_id"] = _operation_id(binding)
    directory = _store(profile_dir, OPERATIONS_DIR_NAME)
    _write_document(directory / f"{binding['operation_id']}.json", binding)
    _prune(directory, MAX_RETAINED_OPERATIONS)
    return binding


def _checked_alias(profile: Mapping[str, object], alias: object) -> str:
    try:
        return records.check_collection_alias(profile, alias)
    except records.UnsafeRemoteTarget as exc:
        raise InvalidOperationRequest from exc


def read_record(profile_name: str, contact_id: str) -> dict[str, object]:
    """Read one contact fresh from the server, with its current revision."""
    with _locked(profile_name) as (_profile_dir, profile, _profile_bytes):
        return _record_document(profile_name, _fresh_record(profile, contact_id))


def _record_document(
    profile_name: str, record: records.Record
) -> dict[str, object]:
    return {
        "record_schema_version": RECORD_SCHEMA_VERSION,
        "profile": profile_name,
        "collection_alias": record.collection_alias,
        "contact_id": record.contact_id,
        "revision": record.revision,
        "raw_vcard": record.raw_vcard,
        "contact": _contact_of(record.raw_vcard, record.collection_alias),
    }


def read_collection(profile_name: str, collection_alias: str) -> list[dict[str, object]]:
    """Read every record of one allowlisted collection fresh, with revisions.

    A consumer that has to enumerate candidates needs authoritative records
    rather than the normalized local snapshot, which is a lossy projection.
    """
    with _locked(profile_name) as (_profile_dir, profile, _profile_bytes):
        alias = _checked_alias(profile, collection_alias)
        found = cast(
            list[records.Record],
            _remote(lambda: records.fetch_collection(profile, alias)),
        )
        return [_record_document(profile_name, record) for record in found]


def prepare_create(
    profile_name: str, collection_alias: str, changes: Mapping[str, object]
) -> dict[str, object]:
    """Prepare a create in one explicitly selected collection."""
    with _locked(profile_name) as (profile_dir, profile, profile_bytes):
        alias = _checked_alias(profile, collection_alias)
        try:
            uid = records.new_operation_uid()
            after = vcards.build_card(uid, changes)
        except vcards.InvalidChange as exc:
            raise InvalidOperationRequest from exc
        return _prepared(
            profile_dir, profile, profile_bytes, profile_name, "create", alias,
            ids.contact_id(uid), None, None, None, after,
        )


def _prepare_existing(
    profile_name: str, contact_id: str, verb: str, build: Callable[[str], str | None]
) -> dict[str, object]:
    with _locked(profile_name) as (profile_dir, profile, profile_bytes):
        record = _fresh_record(profile, contact_id)
        alias = _checked_alias(profile, record.collection_alias)
        try:
            after = build(record.raw_vcard)
        except vcards.InvalidChange as exc:
            raise InvalidOperationRequest from exc
        return _prepared(
            profile_dir, profile, profile_bytes, profile_name, verb, alias,
            record.contact_id, record.href, record.revision, record.raw_vcard, after,
        )


def prepare_update(
    profile_name: str, contact_id: str, changes: Mapping[str, object]
) -> dict[str, object]:
    """Prepare a common-field edit of one identified contact."""
    return _prepare_existing(
        profile_name, contact_id, "update", lambda raw: vcards.apply_changes(raw, changes)
    )


def prepare_replace(
    profile_name: str, contact_id: str, raw_vcard: str
) -> dict[str, object]:
    """Prepare a validated, lossless whole-card replacement of one contact.

    The caller supplies the complete card, so nothing is merged or guessed
    here. The replacement must be exactly one vCard that keeps the target's
    own identity and that this skill can read back.
    """

    def _build(current: str) -> str:
        vcards.validate_replacement(current, raw_vcard)
        return raw_vcard

    return _prepare_existing(profile_name, contact_id, "replace", _build)


def prepare_delete(profile_name: str, contact_id: str) -> dict[str, object]:
    """Prepare the removal of one specifically identified contact."""
    return _prepare_existing(profile_name, contact_id, "delete", lambda _raw: None)


def _load_operation(profile_dir: Path, operation_id: object) -> dict[str, object]:
    if not isinstance(operation_id, str) or _DIGEST_RE.fullmatch(operation_id) is None:
        raise InvalidOperationRequest
    directory = _store(profile_dir, OPERATIONS_DIR_NAME)
    operation = _read_document(directory / f"{operation_id}.json", _OPERATION_KEYS)
    try:
        # The same closed contract a consumer validates, enforced on load.
        schemas.validate_write_document(operation)
    except ValueError as exc:
        raise InvalidOperationRequest from exc
    if (
        operation["operation_schema_version"] != OPERATION_SCHEMA_VERSION
        or operation["operation"] not in VERBS
        or operation["operation_id"] != operation_id
        or _operation_id(operation) != operation_id
    ):
        # The stored preview must still be exactly the reviewed binding.
        raise InvalidOperationRequest
    return operation


def _check_profile_binding(
    operation: Mapping[str, object], profile_name: str, profile_bytes: bytes
) -> None:
    """Refuse an operation reviewed against any other profile configuration.

    The operation records the exact canonical `profile.json` bytes it was
    prepared against. If the profile has since been changed or recreated —
    different namespace, different server, different collections — the review
    no longer describes what would happen, so it is refused rather than
    silently re-aimed at the new configuration.
    """
    if operation["profile"] != profile_name:
        raise InvalidOperationRequest
    if operation["profile_generation_sha256"] != hashlib.sha256(profile_bytes).hexdigest():
        raise ProfileBindingMismatch


def _receipt_path(profile_dir: Path, operation_id: str) -> Path:
    return _store(profile_dir, RECEIPTS_DIR_NAME) / f"{operation_id}.json"


def _load_receipt(
    profile_dir: Path, operation: Mapping[str, object]
) -> dict[str, object] | None:
    """Load the receipt for one operation, or refuse anything that is not it.

    A receipt is private local state, not evidence in itself: it is validated
    against the closed contract and must agree with the operation it claims to
    describe. Whether the recorded outcome is *true* is decided by reading the
    server, never by trusting this file.
    """
    operation_id = cast(str, operation["operation_id"])
    path = _receipt_path(profile_dir, operation_id)
    if not profiles.check_managed_path(path):
        return None
    receipt = _read_document(path, _RECEIPT_KEYS)
    try:
        schemas.validate_write_document(receipt)
    except ValueError as exc:
        raise InvalidOperationRequest from exc
    bound = ("operation_id", "operation", "profile", "collection_alias", "contact_id")
    if any(receipt[key] != operation[key] for key in bound):
        raise InvalidOperationRequest
    return receipt


def _write_receipt(
    profile_dir: Path,
    operation: Mapping[str, object],
    outcome: str,
    revision: str | None,
    attempted_at: str,
) -> dict[str, object]:
    receipt: dict[str, object] = {
        "receipt_schema_version": RECEIPT_SCHEMA_VERSION,
        "operation_id": operation["operation_id"],
        "operation": operation["operation"],
        "profile": operation["profile"],
        "collection_alias": operation["collection_alias"],
        "contact_id": operation["contact_id"],
        "outcome": outcome,
        "result_revision": revision,
        "attempted_at": attempted_at,
        "completed_at": None if outcome == "unknown" else _now(),
    }
    directory = _store(profile_dir, RECEIPTS_DIR_NAME)
    _write_document(directory / f"{operation['operation_id']}.json", receipt)
    _prune(directory, MAX_RETAINED_OPERATIONS)
    return receipt


_invalidate_cache = profiles.invalidate_cache
cache_is_invalid = profiles.cache_is_invalid
clear_cache_invalid = profiles.clear_cache_invalid


def _has_unresolved_operation(profile_dir: Path) -> bool:
    """Is any operation still unresolved? Anything unreadable counts as yes."""
    directory = _store(profile_dir, RECEIPTS_DIR_NAME)
    for path in directory.glob("*.json"):
        try:
            receipt = schemas.parse_json(path.read_bytes())
        except (OSError, ValueError):
            return True
        if not isinstance(receipt, dict) or receipt.get("outcome") == "unknown":
            return True
    return False


def _release_refused(
    profile_dir: Path, operation_id: str, *, marker_existed: bool
) -> None:
    """Undo the pre-dispatch state after the server refused the request itself.

    A definitive refusal is positive evidence that nothing was applied, so the
    operation becomes cleanly retryable again. The conservative cache marker is
    withdrawn only if this call is what set it and no other operation is still
    unresolved; otherwise it stays, because it is not this call's to clear.
    """
    _receipt_path(profile_dir, operation_id).unlink(missing_ok=True)
    if not marker_existed and not _has_unresolved_operation(profile_dir):
        clear_cache_invalid(profile_dir)


def _refresh(
    profile_dir: Path, profile: Mapping[str, object], profile_bytes: bytes
) -> tuple[str, str | None]:
    """Re-sync and republish so ordinary reads stop answering from stale data.

    Remote success is already final by the time this runs, so a refresh failure
    is reported as a stale cache, never as a failed write.
    """
    try:
        transport.run_operation("sync", profile, profile_dir)
        generation = generations.publish(profile_dir, profile, profile_bytes)
    except Exception:  # noqa: BLE001 - any refresh failure leaves the marker in place
        return "stale", None
    clear_cache_invalid(profile_dir)
    return "refreshed", generation


def _result(
    operation: Mapping[str, object],
    outcome: str,
    remote_write: bool,
    revision: str | None,
    local_cache: str,
    generation: str | None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "result_schema_version": RESULT_SCHEMA_VERSION,
        "operation_id": operation["operation_id"],
        "operation": operation["operation"],
        "profile": operation["profile"],
        "collection_alias": operation["collection_alias"],
        "contact_id": operation["contact_id"],
        "outcome": outcome,
        "remote_write": remote_write,
        "result_revision": revision,
        "verified": True,
        "local_cache": local_cache,
        "generation": generation,
    }
    # Never hand a consumer a document this program would itself reject.
    schemas.validate_write_document(result)
    return result


def _prove_record(
    expected: str, alias: str, contact_id: str, found: records.Record
) -> None:
    """Prove one remote record really is the card it is supposed to be.

    Two checks, because one is not enough: the modeled projection must match,
    and every raw logical line of the expected card must still be present. The
    second is what catches a server that accepted a write and quietly dropped a
    photo or an unknown extension the projection does not model.

    Every path that concludes something about a remote record — the direct
    read-back, repeating a settled receipt, and reconciliation — uses exactly
    this function, so none of them can settle on the lossy projection alone.
    """
    if found.contact_id != contact_id:
        raise VerificationFailed
    if _canonical_contact(found.raw_vcard, alias) != _canonical_contact(expected, alias):
        raise VerificationFailed
    try:
        vcards.check_round_trip(expected, found.raw_vcard)
    except vcards.InvalidChange as exc:
        raise VerificationFailed from exc


def _verify_written(operation: Mapping[str, object], written: records.Record) -> None:
    """Prove the record on the server is what the operation intended, in full."""
    _prove_record(
        cast(str, operation["after_vcard"]),
        cast(str, operation["collection_alias"]),
        cast(str, operation["contact_id"]),
        written,
    )


def _apply_mutation(
    profile: Mapping[str, object], operation: Mapping[str, object]
) -> records.Record | None:
    verb = operation["operation"]
    alias = cast(str, operation["collection_alias"])
    if verb == "create":
        return cast(
            records.Record,
            _remote(lambda: records.create_record(
                profile, alias, cast(str, operation["after_vcard"]))),
        )
    href = cast(str, operation["href"])
    revision = cast(str, operation["base_revision"])
    if verb == "delete":
        _remote(lambda: records.delete_record(profile, alias, href, revision))
        return None
    return cast(
        records.Record,
        _remote(lambda: records.update_record(
            profile, alias, href, cast(str, operation["after_vcard"]), revision)),
    )


def _settle(
    profile_dir: Path,
    profile: Mapping[str, object],
    profile_bytes: bytes,
    operation: Mapping[str, object],
    outcome: str,
    revision: str | None,
    attempted_at: str,
    remote_write: bool,
) -> dict[str, object]:
    """Record the outcome, then make the local cache honest about it."""
    _invalidate_cache(profile_dir)
    _write_receipt(profile_dir, operation, outcome, revision, attempted_at)
    local_cache, generation = _refresh(profile_dir, profile, profile_bytes)
    return _result(operation, outcome, remote_write, revision, local_cache, generation)


def _confirm_against_server(
    profile: Mapping[str, object], operation: Mapping[str, object]
) -> None:
    """Re-prove a recorded success by reading the server, not the receipt.

    A receipt is unauthenticated local state. A corrupted, stale, or forged one
    could otherwise make this program report an applied, verified mutation it
    never performed, so a settled outcome is only ever repeated back after the
    server itself confirms it.
    """
    alias = cast(str, operation["collection_alias"])
    contact_id = cast(str, operation["contact_id"])
    found = cast(
        records.Record | None,
        _remote(lambda: records.probe_contact(profile, alias, contact_id)),
    )
    if operation["operation"] == "delete":
        if found is not None:
            raise VerificationFailed
        return
    if found is None:
        raise VerificationFailed
    _verify_written(operation, found)


def apply_operation(profile_name: str, operation_id: str) -> dict[str, object]:
    """Apply exactly one prepared operation, or refuse without touching anything."""
    with _locked(profile_name) as (profile_dir, profile, profile_bytes):
        operation = _load_operation(profile_dir, operation_id)
        _check_profile_binding(operation, profile_name, profile_bytes)
        receipt = _load_receipt(profile_dir, operation)
        if receipt is not None:
            outcome = cast(str, receipt["outcome"])
            if outcome == "unknown":
                raise ReconciliationRequired
            if outcome not in ("applied", "already_applied"):
                # `not_applied` is removed when it is decided; retaining one
                # means the private state disagrees with itself.
                raise InvalidOperationRequest
            # Settled: confirm it is still true remotely, then repeat it back.
            _confirm_against_server(profile, operation)
            return _result(
                operation, outcome, False, cast(str | None, receipt["result_revision"]),
                "stale" if cache_is_invalid(profile_dir) else "refreshed", None,
            )
        attempted_at = _now()
        # Both of these happen before the request leaves, and a failure of
        # either aborts before anything is dispatched. The cache marker goes
        # first: from the moment the request is in flight the published
        # generation may have been outrun, so no local read may claim to be
        # current until a sync authoritatively republishes it. The receipt
        # follows, so an interrupted apply is always discoverable.
        marker_existed = cache_is_invalid(profile_dir)
        _invalidate_cache(profile_dir)
        _write_receipt(profile_dir, operation, "unknown", None, attempted_at)
        try:
            written = _apply_mutation(profile, operation)
        except records.UnknownOutcome as exc:
            raise UnknownOutcome from exc
        except records.RecordAlreadyExists as exc:
            _release_refused(profile_dir, operation_id, marker_existed=marker_existed)
            raise AlreadyExists from exc
        except WriteRefused:
            # The server refused the request itself, so the reviewed operation
            # stays cleanly retryable.
            _release_refused(profile_dir, operation_id, marker_existed=marker_existed)
            raise
        if written is not None:
            # The write is already final here. A failed proof is an unresolved
            # operation to reconcile, never a clean retry, so the unknown
            # receipt written above is deliberately left in place.
            _verify_written(operation, written)
        revision = None if written is None else written.revision
        return _settle(
            profile_dir, profile, profile_bytes, operation, "applied", revision,
            attempted_at, True,
        )


def _reconciled_outcome(
    profile: Mapping[str, object], operation: Mapping[str, object]
) -> tuple[str, str | None]:
    """Decide what actually happened by reading the server, never by guessing."""
    alias = cast(str, operation["collection_alias"])
    contact_id = cast(str, operation["contact_id"])
    found = cast(
        records.Record | None,
        _remote(lambda: records.probe_contact(profile, alias, contact_id)),
    )
    verb = operation["operation"]
    if verb == "delete":
        if found is None:
            return "already_applied", None
        if found.revision == operation["base_revision"]:
            # The reviewed revision is still reported, so nothing landed — but
            # prove the record really is the before-image rather than trusting
            # the validator token alone. A server with a stale or weak `ETag`
            # can report the reviewed revision over a record that has moved on,
            # and concluding `not_applied` from the token would make the
            # reviewed delete applicable again against a record nobody
            # reviewed. This is the same proof the non-delete branch uses.
            _prove_record(cast(str, operation["before_vcard"]), alias, contact_id, found)
            return "not_applied", None
        raise RevisionConflict
    if found is None:
        if verb == "create":
            return "not_applied", None
        raise ContactNotFound
    contact_id = cast(str, operation["contact_id"])
    try:
        # The same complete proof direct apply uses: a projection match alone
        # must never settle an operation whose raw lines were lost.
        _prove_record(cast(str, operation["after_vcard"]), alias, contact_id, found)
    except VerificationFailed:
        if verb == "create" or found.revision != operation["base_revision"]:
            # Something is there that is neither the reviewed record nor the
            # intended one; only a fresh review can say what to do about it.
            raise
        # The reviewed revision is still in place, so nothing landed — but
        # prove the card really is the before-image rather than trusting the
        # validator token alone.
        _prove_record(cast(str, operation["before_vcard"]), alias, contact_id, found)
        return "not_applied", None
    return "already_applied", found.revision


def reconcile_operation(profile_name: str, operation_id: str) -> dict[str, object]:
    """Resolve one unresolved operation against the server's actual state."""
    with _locked(profile_name) as (profile_dir, profile, profile_bytes):
        operation = _load_operation(profile_dir, operation_id)
        _check_profile_binding(operation, profile_name, profile_bytes)
        receipt = _load_receipt(profile_dir, operation)
        if receipt is None or receipt["outcome"] != "unknown":
            raise InvalidOperationRequest
        outcome, revision = _reconciled_outcome(profile, operation)
        if outcome == "not_applied":
            # Nothing landed, so the reviewed binding is cleanly applicable again.
            _receipt_path(profile_dir, operation_id).unlink(missing_ok=True)
            local_cache = "stale" if cache_is_invalid(profile_dir) else "refreshed"
            return _result(operation, outcome, False, None, local_cache, None)
        return _settle(
            profile_dir, profile, profile_bytes, operation, outcome, revision,
            cast(str, receipt["attempted_at"]), False,
        )
