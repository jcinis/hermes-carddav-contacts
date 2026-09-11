"""`snapshot --profile NAME --json` contract tests."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

from tests.support_reads import (
    ENVIRONMENT_KEYS,
    SYNCED_AT,
    SYNCED_AT_EPOCH,
    card,
    load,
    publish,
    setup_profile,
    write_mirror,
)


@pytest.fixture(autouse=True)
def _hermetic_environment() -> Iterator[None]:
    original = {key: os.environ[key] for key in ENVIRONMENT_KEYS if key in os.environ}
    for key in ENVIRONMENT_KEYS:
        os.environ.pop(key, None)
    yield
    for key in ENVIRONMENT_KEYS:
        os.environ.pop(key, None)
    os.environ.update(original)


@pytest.fixture
def cli() -> ModuleType:
    return load("cli")


def test_snapshot_prints_the_validated_source_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cli: ModuleType,
    capsys: pytest.CaptureFixture[str],
) -> None:
    profile_dir = setup_profile(cli, tmp_path)
    write_mirror(
        profile_dir,
        {
            "one.vcf": card("one", "Example One", "EMAIL;TYPE=WORK:one@example.invalid"),
            "two.vcf": card("two", "Example Two"),
        },
    )
    generation = publish(profile_dir)
    profile_bytes = (profile_dir / "profile.json").read_bytes()
    monkeypatch.setattr(cli.reads.time, "time", lambda: SYNCED_AT_EPOCH + 120)

    assert cli.main(["snapshot", "--profile", "demo", "--json"]) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    schemas = load("schemas")
    schemas.validate_command(payload)
    assert payload["command"] == "snapshot"
    assert payload["profile"] == "demo"
    assert payload["generation"] == generation
    assert payload["profile_generation_sha256"] == cli.hashlib.sha256(profile_bytes).hexdigest()
    assert payload["synced_at"] == SYNCED_AT
    assert payload["freshness"] == {
        "age_seconds": 120,
        "stale_after_seconds": 3600,
        "stale": False,
        "clock_skew": False,
    }
    assert payload["data"]["account_namespace"] == "example"
    assert payload["data"]["collections"] == ["contacts-a"]
    assert [contact["name"]["display"] for contact in payload["data"]["contacts"]] == [
        "Example One",
        "Example Two",
    ]
    assert payload["data"]["contacts"][0]["emails"] == [
        {"value": "one@example.invalid", "types": ["work"], "label": None, "preference": None}
    ]


def test_snapshot_without_a_current_generation_fails_closed(
    tmp_path: Path, cli: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    setup_profile(cli, tmp_path)

    assert cli.main(["snapshot", "--profile", "demo", "--json"]) == 2

    assert capsys.readouterr() == ("", "error: no current generation\n")


def test_a_tampered_payload_is_refused_before_anything_is_printed(
    tmp_path: Path, cli: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    profile_dir = setup_profile(cli, tmp_path)
    write_mirror(profile_dir, {"one.vcf": card("one", "Example One")})
    generation = publish(profile_dir)
    database = profile_dir / "generations" / generation / "index.sqlite3"
    connection = cli.reads.index.sqlite3.connect(database, isolation_level=None)
    try:
        connection.execute(
            "UPDATE contacts SET payload = ? WHERE contact_id = "
            "(SELECT contact_id FROM contacts)",
            ('{"contact_id":"deadbeefdeadbeef","unexpected":true}',),
        )
    finally:
        connection.close()

    assert cli.main(["snapshot", "--profile", "demo", "--json"]) == 2

    assert capsys.readouterr() == ("", "error: unsafe profile state\n")


def test_snapshot_carries_flattened_multi_valued_name_components(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cli: ModuleType,
    capsys: pytest.CaptureFixture[str],
) -> None:
    profile_dir = setup_profile(cli, tmp_path)
    write_mirror(
        profile_dir,
        {"one.vcf": card("one", "Example One", "N:Doe;Jane;Q,R;Dr.,Prof.;Jr.")},
    )
    generation = publish(profile_dir)
    monkeypatch.setattr(cli.reads.time, "time", lambda: SYNCED_AT_EPOCH)

    assert cli.main(["snapshot", "--profile", "demo", "--json"]) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    load("schemas").validate_command(payload)
    assert payload["generation"] == generation
    assert payload["synced_at"] == SYNCED_AT
    assert payload["data"]["contacts"][0]["name"] == {
        "display": "Example One",
        "prefix": "Dr., Prof.",
        "given": "Jane",
        "additional": "Q, R",
        "family": "Doe",
        "suffix": "Jr.",
    }
