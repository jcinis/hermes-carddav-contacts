"""Publish one immutable indexed generation and swap the `current` pointer.

The pointer carries both the selected generation and the time that sync
succeeded, so one atomic commit records both facts (see
`references/configuration.md`).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

_SCRIPT_DIR = Path(__file__).parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import index

MANIFEST_SCHEMA_VERSION = "carddav-generation/1.0"
POINTER_SCHEMA_VERSION = "carddav-current/1.1"
LEGACY_POINTER_SCHEMA_VERSION = "carddav-current/1.0"
MANAGED_DIR_MODE = 0o700
MANAGED_FILE_MODE = 0o600
GENERATION_ENTRIES = ("index.sqlite3", "manifest.json", "mirror")
_GENERATION_RE = re.compile(r"[0-9a-f]{64}\Z")
_TIMESTAMP_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")


class UnsafeGenerationState(Exception):
    """A pointer or manifest is malformed, missing, or bound to another profile."""


class PublicationFailed(Exception):
    """A controlled failure before publication; staging is removed."""


def _parse_canonical(raw: bytes, keys: tuple[str, ...]) -> dict[str, object]:
    """Accept only the exact canonical bytes this program writes."""
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise UnsafeGenerationState from exc
    if not isinstance(value, dict) or set(value) != set(keys):
        raise UnsafeGenerationState
    if raw != _canonical_bytes(value):
        raise UnsafeGenerationState
    return value


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"


def _replace_file(path: Path, payload: bytes, on_commit: Callable[[], None] | None = None) -> None:
    """Same-directory tempfile, fchmod, fsync, atomic replace, fsync directory.

    `on_commit` runs immediately after the replace succeeds, so a caller can tell
    a pre-commit failure (nothing published) from a later durability failure
    (the new pointer is already visible and must not be rolled back).
    """
    directory = path.parent
    fd, raw = tempfile.mkstemp(dir=directory, prefix="." + path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fchmod(handle.fileno(), MANAGED_FILE_MODE)
            os.fsync(handle.fileno())
        os.replace(raw, path)
    except BaseException:
        Path(raw).unlink(missing_ok=True)
        raise
    if on_commit is not None:
        on_commit()
    _fsync_directory(directory)


def _fsync_directory(path: Path) -> None:
    """Make a directory's own entries durable."""
    dir_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _fsync_file(path: Path) -> None:
    """Make one already-written regular file's contents durable."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_tree(path: Path) -> None:
    """Make a whole staging tree durable bottom-up, contents before directories."""
    for child in sorted(path.iterdir()):
        if stat.S_ISDIR(child.lstat().st_mode):
            _fsync_tree(child)
        else:
            _fsync_file(child)
    _fsync_directory(path)


def _private_existing(path: Path, *, directory: bool) -> bool:
    """Report whether a safe managed path exists; refuse unsafe state, never repair it."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise UnsafeGenerationState from exc
    expected_mode = MANAGED_DIR_MODE if directory else MANAGED_FILE_MODE
    valid_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (
        not valid_type
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != expected_mode
        or (not directory and info.st_nlink != 1)
    ):
        raise UnsafeGenerationState
    return True


def _check_tree(path: Path) -> None:
    """Recursively preflight an existing managed directory without repairing it."""
    if not _private_existing(path, directory=True):
        return
    for child in sorted(path.iterdir()):
        try:
            info = child.lstat()
        except OSError as exc:
            raise UnsafeGenerationState from exc
        if stat.S_ISDIR(info.st_mode):
            _check_tree(child)
        else:
            _private_existing(child, directory=False)


def _mkdir_private(path: Path) -> None:
    if _private_existing(path, directory=True):
        return
    try:
        path.mkdir(mode=MANAGED_DIR_MODE)
    except FileExistsError:  # A cooperating process may have created it first.
        if not _private_existing(path, directory=True):
            raise UnsafeGenerationState from None
        return
    _fsync_directory(path.parent)


def _copy_mirror(mirror: Path, target: Path, collections: list[str]) -> None:
    """Copy, never move: a later sync must not be able to change published data."""
    target.mkdir(mode=MANAGED_DIR_MODE)
    for collection in collections:
        destination = target / collection
        destination.mkdir(mode=MANAGED_DIR_MODE)
        for card in sorted((mirror / collection).glob("*.vcf")):
            shutil.copyfile(card, destination / card.name)
            (destination / card.name).chmod(MANAGED_FILE_MODE)


