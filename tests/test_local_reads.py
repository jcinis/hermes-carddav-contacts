"""Every local read command is offline, credential-free, and freshness-exact."""

from __future__ import annotations

import json
import os
import socket
import subprocess
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

from tests.support_reads import (
    ENVIRONMENT_KEYS,
    SYNCED_AT_EPOCH,
    card,
    load,
    publish,
    setup_profile,
    write_mirror,
)

POISON = "poisoned-value-never-read"
CADENCE = 3600


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


def _invocations(contact_id: str) -> list[list[str]]:
    return [
        ["status", "--profile", "demo", "--json"],
        ["snapshot", "--profile", "demo", "--json"],
        ["show", "--profile", "demo", "--id", contact_id, "--json"],
        ["search", "--profile", "demo", "--query", "example", "--json"],
        ["audit", "--profile", "demo", "--json"],
    ]


def _prepare(cli: ModuleType, tmp_path: Path) -> str:
    profile_dir = setup_profile(cli, tmp_path)
    write_mirror(
        profile_dir,
        {
            "one.vcf": card("one", "Example One", "EMAIL:one@example.invalid"),
            "two.vcf": card("two", "Example Two", "EMAIL:one@example.invalid"),
        },
    )
    publish(profile_dir)
    return cast(str, load("ids").contact_id("one"))


def _freshness(payload: dict[str, Any]) -> dict[str, object] | None:
    freshness = payload["freshness"]
    assert freshness is None or isinstance(freshness, dict)
    return freshness


def test_reads_succeed_with_no_network_no_subprocess_and_poisoned_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cli: ModuleType,
    capsys: pytest.CaptureFixture[str],
) -> None:
    contact_id = _prepare(cli, tmp_path)
    for key in ("CARDDAV_USERNAME", "CARDDAV_PASSWORD", "DAV_USERNAME", "DAV_PASSWORD"):
        monkeypatch.setenv(key, POISON)

    def no_network(*args: object, **kwargs: object) -> object:
        raise AssertionError("a local read must not open a socket")

    def no_subprocess(*args: object, **kwargs: object) -> object:
        raise AssertionError("a local read must not start a subprocess")

    monkeypatch.setattr(socket, "socket", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(subprocess, "run", no_subprocess)
    monkeypatch.setattr(subprocess, "Popen", no_subprocess)
    monkeypatch.setattr(cli.transport.subprocess, "run", no_subprocess)
    monkeypatch.setattr(os, "fork", no_subprocess, raising=False)

    schemas = load("schemas")
    for invocation in _invocations(contact_id):
        assert cli.main(invocation) == 0, invocation
        captured = capsys.readouterr()
        assert captured.err == ""
        assert POISON not in captured.out
        schemas.validate_command(json.loads(captured.out))


def test_reads_do_not_write_anything_under_the_profile(
    tmp_path: Path, cli: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    contact_id = _prepare(cli, tmp_path)
    root = tmp_path / "carddav-contacts"
    before = {
        path: (path.lstat().st_mtime_ns, path.lstat().st_size)
        for path in sorted(root.rglob("*"))
    }

    for invocation in _invocations(contact_id):
        assert cli.main(invocation) == 0
        capsys.readouterr()

    assert {
        path: (path.lstat().st_mtime_ns, path.lstat().st_size)
        for path in sorted(root.rglob("*"))
    } == before


@pytest.mark.parametrize(
    ("offset", "expected"),
    [
        (0, {"age_seconds": 0, "stale": False, "clock_skew": False}),
        (CADENCE - 1, {"age_seconds": CADENCE - 1, "stale": False, "clock_skew": False}),
        (CADENCE, {"age_seconds": CADENCE, "stale": True, "clock_skew": False}),
        (CADENCE + 1, {"age_seconds": CADENCE + 1, "stale": True, "clock_skew": False}),
        (-1, {"age_seconds": 0, "stale": True, "clock_skew": True}),
        (-86400, {"age_seconds": 0, "stale": True, "clock_skew": True}),
    ],
)
def test_every_command_reports_the_same_freshness_rule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cli: ModuleType,
    capsys: pytest.CaptureFixture[str],
    offset: int,
    expected: dict[str, object],
) -> None:
    contact_id = _prepare(cli, tmp_path)
    monkeypatch.setattr(cli.reads.time, "time", lambda: SYNCED_AT_EPOCH + offset)

    reported = []
    for invocation in _invocations(contact_id):
        assert cli.main(invocation) == 0
        reported.append(_freshness(json.loads(capsys.readouterr().out)))

    assert reported == [{**expected, "stale_after_seconds": CADENCE}] * len(reported)


def test_a_fractional_clock_does_not_round_up_into_staleness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cli: ModuleType,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _prepare(cli, tmp_path)
    monkeypatch.setattr(cli.reads.time, "time", lambda: SYNCED_AT_EPOCH + CADENCE - 0.001)

    assert cli.main(["status", "--profile", "demo", "--json"]) == 0

    assert _freshness(json.loads(capsys.readouterr().out)) == {
        "age_seconds": CADENCE - 1,
        "stale_after_seconds": CADENCE,
        "stale": False,
        "clock_skew": False,
    }
