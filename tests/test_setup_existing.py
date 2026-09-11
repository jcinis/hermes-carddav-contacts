"""Setup preserves existing profiles and rejects unsafe state."""

import fcntl
import json
import os
import select
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_setup import ENTRYPOINT, _load_carddav_contacts_module, _run, _valid_setup_argv


def _created_profile(tmp_path: Path) -> tuple[dict[str, str], Path]:
    env = {"HERMES_HOME": str(tmp_path), "PATH": ""}
    result = _run(_valid_setup_argv(), env)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    return env, tmp_path / "carddav-contacts/profiles/demo/profile.json"


def test_repeat_setup_is_noop(tmp_path: Path) -> None:
    env, path = _created_profile(tmp_path)
    before = path.stat()
    raw = path.read_bytes()
    result = _run(_valid_setup_argv(), env)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert path.read_bytes() == raw
    assert (path.stat().st_ino, path.stat().st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


@pytest.mark.parametrize("change", [
    {"namespace": "different"},
    {"server-url": "https://other.example.invalid/dav"},
    {"collections": ["other-book"]},
])
def test_conflicting_setup_preserves_profile(tmp_path: Path, change: dict[str, object]) -> None:
    env, path = _created_profile(tmp_path)
    before = path.read_bytes()
    result = _run(_valid_setup_argv(**change), env)
    assert (result.returncode, result.stdout, result.stderr) == (
        2, "", "error: profile conflict\n"
    )
    assert path.read_bytes() == before


@pytest.mark.parametrize("damage", [
    "malformed", "noncanonical", "unknown-key", "duplicate-key", "bad-types",
    "legacy-capabilities",
])
def test_corrupt_profile_is_refused_not_replaced(tmp_path: Path, damage: str) -> None:
    env, path = _created_profile(tmp_path)
    original = path.read_bytes()
    data = json.loads(original)
    if damage == "malformed":
        raw = b"not json"
    elif damage == "noncanonical":
        raw = json.dumps(data, indent=2).encode()
    elif damage == "duplicate-key":
        raw = original.replace(b'{', b'{"account_namespace":"discarded",', 1)
    else:
        if damage == "unknown-key":
            data["extra"] = "unrecognized"
        elif damage == "bad-types":
            data["collection_allowlist"] = None
        else:
            # The capability block belongs to carddav-profile/1.0 only; carrying
            # it on a 1.1 profile is an unknown key, not a tolerated leftover.
            data["capabilities"] = {
                "read_only": True, "create_update": False, "cleanup_delete": False,
            }
        raw = json.dumps(data, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    path.write_bytes(raw)
    result = _run(_valid_setup_argv(), env)
    assert (result.returncode, result.stdout, result.stderr) == (
        2, "", "error: unsafe profile state\n"
    )
    assert path.read_bytes() == raw


@pytest.mark.parametrize("relative", ["carddav-contacts", "carddav-contacts/profiles",
                                       "carddav-contacts/profiles/demo"])
@pytest.mark.parametrize("damage", ["symlink", "mode", "file"])
def test_unsafe_directory_is_not_followed_or_repaired(
    tmp_path: Path, relative: str, damage: str
) -> None:
    target = tmp_path / relative
    for parent in reversed(target.relative_to(tmp_path).parents):
        (tmp_path / parent).mkdir(mode=0o700, exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    if damage == "symlink":
        target.symlink_to(outside, target_is_directory=True)
    elif damage == "mode":
        target.mkdir(mode=0o755)
        target.chmod(0o755)
    else:
        target.write_bytes(b"untouched")
    before = target.lstat()
    result = _run(_valid_setup_argv(), {"HERMES_HOME": str(tmp_path), "PATH": ""})
    assert (result.returncode, result.stdout, result.stderr) == (
        2, "", "error: unsafe profile state\n"
    )
    assert target.lstat().st_mode == before.st_mode
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("filename", ["profile.json", "profile.lock"])
@pytest.mark.parametrize("damage", ["symlink", "dangling", "hardlink", "mode", "directory"])
def test_unsafe_file_is_not_opened_or_repaired(
    tmp_path: Path, filename: str, damage: str
) -> None:
    env, path = _created_profile(tmp_path)
    target = path.parent / filename
    outside = tmp_path / "outside-file"
    outside.write_bytes(target.read_bytes())
    outside.chmod(0o600)
    target.unlink()
    if damage == "symlink":
        target.symlink_to(outside)
    elif damage == "dangling":
        target.symlink_to(tmp_path / "absent")
    elif damage == "hardlink":
        os.link(outside, target)
    elif damage == "mode":
        target.write_bytes(outside.read_bytes())
        target.chmod(0o644)
    else:
        target.mkdir(mode=0o700)
    before = target.lstat()
    outside_before = (outside.read_bytes(), outside.stat().st_mode)
    result = _run(_valid_setup_argv(), env)
    assert (result.returncode, result.stdout, result.stderr) == (
        2, "", "error: unsafe profile state\n"
    )
    assert target.lstat().st_mode == before.st_mode
    assert (outside.read_bytes(), outside.stat().st_mode) == outside_before


def test_wrong_owner_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                capsys: pytest.CaptureFixture[str]) -> None:
    env, _ = _created_profile(tmp_path)
    module = _load_carddav_contacts_module()
    monkeypatch.setenv("HERMES_HOME", env["HERMES_HOME"])
    monkeypatch.setattr(module.os, "getuid", lambda: os.geteuid() + 1)
    assert module.main(_valid_setup_argv()) == 2
    assert capsys.readouterr() == ("", "error: unsafe profile state\n")


def test_filesystem_error_is_fixed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                    capsys: pytest.CaptureFixture[str]) -> None:
    module = _load_carddav_contacts_module()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    def fail(*args: object, **kwargs: object) -> None:
        raise PermissionError("sensitive path must not reach stderr")

    monkeypatch.setattr(module.profiles, "write_profile_json", fail)
    assert module.main(_valid_setup_argv()) == 2
    assert capsys.readouterr() == ("", "error: setup failed\n")
    assert not (tmp_path / "carddav-contacts/profiles/demo/profile.json").exists()


@pytest.mark.parametrize("namespace", ["example", "different"])
def test_waiting_setup_reads_profile_only_after_lock_release(
    tmp_path: Path, namespace: str
) -> None:
    env, path = _created_profile(tmp_path)
    raw = path.read_bytes()
    path.unlink()
    # Signal immediately before attempting the real flock: no blind startup sleep.
    code = (
        "import fcntl, runpy, sys\n"
        "original = fcntl.flock\n"
        "def observed(fd, operation):\n"
        "    if operation == fcntl.LOCK_EX:\n"
        "        print('locking', flush=True)\n"
        "    return original(fd, operation)\n"
        "fcntl.flock = observed\n"
        f"sys.argv = {[str(ENTRYPOINT)] + _valid_setup_argv(namespace=namespace)!r}\n"
        f"runpy.run_path({str(ENTRYPOINT)!r}, run_name='__main__')\n"
    )
    with (path.parent / "profile.lock").open() as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        process = subprocess.Popen(
            [sys.executable, "-c", code], env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            assert process.stdout is not None
            # select bounds the readiness handshake if the worker regresses.
            assert select.select([process.stdout], [], [], 5)[0]
            assert process.stdout.readline() == "locking\n"
            with pytest.raises(subprocess.TimeoutExpired):
                process.wait(timeout=0.2)
            assert not path.exists()
            # This process owns the lock, representing the first cooperating setup.
            path.write_bytes(raw)
            path.chmod(0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            stdout, stderr = process.communicate(timeout=5)
            expected = (0, "", "") if namespace == "example" else (
                2, "", "error: profile conflict\n"
            )
            assert (process.returncode, stdout, stderr) == expected
            assert path.read_bytes() == raw
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()


def test_simultaneous_fresh_setup_has_one_winner(tmp_path: Path) -> None:
    env = {"HERMES_HOME": str(tmp_path), "PATH": ""}
    processes = [subprocess.Popen(
        [sys.executable, str(ENTRYPOINT)] + _valid_setup_argv(namespace=name),
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ) for name in ("first", "second")]
    try:
        outputs = [process.communicate(timeout=5) for process in processes]
        assert sorted(process.returncode for process in processes) == [0, 2]
        for process, output in zip(processes, outputs):
            assert output == (("", "") if process.returncode == 0 else (
                "", "error: profile conflict\n"
            ))
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.communicate()


@pytest.mark.parametrize("damage", ["symlink", "dangling", "file"])
def test_unsafe_hermes_home_is_not_followed_or_replaced(tmp_path: Path, damage: str) -> None:
    """An existing HERMES_HOME that is not a real directory is refused before any child write."""
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    absent = tmp_path / "absent"
    home = tmp_path / "home"
    if damage == "symlink":
        home.symlink_to(outside, target_is_directory=True)
    elif damage == "dangling":
        home.symlink_to(absent, target_is_directory=True)
    else:
        home.write_bytes(b"untouched")
    before = home.lstat()
    env = {"HERMES_HOME": str(home), "PATH": ""}

    result = _run(_valid_setup_argv(), env)

    assert (result.returncode, result.stdout, result.stderr) == (
        2, "", "error: unsafe profile state\n"
    )
    assert home.lstat().st_mode == before.st_mode
    assert list(outside.iterdir()) == []
    assert not absent.exists()
    # The refusal must match what every later command reports for the same home.
    status = _run(["status", "--profile", "demo", "--json"], env)
    assert (status.returncode, status.stdout, status.stderr) == (
        2, "", "error: unsafe profile state\n"
    )


def test_wrong_owner_hermes_home_is_refused_before_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The direct entry point refuses a foreign-owned home without creating state below it."""
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    module = _load_carddav_contacts_module()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(module.os, "getuid", lambda: os.geteuid() + 1)

    assert module.main(_valid_setup_argv()) == 2

    assert capsys.readouterr() == ("", "error: unsafe profile state\n")
    assert list(home.iterdir()) == []


def test_operator_home_mode_is_accepted_and_not_repaired(tmp_path: Path) -> None:
    """An operator-provided home root keeps its own mode; only skill-owned state is 0700."""
    home = tmp_path / "home"
    home.mkdir(mode=0o755)
    home.chmod(0o755)
    env = {"HERMES_HOME": str(home), "PATH": ""}

    result = _run(_valid_setup_argv(), env)

    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert stat.S_IMODE(home.stat().st_mode) == 0o755
    assert stat.S_IMODE((home / "carddav-contacts").stat().st_mode) == 0o700
