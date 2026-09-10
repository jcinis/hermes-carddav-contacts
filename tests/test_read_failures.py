"""Every local read command fails closed on damaged or foreign private state."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType

import pytest

from tests.support_reads import (
    ENVIRONMENT_KEYS,
    card,
    load,
    publish,
    setup_profile,
    write_mirror,
)

UNSAFE = ("", "error: unsafe profile state\n")


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


CONTACT_ID = "0" * 16
INVOCATIONS = (
    ["status", "--profile", "demo", "--json"],
    ["snapshot", "--profile", "demo", "--json"],
    ["show", "--profile", "demo", "--id", CONTACT_ID, "--json"],
    ["search", "--profile", "demo", "--query", "example", "--json"],
    ["audit", "--profile", "demo", "--json"],
)


def _publish(cli: ModuleType, tmp_path: Path) -> tuple[Path, str]:
    profile_dir = setup_profile(cli, tmp_path)
    write_mirror(
        profile_dir,
        {"one.vcf": card("one", "Example One"), "two.vcf": card("two", "Example Two")},
    )
    return profile_dir, publish(profile_dir)


def _damage_removed_row(published: Path) -> None:
    connection = sqlite3.connect(published / "index.sqlite3", isolation_level=None)
    try:
        connection.execute("DELETE FROM contacts WHERE contact_id = (SELECT MIN(contact_id) FROM contacts)")
    finally:
        connection.close()


def _damage_manifest_profile_binding(published: Path) -> None:
    manifest = json.loads((published / "manifest.json").read_bytes())
    manifest["profile_generation_sha256"] = "c" * 64
    (published / "manifest.json").write_bytes(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    )


def _damage_missing_database(published: Path) -> None:
    (published / "index.sqlite3").unlink()


def _damage_truncated_database(published: Path) -> None:
    (published / "index.sqlite3").write_bytes(b"SQLite format 3\x00 truncated")


def _damage_unknown_meta_row(published: Path) -> None:
    connection = sqlite3.connect(published / "index.sqlite3", isolation_level=None)
    try:
        connection.execute("INSERT INTO source_meta (key, value) VALUES ('extra', 'x')")
    finally:
        connection.close()


@pytest.mark.parametrize(
    "damage",
    [
        _damage_removed_row,
        _damage_manifest_profile_binding,
        _damage_missing_database,
        _damage_truncated_database,
        _damage_unknown_meta_row,
    ],
    ids=["removed-row", "foreign-profile", "missing-database", "truncated-database", "extra-meta"],
)
def test_damaged_generation_state_fails_every_read_command(
    tmp_path: Path,
    cli: ModuleType,
    capsys: pytest.CaptureFixture[str],
    damage: Callable[[Path], None],
) -> None:
    profile_dir, generation = _publish(cli, tmp_path)
    damage(profile_dir / "generations" / generation)

    for invocation in INVOCATIONS:
        assert cli.main(invocation) == 2, invocation
        assert capsys.readouterr() == UNSAFE, invocation


@pytest.mark.parametrize(
    "pointer",
    [
        b"not a pointer\n",
        b'{"generation":"' + b"0" * 64 + b'","pointer_schema_version":"carddav-current/2.0"}\n',
        b'{"generation":"short","pointer_schema_version":"carddav-current/1.1","synced_at":"2026-09-10T00:00:00Z"}\n',
        b'{"generation":"' + b"0" * 64 + b'","pointer_schema_version":"carddav-current/1.1","synced_at":"not a time"}\n',
    ],
    ids=["garbage", "unknown-version", "bad-generation", "bad-timestamp"],
)
def test_a_malformed_pointer_fails_every_read_command(
    tmp_path: Path, cli: ModuleType, capsys: pytest.CaptureFixture[str], pointer: bytes
) -> None:
    profile_dir, _generation = _publish(cli, tmp_path)
    (profile_dir / "current").write_bytes(pointer)
    (profile_dir / "current").chmod(0o600)

    for invocation in INVOCATIONS:
        assert cli.main(invocation) == 2, invocation
        assert capsys.readouterr() == UNSAFE, invocation


def test_a_pointer_to_a_missing_generation_fails_every_read_command(
    tmp_path: Path, cli: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    profile_dir, generation = _publish(cli, tmp_path)
    for path in sorted((profile_dir / "generations" / generation).rglob("*"), reverse=True):
        path.rmdir() if path.is_dir() else path.unlink()
    (profile_dir / "generations" / generation).rmdir()

    for invocation in INVOCATIONS:
        assert cli.main(invocation) == 2, invocation
        assert capsys.readouterr() == UNSAFE, invocation


def test_reads_of_an_unknown_profile_fail_closed(
    tmp_path: Path, cli: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    _publish(cli, tmp_path)

    for invocation in INVOCATIONS:
        missing = [
            "absent" if argument == "demo" else argument for argument in invocation
        ]
        assert cli.main(missing) == 2, missing
        assert capsys.readouterr() == UNSAFE, missing
