# ABOUTME: Record-level CardDAV reads and conditional writes on vdirsyncer's own storage API.
# ABOUTME: Addresses only collections the configured server advertised; never a caller-supplied URL.

"""Record-level CardDAV transport on pinned vdirsyncer's own storage API.

Every remote read and every conditional write goes through
`vdirsyncer.storage.dav.CardDAVStorage`: `upload` sends `If-None-Match: *`,
`update` and `delete` send `If-Match: <revision>`, and vdirsyncer turns a `412`
into `PreconditionFailed` and a `404`/`410` into `NotFoundError`. No HTTP, DAV,
or XML client is implemented here, and no caller ever supplies a remote URL —
a collection address is only ever one the configured server advertised through
native discovery, under the configured `server_url`, named by the profile's own
allowlist.

Each call opens and closes its own connector under one overall timeout, so a
caller never holds a connection across an approval boundary. A timeout or a
disconnect during a mutation raises `UnknownOutcome`: it is never evidence that
the write did or did not land, and the caller must reconcile rather than replay.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import unquote, urlsplit

import aiohttp
from vdirsyncer import exceptions as dav_exceptions  # type: ignore[import-untyped]
from vdirsyncer.storage.dav import CardDAVStorage  # type: ignore[import-untyped]
from vdirsyncer.vobject import Item  # type: ignore[import-untyped]

_SCRIPT_DIR = Path(__file__).parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import ids
import transport

OPERATION_TIMEOUT = 120

# The native libraries log to the root logger, which Python's last-resort
# handler prints to stderr. A command surface that promises exactly one fixed
# error line cannot let a library warning — or a URL inside one — reach it, so
# their loggers are silenced here at import time rather than reconfigured
# globally for the whole process.
for _quiet in ("vdirsyncer", "aiohttp"):
    _logger = logging.getLogger(_quiet)
    _logger.addHandler(logging.NullHandler())
    _logger.propagate = False


class UnsafeRemoteTarget(Exception):
    """A remote address is outside the configured server, path, or allowlist."""


class RecordNotFound(Exception):
    """The addressed record is authoritatively absent."""


class RevisionMismatch(Exception):
    """The server refused the precondition: the record moved on since it was read."""


class RecordAlreadyExists(Exception):
    """A no-overwrite create found something already at the target address."""


class UnknownOutcome(Exception):
    """The operation's result is unknown; reconcile before doing anything else."""


class RecordTransportFailed(Exception):
    """A remote read or write failed for a reason that is not an outcome."""


@dataclass(frozen=True)
class Record:
    """One fresh remote record: its identity, its revision, and its exact bytes."""

    contact_id: str
    collection_alias: str
    href: str
    revision: str
    raw_vcard: str


def new_operation_uid() -> str:
    """Mint one href-safe UID that stays the same across a create retry."""
    return f"hermes-{uuid.uuid4()}"


_ENCODED_SEPARATOR = re.compile(r"%2f", re.IGNORECASE)


def path_segments(path: object) -> list[str]:
    """Split an absolute path into decoded segments, refusing any traversal form.

    Containment is decided on whole path segments, never on a string prefix:
    `/userother/` shares five characters with `/user/` and no segment at all.
    A `.` or `..` segment is refused outright rather than resolved, in either
    its literal or percent-encoded spelling, and so is an encoded separator,
    which would otherwise hide a segment boundary from this check.
    """
    if type(path) is not str or not path.startswith("/"):
        raise UnsafeRemoteTarget
    segments: list[str] = []
    for raw in path.split("/"):
        if raw == "":
            continue
        if _ENCODED_SEPARATOR.search(raw):
            raise UnsafeRemoteTarget
        decoded = unquote(raw)
        if decoded in (".", "..") or "/" in decoded:
            raise UnsafeRemoteTarget
        if any(ord(character) < 0x20 or ord(character) == 0x7F for character in decoded):
            raise UnsafeRemoteTarget
        segments.append(decoded)
    return segments


