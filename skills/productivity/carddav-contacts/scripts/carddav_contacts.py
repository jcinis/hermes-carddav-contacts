"""CardDAV contacts skill entry point.

`version --json` reports command/source schema versions, this package's
version, capabilities, and pinned runtime dependency versions as a single JSON
object, with no filesystem, network, or subprocess access. `setup` writes a new
profile under `$HERMES_HOME/carddav-contacts/profiles/<profile>/profile.json`
from `--profile`, `--namespace`, `--server-url`, and one or more
`--collection` flags, with no network or subprocess access. `discover` and
`sync` drive read-only vdirsyncer transport; `sync` additionally publishes an
immutable indexed generation and commits the `current` pointer, which selects
that generation and records the successful sync time.

`status`, `snapshot`, `show --id ID`, `search --query TEXT`, and `audit` are
strictly local readers of that one generation: each takes `--profile NAME`,
requires `--json`, prints exactly one JSON object, and opens no socket, starts
no subprocess, and reads no credential. Failures print one fixed stderr line,
nothing on stdout, and exit 2. See SKILL.md and references/ for the exact
command and data-model contracts.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import NoReturn, cast

_SCRIPT_DIR = Path(__file__).parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import os

import generations
import profiles
import queries
import reads
import schemas
import transport
from profiles import CONTACT_ID_PATTERN, IDENTIFIER_PATTERN

_InvalidSetup = profiles.InvalidSetup
_ProfileConflict = profiles.ProfileConflict
_UnsafeProfileState = profiles.UnsafeProfileState

COMMAND_SCHEMA_VERSION = "carddav-command/1.1"
PACKAGE_VERSION = "0.2.0"
SUPPORTED_SOURCE_SCHEMA_VERSIONS = ["carddav-source/1.0"]
# What this installed package can do. Local reads stay offline and routine
# `sync` stays read-only; every write is a separately reviewed operation.
CAPABILITIES = {
    "read_only": False,
    "create": True,
    "update": True,
    "delete": True,
}
DEPENDENCY_VERSIONS = {
    "vdirsyncer": "0.21.0",
    "vobject": "0.9.9",
}



def _version() -> dict[str, object]:
    return {
        "command_schema_version": COMMAND_SCHEMA_VERSION,
        "command": "version",
        "package_version": PACKAGE_VERSION,
        "supported_source_schema_versions": SUPPORTED_SOURCE_SCHEMA_VERSIONS,
        "capabilities": CAPABILITIES,
        "dependency_versions": DEPENDENCY_VERSIONS,
    }


class _InvalidOperation(Exception):
    """The read-only command grammar is invalid."""


class _ContactNotFound(Exception):
    """A well-formed contact ID is not present in the current generation."""


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


# The profile store is shared with the installable API, so the command surface
# and `hermes_carddav_contacts.api` resolve identical state through identical
# checks rather than each keeping its own copy.
_validate_setup_args = profiles.validate_setup_args
_resolve_hermes_home = profiles.resolve_hermes_home
_resolve_profile_paths = profiles.resolve_profile_paths
_read_profile = profiles.read_profile


def _setup(args: argparse.Namespace, hermes_home: Path) -> int:
    root = hermes_home / "carddav-contacts"
    profiles_dir = root / "profiles"
    profile_dir = profiles_dir / args.profile

    profiles.prepare_hermes_home(hermes_home)

    for directory in (root, profiles_dir, profile_dir):
        profiles.make_private_dir(directory)

    lock_path = profile_dir / "profile.lock"
    profile_path = profile_dir / "profile.json"
    if not profiles.check_managed_path(profile_path):
        # A profile directory that still holds derived or reviewed state but has
        # lost its profile.json is damaged, not new. Writing a fresh profile here
        # would silently rebind that retained state — including already reviewed
        # operations — to whatever settings this invocation happens to carry.
        retained = [
            name for name in
            ("operations", "receipts", "generations", "staging", "runtime", "current")
            if (profile_dir / name).exists() or (profile_dir / name).is_symlink()
        ]
        if retained:
            raise _UnsafeProfileState
    if not profiles.check_managed_path(lock_path):
        try:
            fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                         profiles.MANAGED_FILE_MODE)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        profiles.check_managed_path(lock_path)

    with open(lock_path) as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            profile = profiles.build_profile(args.namespace, args.server_url, args.collection)
            if profiles.check_managed_path(profile_path):
                # An existing profile of either schema version is compared on its
                # settings and left exactly as written; it is never upgraded in place.
                if profiles.read_profile(profile_path) != profile:
                    raise _ProfileConflict
            else:
                profiles.write_profile_json(profile_path, profile)
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    return 0


_READ_COMMANDS = ("status", "snapshot", "show", "search", "audit")


_READ_OPTIONS = {"show": "--id", "search": "--query"}
MAX_QUERY_CHARACTERS = 512


def _valid_query(query: str) -> bool:
    """Accept a bounded, printable literal search string; never a pattern language."""
    if not 1 <= len(query) <= MAX_QUERY_CHARACTERS:
        return False
    return not any(
        ord(character) < 0x20 or ord(character) == 0x7F or 0xD800 <= ord(character) <= 0xDFFF
        for character in query
    )


def _parse_read_args(command: str, args: list[str]) -> tuple[str, str | None]:
    """Accept only the exact documented flag grammar of one local read command."""
    flag = _READ_OPTIONS.get(command)
    expected = 3 if flag is None else 5
    if len(args) != expected or args[0] != "--profile" or args[-1] != "--json":
        raise _InvalidOperation
    if not IDENTIFIER_PATTERN.fullmatch(args[1]):
        raise _InvalidOperation
    if flag is None:
        return args[1], None
    if args[2] != flag:
        raise _InvalidOperation
    if command == "show" and CONTACT_ID_PATTERN.fullmatch(args[3]) is None:
        raise _InvalidOperation
    if command == "search" and not _valid_query(args[3]):
        raise _InvalidOperation
    return args[1], args[3]


# One fixed flag sequence per write command. The grammar is closed on purpose:
# a target is always an exact identity or an already prepared operation digest,
# never a name, a pattern, or a caller-supplied remote URL.
_WRITE_GRAMMAR: dict[str, tuple[str, ...]] = {
    "record": ("--profile", "--id"),
    "prepare-create": ("--profile", "--collection", "--changes"),
    "prepare-update": ("--profile", "--id", "--changes"),
    "prepare-delete": ("--profile", "--id"),
    "apply": ("--profile", "--operation"),
    "reconcile": ("--profile", "--operation"),
}
_WRITE_COMMANDS = tuple(_WRITE_GRAMMAR)
_OPERATION_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _write_value(flag: str, value: str) -> object:
    """Validate one write-command argument against its own closed grammar."""
    if flag in ("--profile", "--collection"):
        if IDENTIFIER_PATTERN.fullmatch(value) is None:
            raise _InvalidOperation
        return value
    if flag == "--id":
        if CONTACT_ID_PATTERN.fullmatch(value) is None:
            raise _InvalidOperation
        return value
    if flag == "--operation":
        if _OPERATION_ID_PATTERN.fullmatch(value) is None:
            raise _InvalidOperation
        return value
    try:
        document = schemas.parse_json(value)
    except ValueError as exc:
        raise _InvalidOperation from exc
    if not isinstance(document, dict):
        raise _InvalidOperation
    return document


def _parse_write_args(command: str, args: list[str]) -> dict[str, object]:
    """Accept only the exact documented flag grammar of one write command."""
    flags = _WRITE_GRAMMAR[command]
    if len(args) != 2 * len(flags) + 1 or args[-1] != "--json":
        raise _InvalidOperation
    parsed: dict[str, object] = {}
    for position, flag in enumerate(flags):
        if args[2 * position] != flag:
            raise _InvalidOperation
        parsed[flag.removeprefix("--")] = _write_value(flag, args[2 * position + 1])
    return parsed


def _write_command(command: str, parsed: dict[str, object]) -> int:
    """Run one write command through the same implementation the API exposes."""
    import writes  # imported here so a local read never loads the write transport

    profile_name = cast(str, parsed["profile"])
    if command == "record":
        payload = writes.read_record(profile_name, cast(str, parsed["id"]))
    elif command == "prepare-create":
        payload = writes.prepare_create(
            profile_name,
            cast(str, parsed["collection"]),
            cast(dict[str, object], parsed["changes"]),
        )
    elif command == "prepare-update":
        payload = writes.prepare_update(
            profile_name,
            cast(str, parsed["id"]),
            cast(dict[str, object], parsed["changes"]),
        )
    elif command == "prepare-delete":
        payload = writes.prepare_delete(profile_name, cast(str, parsed["id"]))
    elif command == "apply":
        payload = writes.apply_operation(profile_name, cast(str, parsed["operation"]))
    else:
        payload = writes.reconcile_operation(profile_name, cast(str, parsed["operation"]))
    print(json.dumps(payload))
    return 0


def _parse_network_args(args: list[str]) -> str:
    if len(args) != 2 or args[0] != "--profile" or not IDENTIFIER_PATTERN.fullmatch(args[1]):
        raise _InvalidOperation
    return args[1]


def _network_command(command: str, profile_name: str) -> int:
    profile_dir, profile_path, lock_path = _resolve_profile_paths(profile_name)
    with lock_path.open() as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            profile_bytes = profile_path.read_bytes()
            profile = _read_profile(profile_path)
            transport.run_operation(command, profile, profile_dir)
            if command == "sync":
                generations.publish(profile_dir, profile, profile_bytes)
                # The republished generation now describes the server again.
                profiles.clear_cache_invalid(profile_dir)
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    return 0


_Loaded = tuple[str, str, dict[str, object]]
_Read = tuple[dict[str, object], bytes, _Loaded | None, bool]


def _read_under_lock(profile_name: str) -> _Read:
    """Validate one profile and its single current generation under a shared lock."""
    profile_dir, profile_path, lock_path = _resolve_profile_paths(profile_name)
    with lock_path.open() as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_SH)
        try:
            profile_bytes = profile_path.read_bytes()
            profile = _read_profile(profile_path)
            # A successful remote write that outran its refresh is reported by
            # every read, so no reader can present obsolete data as current.
            invalidated = profiles.cache_is_invalid(profile_dir)
            loaded: _Loaded | None
            try:
                loaded = reads.load_source(profile_dir, profile_bytes)
            except reads.NoCurrentGeneration:
                loaded = None
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    return profile, profile_bytes, loaded, invalidated


def _contacts(source: dict[str, object]) -> list[dict[str, object]]:
    contacts = source["contacts"]
    if not isinstance(contacts, list):
        raise generations.UnsafeGenerationState
    return [cast(dict[str, object], contact) for contact in contacts]


def _status(profile_name: str) -> int:
    profile, profile_bytes, loaded, invalidated = _read_under_lock(profile_name)
    payload: dict[str, object] = {
        "command_schema_version": COMMAND_SCHEMA_VERSION,
        "command": "status",
        "profile": profile_name,
        "current_generation": None,
        "profile_generation_sha256": hashlib.sha256(profile_bytes).hexdigest(),
        "contact_count": None,
        "synced_at": None,
        "freshness": None,
        "cache_invalidated": invalidated,
    }
    if loaded is not None:
        generation, synced_at, source = loaded
        payload["current_generation"] = generation
        payload["contact_count"] = len(_contacts(source))
        payload["synced_at"] = synced_at
        payload["freshness"] = reads.freshness(synced_at, profile["sync_cadence_seconds"])
    print(json.dumps(payload))
    return 0


def _read_command(command: str, profile_name: str, option: str | None = None) -> int:
    """Answer one local read from the single validated current generation."""
    profile, profile_bytes, loaded, invalidated = _read_under_lock(profile_name)
    if loaded is None:
        raise reads.NoCurrentGeneration
    generation, synced_at, source = loaded
    payload: dict[str, object] = {
        "command_schema_version": COMMAND_SCHEMA_VERSION,
        "command": command,
        "profile": profile_name,
        "generation": generation,
        "profile_generation_sha256": hashlib.sha256(profile_bytes).hexdigest(),
        "synced_at": synced_at,
        "freshness": reads.freshness(synced_at, profile["sync_cadence_seconds"]),
        "cache_invalidated": invalidated,
    }
    if command == "snapshot":
        payload["data"] = source
    elif command == "show":
        matches = [
            contact for contact in _contacts(source) if contact["contact_id"] == option
        ]
        if not matches:
            raise _ContactNotFound
        payload["contact"] = matches[0]
    elif command == "search":
        results = queries.search(_contacts(source), cast(str, option))
        payload["query"] = option
        payload["total_matches"] = len(results)
        payload["truncated"] = False
        payload["results"] = results
    elif command == "audit":
        candidates = queries.audit(_contacts(source))
        payload["total_candidate_groups"] = len(candidates)
        payload["truncated"] = False
        payload["candidates"] = candidates
    print(json.dumps(payload))
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
            return _setup(parsed, hermes_home)
        except _InvalidSetup:
            print("error: invalid setup", file=sys.stderr)
            return 2
        except _ProfileConflict:
            print("error: profile conflict", file=sys.stderr)
            return 2
        except _UnsafeProfileState:
            print("error: unsafe profile state", file=sys.stderr)
            return 2
        except OSError:
            print("error: setup failed", file=sys.stderr)
            return 2
    if args[:1] in (["discover"], ["sync"]):
        command = args[0]
        try:
            profile_name = _parse_network_args(args[1:])
        except _InvalidOperation:
            print("error: invalid operation", file=sys.stderr)
            return 2
        try:
            return _network_command(command, profile_name)
        except transport.InvalidCredentials:
            print("error: invalid credentials", file=sys.stderr)
            return 2
        except (
            _InvalidSetup,
            _UnsafeProfileState,
            transport.UnsafeRuntimeState,
            generations.UnsafeGenerationState,
        ):
            print("error: unsafe profile state", file=sys.stderr)
            return 2
        except Exception:  # noqa: BLE001 - fixed CLI boundary redacts all failures
            print(f"error: {command} failed", file=sys.stderr)
            return 2
    if args[:1] and args[0] in _WRITE_COMMANDS:
        command = args[0]
        try:
            arguments = _parse_write_args(command, args[1:])
        except _InvalidOperation:
            print("error: invalid operation", file=sys.stderr)
            return 2
        import writes  # same lazy import as the handler; nothing else loads it

        try:
            return _write_command(command, arguments)
        except writes.InvalidOperationRequest:
            print("error: invalid operation", file=sys.stderr)
            return 2
        except writes.OperationNotFound:
            print("error: operation not found", file=sys.stderr)
            return 2
        except writes.ContactNotFound:
            print("error: contact not found", file=sys.stderr)
            return 2
        except writes.RevisionConflict:
            print("error: revision conflict", file=sys.stderr)
            return 2
        except writes.ProfileBindingMismatch:
            print("error: profile binding mismatch", file=sys.stderr)
            return 2
        except writes.AlreadyExists:
            print("error: already exists", file=sys.stderr)
            return 2
        except writes.VerificationFailed:
            print("error: verification failed", file=sys.stderr)
            return 2
        except writes.ReconciliationRequired:
            print("error: reconciliation required", file=sys.stderr)
            return 2
        except writes.UnknownOutcome:
            print("error: unknown outcome", file=sys.stderr)
            return 2
        except writes.RemoteUnavailable:
            print("error: remote unavailable", file=sys.stderr)
            return 2
        except transport.InvalidCredentials:
            print("error: invalid credentials", file=sys.stderr)
            return 2
        except (
            _InvalidSetup,
            _UnsafeProfileState,
            transport.UnsafeRuntimeState,
            generations.UnsafeGenerationState,
        ):
            print("error: unsafe profile state", file=sys.stderr)
            return 2
        except Exception:  # noqa: BLE001 - fixed CLI boundary redacts all failures
            print(f"error: {command} failed", file=sys.stderr)
            return 2
    if args[:1] and args[0] in _READ_COMMANDS:
        command = args[0]
        try:
            profile_name, option = _parse_read_args(command, args[1:])
        except _InvalidOperation:
            print("error: invalid operation", file=sys.stderr)
            return 2
        try:
            if command == "status":
                return _status(profile_name)
            return _read_command(command, profile_name, option)
        except reads.NoCurrentGeneration:
            print("error: no current generation", file=sys.stderr)
            return 2
        except _ContactNotFound:
            print("error: contact not found", file=sys.stderr)
            return 2
        except (_InvalidSetup, _UnsafeProfileState, generations.UnsafeGenerationState):
            print("error: unsafe profile state", file=sys.stderr)
            return 2
        except Exception:  # noqa: BLE001 - fixed CLI boundary redacts all failures
            print(f"error: {command} failed", file=sys.stderr)
            return 2
    print("error: invalid operation", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
