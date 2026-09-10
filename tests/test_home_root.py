"""The operator-owned HERMES_HOME root is validated identically by every command.

The home root's own mode belongs to the operator: `setup` creates an absent
home `0700` but accepts an existing `0755` one, and every later command must
accept exactly the same home rather than refusing a profile setup just created.
Only type, owner, and never following a link are checked there; everything
under `carddav-contacts/` stays `0700`/`0600` and is unaffected.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

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
READS = (
    ["status", "--profile", "demo", "--json"],
    ["snapshot", "--profile", "demo", "--json"],
    ["search", "--profile", "demo", "--query", "example", "--json"],
    ["audit", "--profile", "demo", "--json"],
)
NETWORK = ["discover", "--profile", "demo"]


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


def _permissive_home(tmp_path: Path) -> Path:
    """An operator-provided home root that is group- and world-traversable."""
    home = tmp_path / "home"
    home.mkdir(mode=0o755)
    home.chmod(0o755)
    return home


def _managed_modes(home: Path) -> list[int]:
    root = home / "carddav-contacts"
    return [
        stat.S_IMODE(path.stat().st_mode)
        for path in (root, root / "profiles", root / "profiles" / "demo")
    ]


def test_permissive_home_setup_then_status_reports_no_generation(
    tmp_path: Path, cli: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    home = _permissive_home(tmp_path)
    setup_profile(cli, home)
    capsys.readouterr()

    assert cli.main(["status", "--profile", "demo", "--json"]) == 0

    out, err = capsys.readouterr()
    assert err == ""
    payload = json.loads(out)
    assert (
        payload["current_generation"],
        payload["contact_count"],
        payload["synced_at"],
        payload["freshness"],
    ) == (None, None, None, None)
    assert stat.S_IMODE(home.stat().st_mode) == 0o755
    assert _managed_modes(home) == [0o700, 0o700, 0o700]


@pytest.mark.parametrize("argv", READS)
def test_permissive_home_serves_published_reads(
    tmp_path: Path, cli: ModuleType, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    home = _permissive_home(tmp_path)
    profile_dir = setup_profile(cli, home)
    write_mirror(
        profile_dir,
        {"one.vcf": card("one", "Example One", "EMAIL:one@example.invalid")},
    )
    publish(profile_dir)
    capsys.readouterr()

    assert cli.main(argv) == 0

    out, err = capsys.readouterr()
    assert err == ""
    payload = json.loads(out)
    assert (payload["command"], payload["profile"]) == (argv[0], "demo")
    assert stat.S_IMODE(home.stat().st_mode) == 0o755
    assert _managed_modes(home) == [0o700, 0o700, 0o700]


def test_permissive_home_passes_the_network_preflight(
    tmp_path: Path, cli: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No real network: the pinned runtime is replaced by a synthetic call recorder."""
    home = _permissive_home(tmp_path)
    setup_profile(cli, home)
    monkeypatch.setenv("CARDDAV_USERNAME", "synthetic-user")
    monkeypatch.setenv("CARDDAV_PASSWORD", "synthetic-password")
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        calls.append(argv)
        return cli.transport.subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(cli.transport.subprocess, "run", fake_run)

    assert cli.main(NETWORK) == 0

    assert len(calls) == 1
    assert stat.S_IMODE(home.stat().st_mode) == 0o755


@pytest.mark.parametrize("argv", [*READS, NETWORK])
def test_missing_home_is_refused_and_never_created(
    tmp_path: Path, cli: ModuleType, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    home = tmp_path / "absent-home"
    os.environ["HERMES_HOME"] = str(home)

    assert cli.main(argv) == 2

    assert capsys.readouterr() == UNSAFE
    assert not home.exists()


@pytest.mark.parametrize("damage", ["symlink", "dangling", "file"])
def test_unsafe_home_is_refused_by_later_commands_without_following(
    tmp_path: Path, cli: ModuleType, capsys: pytest.CaptureFixture[str], damage: str
) -> None:
    real = _permissive_home(tmp_path)
    profile_dir = setup_profile(cli, real)
    write_mirror(profile_dir, {"one.vcf": card("one", "Example One")})
    publish(profile_dir)
    before = sorted(path.relative_to(real) for path in real.rglob("*"))
    absent = tmp_path / "absent-target"
    home = tmp_path / "linked-home"
    if damage == "symlink":
        home.symlink_to(real, target_is_directory=True)
    elif damage == "dangling":
        home.symlink_to(absent, target_is_directory=True)
    else:
        home.write_bytes(b"untouched")
    os.environ["HERMES_HOME"] = str(home)
    capsys.readouterr()

    assert cli.main(["status", "--profile", "demo", "--json"]) == 2

    assert capsys.readouterr() == UNSAFE
    assert sorted(path.relative_to(real) for path in real.rglob("*")) == before
    assert not absent.exists()


def test_wrong_owner_home_is_refused_by_later_commands(
    tmp_path: Path,
    cli: ModuleType,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _permissive_home(tmp_path)
    setup_profile(cli, home)
    monkeypatch.setattr(cli.os, "getuid", lambda: os.geteuid() + 1)
    capsys.readouterr()

    assert cli.main(["status", "--profile", "demo", "--json"]) == 2

    assert capsys.readouterr() == UNSAFE


@pytest.mark.parametrize(
    "relative",
    ["carddav-contacts", "carddav-contacts/profiles", "carddav-contacts/profiles/demo"],
)
def test_permissive_managed_directory_is_still_refused(
    tmp_path: Path, cli: ModuleType, capsys: pytest.CaptureFixture[str], relative: str
) -> None:
    """The relaxed rule stops at the home root: managed state must stay private."""
    home = _permissive_home(tmp_path)
    profile_dir = setup_profile(cli, home)
    write_mirror(profile_dir, {"one.vcf": card("one", "Example One")})
    publish(profile_dir)
    target = home / relative
    target.chmod(0o755)
    capsys.readouterr()

    assert cli.main(["status", "--profile", "demo", "--json"]) == 2

    assert capsys.readouterr() == UNSAFE
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