def check_collection_url(server_url: object, candidate: object) -> str:
    """Accept a discovered collection URL only inside the configured server."""
    if type(server_url) is not str or type(candidate) is not str or not candidate:
        raise UnsafeRemoteTarget
    base, target = urlsplit(server_url), urlsplit(candidate)
    if target.query or target.fragment:
        # A collection address is a path; a query or fragment is never part of it.
        raise UnsafeRemoteTarget
    if not target.scheme or not target.netloc:
        raise UnsafeRemoteTarget
    if target.username is not None or target.password is not None:
        raise UnsafeRemoteTarget
    if (target.scheme, target.netloc) != (base.scheme, base.netloc):
        raise UnsafeRemoteTarget
    base_segments = path_segments(base.path or "/")
    target_segments = path_segments(target.path or "/")
    if target_segments[: len(base_segments)] != base_segments:
        raise UnsafeRemoteTarget
    return candidate


def check_collection_alias(profile: Mapping[str, object], alias: object) -> str:
    """Accept only a collection this profile already selected."""
    allowlist = profile.get("collection_allowlist")
    if not isinstance(allowlist, list) or type(alias) is not str or alias not in allowlist:
        raise UnsafeRemoteTarget
    return alias


def check_href(collection_path: object, candidate: object) -> str:
    """Accept only an item href strictly inside the resolved collection path.

    An href is a path on the already bound origin. A full URL, a
    protocol-relative authority, a query, a fragment, or any traversal form is
    refused before the native library is ever handed the target.
    """
    if type(collection_path) is not str or type(candidate) is not str:
        raise UnsafeRemoteTarget
    parsed = urlsplit(candidate)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise UnsafeRemoteTarget
    collection_segments = path_segments(collection_path)
    target_segments = path_segments(parsed.path)
    if len(target_segments) <= len(collection_segments):
        raise UnsafeRemoteTarget
    if target_segments[: len(collection_segments)] != collection_segments:
        raise UnsafeRemoteTarget
    return candidate


def _server_url(profile: Mapping[str, object]) -> str:
    server_url = profile.get("server_url")
    if type(server_url) is not str or not server_url:
        raise UnsafeRemoteTarget
    return server_url


def _run[T](action: Callable[[aiohttp.TCPConnector], Awaitable[T]], *, mutating: bool) -> T:
    """Run one native operation with its own connector under one timeout."""

    async def _main() -> T:
        connector = aiohttp.TCPConnector()
        try:
            return await asyncio.wait_for(action(connector), OPERATION_TIMEOUT)
        finally:
            await connector.close()

    try:
        return asyncio.run(_main())
    except (
        UnsafeRemoteTarget,
        RecordAlreadyExists,
        RecordNotFound,
        RevisionMismatch,
        UnknownOutcome,
        transport.InvalidCredentials,
    ):
        raise
    except (TimeoutError, aiohttp.ServerDisconnectedError, ConnectionResetError) as exc:
        # The request may or may not have been applied; never replay it blindly.
        raise (UnknownOutcome if mutating else RecordTransportFailed) from exc
    except Exception as exc:  # native failures are redacted at this seam
        raise RecordTransportFailed from exc


def _definitive_refusal(exc: BaseException) -> bool:
    """True only when the request provably did not change anything.

    Once a mutation request has been handed to the native library, a failure is
    ambiguous unless the evidence says otherwise: either no connection was ever
    established, or the server itself answered with a definitive client-error
    refusal. Everything else — including a `5xx`, a mid-flight disconnect, or an
    unusable read-back — leaves the outcome unknown and must be reconciled, not
    replayed.
    """
    if isinstance(exc, aiohttp.ClientConnectorError):
        return True
    status = getattr(exc, "status", None)
    # 408 and 429 say "try again", not "nothing happened".
    return isinstance(status, int) and 400 <= status < 500 and status not in (408, 429)


