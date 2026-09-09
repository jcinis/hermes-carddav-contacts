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


def _valid_setup_argv(**overrides: object) -> list[str]:
    profile = overrides.get("profile", "demo")
    namespace = overrides.get("namespace", "example")
    server_url = overrides.get("server-url", "https://example.test/dav")
    collections = overrides.get("collections", ["contacts-a"])
    assert isinstance(collections, list)

    argv = ["setup"]
    if profile is not None:
        argv += ["--profile", str(profile)]
    if namespace is not None:
        argv += ["--namespace", str(namespace)]
    if server_url is not None:
        argv += ["--server-url", str(server_url)]
    for collection in collections:
        argv += ["--collection", str(collection)]
    return argv


def _assert_invalid_setup(argv: list[str], tmp_path: Path) -> None:
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    env = {"HERMES_HOME": str(hermes_home), "PATH": ""}

    before = sorted(hermes_home.rglob("*"))

    result = _run(argv, env)

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "error: invalid setup\n"
    assert "Traceback" not in result.stderr
    assert "usage:" not in result.stderr

    after = sorted(hermes_home.rglob("*"))
    assert after == before


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


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(_valid_setup_argv(profile=None), id="missing_profile"),
        pytest.param(_valid_setup_argv(namespace=None), id="missing_namespace"),
        pytest.param(_valid_setup_argv(**{"server-url": None}), id="missing_server_url"),
        pytest.param(_valid_setup_argv(collections=[]), id="missing_collection"),
        pytest.param(_valid_setup_argv() + ["--bogus", "x"], id="unknown_option"),
        pytest.param(
            _valid_setup_argv()[:-1],
            id="missing_value_trailing",
        ),
        pytest.param(
            ["setup", "--profile", "--namespace", "example"],
            id="missing_value_before_next_flag",
        ),
    ],
)
def test_setup_rejects_malformed_arguments(argv: list[str], tmp_path: Path) -> None:
    _assert_invalid_setup(argv, tmp_path)


_BAD_IDENTIFIERS = [
    pytest.param("Demo", id="uppercase"),
    pytest.param("a/b", id="slash"),
    pytest.param("../etc", id="traversal"),
    pytest.param(".demo", id="leading_dot"),
    pytest.param("-demo", id="leading_hyphen"),
    pytest.param("_demo", id="leading_underscore"),
    pytest.param("café", id="non_ascii"),
    pytest.param("", id="empty"),
    pytest.param("a" * 65, id="overlength"),
]


@pytest.mark.parametrize("field", ["profile", "namespace", "collection"])
@pytest.mark.parametrize("bad_value", _BAD_IDENTIFIERS)
def test_setup_rejects_locked_identifier_grammar_violations(
    field: str, bad_value: str, tmp_path: Path
) -> None:
    if field == "collection":
        argv = _valid_setup_argv(collections=[bad_value])
    else:
        argv = _valid_setup_argv(**{field: bad_value})
    _assert_invalid_setup(argv, tmp_path)


@pytest.mark.parametrize(
    "flag,abbreviation",
    [
        ("--profile", "--prof"),
        ("--collection", "--col"),
        ("--server-url", "--server"),
    ],
)
def test_setup_rejects_abbreviated_options(flag: str, abbreviation: str, tmp_path: Path) -> None:
    argv = _valid_setup_argv()
    argv = [abbreviation if token == flag else token for token in argv]
    _assert_invalid_setup(argv, tmp_path)


@pytest.mark.parametrize("field", ["profile", "namespace", "collection"])
def test_setup_rejects_trailing_newline_identifier(field: str, tmp_path: Path) -> None:
    bad_value = "demo\n"
    if field == "collection":
        argv = _valid_setup_argv(collections=[bad_value])
    else:
        argv = _valid_setup_argv(**{field: bad_value})
    _assert_invalid_setup(argv, tmp_path)


@pytest.mark.parametrize("flag", ["--profile", "--namespace", "--server-url"])
def test_setup_rejects_repeated_singleton_option(flag: str, tmp_path: Path) -> None:
    argv = _valid_setup_argv()
    index = argv.index(flag)
    duplicate = argv[index : index + 2]
    argv = argv + duplicate
    _assert_invalid_setup(argv, tmp_path)


def test_setup_rejects_invalid_second_of_multiple_collections(tmp_path: Path) -> None:
    argv = _valid_setup_argv(collections=["contacts-a", "Bad/Name"])
    _assert_invalid_setup(argv, tmp_path)


@pytest.mark.parametrize(
    "collections",
    [
        pytest.param(["contacts-a", "contacts-a"], id="two_exact_duplicates"),
        pytest.param(
            ["contacts-a", "contacts-b", "contacts-a"],
            id="duplicate_with_distinct_between",
        ),
        pytest.param(["contacts-a", "contacts-a", "contacts-a"], id="three_exact_duplicates"),
    ],
)
def test_setup_rejects_duplicate_collection_values(
    collections: list[str], tmp_path: Path
) -> None:
    argv = _valid_setup_argv(collections=collections)
    _assert_invalid_setup(argv, tmp_path)


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
