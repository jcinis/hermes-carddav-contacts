"""Read-only CardDAV synchronization contract tests."""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
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
    spec = importlib.util.spec_from_file_location("carddav_contacts_sync", ENTRYPOINT)
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
            ]
        ) == 0


@pytest.fixture
def module() -> ModuleType:
    return _load()


def test_sync_requires_native_discovery_state_and_does_not_create_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    _setup(module, tmp_path)
    monkeypatch.setenv("DAV_USERNAME", "dav-user")
    monkeypatch.setenv("DAV_PASSWORD", "dav-password")
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        calls.append(argv)
        return module.transport.subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(module.transport.subprocess, "run", fake_run)
    assert module.main(["sync", "--profile", "demo"]) == 2
    assert capsys.readouterr() == ("", "error: sync failed\n")
    assert calls == []
    assert not (tmp_path / "carddav-contacts/profiles/demo/runtime/status").exists()


def test_sync_uses_existing_status_and_only_selected_profile_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module: ModuleType
) -> None:
    _setup(module, tmp_path, "alpha")
    _setup(module, tmp_path, "beta")
    runtime = tmp_path / "carddav-contacts/profiles/alpha/runtime"
    runtime.mkdir(mode=0o700)
    (runtime / "status").mkdir(mode=0o700)
    (runtime / "status" / "contacts.collections").write_bytes(b"native status")
    (runtime / "status" / "contacts.collections").chmod(0o600)
    monkeypatch.setenv("DAV_USERNAME", "dav-user")
    monkeypatch.setenv("DAV_PASSWORD", "dav-password")
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        calls.append((argv, kwargs))
        config = Path(argv[argv.index("-c") + 1])
        text = config.read_text()
        assert 'path = ' + repr(str(runtime / "mirror")) not in text
        assert str(runtime / "mirror") in text
        assert str(tmp_path / "carddav-contacts/profiles/beta") not in text
        return module.transport.subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(module.transport.subprocess, "run", fake_run)
    assert module.main(["sync", "--profile", "alpha"]) == 0
    assert calls[0][0][-2:] == ["sync", "contacts"]
    assert (runtime / "status" / "contacts.collections").read_bytes() == b"native status"
    assert not list(runtime.glob(".vdirsyncer.*"))


def test_sync_timeout_is_fixed_and_preserves_existing_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    _setup(module, tmp_path)
    runtime = tmp_path / "carddav-contacts/profiles/demo/runtime"
    runtime.mkdir(mode=0o700)
    status = runtime / "status"
    status.mkdir(mode=0o700)
    (status / "contacts.collections").write_bytes(b"status")
    (status / "contacts.collections").chmod(0o600)
    mirror = runtime / "mirror"
    mirror.mkdir(mode=0o700)
    (mirror / "contacts-a").mkdir(mode=0o700)
    monkeypatch.setenv("DAV_USERNAME", "dav-user")
    monkeypatch.setenv("DAV_PASSWORD", "dav-password")

    def timeout(*args: Any, **kwargs: Any) -> Any:
        raise module.transport.subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(module.transport.subprocess, "run", timeout)
    assert module.main(["sync", "--profile", "demo"]) == 2
    assert capsys.readouterr() == ("", "error: sync failed\n")
    assert (status / "contacts.collections").read_bytes() == b"status"
    assert not list(runtime.glob(".vdirsyncer.*"))


def test_config_cleanup_failure_is_not_reported_as_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    _setup(module, tmp_path)
    monkeypatch.setenv("DAV_USERNAME", "dav-user")
    monkeypatch.setenv("DAV_PASSWORD", "dav-password")

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        return module.transport.subprocess.CompletedProcess(argv, 0)

    real_unlink = Path.unlink

    def fail_config_unlink(path: Path, *args: Any, **kwargs: Any) -> None:
        if path.name.startswith(".vdirsyncer."):
            raise PermissionError("synthetic cleanup failure")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(module.transport.subprocess, "run", fake_run)
    monkeypatch.setattr(Path, "unlink", fail_config_unlink)
    assert module.main(["discover", "--profile", "demo"]) == 2
    assert capsys.readouterr() == ("", "error: discover failed\n")