def _request_failure(exc: BaseException) -> Exception:
    """Classify a failure raised *by the mutation request itself*.

    This is the only place a clean, retryable refusal may be concluded, and
    only on positive evidence that nothing was applied.
    """
    if isinstance(exc, UnknownOutcome):
        return exc
    if _definitive_refusal(exc):
        return RecordTransportFailed()
    return UnknownOutcome()


def _verification_failure(exc: BaseException) -> Exception:
    """Classify a failure raised *after* the mutation request returned success.

    Always unknown. Once the server has accepted the write, no later evidence
    says it did not land: a read-back `401`, `404`, connector failure, timeout,
    or unparsable card describes the read, not the write. Reusing the
    definitive-refusal rule here would present an applied mutation as cleanly
    retryable and allow it to be replayed.
    """
    del exc  # the reason is deliberately irrelevant to the classification
    return UnknownOutcome()


async def _storage(
    profile: Mapping[str, object], alias: str, connector: aiohttp.TCPConnector
) -> tuple[CardDAVStorage, str]:
    """Resolve one allowlisted collection through native discovery, then bind it."""
    check_collection_alias(profile, alias)
    server_url = _server_url(profile)
    username_var, password_var, username, password = transport.resolve_credentials(os.environ)
    del username_var, password_var
    found: str | None = None
    async for collection in CardDAVStorage.discover(
        url=server_url, username=username, password=password, connector=connector
    ):
        if collection.get("collection") == alias:
            found = check_collection_url(server_url, collection.get("url"))
            break
    if found is None:
        raise RecordNotFound
    storage = CardDAVStorage(
        url=found, username=username, password=password, connector=connector
    )
    return storage, urlsplit(found).path


async def _read_all(storage: CardDAVStorage, alias: str, path: str) -> list[Record]:
    hrefs = [check_href(path, href) async for href, _etag in storage.list()]
    found: list[Record] = []
    async for href, item, etag in storage.get_multi(hrefs):
        found.append(_record(alias, check_href(path, href), etag, item.raw))
    return found


def _record(alias: str, href: str, revision: object, raw: object) -> Record:
    if type(revision) is not str or not revision or type(raw) is not str or not raw:
        # A server that answers without a usable validator cannot be written to
        # safely, because no later write could be made conditional on it.
        raise RecordTransportFailed
    try:
        contact_id = ids.contact_id(_uid(raw))
    except ValueError as exc:
        raise RecordTransportFailed from exc
    return Record(
        contact_id=contact_id,
        collection_alias=alias,
        href=href,
        revision=revision,
        raw_vcard=raw,
    )


def _uid(raw: str) -> str:
    item = Item(raw)
    uid = item.uid
    if type(uid) is not str:
        raise RecordTransportFailed
    return uid


def fetch_collection(profile: Mapping[str, object], alias: str) -> list[Record]:
    """Read every record of one allowlisted collection, fresh from the server."""

    async def _action(connector: aiohttp.TCPConnector) -> list[Record]:
        storage, path = await _storage(profile, alias, connector)
        return await _read_all(storage, alias, path)

    return _run(_action, mutating=False)


def fetch_record(profile: Mapping[str, object], aliases: Sequence[str], contact_id: str) -> Record:
    """Find one record by its opaque contact ID across the given collections."""
    matches: list[Record] = []
    for alias in aliases:
        matches += [
            record for record in fetch_collection(profile, alias)
            if record.contact_id == contact_id
        ]
    if not matches:
        raise RecordNotFound
    if len(matches) > 1:
        # One identity in two places is an ambiguity this skill never resolves.
        raise RecordTransportFailed
    return matches[0]


