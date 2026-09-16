# ABOUTME: Offline tests for preview binding, conditional apply, receipts, and cache invalidation.
# ABOUTME: The record transport and the refresh are stubbed; live writes are proven in integration.

"""Orchestration contract for `scripts/writes.py`.

These tests stand in for the server so the parts that must hold regardless of
any server — that a preview binds an exact revision, that applying twice never
writes twice, that an unknown outcome forces reconciliation instead of a
replay, and that a failed refresh is reported as a stale cache rather than a
failed write — are pinned without a network.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import pytest

from tests import support_reads as support

cli = support.load("cli")
writes = support.load("writes")
ids = support.load("ids")

UID = "hermes-00000000-0000-4000-8000-000000000000"
CONTACT_ID = ids.contact_id(UID)
CHANGES: dict[str, object] = {
    "change_schema_version": "carddav-change/1.0",
    "set": {"display": "Example Person"},
    "clear": [],
    "replace": {},
}


class _Remote:
    """A minimal stand-in for one collection of a CardDAV server."""

    def __init__(self) -> None:
        self.cards: dict[str, tuple[str, str]] = {}  # contact_id -> (raw, revision)
        self.writes: list[str] = []
        self.fail_next: Exception | None = None

    def record(self, contact_id: str) -> Any:
        raw, revision = self.cards[contact_id]
        return writes.records.Record(
            contact_id=contact_id, collection_alias="contacts-a",
            href=f"/dav/contacts-a/{contact_id}.vcf", revision=revision, raw_vcard=raw,
        )

    def seed(self, contact_id: str, raw: str, revision: str = "etag-1") -> None:
        self.cards[contact_id] = (raw, revision)

    def _maybe_fail(self) -> None:
        if self.fail_next is not None:
            failure, self.fail_next = self.fail_next, None
            raise failure


@pytest.fixture
def remote(monkeypatch: pytest.MonkeyPatch) -> _Remote:
    server = _Remote()

    def fetch_record(profile: object, aliases: object, contact_id: str) -> Any:
        if contact_id not in server.cards:
            raise writes.records.RecordNotFound
        return server.record(contact_id)

    def probe_contact(profile: object, alias: object, contact_id: str) -> Any:
        return server.record(contact_id) if contact_id in server.cards else None

    def create_record(profile: object, alias: object, raw: str) -> Any:
        server._maybe_fail()
        contact_id = ids.contact_id(writes.records.Item(raw).uid)
        if contact_id in server.cards:
            raise writes.records.RecordAlreadyExists
        server.cards[contact_id] = (raw, "etag-created")
        server.writes.append(f"create:{contact_id}")
        return server.record(contact_id)

    def update_record(
        profile: object, alias: object, href: object, raw: str, revision: str
    ) -> Any:
        server._maybe_fail()
        contact_id = ids.contact_id(writes.records.Item(raw).uid)
        if server.cards.get(contact_id, (None, None))[1] != revision:
            raise writes.records.RevisionMismatch
        server.cards[contact_id] = (raw, "etag-updated")
        server.writes.append(f"update:{contact_id}")
        return server.record(contact_id)

    def delete_record(profile: object, alias: object, href: str, revision: str) -> None:
        server._maybe_fail()
        contact_id = href.rsplit("/", 1)[1].removesuffix(".vcf")
        if server.cards.get(contact_id, (None, None))[1] != revision:
            raise writes.records.RevisionMismatch
        del server.cards[contact_id]
        server.writes.append(f"delete:{contact_id}")

    monkeypatch.setattr(writes.records, "new_operation_uid", lambda: UID)
    monkeypatch.setattr(writes.records, "fetch_record", fetch_record)
    monkeypatch.setattr(writes.records, "probe_contact", probe_contact)
    monkeypatch.setattr(writes.records, "create_record", create_record)
    monkeypatch.setattr(writes.records, "update_record", update_record)
    monkeypatch.setattr(writes.records, "delete_record", delete_record)
    monkeypatch.setattr(writes.transport, "run_operation", lambda *a, **k: None)
    monkeypatch.setattr(writes.generations, "publish", lambda *a, **k: "0" * 64)
    return server


@pytest.fixture
def profile_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for key in support.ENVIRONMENT_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("CARDDAV_USERNAME", "fixture-user")
    monkeypatch.setenv("CARDDAV_PASSWORD", "synthetic-test-password")
    return support.setup_profile(cli, tmp_path)


def _assert_private(path: Path) -> None:
    info = path.lstat()
    assert stat.S_IMODE(info.st_mode) == (0o700 if path.is_dir() else 0o600)
    assert not path.is_symlink()


def test_prepare_create_binds_a_reviewable_operation_without_writing_remotely(
    profile_dir: Path, remote: _Remote
) -> None:
    operation = writes.prepare_create("demo", "contacts-a", CHANGES)

    assert operation["operation"] == "create"
    assert operation["contact_id"] == CONTACT_ID
    assert operation["base_revision"] is None
    assert operation["before_contact"] is None
    assert operation["after_contact"]["name"]["display"] == "Example Person"
    assert remote.writes == []

    stored = profile_dir / "operations" / f"{operation['operation_id']}.json"
    _assert_private(stored)
    _assert_private(stored.parent)
    assert json.loads(stored.read_bytes()) == operation


def test_apply_creates_once_and_a_repeat_apply_never_writes_again(
    profile_dir: Path, remote: _Remote
) -> None:
    operation = writes.prepare_create("demo", "contacts-a", CHANGES)

    first = writes.apply_operation("demo", operation["operation_id"])
    second = writes.apply_operation("demo", operation["operation_id"])

    assert first["outcome"] == "applied"
    assert first["remote_write"] is True
    assert first["local_cache"] == "refreshed"
    assert second["outcome"] == "applied"
    assert remote.writes == [f"create:{CONTACT_ID}"]
    _assert_private(profile_dir / "receipts" / f"{operation['operation_id']}.json")


def test_an_unknown_operation_id_is_refused(profile_dir: Path, remote: _Remote) -> None:
    with pytest.raises(writes.OperationNotFound):
        writes.apply_operation("demo", "f" * 64)
    with pytest.raises(writes.InvalidOperationRequest):
        writes.apply_operation("demo", "not-a-digest")


def test_a_stale_update_fails_closed_and_leaves_no_receipt(
    profile_dir: Path, remote: _Remote
) -> None:
    remote.seed(CONTACT_ID, support.card(UID, "Example Person"))
    operation = writes.prepare_update("demo", CONTACT_ID, {
        **CHANGES, "set": {"display": "Renamed Person"}})
    assert operation["base_revision"] == "etag-1"

    remote.seed(CONTACT_ID, support.card(UID, "Changed Elsewhere"), "etag-2")

    with pytest.raises(writes.RevisionConflict):
        writes.apply_operation("demo", operation["operation_id"])

    assert remote.writes == []
    assert not (profile_dir / "receipts" / f"{operation['operation_id']}.json").exists()


def test_an_unknown_outcome_requires_reconciliation_instead_of_a_replay(
    profile_dir: Path, remote: _Remote
) -> None:
    operation = writes.prepare_create("demo", "contacts-a", CHANGES)
    remote.fail_next = writes.records.UnknownOutcome()

    with pytest.raises(writes.UnknownOutcome):
        writes.apply_operation("demo", operation["operation_id"])
    receipt = json.loads(
        (profile_dir / "receipts" / f"{operation['operation_id']}.json").read_bytes())
    assert receipt["outcome"] == "unknown"

    with pytest.raises(writes.ReconciliationRequired):
        writes.apply_operation("demo", operation["operation_id"])
    assert remote.writes == []

    # The write never landed, so reconciliation reports it and clears the way.
    resolved = writes.reconcile_operation("demo", operation["operation_id"])
    assert resolved["outcome"] == "not_applied"
    assert writes.apply_operation("demo", operation["operation_id"])["outcome"] == "applied"
    assert remote.writes == [f"create:{CONTACT_ID}"]


def test_reconciliation_recognizes_a_write_that_did_land(
    profile_dir: Path, remote: _Remote
) -> None:
    operation = writes.prepare_create("demo", "contacts-a", CHANGES)
    remote.fail_next = writes.records.UnknownOutcome()
    with pytest.raises(writes.UnknownOutcome):
        writes.apply_operation("demo", operation["operation_id"])
    # The server actually applied the interrupted request.
    remote.seed(CONTACT_ID, str(operation["after_vcard"]), "etag-created")

    resolved = writes.reconcile_operation("demo", operation["operation_id"])

    assert resolved["outcome"] == "already_applied"
    assert writes.apply_operation("demo", operation["operation_id"])["outcome"] == "already_applied"
    assert remote.writes == []


def test_a_failed_refresh_reports_a_stale_cache_not_a_failed_write(
    profile_dir: Path, remote: _Remote, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fail(*args: object, **kwargs: object) -> None:
        raise writes.transport.OperationFailed()

    monkeypatch.setattr(writes.transport, "run_operation", _fail)
    operation = writes.prepare_create("demo", "contacts-a", CHANGES)

    result = writes.apply_operation("demo", operation["operation_id"])

    assert (result["outcome"], result["remote_write"]) == ("applied", True)
    assert result["local_cache"] == "stale"
    _assert_private(profile_dir / writes.CACHE_INVALID_NAME)


def test_delete_requires_the_reviewed_revision_and_reports_removal(
    profile_dir: Path, remote: _Remote
) -> None:
    remote.seed(CONTACT_ID, support.card(UID, "Example Person"))
    operation = writes.prepare_delete("demo", CONTACT_ID)
    assert operation["after_vcard"] is None
    assert operation["before_vcard"] is not None

    result = writes.apply_operation("demo", operation["operation_id"])

    assert (result["outcome"], result["remote_write"]) == ("applied", True)
    assert remote.writes == [f"delete:{CONTACT_ID}"]
    assert CONTACT_ID not in remote.cards


def test_a_prepare_never_addresses_a_collection_outside_the_allowlist(
    profile_dir: Path, remote: _Remote
) -> None:
    with pytest.raises(writes.InvalidOperationRequest):
        writes.prepare_create("demo", "not-allowed", CHANGES)


def test_a_missing_contact_is_refused_before_any_write(
    profile_dir: Path, remote: _Remote
) -> None:
    with pytest.raises(writes.ContactNotFound):
        writes.prepare_update("demo", "0" * 16, CHANGES)
    with pytest.raises(writes.ContactNotFound):
        writes.prepare_delete("demo", "0" * 16)
    assert remote.writes == []