def test_sync_runs_a_second_native_pass_and_fails_if_it_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    _setup(module, tmp_path)
    runtime = tmp_path / "carddav-contacts/profiles/demo/runtime"
    runtime.mkdir(mode=0o700)
    status = runtime / "status"
    status.mkdir(mode=0o700)
    (status / "contacts.collections").write_bytes(b"cache")
    (status / "contacts.collections").chmod(0o600)
    monkeypatch.setenv("DAV_USERNAME", "dav-user")
    monkeypatch.setenv("DAV_PASSWORD", "dav-password")
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        calls.append(argv)
        return module.transport.subprocess.CompletedProcess(argv, 1 if len(calls) == 2 else 0)

    monkeypatch.setattr(module.transport.subprocess, "run", fake_run)
    assert module.main(["sync", "--profile", "demo"]) == 2
    assert capsys.readouterr() == ("", "error: sync failed\n")
    assert len(calls) == 2
    assert (status / "contacts.collections").read_bytes() == b"cache"
    assert not list(runtime.glob(".vdirsyncer.*"))


def test_unsafe_runtime_state_is_not_repaired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    _setup(module, tmp_path)
    runtime = tmp_path / "carddav-contacts/profiles/demo/runtime"
    runtime.mkdir(mode=0o700)
    (runtime / "status").write_bytes(b"status")
    (runtime / "status").chmod(0o644)
    monkeypatch.setenv("DAV_USERNAME", "dav-user")
    monkeypatch.setenv("DAV_PASSWORD", "dav-password")
    assert module.main(["sync", "--profile", "demo"]) == 2
    assert capsys.readouterr() == ("", "error: unsafe profile state\n")
    assert (runtime / "status").stat().st_mode & 0o777 == 0o644


def test_sync_publishes_an_indexed_generation_from_the_mirror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    _setup(module, tmp_path)
    profile_dir = tmp_path / "carddav-contacts/profiles/demo"
    runtime = profile_dir / "runtime"
    runtime.mkdir(mode=0o700)
    status = runtime / "status"
    status.mkdir(mode=0o700)
    (status / "contacts.collections").write_bytes(b"cache")
    (status / "contacts.collections").chmod(0o600)
    monkeypatch.setenv("DAV_USERNAME", "dav-user")
    monkeypatch.setenv("DAV_PASSWORD", "dav-password")
    card = b"BEGIN:VCARD\r\nVERSION:3.0\r\nUID:one\r\nFN:Example One\r\nEND:VCARD\r\n"

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        path = runtime / "mirror" / "contacts-a" / "one.vcf"
        path.write_bytes(card)
        path.chmod(0o600)
        return module.transport.subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(module.transport.subprocess, "run", fake_run)
    assert module.main(["sync", "--profile", "demo"]) == 0
    assert capsys.readouterr() == ("", "")

    assert module.main(["status", "--profile", "demo", "--json"]) == 0
    generation = json.loads(capsys.readouterr().out)["current_generation"]
    published = profile_dir / "generations" / generation
    assert (published / "mirror" / "contacts-a" / "one.vcf").read_bytes() == card
    connection = sqlite3.connect(f"file:{published / 'index.sqlite3'}?mode=ro", uri=True)
    try:
        rows = connection.execute("SELECT display_name FROM contacts").fetchall()
    finally:
        connection.close()
    assert rows == [("Example One",)]


def test_unsafe_generations_root_fails_sync_as_unsafe_profile_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    _setup(module, tmp_path)
    profile_dir = tmp_path / "carddav-contacts/profiles/demo"
    runtime = profile_dir / "runtime"
    runtime.mkdir(mode=0o700)
    status = runtime / "status"
    status.mkdir(mode=0o700)
    (status / "contacts.collections").write_bytes(b"cache")
    (status / "contacts.collections").chmod(0o600)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    (profile_dir / "generations").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("DAV_USERNAME", "dav-user")
    monkeypatch.setenv("DAV_PASSWORD", "dav-password")

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        return module.transport.subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(module.transport.subprocess, "run", fake_run)
    assert module.main(["sync", "--profile", "demo"]) == 2
    assert capsys.readouterr() == ("", "error: unsafe profile state\n")
    assert list(outside.iterdir()) == []
    assert not (profile_dir / "current").exists()
