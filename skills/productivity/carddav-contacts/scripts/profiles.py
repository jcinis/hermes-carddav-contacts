# ABOUTME: The profile store shared by the command surface and the installable API.
# ABOUTME: Accepts carddav-profile/1.0 and 1.1; never rewrites an existing profile in place.

"""One profile store shared by the command surface and the installable API.

Profile identity, `$HERMES_HOME` resolution, the private-path preflight, and
the canonical `profile.json` encoding live here so the CLI and the public
Python API resolve exactly the same state through exactly the same checks,
rather than each growing its own copy.

`carddav-profile/1.1` drops the `capabilities` block that
`carddav-profile/1.0` copied into every profile: what this package can do is a
property of the installed package, reported by `version --json`, not of an
immutable file written once at setup time. A `carddav-profile/1.0` file is
still accepted exactly as written and is never rewritten in place — it is only
normalized in memory — so a profile created by the read-only release keeps
working, keeps its identifiers, and keeps the `profile_generation_sha256` that
its already published generations are bound to.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import stat
import tempfile
from pathlib import Path

PROFILE_SCHEMA_VERSION = "carddav-profile/1.1"
LEGACY_PROFILE_SCHEMA_VERSION = "carddav-profile/1.0"
LEGACY_CAPABILITIES = {"read_only": True, "create_update": False, "cleanup_delete": False}
SYNC_CADENCE_SECONDS = 3600
MANAGED_DIR_MODE = 0o700
MANAGED_FILE_MODE = 0o600
IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
CONTACT_ID_PATTERN = re.compile(r"^[0-9a-f]{16}$")

_URL_STRUCTURE_PATTERN = re.compile(r"\A(?P<scheme>[^:/?#]*)://(?P<authority>[^/?#]*)[^?#]*\Z")
_CONTROL_OR_WHITESPACE_PATTERN = re.compile(r"[\x00-\x20\x7f]")
_PORT_PATTERN = re.compile(r"[0-9]+")
_MAX_PORT = 65535
_MAX_PORT_DIGITS = len(str(_MAX_PORT))
_SUPPORTED_URL_SCHEMES = ("http", "https")


class InvalidSetup(Exception):
    """Raised for any invalid `setup` invocation; maps to a fixed exit-2 result."""


class ProfileConflict(Exception):
    """An immutable profile already exists with different settings."""


class UnsafeProfileState(Exception):
    """Stored state does not satisfy the private profile contract."""


def _validate_port(remainder: str) -> None:
    if remainder == "":
        return
    if not _PORT_PATTERN.fullmatch(remainder[1:]) or not remainder.startswith(":"):
        raise InvalidSetup("invalid server URL: malformed port")
    digits = remainder[1:].lstrip("0") or "0"
    if len(digits) > _MAX_PORT_DIGITS or int(digits) > _MAX_PORT:
        raise InvalidSetup("invalid server URL: port out of range")


def _validate_authority(authority: str) -> None:
    if "@" in authority:
        raise InvalidSetup("invalid server URL: userinfo not allowed")
    if authority.startswith("["):
        end = authority.find("]")
        if end == -1:
            raise InvalidSetup("invalid server URL: malformed IPv6 host")
        host, remainder = authority[1:end], authority[end + 1 :]
        try:
            ipaddress.IPv6Address(host)
        except ValueError as exc:
            raise InvalidSetup("invalid server URL: malformed IPv6 host") from exc
        _validate_port(remainder)
        return
    host, sep, port = authority.partition(":")
    if not host:
        raise InvalidSetup("invalid server URL: missing host")
    _validate_port(sep + port)


def validate_server_url(url: str) -> None:
    """Accept only an absolute, credential-free HTTP(S) address."""
    if _CONTROL_OR_WHITESPACE_PATTERN.search(url):
        raise InvalidSetup("invalid server URL: control or whitespace character")
    if "?" in url or "#" in url:
        raise InvalidSetup("invalid server URL: query or fragment delimiter")
    match = _URL_STRUCTURE_PATTERN.fullmatch(url)
    if match is None or match.group("scheme") not in _SUPPORTED_URL_SCHEMES:
        raise InvalidSetup("invalid server URL: missing or unsupported scheme")
    _validate_authority(match.group("authority"))


def normalize_server_url(url: str) -> str:
    return url.rstrip("/") + "/"


def validate_setup_args(args: argparse.Namespace) -> None:
    """Validate one already parsed `setup` invocation."""
    if not IDENTIFIER_PATTERN.fullmatch(args.profile):
        raise InvalidSetup("invalid profile identifier")
    if not IDENTIFIER_PATTERN.fullmatch(args.namespace):
        raise InvalidSetup("invalid namespace identifier")
    validate_server_url(args.server_url)
    for collection in args.collection:
        if not IDENTIFIER_PATTERN.fullmatch(collection):
            raise InvalidSetup("invalid collection identifier")
    if len(set(args.collection)) != len(args.collection):
        raise InvalidSetup("duplicate collection")


def build_profile(namespace: str, server_url: str, collections: list[str]) -> dict[str, object]:
    """Build the canonical `carddav-profile/1.1` object."""
    return {
        "profile_schema_version": PROFILE_SCHEMA_VERSION,
        "account_namespace": namespace,
        "server_url": normalize_server_url(server_url),
        "collection_allowlist": sorted(collections),
        "sync_cadence_seconds": SYNC_CADENCE_SECONDS,
    }


def _build_legacy_profile(
    namespace: str, server_url: str, collections: list[str]
) -> dict[str, object]:
    """Rebuild the exact `carddav-profile/1.0` object the read-only release wrote."""
    return {
        **build_profile(namespace, server_url, collections),
        "profile_schema_version": LEGACY_PROFILE_SCHEMA_VERSION,
        "capabilities": dict(LEGACY_CAPABILITIES),
    }


def profile_bytes(profile: dict[str, object]) -> bytes:
    return json.dumps(profile, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"


def read_profile(path: Path) -> dict[str, object]:
    """Read one profile of either schema version, normalized to the current shape."""
    raw = path.read_bytes()
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise UnsafeProfileState
        version = data.get("profile_schema_version")
        namespace = data.get("account_namespace")
        server_url = data.get("server_url")
        collections = data.get("collection_allowlist")
        if (
            version not in (PROFILE_SCHEMA_VERSION, LEGACY_PROFILE_SCHEMA_VERSION)
            or not isinstance(namespace, str)
            or not isinstance(server_url, str)
            or not isinstance(collections, list)
            or not collections
            or not all(isinstance(value, str) for value in collections)
        ):
            raise UnsafeProfileState
        validate_setup_args(argparse.Namespace(
            profile=path.parent.name, namespace=namespace,
            server_url=server_url, collection=collections,
        ))
        profile = build_profile(namespace, server_url, collections)
        stored = (
            profile if version == PROFILE_SCHEMA_VERSION
            else _build_legacy_profile(namespace, server_url, collections)
        )
        # Rebuilding also enforces closed keys, literal types, sorted lists,
        # and canonical encoding (including refusal of duplicate JSON keys).
        if raw != profile_bytes(stored):
            raise UnsafeProfileState
        return profile
    except (ValueError, UnicodeError, InvalidSetup) as exc:
        raise UnsafeProfileState from exc


def write_profile_json(path: Path, profile: dict[str, object]) -> None:
    payload = profile_bytes(profile)
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


def resolve_hermes_home() -> Path:
    if "HERMES_HOME" in os.environ:
        candidate = Path(os.environ["HERMES_HOME"])
        if not os.environ["HERMES_HOME"] or not candidate.is_absolute():
            raise InvalidSetup("invalid HERMES_HOME: must be an absolute path")
        return candidate
    home = os.environ.get("HOME")
    if home is None or not Path(home).is_absolute():
        raise InvalidSetup("invalid HOME: must be an absolute path")
    return Path(home) / ".hermes"


def check_managed_path(path: Path, *, directory: bool = False) -> bool:
    """Check existing state without following links or repairing permissions."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    mode = MANAGED_DIR_MODE if directory else MANAGED_FILE_MODE
    valid_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (
        not valid_type
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != mode
        or (not directory and info.st_nlink != 1)
    ):
        raise UnsafeProfileState
    return True


