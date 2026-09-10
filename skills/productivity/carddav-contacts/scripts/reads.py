"""Validated, strictly local reads of one profile's current generation.

Every read resolves `current`, revalidates the published generation, and
recomputes the canonical source digest over every payload it is about to
return, so a reader never reports data the sync path would refuse.
"""

from __future__ import annotations

import hashlib
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

_SCRIPT_DIR = Path(__file__).parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import generations
import index
import schemas

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class NoCurrentGeneration(Exception):
    """No generation is published yet, so there is nothing to read."""


def _epoch(timestamp: str) -> int:
    try:
        moment = datetime.strptime(timestamp, TIMESTAMP_FORMAT).replace(tzinfo=UTC)
    except ValueError as exc:
        raise generations.UnsafeGenerationState from exc
    return int(moment.timestamp())


def freshness(synced_at: str, stale_after_seconds: object, now: float | None = None) -> dict[str, object]:
    """Describe how long ago the last successful sync finished."""
    if type(stale_after_seconds) is not int or stale_after_seconds <= 0:
        raise generations.UnsafeGenerationState
    moment = int(time.time() if now is None else now)
    synced = _epoch(synced_at)
    # A future timestamp is surfaced as skew, never smoothed into a fresh age.
    clock_skew = moment < synced
    age_seconds = 0 if clock_skew else moment - synced
    return {
        "age_seconds": age_seconds,
        "stale_after_seconds": stale_after_seconds,
        "stale": clock_skew or age_seconds >= stale_after_seconds,
        "clock_skew": clock_skew,
    }


def _contacts(database: Path, meta: dict[str, object]) -> list[object]:
    contacts: list[object] = []
    try:
        rows = index.read_contacts(database)
    except index.UnreadableIndex as exc:
        raise generations.UnsafeGenerationState from exc
    for contact_id, collection_alias, display_name, payload in rows:
        try:
            contact = schemas.parse_json(payload)
        except ValueError as exc:
            raise generations.UnsafeGenerationState from exc
        if not isinstance(contact, dict):
            raise generations.UnsafeGenerationState
        name = contact.get("name")
        if (
            contact.get("contact_id") != contact_id
            or contact.get("collection_alias") != collection_alias
            or not isinstance(name, dict)
            or name.get("display") != display_name
        ):
            raise generations.UnsafeGenerationState
        contacts.append(contact)
    return contacts


def load_source(profile_dir: Path, profile_bytes: bytes) -> tuple[str, str, dict[str, object]]:
    """Return the current generation, its sync time, and its validated payload."""
    pointer = generations.read_pointer(profile_dir, profile_bytes)
    if pointer is None:
        raise NoCurrentGeneration
    generation = pointer["generation"]
    synced_at = pointer["synced_at"]
    if not isinstance(generation, str) or not isinstance(synced_at, str):
        # A pointer without a recorded sync time cannot answer a freshness question.
        raise generations.UnsafeGenerationState
    database = profile_dir / "generations" / generation / "index.sqlite3"
    try:
        meta = index.read_meta(database)
    except index.UnreadableIndex as exc:
        raise generations.UnsafeGenerationState from exc
    source = {
        "schema_version": meta["schema_version"],
        "account_namespace": meta["account_namespace"],
        "collections": meta["collections"],
        "contacts": _contacts(database, meta),
    }
    try:
        canonical = schemas.canonical_source(source)
    except ValueError as exc:
        raise generations.UnsafeGenerationState from exc
    if hashlib.sha256(canonical).hexdigest() != generation:
        raise generations.UnsafeGenerationState
    return generation, synced_at, cast(dict[str, object], schemas.parse_json(canonical))