def probe_contact(
    profile: Mapping[str, object], alias: str, contact_id: str
) -> Record | None:
    """Reconciliation read: is this exact identity present in this collection now?

    An authoritative absence is `None`. A transport failure is raised, because
    a failed read is never evidence that a record is gone.
    """
    matches = [
        record for record in fetch_collection(profile, alias)
        if record.contact_id == contact_id
    ]
    if len(matches) > 1:
        raise RecordTransportFailed
    return matches[0] if matches else None


def probe_href(profile: Mapping[str, object], alias: str, href: str) -> Record | None:
    """Read one exact address, returning None only for an authoritative absence."""

    async def _action(connector: aiohttp.TCPConnector) -> Record | None:
        storage, path = await _storage(profile, alias, connector)
        target = check_href(path, href)
        try:
            item, etag = await storage.get(target)
        except dav_exceptions.NotFoundError:
            return None
        return _record(alias, target, etag, item.raw)

    return _run(_action, mutating=False)


def create_record(profile: Mapping[str, object], alias: str, raw_vcard: str) -> Record:
    """Create one record with a no-overwrite precondition, then read it back."""

    async def _action(connector: aiohttp.TCPConnector) -> Record:
        storage, path = await _storage(profile, alias, connector)
        try:
            href, _etag = await storage.upload(Item(raw_vcard))
        except dav_exceptions.PreconditionFailed as exc:
            raise RecordAlreadyExists from exc
        except dav_exceptions.AlreadyExistingError as exc:
            raise RecordAlreadyExists from exc
        except Exception as exc:  # the request may already have been applied
            raise _request_failure(exc) from exc
        try:
            return await _verify(storage, alias, check_href(path, href))
        except Exception as exc:  # the write landed; only the proof is missing
            raise _verification_failure(exc) from exc

    return _run(_action, mutating=True)


def update_record(
    profile: Mapping[str, object], alias: str, href: str, raw_vcard: str, revision: str
) -> Record:
    """Replace one record only if it still carries the reviewed revision."""

    async def _action(connector: aiohttp.TCPConnector) -> Record:
        storage, path = await _storage(profile, alias, connector)
        target = check_href(path, href)
        try:
            await storage.update(target, Item(raw_vcard), revision)
        except dav_exceptions.PreconditionFailed as exc:
            raise RevisionMismatch from exc
        except dav_exceptions.NotFoundError as exc:
            raise RecordNotFound from exc
        except Exception as exc:  # the request may already have been applied
            raise _request_failure(exc) from exc
        try:
            return await _verify(storage, alias, target)
        except Exception as exc:  # the write landed; only the proof is missing
            raise _verification_failure(exc) from exc

    return _run(_action, mutating=True)


def delete_record(
    profile: Mapping[str, object], alias: str, href: str, revision: str
) -> None:
    """Delete one record only if it still carries the reviewed revision.

    Success requires an authoritative not-found read afterwards. A network or
    authentication failure during that read is an unknown outcome, never proof
    that the record is gone.
    """

    async def _action(connector: aiohttp.TCPConnector) -> None:
        storage, path = await _storage(profile, alias, connector)
        target = check_href(path, href)
        try:
            await storage.delete(target, revision)
        except dav_exceptions.PreconditionFailed as exc:
            raise RevisionMismatch from exc
        except dav_exceptions.NotFoundError as exc:
            raise RecordNotFound from exc
        except Exception as exc:  # the request may already have been applied
            raise _request_failure(exc) from exc
        try:
            await storage.get(target)
        except dav_exceptions.NotFoundError:
            return
        except Exception as exc:  # a failed read is never proof of deletion
            raise _verification_failure(exc) from exc
        raise UnknownOutcome

    _run(_action, mutating=True)


async def _verify(storage: CardDAVStorage, alias: str, href: str) -> Record:
    """Read the exact record back, so a write is never reported from its request."""
    try:
        item, etag = await storage.get(href)
    except dav_exceptions.NotFoundError as exc:
        raise UnknownOutcome from exc
    return _record(alias, href, etag, cast(Any, item).raw)