def check_hermes_home(hermes_home: Path) -> None:
    """Check the operator-owned home root: never create, follow, or repair it.

    The home root belongs to the operator, so its mode is whatever they gave it;
    only its type and owner are checked, and `lstat` keeps a symlink from being
    followed. Everything below it is skill-owned and stays `0700`/`0600` under
    `check_managed_path`. Every command applies this same check, so a home that
    `setup` accepted is never rejected by a later command.
    """
    try:
        info = hermes_home.lstat()
    except FileNotFoundError as exc:
        raise UnsafeProfileState from exc
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise UnsafeProfileState


def prepare_hermes_home(hermes_home: Path) -> None:
    """Create an absent home privately; `setup` is the only command that may."""
    try:
        hermes_home.lstat()
    except FileNotFoundError:
        try:
            hermes_home.mkdir(mode=MANAGED_DIR_MODE)
        except FileExistsError:
            pass  # Another cooperating setup may have just created it.
    check_hermes_home(hermes_home)


def resolve_profile_paths(profile_name: str) -> tuple[Path, Path, Path]:
    """Preflight one existing profile and return its directory, file, and lock."""
    hermes_home = resolve_hermes_home()
    root = hermes_home / "carddav-contacts"
    profiles_dir = root / "profiles"
    profile_dir = profiles_dir / profile_name
    profile_path = profile_dir / "profile.json"
    lock_path = profile_dir / "profile.lock"

    check_hermes_home(hermes_home)
    for directory in (root, profiles_dir, profile_dir):
        if not check_managed_path(directory, directory=True):
            raise UnsafeProfileState
    for path in (profile_path, lock_path):
        if not check_managed_path(path):
            raise UnsafeProfileState
    return profile_dir, profile_path, lock_path


def make_private_dir(path: Path) -> Path:
    """Create or accept one private `0700` directory inside a profile."""
    if not check_managed_path(path, directory=True):
        try:
            path.mkdir(mode=MANAGED_DIR_MODE)
        except FileExistsError:
            pass  # Another cooperating process may have created it first.
        check_managed_path(path, directory=True)
    return path


def write_private_file(path: Path, payload: bytes) -> None:
    """Replace one private `0600` file atomically inside its own directory."""
    directory = path.parent
    fd, tmp_name = tempfile.mkstemp(dir=directory, prefix="." + path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fchmod(handle.fileno(), MANAGED_FILE_MODE)
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    dir_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


CACHE_INVALID_NAME = "cache-invalid"


def invalidate_cache(profile_dir: Path) -> None:
    """Mark the published generation as no longer describing the server."""
    write_private_file(profile_dir / CACHE_INVALID_NAME, b"remote-write\n")


def cache_is_invalid(profile_dir: Path) -> bool:
    """Report whether a remote write has outrun the published generation.

    This lives beside the profile store rather than in the write path so an
    ordinary local read can answer it without loading any transport.
    """
    return check_managed_path(profile_dir / CACHE_INVALID_NAME)


def clear_cache_invalid(profile_dir: Path) -> None:
    """Clear the marker once a successful sync has republished the generation."""
    (profile_dir / CACHE_INVALID_NAME).unlink(missing_ok=True)
