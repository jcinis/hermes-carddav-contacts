"""Private vdirsyncer transport for read-only CardDAV operations."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

MANAGED_DIR_MODE = 0o700
MANAGED_FILE_MODE = 0o600
OPERATION_TIMEOUT = 120


class InvalidCredentials(Exception):
    """The environment does not contain one complete credential pair."""


class UnsafeRuntimeState(Exception):
    """A persisted runtime path is not private and regular."""


class OperationFailed(Exception):
    """The native vdirsyncer operation did not complete successfully."""


def resolve_credentials(environ: Mapping[str, str]) -> tuple[str, str, str, str]:
    """Select one complete pair without mixing the two supported namespaces."""
    carddav_keys = ("CARDDAV_USERNAME", "CARDDAV_PASSWORD")
    dav_keys = ("DAV_USERNAME", "DAV_PASSWORD")
    carddav_present = any(key in environ for key in carddav_keys)
    if carddav_present:
        if all(environ.get(key, "") for key in carddav_keys):
            return (*carddav_keys, environ[carddav_keys[0]], environ[carddav_keys[1]])
        raise InvalidCredentials
    if all(environ.get(key, "") for key in dav_keys):
        return (*dav_keys, environ[dav_keys[0]], environ[dav_keys[1]])
    raise InvalidCredentials


def _private_existing(path: Path, *, directory: bool) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    valid_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (
        not valid_type
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != (MANAGED_DIR_MODE if directory else MANAGED_FILE_MODE)
        or (not directory and info.st_nlink != 1)
    ):
        raise UnsafeRuntimeState


def _check_tree(path: Path) -> None:
    """Reject unsafe existing entries without following links or repairing them."""
    _private_existing(path, directory=True)
    if not path.exists():
        return
    for child in path.iterdir():
        try:
            info = child.lstat()
        except FileNotFoundError as exc:
            raise UnsafeRuntimeState from exc
        if stat.S_ISDIR(info.st_mode):
            _check_tree(child)
        else:
            _private_existing(child, directory=False)


def _mkdir_private(path: Path) -> None:
    if path.exists() or path.is_symlink():
        _private_existing(path, directory=True)
        return
    try:
        path.mkdir(mode=MANAGED_DIR_MODE)
    except FileExistsError:
        _private_existing(path, directory=True)


def prepare_runtime(runtime: Path, collections: list[str]) -> None:
    """Preflight and create only the selected profile's private runtime paths."""
    if runtime.exists() or runtime.is_symlink():
        _check_tree(runtime)
    else:
        _mkdir_private(runtime)
    mirror = runtime / "mirror"
    _mkdir_private(mirror)
    for collection in collections:
        _mkdir_private(mirror / collection)
    _check_tree(runtime)


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_config(
    profile: Mapping[str, object], runtime: Path, username_var: str, password_var: str
) -> bytes:
    """Render the complete ephemeral native vdirsyncer configuration."""
    server_url = profile["server_url"]
    collections = profile["collection_allowlist"]
    if not isinstance(server_url, str) or not isinstance(collections, list):
        raise OperationFailed
    if not all(isinstance(value, str) for value in collections):
        raise OperationFailed
    local_path = runtime / "mirror"
    lines = [
        "[general]",
        f"status_path = {_toml_string(str(runtime / 'status'))}",
        "",
        "[storage remote]",
        'type = "carddav"',
        f"url = {_toml_string(server_url)}",
        f"username.fetch = [\"command\", \"/usr/bin/printenv\", {_toml_string(username_var)}]",
        f"password.fetch = [\"command\", \"/usr/bin/printenv\", {_toml_string(password_var)}]",
        "read_only = true",
        "",
        "[storage local]",
        'type = "filesystem"',
        f"path = {_toml_string(str(local_path))}",
        'fileext = ".vcf"',
        "",
        "[pair contacts]",
        'a = "remote"',
        'b = "local"',
        f"collections = {json.dumps(collections, ensure_ascii=False)}",
        'partial_sync = "revert"',
        'conflict_resolution = "a wins"',
        "",
    ]
    return "\n".join(lines).encode("utf-8")


def _write_config(runtime: Path, payload: bytes) -> Path:
    fd, raw_path = tempfile.mkstemp(dir=runtime, prefix=".vdirsyncer.", suffix=".conf")
    path = Path(raw_path)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fchmod(handle.fileno(), MANAGED_FILE_MODE)
            os.fsync(handle.fileno())
        return path
    except BaseException:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise


def run_operation(command: str, profile: Mapping[str, object], profile_dir: Path) -> None:
    """Run exactly one native operation with a sanitized environment."""
    runtime = profile_dir / "runtime"
    collections = profile.get("collection_allowlist")
    if not isinstance(collections, list) or not all(isinstance(value, str) for value in collections):
        raise OperationFailed
    username_var, password_var, username, password = resolve_credentials(os.environ)
    prepare_runtime(runtime, collections)
    status = runtime / "status"
    if command == "sync":
        _private_existing(status, directory=True)
        cache = status / "contacts.collections"
        _private_existing(cache, directory=False)
        if not status.exists() or not cache.exists():
            raise OperationFailed
    config_path: Path | None = None
    failure = False
    try:
        config_path = _write_config(
            runtime, render_config(profile, runtime, username_var, password_var)
        )
        child_env = {
            "PATH": os.defpath,
            username_var: username,
            password_var: password,
        }
        passes = 2 if command == "sync" else 1
        for _ in range(passes):
            completed = subprocess.run(
                [sys.executable, "-m", "vdirsyncer", "-c", str(config_path), command, "contacts"],
                cwd=runtime,
                env=child_env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=OPERATION_TIMEOUT,
                umask=0o077,
                check=False,
            )
            if completed.returncode != 0:
                raise OperationFailed
    except (OSError, subprocess.SubprocessError, OperationFailed, ValueError, TypeError, KeyError, RuntimeError):
        failure = True
    finally:
        if config_path is not None:
            try:
                config_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                failure = True
    if failure:
        raise OperationFailed
