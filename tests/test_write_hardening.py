# ABOUTME: Regressions for the release-review blockers in the write safety boundary.
# ABOUTME: Offline; the record transport is stubbed and no real server is contacted.

"""Release-review regressions: receipts, profile binding, raw preservation, ambiguity.

Each test here reproduces one finding from the independent review, so a
regression cannot pass unnoticed:

1. a forged, foreign, or malformed receipt must never be reported as a
   verified success, and a settled receipt is confirmed against the server;
2. an operation may only be applied under the exact profile bytes it was bound
   to, and `setup` may not rebind a profile directory that retains state;
3. read-back must detect loss of unmodeled raw vCard lines, while tolerating
   harmless server serialization differences;
4. any failure once a mutation request has been dispatched leaves an
   unresolved receipt and requires reconciliation instead of a replay.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from tests import support_reads as support

if str(support.SCRIPTS) not in sys.path:
    sys.path.insert(0, str(support.SCRIPTS))

import generations
import profiles
import records
import transport
import writes
from vdirsyncer.vobject import Item  # type: ignore[import-untyped]

cli = support.load("cli")
ids = support.load("ids")

UID = "hermes-33333333-3333-4333-8333-333333333333"
CONTACT_ID = ids.contact_id(UID)
ALIAS = "contacts-a"
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


class _Remote:
    """A stand-in for one collection, recording every mutation it is asked for."""

    def __init__(self) -> None:
        self.cards: dict[str, tuple[str, str]] = {}
        self.writes: list[str] = []
        self.reads: list[str] = []
        # When set, the write is accepted and then the read-back fails.
        self.readback_failure: Exception | None = None
        self.readback_override: str | None = None

    def record(self, contact_id: str) -> Any:
        raw, revision = self.cards[contact_id]
        return records.Record(
            contact_id=contact_id, collection_alias=ALIAS,
            href=f"/dav/{ALIAS}/{contact_id}.vcf", revision=revision, raw_vcard=raw,
        )

    def _settle(self, contact_id: str, raw: str, revision: str) -> Any:
        self.cards[contact_id] = (self.readback_override or raw, revision)
        if self.readback_failure is not None:
            failure, self.readback_failure = self.readback_failure, None
            raise failure
        return self.record(contact_id)


@pytest.fixture
def remote(monkeypatch: pytest.MonkeyPatch) -> _Remote:
    server = _Remote()

    def fetch_record(profile: object, aliases: object, contact_id: str) -> Any:
        if contact_id not in server.cards:
            raise records.RecordNotFound
        return server.record(contact_id)

    def probe_contact(profile: object, alias: object, contact_id: str) -> Any:
        server.reads.append(f"probe:{contact_id}")
        return server.record(contact_id) if contact_id in server.cards else None

    def create_record(profile: object, alias: object, raw: str) -> Any:
        contact_id = ids.contact_id(Item(raw).uid)
        if contact_id in server.cards:
            raise records.RecordAlreadyExists
        server.writes.append(f"create:{contact_id}")
        return server._settle(contact_id, raw, "etag-created")

    def update_record(
        profile: object, alias: object, href: object, raw: str, revision: str
    ) -> Any:
        contact_id = ids.contact_id(Item(raw).uid)
        if server.cards.get(contact_id, (None, None))[1] != revision:
            raise records.RevisionMismatch
        server.writes.append(f"update:{contact_id}")
        return server._settle(contact_id, raw, "etag-updated")

    def delete_record(profile: object, alias: object, href: str, revision: str) -> None:
        contact_id = href.rsplit("/", 1)[1].removesuffix(".vcf")
        if server.cards.get(contact_id, (None, None))[1] != revision:
            raise records.RevisionMismatch
        server.writes.append(f"delete:{contact_id}")
        del server.cards[contact_id]
        if server.readback_failure is not None:
            failure, server.readback_failure = server.readback_failure, None
            raise failure

    monkeypatch.setattr(records, "new_operation_uid", lambda: UID)
    monkeypatch.setattr(records, "fetch_record", fetch_record)
    monkeypatch.setattr(records, "probe_contact", probe_contact)
    monkeypatch.setattr(records, "create_record", create_record)
    monkeypatch.setattr(records, "update_record", update_record)
    monkeypatch.setattr(records, "delete_record", delete_record)
    monkeypatch.setattr(transport, "run_operation", lambda *a, **k: None)
    monkeypatch.setattr(generations, "publish", lambda *a, **k: "0" * 64)
    return server


@pytest.fixture
def profile_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for key in support.ENVIRONMENT_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("CARDDAV_USERNAME", "fixture-user")
    monkeypatch.setenv("CARDDAV_PASSWORD", "synthetic-test-password")
    return support.setup_profile(cli, tmp_path)


def _receipt_path(profile_dir: Path, operation_id: str) -> Path:
    directory = profile_dir / "receipts"
    if not directory.exists():
        directory.mkdir(mode=0o700)
    return directory / f"{operation_id}.json"


def _write_receipt(path: Path, receipt: dict[str, object]) -> None:
    payload = json.dumps(
        receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8") + b"\n"
    path.write_bytes(payload)
    path.chmod(0o600)


def _forged(
    operation: dict[str, object], overrides: dict[str, object] | None = None
) -> dict[str, object]:
    receipt: dict[str, object] = {
        "receipt_schema_version": "carddav-receipt/1.0",
        "operation_id": operation["operation_id"],
        "operation": operation["operation"],
        "profile": operation["profile"],
        "collection_alias": operation["collection_alias"],
        "contact_id": operation["contact_id"],
        "outcome": "applied",
        "result_revision": "etag-created",
        "attempted_at": "2026-09-11T00:00:00Z",
        "completed_at": "2026-09-11T00:00:01Z",
    }
    receipt.update(overrides or {})
    return receipt


# --- Blocker 1: receipts ----------------------------------------------------


def test_a_forged_applied_receipt_is_not_reported_as_verified_success(
    profile_dir: Path, remote: _Remote
) -> None:
    operation = writes.prepare_create("demo", ALIAS, CHANGES)
    _write_receipt(
        _receipt_path(profile_dir, str(operation["operation_id"])), _forged(operation)
    )

    # The server has no such record, so nothing may be reported as applied.
    with pytest.raises(writes.VerificationFailed):
        writes.apply_operation("demo", str(operation["operation_id"]))

    assert remote.writes == []


def test_a_settled_receipt_is_confirmed_against_the_server_before_being_reported(
    profile_dir: Path, remote: _Remote
) -> None:
    operation = writes.prepare_create("demo", ALIAS, CHANGES)
    first = writes.apply_operation("demo", str(operation["operation_id"]))
    assert first["outcome"] == "applied"

    remote.reads.clear()
    second = writes.apply_operation("demo", str(operation["operation_id"]))

    assert (second["outcome"], second["remote_write"]) == ("applied", False)
    assert second["verified"] is True
    # Confirmed by reading the server, not by trusting the stored receipt.
    assert remote.reads == [f"probe:{CONTACT_ID}"]
    assert remote.writes == [f"create:{CONTACT_ID}"]

    # If the record then disappears, the recorded success is no longer claimable.
    del remote.cards[CONTACT_ID]
    with pytest.raises(writes.VerificationFailed):
        writes.apply_operation("demo", str(operation["operation_id"]))


@pytest.mark.parametrize("overrides", [
    {"outcome": "definitely"},
    {"outcome": True},
    {"operation_id": "b" * 64},
    {"contact_id": "0" * 16},
    {"collection_alias": "somewhere-else"},
    {"profile": "other"},
    {"operation": "delete"},
    {"attempted_at": "2026-02-30T00:00:00Z"},
    {"result_revision": ""},
    {"completed_at": None},
])
def test_a_malformed_or_foreign_receipt_is_refused(
    profile_dir: Path, remote: _Remote, overrides: dict[str, object]
) -> None:
    operation = writes.prepare_create("demo", ALIAS, CHANGES)
    _write_receipt(
        _receipt_path(profile_dir, str(operation["operation_id"])),
        _forged(operation, overrides),
    )

    with pytest.raises(writes.InvalidOperationRequest):
        writes.apply_operation("demo", str(operation["operation_id"]))

    assert remote.writes == []


# --- Blocker 3: profile binding --------------------------------------------


def _rebind_profile(profile_dir: Path) -> None:
    """Replace the profile with a different, equally valid one."""
    replacement = profiles.build_profile(
        "changed", "https://other.example.invalid/", ["contacts-a"]
    )
    path = profile_dir / "profile.json"
    path.write_bytes(profiles.profile_bytes(replacement))
    path.chmod(0o600)


def test_an_operation_is_refused_when_the_profile_it_was_bound_to_changed(
    profile_dir: Path, remote: _Remote
) -> None:
    operation = writes.prepare_create("demo", ALIAS, CHANGES)
    _rebind_profile(profile_dir)

    with pytest.raises(writes.ProfileBindingMismatch):
        writes.apply_operation("demo", str(operation["operation_id"]))

    assert remote.writes == []


def test_reconcile_also_refuses_an_operation_bound_to_a_replaced_profile(
    profile_dir: Path, remote: _Remote
) -> None:
    operation = writes.prepare_create("demo", ALIAS, CHANGES)
    remote.readback_failure = records.UnknownOutcome()
    with pytest.raises(writes.UnknownOutcome):
        writes.apply_operation("demo", str(operation["operation_id"]))
    _rebind_profile(profile_dir)

    with pytest.raises(writes.ProfileBindingMismatch):
        writes.reconcile_operation("demo", str(operation["operation_id"]))


def test_setup_refuses_a_missing_profile_when_private_state_remains(
    profile_dir: Path, remote: _Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    writes.prepare_create("demo", ALIAS, CHANGES)
    (profile_dir / "profile.json").unlink()
    capsys.readouterr()

    result = cli.main([
        "setup", "--profile", "demo", "--namespace", "changed",
        "--server-url", "https://other.example.invalid/", "--collection", "contacts-a",
    ])

    assert result == 2
    assert capsys.readouterr() == ("", "error: unsafe profile state\n")
    assert not (profile_dir / "profile.json").exists()


# --- Blocker 4: raw vCard preservation on read-back -------------------------


def test_a_server_that_drops_an_unmodeled_property_is_not_reported_as_verified(
    profile_dir: Path, remote: _Remote
) -> None:
    remote.cards[CONTACT_ID] = (RICH_CARD, "etag-1")
    operation = writes.prepare_update(
        "demo", CONTACT_ID, {**CHANGES, "set": {"display": "Renamed Person"}})
    # The server accepts the write but returns a card missing the private
    # extension and the photo; the modeled projection is unchanged.
    remote.readback_override = (
        RICH_CARD.replace("FN:Example Person", "FN:Renamed Person")
        .replace("PHOTO;ENCODING=b;TYPE=PNG:aGVsbG8=\r\n", "")
        .replace("X-EXAMPLE-CUSTOM:keep-me\r\n", "")
    )

    with pytest.raises(writes.VerificationFailed):
        writes.apply_operation("demo", str(operation["operation_id"]))

    # The write was dispatched, so the operation must stay unresolved.
    assert remote.writes == [f"update:{CONTACT_ID}"]
    assert json.loads(
        _receipt_path(profile_dir, str(operation["operation_id"])).read_bytes()
    )["outcome"] == "unknown"


def test_harmless_server_serialization_differences_still_verify(
    profile_dir: Path, remote: _Remote
) -> None:
    remote.cards[CONTACT_ID] = (RICH_CARD, "etag-1")
    operation = writes.prepare_update(
        "demo", CONTACT_ID, {**CHANGES, "set": {"display": "Renamed Person"}})
    intended = str(operation["after_vcard"])
    # Reordered properties, reordered/lowercased parameters, and LF endings are
    # all normalizations that lose nothing.
    reordered = [
        line for line in intended.replace("\r\n", "\n").split("\n")
        if line and line not in ("BEGIN:VCARD", "END:VCARD")
    ]
    reordered.sort()
    remote.readback_override = "\n".join(
        ["BEGIN:VCARD", *reordered, "END:VCARD"]
    ).replace("PHOTO;ENCODING=b;TYPE=PNG:", "PHOTO;type=png;encoding=b:") + "\n"

    result = writes.apply_operation("demo", str(operation["operation_id"]))

    assert (result["outcome"], result["verified"]) == ("applied", True)


# --- Blocker 5: ambiguity after the mutation is dispatched ------------------


@pytest.mark.parametrize("verb", ["create", "update", "delete"])
def test_an_unusable_read_back_leaves_the_operation_unresolved(
    profile_dir: Path, remote: _Remote, verb: str
) -> None:
    if verb == "create":
        operation = writes.prepare_create("demo", ALIAS, CHANGES)
    else:
        remote.cards[CONTACT_ID] = (RICH_CARD, "etag-1")
        operation = (
            writes.prepare_update("demo", CONTACT_ID, CHANGES) if verb == "update"
            else writes.prepare_delete("demo", CONTACT_ID)
        )
    operation_id = str(operation["operation_id"])
    # The write is accepted and then the read-back is unusable.
    remote.readback_failure = records.UnknownOutcome()

    with pytest.raises(writes.UnknownOutcome):
        writes.apply_operation("demo", operation_id)

    dispatched = list(remote.writes)
    assert len(dispatched) == 1
    receipt = json.loads(_receipt_path(profile_dir, operation_id).read_bytes())
    assert receipt["outcome"] == "unknown"

    # A second apply must reconcile, never replay the mutation.
    with pytest.raises(writes.ReconciliationRequired):
        writes.apply_operation("demo", operation_id)
    assert remote.writes == dispatched


def test_a_post_dispatch_read_back_failure_is_never_a_clean_retry(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """At the transport seam: once the request left, nothing is cleanly retryable."""

    class _Storage:
        async def upload(self, item: object) -> tuple[str, str]:
            return "/dav/contacts-a/one.vcf", "etag"

        async def update(self, href: str, item: object, etag: str) -> str:
            return "etag"

        async def delete(self, href: str, etag: str) -> None:
            return None

        async def get(self, href: str) -> tuple[object, str]:
            # A malformed read-back: no usable card or validator token.
            raise ValueError("unusable read-back")

    async def _storage(profile: object, alias: str, connector: object) -> tuple[Any, str]:
        return _Storage(), "/dav/contacts-a/"

    monkeypatch.setattr(records, "_storage", _storage)
    profile = {"server_url": "https://carddav.example.invalid/",
               "collection_allowlist": ["contacts-a"], "account_namespace": "example"}
    monkeypatch.setenv("CARDDAV_USERNAME", "fixture-user")
    monkeypatch.setenv("CARDDAV_PASSWORD", "synthetic-test-password")
    card = support.card("one", "Example One")

    with pytest.raises(records.UnknownOutcome):
        records.create_record(profile, "contacts-a", card)
    with pytest.raises(records.UnknownOutcome):
        records.update_record(
            profile, "contacts-a", "/dav/contacts-a/one.vcf", card, "etag-1")
    with pytest.raises(records.UnknownOutcome):
        records.delete_record(profile, "contacts-a", "/dav/contacts-a/one.vcf", "etag-1")