def _read_manifest(published: Path) -> dict[str, object]:
    """Read the canonical manifest of an existing published generation."""
    manifest_path = published / "manifest.json"
    if not _private_existing(manifest_path, directory=False):
        raise UnsafeGenerationState
    try:
        raw = manifest_path.read_bytes()
    except OSError as exc:
        raise UnsafeGenerationState from exc
    return _parse_canonical(
        raw, ("generation", "manifest_schema_version", "profile_generation_sha256")
    )


def _profile_scope(profile_bytes: bytes) -> tuple[str, list[str]]:
    """Read the account namespace and collection scope out of canonical profile bytes."""
    try:
        profile = json.loads(profile_bytes)
    except (ValueError, UnicodeError) as exc:
        raise UnsafeGenerationState from exc
    if not isinstance(profile, dict):
        raise UnsafeGenerationState
    namespace = profile.get("account_namespace")
    collections = profile.get("collection_allowlist")
    if (
        not isinstance(namespace, str)
        or not isinstance(collections, list)
        or not all(isinstance(value, str) for value in collections)
    ):
        raise UnsafeGenerationState
    return namespace, sorted(collections)


def _check_index(published: Path, generation: str, profile_bytes: bytes) -> None:
    """Check the published SQLite index agrees with this generation and profile."""
    database = published / "index.sqlite3"
    if not _private_existing(database, directory=False):
        raise UnsafeGenerationState
    try:
        meta = index.read_meta(database)
    except index.UnreadableIndex as exc:
        raise UnsafeGenerationState from exc
    namespace, collections = _profile_scope(profile_bytes)
    if (
        meta["schema_version"] != index.SOURCE_SCHEMA_VERSION
        or meta["generation"] != generation
        or meta["profile_generation_sha256"] != hashlib.sha256(profile_bytes).hexdigest()
        or meta["account_namespace"] != namespace
        or meta["collections"] != collections
    ):
        raise UnsafeGenerationState


def _check_published(published: Path, generation: str, profile_bytes: bytes) -> None:
    """Validate an existing published generation, for both reuse and `current`.

    One shared check so a generation that reuse treats as publishable is exactly
    a generation a reader will accept: safe tree shape, exactly the expected
    entries, a canonical manifest, and an index bound to this generation and
    profile.
    """
    if not _private_existing(published, directory=True):
        raise UnsafeGenerationState
    _check_tree(published)
    if sorted(child.name for child in published.iterdir()) != sorted(GENERATION_ENTRIES):
        raise UnsafeGenerationState
    if not _private_existing(published / "mirror", directory=True):
        raise UnsafeGenerationState
    manifest = _read_manifest(published)
    if (
        manifest["manifest_schema_version"] != MANIFEST_SCHEMA_VERSION
        or manifest["generation"] != generation
        or manifest["profile_generation_sha256"] != hashlib.sha256(profile_bytes).hexdigest()
    ):
        raise UnsafeGenerationState
    _check_index(published, generation, profile_bytes)


def _read_pointer_bytes(profile_dir: Path) -> bytes | None:
    """Read the pointer only after refusing any unsafe existing pointer path."""
    pointer_path = profile_dir / "current"
    if not _private_existing(pointer_path, directory=False):
        return None
    try:
        return pointer_path.read_bytes()
    except OSError as exc:
        raise UnsafeGenerationState from exc


