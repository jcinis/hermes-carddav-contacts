"""CardDAV contacts skill entry point.

`version --json` and `setup` are implemented. `version --json` reports
command/source schema versions, this package's version, capabilities, and
pinned runtime dependency versions as a single JSON object, with no
filesystem, network, or subprocess access. `setup` writes a new profile
under `$HERMES_HOME/carddav-contacts/profiles/<profile>/profile.json` from
`--profile`, `--namespace`, `--server-url`, and one or more `--collection`
flags, with no network or subprocess access. `version` without `--json`,
no arguments, and every other command (sync, search, show, snapshot,
audit, ...) are not yet implemented; see SKILL.md and references/ for the
planned command and data-model contracts.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import tempfile
from pathlib import Path

COMMAND_SCHEMA_VERSION = "carddav-command/1.0"
PACKAGE_VERSION = "0.1.0"
SUPPORTED_SOURCE_SCHEMA_VERSIONS = ["carddav-source/1.0"]
CAPABILITIES = {
    "read_only": True,
    "create_update": False,
    "cleanup_delete": False,
}
DEPENDENCY_VERSIONS = {
    "vdirsyncer": "0.21.0",
    "vobject": "0.9.9",
}

PROFILE_SCHEMA_VERSION = "carddav-profile/1.0"
SYNC_CADENCE_SECONDS = 3600
MANAGED_DIR_MODE = 0o700
MANAGED_FILE_MODE = 0o600


def _version() -> dict[str, object]:
    return {
        "command_schema_version": COMMAND_SCHEMA_VERSION,
        "command": "version",
        "package_version": PACKAGE_VERSION,
        "supported_source_schema_versions": SUPPORTED_SOURCE_SCHEMA_VERSIONS,
        "capabilities": CAPABILITIES,
        "dependency_versions": DEPENDENCY_VERSIONS,
    }


def _parse_setup_args(args: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="carddav_contacts.py setup")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--server-url", required=True)
    parser.add_argument("--collection", action="append", required=True)
    return parser.parse_args(args)


def _build_profile(namespace: str, server_url: str, collections: list[str]) -> dict[str, object]:
    normalized_server_url = server_url if server_url.endswith("/") else server_url + "/"
    return {
        "profile_schema_version": PROFILE_SCHEMA_VERSION,
        "account_namespace": namespace,
        "server_url": normalized_server_url,
        "collection_allowlist": sorted(collections),
        "sync_cadence_seconds": SYNC_CADENCE_SECONDS,
        "capabilities": dict(CAPABILITIES),
    }


def _write_profile_json(path: Path, profile: dict[str, object]) -> None:
    payload = json.dumps(profile, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    directory = path.parent
    fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=".profile.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as tmp_file:
            tmp_file.write(payload)
            tmp_file.flush()
            os.fchmod(tmp_file.fileno(), MANAGED_FILE_MODE)
            os.fsync(tmp_file.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        os.unlink(tmp_name)
        raise
    dir_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _setup(args: argparse.Namespace) -> int:
    hermes_home = Path(os.environ["HERMES_HOME"])
    root = hermes_home / "carddav-contacts"
    profiles_dir = root / "profiles"
    profile_dir = profiles_dir / args.profile

    for directory in (root, profiles_dir, profile_dir):
        directory.mkdir(mode=MANAGED_DIR_MODE, exist_ok=True)
        os.chmod(directory, MANAGED_DIR_MODE)

    lock_path = profile_dir / "profile.lock"
    lock_path.touch(exist_ok=True)
    os.chmod(lock_path, MANAGED_FILE_MODE)

    with open(lock_path) as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            profile = _build_profile(args.namespace, args.server_url, args.collection)
            _write_profile_json(profile_dir / "profile.json", profile)
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args == ["version", "--json"]:
        print(json.dumps(_version()))
        return 0
    if args[:1] == ["setup"]:
        parsed = _parse_setup_args(args[1:])
        return _setup(parsed)
    return 1


if __name__ == "__main__":
    sys.exit(main())
