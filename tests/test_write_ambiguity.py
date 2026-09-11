# ABOUTME: Regressions for the re-review: post-write read-back ambiguity, cache marker, reconcile proof.
# ABOUTME: Drives the real records/writes seams with synthetic storage; no server and no credentials.

"""Re-review regressions: what happens *after* a mutation request succeeds.

Three classes of bug, each proven at the seam that actually decides:

1. once `upload`/`update`/`delete` has returned success, **every** unusable
   read-back — 401, 404, connector failure, timeout, malformed card or ETag —
   is an unknown outcome that keeps the operation unresolved. Only a refusal of
   the mutation request itself may be a clean retry. These tests patch the
   storage seam, not the public `records` functions, so the classification code
   under test is the real one;
2. the cache is invalidated *before* dispatch, and an unknown or
   verification-failed outcome leaves that marker in place, so a cached read
   never reports `cache_invalidated: false` over a possibly outrun generation;
3. reconciliation settles `already_applied` only on the same complete proof
   direct apply uses, raw vCard lines included.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from tests import support_reads as support

if str(support.SCRIPTS) not in sys.path:
    sys.path.insert(0, str(support.SCRIPTS))

import generations
import profiles
import records
import transport
import writes
from vdirsyncer import exceptions as dav_exceptions  # type: ignore[import-untyped]

cli = support.load("cli")
ids = support.load("ids")

UID = "hermes-44444444-4444-4444-8444-444444444444"
CONTACT_ID = ids.contact_id(UID)
ALIAS = "contacts-a"
COLLECTION_PATH = f"/dav/{ALIAS}/"
HREF = f"{COLLECTION_PATH}one.vcf"
CHANGES: dict[str, object] = {
    "change_schema_version": "carddav-change/1.0",
    "set": {"display": "Example Person"},
    "clear": [],
    "replace": {},
}
RICH_CARD = (
    "BEGIN:VCARD\r\n"
    "VERSION:3.0\r\n"
    f"UID:{UID}\r\n"
    "FN:Example Person\r\n"
    "CATEGORIES:example-category\r\n"
    "PHOTO;ENCODING=b;TYPE=PNG:aGVsbG8=\r\n"
    "X-EXAMPLE-CUSTOM:keep-me\r\n"
    "END:VCARD\r\n"
)


def _response_error(status: int) -> aiohttp.ClientResponseError:
    return aiohttp.ClientResponseError(
        request_info=None,  # type: ignore[arg-type]
        history=(),
        status=status,
        message=f"synthetic {status}",
    )


def _connector_error() -> aiohttp.ClientConnectorError:
    return aiohttp.ClientConnectorError(
        connection_key=None,  # type: ignore[arg-type]
        os_error=OSError("synthetic connect failure"),
    )


# Every one of these is raised *after* the mutation request returned success.
READ_BACK_FAILURES = {
    "http-401": _response_error(401),
    "http-403": _response_error(403),
    "http-404": dav_exceptions.NotFoundError("synthetic 404"),
    "http-500": _response_error(500),
    "connector": _connector_error(),
    "timeout": TimeoutError("synthetic read-back timeout"),
    "malformed-card": ValueError("unparsable read-back"),
}


class _Item:
    def __init__(self, raw: str) -> None:
        self.raw = raw


class _Storage:
    """A synthetic CardDAV storage: the mutation succeeds, the read-back may not."""

    def __init__(
        self,
        read_back_failure: BaseException | None = None,
        mutation_failure: BaseException | None = None,
        read_back_card: str | None = None,
    ) -> None:
        self.read_back_failure = read_back_failure
        self.mutation_failure = mutation_failure
        self.read_back_card = read_back_card
        self.mutations: list[str] = []

    def _mutate(self, label: str) -> None:
        if self.mutation_failure is not None:
            raise self.mutation_failure
        self.mutations.append(label)

    async def upload(self, item: object) -> tuple[str, str]:
        self._mutate("upload")
        return HREF, '"etag-created"'

    async def update(self, href: str, item: object, etag: str) -> str:
        self._mutate("update")
        return '"etag-updated"'

    async def delete(self, href: str, etag: str) -> None:
        self._mutate("delete")

    async def get(self, href: str) -> tuple[object, str]:
        if self.read_back_failure is not None:
            raise self.read_back_failure
        return _Item(self.read_back_card or RICH_CARD), '"etag-created"'


@pytest.fixture
def storage_seam(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Patch only the collection-resolution seam, leaving all classification real."""
    holder: dict[str, _Storage] = {}

    async def _storage(profile: object, alias: str, connector: object) -> tuple[Any, str]:
        return holder["storage"], COLLECTION_PATH

    monkeypatch.setattr(records, "_storage", _storage)
    monkeypatch.setenv("CARDDAV_USERNAME", "fixture-user")
    monkeypatch.setenv("CARDDAV_PASSWORD", "synthetic-test-password")
    return holder


