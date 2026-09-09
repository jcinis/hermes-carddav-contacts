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
import ipaddress
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import NoReturn

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
IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_URL_STRUCTURE_PATTERN = re.compile(r"\A(?P<scheme>[^:/?#]*)://(?P<authority>[^/?#]*)[^?#]*\Z")
_CONTROL_OR_WHITESPACE_PATTERN = re.compile(r"[\x00-\x20\x7f]")
_PORT_PATTERN = re.compile(r"[0-9]+")
_MAX_PORT = 65535
_MAX_PORT_DIGITS = len(str(_MAX_PORT))
_SUPPORTED_URL_SCHEMES = ("http", "https")


def _version() -> dict[str, object]:
    return {
        "command_schema_version": COMMAND_SCHEMA_VERSION,
        "command": "version",
        "package_version": PACKAGE_VERSION,
        "supported_source_schema_versions": SUPPORTED_SOURCE_SCHEMA_VERSIONS,
        "capabilities": CAPABILITIES,
        "dependency_versions": DEPENDENCY_VERSIONS,
    }


class _InvalidSetup(Exception):
    """Raised for any invalid `setup` invocation; maps to a fixed exit-2 result."""


class _SetupArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise _InvalidSetup(message)


_SINGLETON_OPTIONS = ("--profile", "--namespace", "--server-url")


def _reject_repeated_singleton_options(args: list[str]) -> None:
    for option in _SINGLETON_OPTIONS:
        occurrences = sum(1 for arg in args if arg == option or arg.startswith(option + "="))
        if occurrences > 1:
            raise _InvalidSetup(f"repeated option: {option}")


def _parse_setup_args(args: list[str]) -> argparse.Namespace:
    _reject_repeated_singleton_options(args)
    parser = _SetupArgumentParser(
        prog="carddav_contacts.py setup", add_help=False, allow_abbrev=False
    )
    parser.add_argument("--profile", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--server-url", required=True)
    parser.add_argument("--collection", action="append", required=True)
    return parser.parse_args(args)


def _validate_port(remainder: str) -> None:
    if remainder == "":
        return
    if not _PORT_PATTERN.fullmatch(remainder[1:]) or not remainder.startswith(":"):
        raise _InvalidSetup("invalid server URL: malformed port")
    digits = remainder[1:].lstrip("0") or "0"
    if len(digits) > _MAX_PORT_DIGITS or int(digits) > _MAX_PORT:
        raise _InvalidSetup("invalid server URL: port out of range")


def _validate_authority(authority: str) -> None:
    if "@" in authority:
        raise _InvalidSetup("invalid server URL: userinfo not allowed")
    if authority.startswith("["):
        end = authority.find("]")
        if end == -1:
            raise _InvalidSetup("invalid server URL: malformed IPv6 host")
        host, remainder = authority[1:end], authority[end + 1 :]
        try:
            ipaddress.IPv6Address(host)
        except ValueError as exc:
            raise _InvalidSetup("invalid server URL: malformed IPv6 host") from exc
        _validate_port(remainder)
        return
    host, sep, port = authority.partition(":")
    if not host:
        raise _InvalidSetup("invalid server URL: missing host")
    _validate_port(sep + port)


def _validate_server_url(url: str) -> None:
    if _CONTROL_OR_WHITESPACE_PATTERN.search(url):
        raise _InvalidSetup("invalid server URL: control or whitespace character")
    if "?" in url or "#" in url:
        raise _InvalidSetup("invalid server URL: query or fragment delimiter")
    match = _URL_STRUCTURE_PATTERN.fullmatch(url)
    if match is None or match.group("scheme") not in _SUPPORTED_URL_SCHEMES:
        raise _InvalidSetup("invalid server URL: missing or unsupported scheme")
    _validate_authority(match.group("authority"))


def _normalize_server_url(url: str) -> str:
    return url.rstrip("/") + "/"


def _validate_setup_args(args: argparse.Namespace) -> None:
    if not IDENTIFIER_PATTERN.fullmatch(args.profile):
        raise _InvalidSetup("invalid profile identifier")
    if not IDENTIFIER_PATTERN.fullmatch(args.namespace):
        raise _InvalidSetup("invalid namespace identifier")
    _validate_server_url(args.server_url)
    for collection in args.collection:
        if not IDENTIFIER_PATTERN.fullmatch(collection):
            raise _InvalidSetup("invalid collection identifier")
    if len(set(args.collection)) != len(args.collection):
        raise _InvalidSetup("duplicate collection")


def _build_profile(namespace: str, server_url: str, collections: list[str]) -> dict[str, object]:
    return {
        "profile_schema_version": PROFILE_SCHEMA_VERSION,
        "account_namespace": namespace,
        "server_url": _normalize_server_url(server_url),
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


def _resolve_hermes_home() -> Path:
    if "HERMES_HOME" in os.environ:
        candidate = Path(os.environ["HERMES_HOME"])
        if not candidate.is_absolute():
            raise _InvalidSetup("invalid HERMES_HOME: must be an absolute path")
        return candidate
    home = os.environ.get("HOME")
    if home is None or not Path(home).is_absolute():
        raise _InvalidSetup("invalid HOME: must be an absolute path")
    return Path(home) / ".hermes"


def _setup(args: argparse.Namespace, hermes_home: Path) -> int:
    root = hermes_home / "carddav-contacts"
    profiles_dir = root / "profiles"
    profile_dir = profiles_dir / args.profile

    hermes_home.mkdir(mode=MANAGED_DIR_MODE, exist_ok=True)

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
        try:
            parsed = _parse_setup_args(args[1:])
            _validate_setup_args(parsed)
            hermes_home = _resolve_hermes_home()
        except _InvalidSetup:
            print("error: invalid setup", file=sys.stderr)
            return 2
        return _setup(parsed, hermes_home)
    return 1


if __name__ == "__main__":
    sys.exit(main())
