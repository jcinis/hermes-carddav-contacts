"""Read-only CardDAV discovery contract tests."""

from __future__ import annotations

import importlib.util
import os
import stat
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ENTRYPOINT = (
    Path(__file__).parent.parent
    / "skills"
    / "productivity"
    / "carddav-contacts"
    / "scripts"
    / "carddav_contacts.py"
)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("carddav_contacts_discover", ENTRYPOINT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _hermetic_environment() -> Iterator[None]:
    keys = ("HERMES_HOME", "CARDDAV_USERNAME", "CARDDAV_PASSWORD", "DAV_USERNAME", "DAV_PASSWORD")
    original = {key: os.environ[key] for key in keys if key in os.environ}
    for key in keys:
        os.environ.pop(key, None)
    yield
    for key in keys:
        os.environ.pop(key, None)
    os.environ.update(original)


def _setup(module: ModuleType, home: Path, profile: str = "demo") -> None:
    os.environ["HERMES_HOME"] = str(home)
    assert module.main(
            [
                "setup",
                "--profile",
                profile,
                "--namespace",
                "example",
                "--server-url",
                "https://example.invalid/dav",
                "--collection",
                "contacts-a",
                "--collection",
                "contacts-b",
            ]
        ) == 0


@pytest.fixture
def module() -> ModuleType:
    return _load()


def test_discover_renders_private_readonly_config_and_removes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module: ModuleType
) -> None:
    _setup(module, tmp_path)
    monkeypatch.setenv("CARDDAV_USERNAME", "synthetic-user")
    monkeypatch.setenv("CARDDAV_PASSWORD", "synthetic-password")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-propagate")
    calls: list[tuple[list[str], dict[str, Any]]] = []

    class Result:
        returncode = 0

    def fake_run(argv: list[str], **kwargs: Any) -> Result:
        calls.append((argv, kwargs))
        config = Path(argv[argv.index("-c") + 1])
        rendered = config.read_text()
        assert "synthetic-user" not in rendered
        assert "synthetic-password" not in rendered
        assert 'username.fetch = ["command", "/usr/bin/printenv", "CARDDAV_USERNAME"]' in rendered
        assert 'password.fetch = ["command", "/usr/bin/printenv", "CARDDAV_PASSWORD"]' in rendered
        assert 'read_only = true' in rendered
        assert 'partial_sync = "revert"' in rendered
        assert 'conflict_resolution = "a wins"' in rendered
        assert 'collections = ["contacts-a", "contacts-b"]' in rendered
        assert stat.S_IMODE(config.stat().st_mode) == 0o600
        runtime = tmp_path / "carddav-contacts/profiles/demo/runtime"
        assert (runtime / "mirror/contacts-a").is_dir()
        assert (runtime / "mirror/contacts-b").is_dir()
        assert kwargs["stdin"] is module_subprocess.DEVNULL
        assert kwargs["stdout"] is module_subprocess.DEVNULL
        assert kwargs["stderr"] is module_subprocess.DEVNULL
        assert kwargs["timeout"] == 120
        assert kwargs["umask"] == 0o077
        child_env = kwargs["env"]
        assert child_env["PATH"] == os.defpath
        assert child_env["CARDDAV_USERNAME"] == "synthetic-user"
        assert child_env["CARDDAV_PASSWORD"] == "synthetic-password"
        assert "UNRELATED_SECRET" not in child_env
        return Result()

    module_subprocess = module.transport.subprocess
    monkeypatch.setattr(module_subprocess, "run", fake_run)
    assert module.main(["discover", "--profile", "demo"]) == 0
    assert len(calls) == 1
    argv, _kwargs = calls[0]
    assert argv[:5] == [module.sys.executable, "-m", "vdirsyncer", "-c", argv[4]]
    assert argv[-2:] == ["discover", "contacts"]
    assert not list((tmp_path / "carddav-contacts/profiles/demo/runtime").glob("*.conf"))
    assert not (tmp_path / "carddav-contacts/profiles/demo/runtime/discovery.json").exists()


def test_discover_uses_dav_pair_when_carddav_pair_is_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module: ModuleType
) -> None:
    _setup(module, tmp_path)
    monkeypatch.setenv("DAV_USERNAME", "dav-user")
    monkeypatch.setenv("DAV_PASSWORD", "dav-password")
    calls: list[dict[str, Any]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        calls.append(kwargs)
        return module.transport.subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(module.transport.subprocess, "run", fake_run)
    assert module.main(["discover", "--profile", "demo"]) == 0
    assert calls[0]["env"]["DAV_USERNAME"] == "dav-user"
    assert calls[0]["env"]["DAV_PASSWORD"] == "dav-password"
    assert "CARDDAV_USERNAME" not in calls[0]["env"]
    assert "CARDDAV_PASSWORD" not in calls[0]["env"]


@pytest.mark.parametrize(
    "env",
    [
        {"CARDDAV_USERNAME": "only-carddav"},
        {"CARDDAV_USERNAME": "", "CARDDAV_PASSWORD": "carddav-password"},
        {"CARDDAV_USERNAME": "carddav-user", "CARDDAV_PASSWORD": ""},
        {"CARDDAV_USERNAME": "", "CARDDAV_PASSWORD": "", "DAV_USERNAME": "dav-user", "DAV_PASSWORD": "dav-password"},
        {"DAV_USERNAME": "dav-user"},
        {"DAV_USERNAME": "", "DAV_PASSWORD": "dav-password"},
    ],
)
def test_discover_rejects_partial_or_empty_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module: ModuleType, env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    _setup(module, tmp_path)
    for key in ("CARDDAV_USERNAME", "CARDDAV_PASSWORD", "DAV_USERNAME", "DAV_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(module.transport.subprocess, "run", pytest.fail)

    assert module.main(["discover", "--profile", "demo"]) == 2
    assert capsys.readouterr() == ("", "error: invalid credentials\n")


def test_discover_failure_is_fixed_and_config_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    _setup(module, tmp_path)
    monkeypatch.setenv("DAV_USERNAME", "dav-user")
    monkeypatch.setenv("DAV_PASSWORD", "dav-password")

    def fail(*args: Any, **kwargs: Any) -> Any:
        return module.transport.subprocess.CompletedProcess(args[0], 1)

    monkeypatch.setattr(module.transport.subprocess, "run", fail)
    assert module.main(["discover", "--profile", "demo"]) == 2
    assert capsys.readouterr() == ("", "error: discover failed\n")
    runtime = tmp_path / "carddav-contacts/profiles/demo/runtime"
    assert list(runtime.glob(".vdirsyncer.*")) == []


def test_network_command_rejects_invalid_operation_without_creating_state(
    tmp_path: Path, module: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    assert module.main(["discover", "--profile", "Bad/Name"]) == 2
    assert capsys.readouterr() == ("", "error: invalid operation\n")
    assert not list(tmp_path.iterdir())


def test_unknown_operation_uses_fixed_invalid_operation_error(
    module: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    assert module.main(["unknown"]) == 2
    assert capsys.readouterr() == ("", "error: invalid operation\n")


def test_malformed_existing_profile_is_unsafe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    _setup(module, tmp_path)
    profile = tmp_path / "carddav-contacts/profiles/demo/profile.json"
    profile.write_text("{}")
    monkeypatch.setenv("DAV_USERNAME", "dav-user")
    monkeypatch.setenv("DAV_PASSWORD", "dav-password")
    assert module.main(["discover", "--profile", "demo"]) == 2
    assert capsys.readouterr() == ("", "error: unsafe profile state\n")


# Imported under an alias so tests can assert identity without depending on a
# private implementation detail of the entry point.
module_subprocess: Any