PROFILE: dict[str, object] = {
    "server_url": "https://carddav.example.invalid/",
    "collection_allowlist": [ALIAS],
    "account_namespace": "example",
}


# --- Blocker 1: read-back failures are never a clean retry ------------------


def _read_back_cases() -> list[tuple[str, str]]:
    """Every (verb, failure) pair where the read-back genuinely failed.

    A `404` on a delete's confirmation read is the one exception: there it is
    the authoritative proof that the record is gone, not a failed read, so it
    is covered by its own positive test below.
    """
    return [
        (verb, label)
        for verb in ("create", "update", "delete")
        for label in sorted(READ_BACK_FAILURES)
        if not (verb == "delete" and label == "http-404")
    ]


@pytest.mark.parametrize(("verb", "label"), _read_back_cases())
def test_every_failed_read_back_after_an_accepted_write_is_unknown(
    storage_seam: Any, verb: str, label: str
) -> None:
    storage = _Storage(read_back_failure=READ_BACK_FAILURES[label])
    storage_seam["storage"] = storage

    with pytest.raises(records.UnknownOutcome):
        if verb == "create":
            records.create_record(PROFILE, ALIAS, RICH_CARD)
        elif verb == "update":
            records.update_record(PROFILE, ALIAS, HREF, RICH_CARD, '"etag-1"')
        else:
            records.delete_record(PROFILE, ALIAS, HREF, '"etag-1"')

    # The mutation really was dispatched and accepted before the read-back failed.
    assert storage.mutations == [
        {"create": "upload", "update": "update", "delete": "delete"}[verb]
    ]


def test_a_delete_is_only_confirmed_by_an_authoritative_not_found(
    storage_seam: Any
) -> None:
    """The one read-back "failure" that is really proof: 404 after a delete."""
    storage = _Storage(read_back_failure=dav_exceptions.NotFoundError("synthetic 404"))
    storage_seam["storage"] = storage

    records.delete_record(PROFILE, ALIAS, HREF, '"etag-1"')

    assert storage.mutations == ["delete"]


def test_a_delete_whose_record_is_still_present_is_unknown(storage_seam: Any) -> None:
    # The delete was accepted but the record reads back as still there.
    storage_seam["storage"] = _Storage()
    with pytest.raises(records.UnknownOutcome):
        records.delete_record(PROFILE, ALIAS, HREF, '"etag-1"')


@pytest.mark.parametrize("verb", ["create", "update", "delete"])
def test_the_mutation_request_itself_may_still_be_a_definitive_refusal(
    storage_seam: Any, verb: str
) -> None:
    """The clean-retry path survives, but only for the request, not the proof."""
    storage = _Storage(mutation_failure=_response_error(401))
    storage_seam["storage"] = storage

    with pytest.raises(records.RecordTransportFailed):
        if verb == "create":
            records.create_record(PROFILE, ALIAS, RICH_CARD)
        elif verb == "update":
            records.update_record(PROFILE, ALIAS, HREF, RICH_CARD, '"etag-1"')
        else:
            records.delete_record(PROFILE, ALIAS, HREF, '"etag-1"')

    assert storage.mutations == []