def _pointer_timestamp(now: float | None) -> str:
    """Render the whole-second UTC time this successful sync finished."""
    seconds = int(time.time() if now is None else now)
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def publish(
    profile_dir: Path,
    profile: Mapping[str, object],
    profile_bytes: bytes,
    now: float | None = None,
) -> str:
    """Index the mirror in private staging, then publish it atomically."""
    collections = profile["collection_allowlist"]
    if not isinstance(collections, list) or not all(
        isinstance(value, str) for value in collections
    ):
        raise PublicationFailed
    profile_hash = hashlib.sha256(profile_bytes).hexdigest()
    mirror = profile_dir / "runtime" / "mirror"
    staging_root = profile_dir / "staging"
    generations_dir = profile_dir / "generations"
    if not _private_existing(profile_dir, directory=True):
        raise UnsafeGenerationState
    _private_existing(profile_dir / "current", directory=False)
    _mkdir_private(staging_root)
    _mkdir_private(generations_dir)

    staging = Path(tempfile.mkdtemp(dir=staging_root, prefix="publish."))
    published_here = False
    committed = False

    def _committed() -> None:
        nonlocal committed
        committed = True

    try:
        staging.chmod(MANAGED_DIR_MODE)
        source = index.build_source(mirror, profile)
        _copy_mirror(mirror, staging / "mirror", sorted(collections))
        generation = index.write_index(staging / "index.sqlite3", source, profile_hash)
        _replace_file(
            staging / "manifest.json",
            _canonical_bytes(
                {
                    "generation": generation,
                    "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
                    "profile_generation_sha256": profile_hash,
                }
            ),
        )
        published = generations_dir / generation
        if _private_existing(published, directory=True):
            # Identical content: keep the published generation exactly as it is,
            # but only once it validates exactly as a reader would. The pointer is
            # still committed, because it also records this successful sync time.
            _check_published(published, generation, profile_bytes)
            shutil.rmtree(staging)
        else:
            # Contents first, then the directory entry, so a crash after the
            # pointer commit can never expose a partially durable generation.
            _fsync_tree(staging)
            os.rename(staging, published)
            published_here = True
            _fsync_directory(generations_dir)
        # Sampled here, at the last step before the pointer commit, so the
        # recorded time covers every validation and durability step this sync
        # actually did rather than an earlier point in the same call.
        pointer = _canonical_bytes(
            {
                "generation": generation,
                "pointer_schema_version": POINTER_SCHEMA_VERSION,
                "synced_at": _pointer_timestamp(now),
            }
        )
        _replace_file(profile_dir / "current", pointer, _committed)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        if published_here and not committed:
            # Nothing referenced this directory yet; a committed pointer is never rolled back.
            shutil.rmtree(published, ignore_errors=True)
        raise
    return generation


def _parse_pointer(raw: bytes) -> dict[str, object]:
    """Parse either pointer version; only the current one carries `synced_at`."""
    try:
        version = json.loads(raw).get("pointer_schema_version")
    except (AttributeError, ValueError, UnicodeError) as exc:
        raise UnsafeGenerationState from exc
    if version == LEGACY_POINTER_SCHEMA_VERSION:
        pointer = _parse_canonical(raw, ("generation", "pointer_schema_version"))
        pointer["synced_at"] = None
    elif version == POINTER_SCHEMA_VERSION:
        pointer = _parse_canonical(raw, ("generation", "pointer_schema_version", "synced_at"))
        synced_at = pointer["synced_at"]
        if not isinstance(synced_at, str) or _TIMESTAMP_RE.fullmatch(synced_at) is None:
            raise UnsafeGenerationState
        try:
            datetime.strptime(synced_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        except ValueError as exc:
            raise UnsafeGenerationState from exc
    else:
        raise UnsafeGenerationState
    generation = pointer["generation"]
    if not isinstance(generation, str) or _GENERATION_RE.fullmatch(generation) is None:
        raise UnsafeGenerationState
    return pointer


def read_pointer(profile_dir: Path, profile_bytes: bytes) -> dict[str, object] | None:
    """Resolve `current` to its generation and sync time, or None when unpublished.

    `synced_at` is None for an older `carddav-current/1.0` pointer; callers that
    report freshness must refuse that pointer.
    """
    # Every managed root this resolution reads is preflighted first, so an
    # unsafe generations root is refused rather than reported as "nothing yet".
    if not _private_existing(profile_dir, directory=True):
        raise UnsafeGenerationState
    _private_existing(profile_dir / "generations", directory=True)
    raw = _read_pointer_bytes(profile_dir)
    if raw is None:
        return None
    pointer = _parse_pointer(raw)
    generation = pointer["generation"]
    if not isinstance(generation, str):
        raise UnsafeGenerationState
    if not _private_existing(profile_dir / "generations", directory=True):
        raise UnsafeGenerationState
    _check_published(profile_dir / "generations" / generation, generation, profile_bytes)
    return pointer


def read_current(profile_dir: Path, profile_bytes: bytes) -> str | None:
    """Resolve `current` to its generation, or None when nothing is published."""
    pointer = read_pointer(profile_dir, profile_bytes)
    if pointer is None:
        return None
    return cast(str, pointer["generation"])
