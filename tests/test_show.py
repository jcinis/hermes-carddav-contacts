"""`show --profile NAME --id ID --json` contract tests."""

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


def _published(cli: ModuleType, tmp_path: Path) -> tuple[Path, str]:
    profile_dir = setup_profile(cli, tmp_path)
    write_mirror(
        profile_dir,
        {
            "one.vcf": card("one", "Example One", "TEL;TYPE=CELL:+1-555-0100"),
            "two.vcf": card("two", "Example Two"),
        },
    )
    return profile_dir, publish(profile_dir)


def test_show_prints_one_validated_contact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cli: ModuleType,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _profile_dir, generation = _published(cli, tmp_path)
    ids = load("ids")
    contact_id = ids.contact_id("one")
    monkeypatch.setattr(cli.reads.time, "time", lambda: SYNCED_AT_EPOCH)

    assert cli.main(["show", "--profile", "demo", "--id", contact_id, "--json"]) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    load("schemas").validate_command(payload)
    assert payload["command"] == "show"
    assert payload["profile"] == "demo"
    assert payload["generation"] == generation
    assert payload["synced_at"] == SYNCED_AT
    assert payload["freshness"] == {
        "age_seconds": 0,
        "stale_after_seconds": 3600,
        "stale": False,
        "clock_skew": False,
    }
    assert payload["contact"]["contact_id"] == contact_id
    assert payload["contact"]["name"]["display"] == "Example One"
    assert payload["contact"]["phones"] == [
        {"value": "+1-555-0100", "types": ["cell"], "label": None, "preference": None}
    ]


def test_show_reports_a_missing_contact_as_not_found(
    tmp_path: Path, cli: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    _published(cli, tmp_path)

    assert cli.main(["show", "--profile", "demo", "--id", "0" * 16, "--json"]) == 2

    assert capsys.readouterr() == ("", "error: contact not found\n")


@pytest.mark.parametrize(
    "identifier",
    ["", "0" * 15, "0" * 17, "0" * 15 + "G", ("0" * 15 + "A"), "0123456789abcde-", "  " + "0" * 14],
)
def test_show_refuses_a_malformed_contact_id(
    tmp_path: Path, cli: ModuleType, capsys: pytest.CaptureFixture[str], identifier: str
) -> None:
    _published(cli, tmp_path)

    assert cli.main(["show", "--profile", "demo", "--id", identifier, "--json"]) == 2

    assert capsys.readouterr() == ("", "error: invalid operation\n")