def test_a_malformed_read_back_etag_is_unknown(storage_seam: Any) -> None:
    class _NoEtag(_Storage):
        async def get(self, href: str) -> tuple[object, str]:
            return _Item(RICH_CARD), ""  # no usable validator token

    storage_seam["storage"] = _NoEtag()
    with pytest.raises(records.UnknownOutcome):
        records.create_record(PROFILE, ALIAS, RICH_CARD)


# --- The same, through the public apply path --------------------------------


@pytest.fixture
def profile_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for key in support.ENVIRONMENT_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("CARDDAV_USERNAME", "fixture-user")
    monkeypatch.setenv("CARDDAV_PASSWORD", "synthetic-test-password")
    monkeypatch.setattr(records, "new_operation_uid", lambda: UID)
    monkeypatch.setattr(transport, "run_operation", lambda *a, **k: None)
    monkeypatch.setattr(generations, "publish", lambda *a, **k: "0" * 64)
    return support.setup_profile(cli, tmp_path)


def _receipt(profile_dir: Path, operation_id: str) -> Path:
    return profile_dir / "receipts" / f"{operation_id}.json"


def test_apply_with_an_accepted_upload_and_a_read_back_404_never_replays(
    profile_dir: Path, storage_seam: Any
) -> None:
    storage = _Storage(read_back_failure=dav_exceptions.NotFoundError("synthetic 404"))
    storage_seam["storage"] = storage
    operation = writes.prepare_create("demo", ALIAS, CHANGES)
    operation_id = str(operation["operation_id"])

    with pytest.raises(writes.UnknownOutcome):
        writes.apply_operation("demo", operation_id)

    assert storage.mutations == ["upload"]
    assert json.loads(_receipt(profile_dir, operation_id).read_bytes())["outcome"] == "unknown"

    with pytest.raises(writes.ReconciliationRequired):
        writes.apply_operation("demo", operation_id)
    assert storage.mutations == ["upload"]


# --- Blocker 2: the cache marker ------------------------------------------


def _publish_generation(profile_dir: Path) -> str:
    support.write_mirror(profile_dir, {"one.vcf": support.card("resident", "Example One")})
    return support.publish(profile_dir)


def test_the_cache_is_invalidated_before_the_mutation_is_dispatched(
    profile_dir: Path, storage_seam: Any
) -> None:
    _publish_generation(profile_dir)
    marker = profile_dir / profiles.CACHE_INVALID_NAME
    seen: list[bool] = []

    class _Checking(_Storage):
        async def upload(self, item: object) -> tuple[str, str]:
            # The marker must already exist by the time the request goes out.
            seen.append(marker.exists())
            return await super().upload(item)

    storage_seam["storage"] = _Checking(
        read_back_failure=dav_exceptions.NotFoundError("synthetic 404"))
    operation = writes.prepare_create("demo", ALIAS, CHANGES)

    with pytest.raises(writes.UnknownOutcome):
        writes.apply_operation("demo", str(operation["operation_id"]))

    assert seen == [True]
    assert marker.exists()


