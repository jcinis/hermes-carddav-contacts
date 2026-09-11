# ABOUTME: Command-surface tests for the record read, the prepare/apply pair, and reconcile.
# ABOUTME: The record transport is stubbed in-process; the live proof is the integration suite.

"""Exact CRUD command grammar, output documents, and fixed failure lines.

The write commands share one implementation with the public API, so these
tests only pin what the command surface itself owns: which invocations are
accepted at all, that the operation digest is what binds a preview to its
application, and that every refusal is one fixed content-free line on stderr
with nothing on stdout.
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
import records
import transport
from vdirsyncer.vobject import Item  # type: ignore[import-untyped]

cli = support.load("cli")
ids = support.load("ids")

UID = "hermes-11111111-1111-4111-8111-111111111111"
CONTACT_ID = ids.contact_id(UID)
CHANGES = json.dumps({
    "change_schema_version": "carddav-change/1.0",
    "set": {"display": "Example Person", "given": "Example"},
    "clear": [],
    "replace": {},
})


@pytest.fixture
def remote(monkeypatch: pytest.MonkeyPatch) -> dict[str, tuple[str, str]]:
    cards: dict[str, tuple[str, str]] = {}

    def record(contact_id: str) -> Any:
        raw, revision = cards[contact_id]
        return records.Record(
            contact_id=contact_id, collection_alias="contacts-a",
            href=f"/dav/contacts-a/{contact_id}.vcf", revision=revision, raw_vcard=raw,
        )

    def fetch_record(profile: object, aliases: object, contact_id: str) -> Any:
        if contact_id not in cards:
            raise records.RecordNotFound
        return record(contact_id)

    def create_record(profile: object, alias: object, raw: str) -> Any:
        contact_id = ids.contact_id(Item(raw).uid)
        cards[contact_id] = (raw, "etag-created")
        return record(contact_id)

    def update_record(
        profile: object, alias: object, href: object, raw: str, revision: str
    ) -> Any:
        contact_id = ids.contact_id(Item(raw).uid)
        if cards.get(contact_id, (None, None))[1] != revision:
            raise records.RevisionMismatch
        cards[contact_id] = (raw, "etag-updated")
        return record(contact_id)

    def delete_record(profile: object, alias: object, href: str, revision: str) -> None:
        contact_id = href.rsplit("/", 1)[1].removesuffix(".vcf")
        if cards.get(contact_id, (None, None))[1] != revision:
            raise records.RevisionMismatch
        del cards[contact_id]

    monkeypatch.setattr(records, "new_operation_uid", lambda: UID)
    monkeypatch.setattr(records, "fetch_record", fetch_record)
    monkeypatch.setattr(records, "create_record", create_record)
    monkeypatch.setattr(records, "update_record", update_record)
    monkeypatch.setattr(records, "delete_record", delete_record)
    monkeypatch.setattr(
        records, "probe_contact",
        lambda profile, alias, contact_id: record(contact_id) if contact_id in cards else None,
    )
    monkeypatch.setattr(transport, "run_operation", lambda *a, **k: None)
    monkeypatch.setattr(generations, "publish", lambda *a, **k: "0" * 64)
    return cards


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for key in support.ENVIRONMENT_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("CARDDAV_USERNAME", "fixture-user")
    monkeypatch.setenv("CARDDAV_PASSWORD", "synthetic-test-password")
    support.setup_profile(cli, tmp_path)
    return tmp_path


def _run(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, Any, str]:
    code = cli.main(list(args))
    captured = capsys.readouterr()
    payload = json.loads(captured.out) if captured.out else None
    return code, payload, captured.err


def test_the_full_create_read_update_delete_cycle_through_the_command_surface(
    home: Path, remote: dict[str, tuple[str, str]], capsys: pytest.CaptureFixture[str]
) -> None:
    code, operation, err = _run(
        capsys, "prepare-create", "--profile", "demo",
        "--collection", "contacts-a", "--changes", CHANGES, "--json")
    assert (code, err) == (0, "")
    assert operation["operation"] == "create"
    assert operation["after_contact"]["name"]["display"] == "Example Person"
    assert remote == {}

    code, result, err = _run(
        capsys, "apply", "--profile", "demo",
        "--operation", operation["operation_id"], "--json")
    assert (code, err) == (0, "")
    assert (result["outcome"], result["remote_write"]) == ("applied", True)
    assert CONTACT_ID in remote

    code, fresh, err = _run(capsys, "record", "--profile", "demo", "--id", CONTACT_ID, "--json")
    assert (code, err) == (0, "")
    assert fresh["revision"] == "etag-created"
    assert fresh["contact"]["name"]["display"] == "Example Person"

    renamed = json.dumps({
        "change_schema_version": "carddav-change/1.0",
        "set": {"display": "Renamed Person"}, "clear": ["given"], "replace": {},
    })
    code, operation, err = _run(
        capsys, "prepare-update", "--profile", "demo",
        "--id", CONTACT_ID, "--changes", renamed, "--json")
    assert (code, err) == (0, "")
    assert operation["base_revision"] == "etag-created"
    assert operation["before_contact"]["name"]["display"] == "Example Person"
    assert operation["after_contact"]["name"]["display"] == "Renamed Person"
    assert operation["after_contact"]["name"]["given"] is None

    code, result, err = _run(
        capsys, "apply", "--profile", "demo",
        "--operation", operation["operation_id"], "--json")
    assert (code, result["outcome"], err) == (0, "applied", "")

    code, operation, err = _run(
        capsys, "prepare-delete", "--profile", "demo", "--id", CONTACT_ID, "--json")
    assert (code, err) == (0, "")
    assert operation["operation"] == "delete"

    code, result, err = _run(
        capsys, "apply", "--profile", "demo",
        "--operation", operation["operation_id"], "--json")
    assert (code, result["outcome"], err) == (0, "applied", "")
    assert remote == {}


def test_a_stale_apply_prints_one_fixed_conflict_line(
    home: Path, remote: dict[str, tuple[str, str]], capsys: pytest.CaptureFixture[str]
) -> None:
    remote[CONTACT_ID] = (support.card(UID, "Example Person"), "etag-1")
    _, operation, _ = _run(
        capsys, "prepare-update", "--profile", "demo",
        "--id", CONTACT_ID, "--changes", CHANGES, "--json")
    remote[CONTACT_ID] = (support.card(UID, "Changed Elsewhere"), "etag-2")

    code, payload, err = _run(
        capsys, "apply", "--profile", "demo",
        "--operation", operation["operation_id"], "--json")

    assert (code, payload, err) == (2, None, "error: revision conflict\n")


@pytest.mark.parametrize("argv", [
    ["prepare-create", "--profile", "demo", "--collection", "contacts-a", "--changes", CHANGES],
    ["prepare-create", "--profile", "demo", "--changes", CHANGES, "--json"],
    ["prepare-create", "--profile", "demo", "--collection", "Contacts-A",
     "--changes", CHANGES, "--json"],
    ["prepare-create", "--profile", "demo", "--collection", "contacts-a",
     "--changes", "not json", "--json"],
    ["prepare-create", "--profile", "demo", "--collection", "contacts-a",
     "--changes", '{"set":{},"set":{}}', "--json"],
    ["prepare-update", "--profile", "demo", "--id", "NOTHEX", "--changes", CHANGES, "--json"],
    ["prepare-delete", "--profile", "demo", "--json"],
    ["apply", "--profile", "demo", "--operation", "short", "--json"],
    ["apply", "--profile", "demo", "--operation", "a" * 64],
    ["record", "--profile", "demo", "--id", CONTACT_ID],
    ["reconcile", "--profile", "demo", "--json"],
])
def test_malformed_write_invocations_are_refused_before_any_state_is_touched(
    home: Path, remote: dict[str, tuple[str, str]], capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    code, payload, err = _run(capsys, *argv)

    assert (code, payload) == (2, None)
    assert err == "error: invalid operation\n"
    assert remote == {}


def test_an_absent_operation_and_an_absent_contact_have_distinct_lines(
    home: Path, remote: dict[str, tuple[str, str]], capsys: pytest.CaptureFixture[str]
) -> None:
    code, _payload, err = _run(
        capsys, "apply", "--profile", "demo", "--operation", "a" * 64, "--json")
    assert (code, err) == (2, "error: operation not found\n")

    code, _payload, err = _run(
        capsys, "record", "--profile", "demo", "--id", "0" * 16, "--json")
    assert (code, err) == (2, "error: contact not found\n")


def test_a_stale_cache_is_reported_by_every_local_read(
    home: Path, remote: dict[str, tuple[str, str]], capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_dir = home / "carddav-contacts" / "profiles" / "demo"
    support.write_mirror(profile_dir, {"one.vcf": support.card("one", "Example One")})
    support.publish(profile_dir)

    code, status, err = _run(capsys, "status", "--profile", "demo", "--json")
    assert (code, err, status["cache_invalidated"]) == (0, "", False)

    def _fail(*args: object, **kwargs: object) -> None:
        raise transport.OperationFailed

    monkeypatch.setattr(transport, "run_operation", _fail)
    _, operation, _ = _run(
        capsys, "prepare-create", "--profile", "demo",
        "--collection", "contacts-a", "--changes", CHANGES, "--json")
    _, result, _ = _run(
        capsys, "apply", "--profile", "demo",
        "--operation", operation["operation_id"], "--json")
    assert (result["outcome"], result["local_cache"]) == ("applied", "stale")

    for command in ("status", "snapshot", "search"):
        argv = ["--profile", "demo", "--json"]
        if command == "search":
            argv = ["--profile", "demo", "--query", "example", "--json"]
        code, payload, err = _run(capsys, command, *argv)
        assert (code, err) == (0, "")
        assert payload["cache_invalidated"] is True, command
        support.load("schemas").validate_command(payload)


def test_an_operation_bound_to_replaced_profile_settings_has_its_own_error_line(
    home: Path, remote: dict[str, tuple[str, str]], capsys: pytest.CaptureFixture[str]
) -> None:
    """Review regression: the recorded profile binding is enforced, not advisory."""
    import profiles

    _, operation, _ = _run(
        capsys, "prepare-create", "--profile", "demo",
        "--collection", "contacts-a", "--changes", CHANGES, "--json")

    path = home / "carddav-contacts" / "profiles" / "demo" / "profile.json"
    replacement = profiles.build_profile(
        "changed", "https://other.example.invalid/", ["contacts-a"])
    path.write_bytes(profiles.profile_bytes(replacement))
    path.chmod(0o600)

    code, payload, err = _run(
        capsys, "apply", "--profile", "demo",
        "--operation", operation["operation_id"], "--json")

    assert (code, payload, err) == (2, None, "error: profile binding mismatch\n")
    assert remote == {}
