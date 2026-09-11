"""`status --profile NAME --json` contract tests."""

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


def _at(cli: ModuleType, monkeypatch: pytest.MonkeyPatch, now: float) -> None:
    monkeypatch.setattr(cli.reads.time, "time", lambda: now)


def test_status_reports_the_validated_index_and_freshness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cli: ModuleType,
    capsys: pytest.CaptureFixture[str],
) -> None:
    profile_dir = setup_profile(cli, tmp_path)
    write_mirror(profile_dir, {"one.vcf": card("one", "Example One")})
    generation = publish(profile_dir)
    profile_hash = cli.generations.hashlib.sha256(
        (profile_dir / "profile.json").read_bytes()
    ).hexdigest()
    _at(cli, monkeypatch, SYNCED_AT_EPOCH + 60)

    assert cli.main(["status", "--profile", "demo", "--json"]) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    load("schemas").validate_command(json.loads(captured.out))
    assert json.loads(captured.out) == {
        "command_schema_version": "carddav-command/1.0",
        "command": "status",
        "profile": "demo",
        "current_generation": generation,
        "profile_generation_sha256": profile_hash,
        "contact_count": 1,
        "synced_at": SYNCED_AT,
        "freshness": {
            "age_seconds": 60,
            "stale_after_seconds": 3600,
            "stale": False,
            "clock_skew": False,
        },
    }


def _write_legacy_pointer(profile_dir: Path, generation: str) -> None:
    """Write the older `carddav-current/1.0` pointer, which records no sync time."""
    pointer = profile_dir / "current"
    pointer.write_bytes(
        json.dumps(
            {"generation": generation, "pointer_schema_version": "carddav-current/1.0"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )
    pointer.chmod(0o600)


def test_status_refuses_a_pointer_without_a_recorded_sync_time(
    tmp_path: Path, cli: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    profile_dir = setup_profile(cli, tmp_path)
    write_mirror(profile_dir, {"one.vcf": card("one", "Example One")})
    _write_legacy_pointer(profile_dir, publish(profile_dir))

    assert cli.main(["status", "--profile", "demo", "--json"]) == 2

    assert capsys.readouterr() == ("", "error: unsafe profile state\n")
