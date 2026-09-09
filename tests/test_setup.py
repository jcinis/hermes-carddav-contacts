"""Contract test for `setup`: creates one profile from repeated --collection flags.

Invokes the real entry-point script as a subprocess, wrapped with a Python
audit hook that turns any socket or subprocess creation during setup into a
hard failure, so the "no network, no subprocess" guarantee is enforced
rather than assumed.
"""

from __future__ import annotations

import importlib.util
import json
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ENTRYPOINT = (
    Path(__file__).parent.parent
    / "skills"
    / "productivity"
    / "carddav-contacts"
    / "scripts"
    / "carddav_contacts.py"
)

_FORBIDDEN_EVENTS = ("socket.socket", "subprocess.Popen", "os.posix_spawn", "os.spawn")


def _make_runner_code(argv: list[str]) -> str:
    return (
        "import runpy\n"
        "import sys\n"
        "\n"
        "_FORBIDDEN = " + repr(_FORBIDDEN_EVENTS) + "\n"
        "\n"
        "def _guard(event, args):\n"
        "    if event in _FORBIDDEN:\n"
        "        raise RuntimeError('forbidden event during setup: ' + event)\n"
        "\n"
        "sys.addaudithook(_guard)\n"
        "sys.argv = " + repr([str(ENTRYPOINT)] + argv) + "\n"
        "runpy.run_path(" + repr(str(ENTRYPOINT)) + ", run_name='__main__')\n"
    )


def _run(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    code = _make_runner_code(argv)
    return subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_setup_creates_profile_from_repeated_collection_flags(tmp_path: Path) -> None:
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    env = {"HERMES_HOME": str(hermes_home), "PATH": ""}

    result = _run(
        [
            "setup",
            "--profile",
            "demo",
            "--namespace",
            "example",
            "--server-url",
            "https://example.test/dav",
            "--collection",
            "contacts-b",
            "--collection",
            "contacts-a",
        ],
        env,
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""

    root = hermes_home / "carddav-contacts"
    profiles_dir = root / "profiles"
    profile_dir = profiles_dir / "demo"
    profile_path = profile_dir / "profile.json"
    lock_path = profile_dir / "profile.lock"

    for directory in (root, profiles_dir, profile_dir):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700

    assert stat.S_IMODE(profile_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600

    expected_profile: dict[str, object] = {
        "profile_schema_version": "carddav-profile/1.0",
        "account_namespace": "example",
        "server_url": "https://example.test/dav/",
        "collection_allowlist": ["contacts-a", "contacts-b"],
        "sync_cadence_seconds": 3600,
        "capabilities": {
            "read_only": True,
            "create_update": False,
            "cleanup_delete": False,
        },
    }
    expected_bytes = (
        json.dumps(expected_profile, sort_keys=True, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )

    raw = profile_path.read_bytes()
    assert raw == expected_bytes

    payload = json.loads(raw.decode("utf-8"))
    assert payload == expected_profile
    assert set(payload.keys()) == set(expected_profile.keys())


def _load_carddav_contacts_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("carddav_contacts", ENTRYPOINT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_write_profile_json_removes_tempfile_on_replace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_carddav_contacts_module()
    path = tmp_path / "profile.json"

    def _boom(_src: str, _dst: object) -> None:
        raise OSError("simulated os.replace failure")

    monkeypatch.setattr(module.os, "replace", _boom)

    with pytest.raises(OSError, match="simulated os.replace failure"):
        module._write_profile_json(path, {"a": 1})

    assert not path.exists()
    assert list(tmp_path.glob(".profile.*.tmp")) == []