@pytest.mark.parametrize("failure", ["unknown", "verification"])
def test_an_unresolved_write_leaves_local_reads_saying_the_cache_may_be_stale(
    profile_dir: Path, storage_seam: Any, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    _publish_generation(profile_dir)
    monkeypatch.setattr(
        records, "fetch_record",
        lambda profile, aliases, contact_id: records.Record(
            contact_id=CONTACT_ID, collection_alias=ALIAS, href=HREF,
            revision='"etag-1"', raw_vcard=RICH_CARD),
    )
    operation = writes.prepare_update(
        "demo", CONTACT_ID, {**CHANGES, "set": {"display": "Renamed Person"}})
    intended = str(operation["after_vcard"])

    storage = _Storage()
    if failure == "unknown":
        storage.read_back_failure = dav_exceptions.NotFoundError("synthetic 404")
        expected: type[Exception] = writes.UnknownOutcome
    else:
        # Accepted, but the server returned a card missing the private extension.
        storage.read_back_card = intended.replace("X-EXAMPLE-CUSTOM:keep-me\r\n", "")
        assert storage.read_back_card != intended
        expected = writes.VerificationFailed
    storage_seam["storage"] = storage
    capsys.readouterr()

    with pytest.raises(expected):
        writes.apply_operation("demo", str(operation["operation_id"]))

    assert (profile_dir / profiles.CACHE_INVALID_NAME).exists()
    assert cli.main(["status", "--profile", "demo", "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["current_generation"] is not None
    assert status["cache_invalidated"] is True


def test_no_mutation_is_dispatched_when_the_cache_marker_cannot_be_written(
    profile_dir: Path, storage_seam: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_generation(profile_dir)
    storage = _Storage()
    storage_seam["storage"] = storage
    operation = writes.prepare_create("demo", ALIAS, CHANGES)

    real_write = profiles.write_private_file

    def _fail(path: Path, payload: bytes) -> None:
        if path.name == profiles.CACHE_INVALID_NAME:
            raise OSError("synthetic durability failure")
        real_write(path, payload)

    monkeypatch.setattr(profiles, "write_private_file", _fail)

    with pytest.raises(OSError):
        writes.apply_operation("demo", str(operation["operation_id"]))

    assert storage.mutations == []


# --- Blocker 3: reconciliation uses the full proof --------------------------


def test_reconcile_will_not_settle_a_write_that_lost_unmodeled_lines(
    profile_dir: Path, storage_seam: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The re-review's probe: raw-line loss must never reconcile to a success."""
    monkeypatch.setattr(
        records, "fetch_record",
        lambda profile, aliases, contact_id: records.Record(
            contact_id=CONTACT_ID, collection_alias=ALIAS, href=HREF,
            revision='"etag-1"', raw_vcard=RICH_CARD),
    )
    operation = writes.prepare_update(
        "demo", CONTACT_ID, {**CHANGES, "set": {"display": "Renamed Person"}})
    operation_id = str(operation["operation_id"])
    intended = str(operation["after_vcard"])
    assert "X-EXAMPLE-CUSTOM:keep-me" in intended and "PHOTO" in intended

    # The write is accepted, the read-back fails, so the operation is unresolved.
    storage_seam["storage"] = _Storage(
        read_back_failure=dav_exceptions.NotFoundError("synthetic 404"))
    with pytest.raises(writes.UnknownOutcome):
        writes.apply_operation("demo", operation_id)

    # The record is there, but the server dropped the photo and the extension.
    stripped = (
        intended.replace("PHOTO;ENCODING=b;TYPE=PNG:aGVsbG8=\r\n", "")
        .replace("X-EXAMPLE-CUSTOM:keep-me\r\n", "")
    )
    assert "X-EXAMPLE-CUSTOM" not in stripped
    monkeypatch.setattr(
        records, "probe_contact",
        lambda profile, alias, contact_id: records.Record(
            contact_id=CONTACT_ID, collection_alias=ALIAS, href=HREF,
            revision='"etag-2"', raw_vcard=stripped),
    )

    with pytest.raises(writes.VerificationFailed):
        writes.reconcile_operation("demo", operation_id)

    # Still unresolved: it was never settled as already_applied.
    receipt = json.loads(_receipt(profile_dir, operation_id).read_bytes())
    assert receipt["outcome"] == "unknown"


def test_reconcile_still_settles_an_honest_matching_write(
    profile_dir: Path, storage_seam: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage_seam["storage"] = _Storage(
        read_back_failure=dav_exceptions.NotFoundError("synthetic 404"))
    operation = writes.prepare_create("demo", ALIAS, CHANGES)
    operation_id = str(operation["operation_id"])
    with pytest.raises(writes.UnknownOutcome):
        writes.apply_operation("demo", operation_id)

    def _probe(profile: object, alias: object, contact_id: str) -> Any:
        return records.Record(
            contact_id=CONTACT_ID, collection_alias=ALIAS, href=HREF,
            revision='"etag-2"', raw_vcard=str(operation["after_vcard"]),
        )

    monkeypatch.setattr(records, "probe_contact", _probe)
    result = writes.reconcile_operation("demo", operation_id)

    assert result["outcome"] == "already_applied"
    assert result["result_revision"] == '"etag-2"'


def _unresolved_delete(
    storage_seam: Any, monkeypatch: pytest.MonkeyPatch
) -> dict[str, object]:
    """Prepare a delete of the rich card and leave its outcome unknown."""
    monkeypatch.setattr(
        records, "fetch_record",
        lambda profile, aliases, contact_id: records.Record(
            contact_id=CONTACT_ID, collection_alias=ALIAS, href=HREF,
            revision='"etag-1"', raw_vcard=RICH_CARD),
    )
    operation = writes.prepare_delete("demo", CONTACT_ID)
    assert operation["before_vcard"] == RICH_CARD
    # The delete is accepted, but the confirmation read is unusable.
    storage_seam["storage"] = _Storage(read_back_failure=_response_error(401))
    with pytest.raises(writes.UnknownOutcome):
        writes.apply_operation("demo", str(operation["operation_id"]))
    return operation


def test_delete_reconciliation_proves_the_before_image_not_just_the_revision(
    profile_dir: Path, storage_seam: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A matching validator token is not proof that the record is unchanged.

    A server with a stale or weak `ETag` can report the reviewed revision over
    a record whose content has moved on. Concluding `not_applied` from the
    token alone would make the reviewed delete cleanly applicable again against
    a record nobody reviewed.
    """
    operation = _unresolved_delete(storage_seam, monkeypatch)
    operation_id = str(operation["operation_id"])
    changed = (
        RICH_CARD.replace("FN:Example Person", "FN:Someone Else")
        .replace("PHOTO;ENCODING=b;TYPE=PNG:aGVsbG8=\r\n", "")
        .replace("X-EXAMPLE-CUSTOM:keep-me\r\n", "")
    )
    monkeypatch.setattr(
        records, "probe_contact",
        lambda profile, alias, contact_id: records.Record(
            contact_id=CONTACT_ID, collection_alias=ALIAS, href=HREF,
            revision='"etag-1"', raw_vcard=changed),  # the reviewed revision
    )

    with pytest.raises(writes.VerificationFailed):
        writes.reconcile_operation("demo", operation_id)

    # Still unresolved, so the delete cannot simply be re-applied.
    assert json.loads(_receipt(profile_dir, operation_id).read_bytes())["outcome"] == "unknown"


def test_delete_reconciliation_settles_not_applied_on_an_intact_before_image(
    profile_dir: Path, storage_seam: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    operation = _unresolved_delete(storage_seam, monkeypatch)
    operation_id = str(operation["operation_id"])
    monkeypatch.setattr(
        records, "probe_contact",
        lambda profile, alias, contact_id: records.Record(
            contact_id=CONTACT_ID, collection_alias=ALIAS, href=HREF,
            revision='"etag-1"', raw_vcard=RICH_CARD),
    )

    result = writes.reconcile_operation("demo", operation_id)

    assert (result["outcome"], result["remote_write"]) == ("not_applied", False)
    # Nothing landed, so the reviewed binding is cleanly applicable again.
    assert not _receipt(profile_dir, operation_id).exists()
