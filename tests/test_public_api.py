# ABOUTME: Proves the installable public CRUD API exists and shares the CLI's implementation.
# ABOUTME: Transport is stubbed here; the installed-wheel proof lives in the packaging suite.

"""Public API contract for `hermes_carddav_contacts.api`.

A consumer — the later cleanup workflow included — must be able to import one
supported module and get exactly the operations the command surface runs, not a
second writer. These tests pin the exported surface and prove that an operation
prepared through the API is the same operation the CLI applies.
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

from hermes_carddav_contacts import api

cli = support.load("cli")
ids = support.load("ids")

UID = "hermes-22222222-2222-4222-8222-222222222222"
CONTACT_ID = ids.contact_id(UID)
CHANGES: dict[str, object] = {
    "change_schema_version": api.CHANGE_SCHEMA_VERSION,
    "set": {"display": "Example Person"},
    "clear": [],
    "replace": {},
}

EXPECTED_SURFACE = {
    "read_record",
    "read_collection",
    "prepare_create",
    "prepare_update",
    "prepare_replace",
    "prepare_delete",
    "apply_operation",
    "reconcile_operation",
    "WriteRefused",
    "InvalidOperationRequest",
    "OperationNotFound",
    "ContactNotFound",
    "RevisionConflict",
    "AlreadyExists",
    "VerificationFailed",
    "UnknownOutcome",
    "ReconciliationRequired",
    "RemoteUnavailable",
    "ProfileBindingMismatch",
    "CHANGE_SCHEMA_VERSION",
    "OPERATION_SCHEMA_VERSION",
    "RESULT_SCHEMA_VERSION",
    "RECORD_SCHEMA_VERSION",
    "PACKAGE_VERSION",
    "CAPABILITIES",
}


@pytest.fixture
def remote(monkeypatch: pytest.MonkeyPatch) -> dict[str, tuple[str, str]]:
    cards: dict[str, tuple[str, str]] = {}

    def record(contact_id: str) -> Any:
        raw, revision = cards[contact_id]
        return records.Record(
            contact_id=contact_id, collection_alias="contacts-a",
            href=f"/dav/contacts-a/{contact_id}.vcf", revision=revision, raw_vcard=raw,
        )

    def create_record(profile: object, alias: object, raw: str) -> Any:
        contact_id = ids.contact_id(Item(raw).uid)
        cards[contact_id] = (raw, "etag-created")
        return record(contact_id)

    def fetch_record(profile: object, aliases: object, contact_id: str) -> Any:
        if contact_id not in cards:
            raise records.RecordNotFound
        return record(contact_id)

    monkeypatch.setattr(records, "new_operation_uid", lambda: UID)
    monkeypatch.setattr(records, "create_record", create_record)
    monkeypatch.setattr(records, "fetch_record", fetch_record)
    monkeypatch.setattr(
        records, "fetch_collection",
        lambda profile, alias: [record(key) for key in sorted(cards)],
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


def test_the_public_surface_is_exactly_the_supported_operations() -> None:
    assert set(api.__all__) == EXPECTED_SURFACE
    for name in EXPECTED_SURFACE:
        assert hasattr(api, name), name
    assert api.CAPABILITIES == {
        "read_only": False, "create": True, "update": True, "delete": True}
    assert api.PACKAGE_VERSION == "0.2.0"


def test_an_operation_prepared_through_the_api_is_applied_by_the_cli(
    home: Path, remote: dict[str, tuple[str, str]], capsys: pytest.CaptureFixture[str]
) -> None:
    operation = api.prepare_create("demo", "contacts-a", CHANGES)
    assert operation["operation_schema_version"] == api.OPERATION_SCHEMA_VERSION

    assert cli.main(["apply", "--profile", "demo",
                     "--operation", str(operation["operation_id"]), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["result_schema_version"] == api.RESULT_SCHEMA_VERSION
    assert (result["outcome"], result["remote_write"]) == ("applied", True)
    assert CONTACT_ID in remote

    fresh = api.read_record("demo", CONTACT_ID)
    assert fresh["record_schema_version"] == api.RECORD_SCHEMA_VERSION
    assert fresh["revision"] == "etag-created"
    assert [item["contact_id"] for item in api.read_collection("demo", "contacts-a")] == [
        CONTACT_ID
    ]


def test_a_lossless_whole_card_replacement_goes_through_the_same_api(
    home: Path, remote: dict[str, tuple[str, str]]
) -> None:
    original = support.card(
        UID, "Example Person", "X-EXAMPLE-CUSTOM:keep-me", "NOTE:Example note")
    remote[CONTACT_ID] = (original, "etag-1")
    replacement = original.replace("FN:Example Person", "FN:Replaced Person")

    operation = api.prepare_replace("demo", CONTACT_ID, replacement)

    assert operation["operation"] == "replace"
    assert operation["base_revision"] == "etag-1"
    assert operation["after_vcard"] == replacement
    assert operation["after_contact"]["name"]["display"] == "Replaced Person"  # type: ignore[index]

    with pytest.raises(api.InvalidOperationRequest):
        api.prepare_replace("demo", CONTACT_ID, original.replace(UID, "other-uid"))


def test_public_refusals_are_the_documented_exception_types(
    home: Path, remote: dict[str, tuple[str, str]]
) -> None:
    with pytest.raises(api.ContactNotFound):
        api.read_record("demo", "0" * 16)
    with pytest.raises(api.OperationNotFound):
        api.apply_operation("demo", "a" * 64)
    with pytest.raises(api.InvalidOperationRequest):
        api.prepare_create("demo", "not-allowed", CHANGES)
    assert issubclass(api.ContactNotFound, api.WriteRefused)
