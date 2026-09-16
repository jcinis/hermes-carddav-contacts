"""Immutable generation publication and `current` pointer contract tests."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import stat
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType

import pytest

SCRIPTS = (
    Path(__file__).parent.parent
    / "skills"
    / "productivity"
    / "carddav-contacts"
    / "scripts"
)


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"carddav_{name}", SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _card(uid: str, name: str) -> str:
    return (
        f"BEGIN:VCARD\r\nVERSION:3.0\r\nUID:{uid}\r\nFN:{name}\r\nEND:VCARD\r\n"
    )


def _profile_dir(root: Path, cards: dict[str, str]) -> tuple[Path, dict[str, object], bytes]:
    profile: dict[str, object] = {
        "profile_schema_version": "carddav-profile/1.0",
        "account_namespace": "example",
        "server_url": "https://carddav.example.invalid/",
        "collection_allowlist": ["contacts-a"],
        "sync_cadence_seconds": 3600,
        "capabilities": {"read_only": True, "create_update": False, "cleanup_delete": False},
    }
    profile_dir = root / "profiles" / "demo"
    profile_dir.mkdir(mode=0o700, parents=True)
    profile_bytes = json.dumps(profile, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    (profile_dir / "profile.json").write_bytes(profile_bytes)
    (profile_dir / "profile.json").chmod(0o600)
    collection = profile_dir / "runtime" / "mirror" / "contacts-a"
    for level in (collection.parent.parent, collection.parent, collection):
        level.mkdir(mode=0o700)
    for name, text in cards.items():
        path = collection / name
        path.write_text(text, encoding="utf-8")
        path.chmod(0o600)
    return profile_dir, profile, profile_bytes


def _assert_private(root: Path) -> None:
    for path in [root, *root.rglob("*")]:
        info = path.lstat()
        assert not path.is_symlink()
        assert stat.S_IMODE(info.st_mode) == (0o700 if path.is_dir() else 0o600), path


SYNCED_AT_EPOCH = 1788998400  # 2026-09-10T00:00:00Z
LATER_EPOCH = 1789002000  # 2026-09-10T01:00:00Z


def test_publish_writes_an_immutable_generation_bound_to_the_profile(tmp_path: Path) -> None:
    generations = _load("generations")
    schemas = _load("schemas")
    index = _load("index")
    profile_dir, profile, profile_bytes = _profile_dir(
        tmp_path, {"one.vcf": _card("one", "Example One")}
    )

    generation = generations.publish(profile_dir, profile, profile_bytes, now=SYNCED_AT_EPOCH)

    source = index.build_source(profile_dir / "runtime" / "mirror", profile)
    assert generation == schemas.source_generation(source)
    published = profile_dir / "generations" / generation
    assert json.loads((published / "manifest.json").read_bytes()) == {
        "generation": generation,
        "manifest_schema_version": "carddav-generation/1.0",
        "profile_generation_sha256": generations.hashlib.sha256(profile_bytes).hexdigest(),
    }
    assert (published / "index.sqlite3").is_file()
    assert (published / "mirror" / "contacts-a" / "one.vcf").read_bytes() == _card(
        "one", "Example One"
    ).encode()
    assert json.loads((profile_dir / "current").read_bytes()) == {
        "generation": generation,
        "pointer_schema_version": "carddav-current/1.1",
        "synced_at": "2026-09-10T00:00:00Z",
    }
    assert list((profile_dir / "staging").iterdir()) == []
    _assert_private(profile_dir)


def test_pointer_records_the_successful_sync_time(tmp_path: Path) -> None:
    generations = _load("generations")
    profile_dir, profile, profile_bytes = _profile_dir(
        tmp_path, {"one.vcf": _card("one", "Example One")}
    )

    generation = generations.publish(
        profile_dir, profile, profile_bytes, now=SYNCED_AT_EPOCH + 0.75
    )

    assert json.loads((profile_dir / "current").read_bytes()) == {
        "generation": generation,
        "pointer_schema_version": "carddav-current/1.1",
        "synced_at": "2026-09-10T00:00:00Z",
    }


def test_identical_content_refreshes_the_sync_time_without_generation_churn(
    tmp_path: Path,
) -> None:
    generations = _load("generations")
    profile_dir, profile, profile_bytes = _profile_dir(
        tmp_path, {"one.vcf": _card("one", "Example One")}
    )
    first = generations.publish(profile_dir, profile, profile_bytes, now=SYNCED_AT_EPOCH)
    published = profile_dir / "generations" / first
    published_before = {
        path.relative_to(published): (path.lstat().st_mtime_ns, path.read_bytes())
        for path in published.rglob("*")
        if path.is_file()
    }

    second = generations.publish(profile_dir, profile, profile_bytes, now=LATER_EPOCH)

    assert second == first
    assert [path.name for path in (profile_dir / "generations").iterdir()] == [first]
    assert {
        path.relative_to(published): (path.lstat().st_mtime_ns, path.read_bytes())
        for path in published.rglob("*")
        if path.is_file()
    } == published_before
    assert json.loads((profile_dir / "current").read_bytes()) == {
        "generation": first,
        "pointer_schema_version": "carddav-current/1.1",
        "synced_at": "2026-09-10T01:00:00Z",
    }
    assert list((profile_dir / "staging").iterdir()) == []


class _FakeClock:
    """Stand in for the `time` module so a test can advance publication time."""

    def __init__(self, start: float) -> None:
        self.value = start

    def time(self) -> float:
        return self.value


def test_sync_time_is_sampled_after_the_new_generation_is_durable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generations = _load("generations")
    profile_dir, profile, profile_bytes = _profile_dir(
        tmp_path, {"one.vcf": _card("one", "Example One")}
    )
    clock = _FakeClock(SYNCED_AT_EPOCH)
    real_fsync_directory = generations._fsync_directory

    def slow_fsync_directory(path: Path) -> None:
        real_fsync_directory(path)
        if path == profile_dir / "generations":
            clock.value = LATER_EPOCH  # making the new generation durable took an hour

    monkeypatch.setattr(generations, "time", clock)
    monkeypatch.setattr(generations, "_fsync_directory", slow_fsync_directory)

    generation = generations.publish(profile_dir, profile, profile_bytes)

    assert json.loads((profile_dir / "current").read_bytes()) == {
        "generation": generation,
        "pointer_schema_version": "carddav-current/1.1",
        "synced_at": "2026-09-10T01:00:00Z",
    }


def test_sync_time_is_sampled_after_a_reused_generation_validates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generations = _load("generations")
    profile_dir, profile, profile_bytes = _profile_dir(
        tmp_path, {"one.vcf": _card("one", "Example One")}
    )
    first = generations.publish(profile_dir, profile, profile_bytes, now=SYNCED_AT_EPOCH)
    clock = _FakeClock(SYNCED_AT_EPOCH)
    real_check_published = generations._check_published

    def slow_check_published(published: Path, generation: str, published_bytes: bytes) -> None:
        real_check_published(published, generation, published_bytes)
        clock.value = LATER_EPOCH  # revalidating the reused generation took an hour

    monkeypatch.setattr(generations, "time", clock)
    monkeypatch.setattr(generations, "_check_published", slow_check_published)

    assert generations.publish(profile_dir, profile, profile_bytes) == first

    assert json.loads((profile_dir / "current").read_bytes()) == {
        "generation": first,
        "pointer_schema_version": "carddav-current/1.1",
        "synced_at": "2026-09-10T01:00:00Z",
    }


def test_failed_publication_cleans_staging_and_keeps_the_previous_generation(
    tmp_path: Path,
) -> None:
    generations = _load("generations")
    profile_dir, profile, profile_bytes = _profile_dir(
        tmp_path, {"one.vcf": _card("one", "Example One")}
    )
    first = generations.publish(profile_dir, profile, profile_bytes)
    pointer = profile_dir / "current"
    before = (pointer.read_bytes(), pointer.lstat().st_mtime_ns)
    duplicate = profile_dir / "runtime" / "mirror" / "contacts-a" / "copy.vcf"
    duplicate.write_text(_card("one", "Example Duplicate"), encoding="utf-8")
    duplicate.chmod(0o600)

    try:
        generations.publish(profile_dir, profile, profile_bytes)
    except generations.index.InvalidContact:
        pass
    else:
        raise AssertionError("expected InvalidContact")

    assert list((profile_dir / "staging").iterdir()) == []
    assert [path.name for path in (profile_dir / "generations").iterdir()] == [first]
    assert (pointer.read_bytes(), pointer.lstat().st_mtime_ns) == before


def test_read_current_is_null_before_any_generation(tmp_path: Path) -> None:
    generations = _load("generations")
    profile_dir, _profile, profile_bytes = _profile_dir(
        tmp_path, {"one.vcf": _card("one", "Example One")}
    )

    assert generations.read_current(profile_dir, profile_bytes) is None


def test_read_current_resolves_the_published_generation(tmp_path: Path) -> None:
    generations = _load("generations")
    profile_dir, profile, profile_bytes = _profile_dir(
        tmp_path, {"one.vcf": _card("one", "Example One")}
    )
    generation = generations.publish(profile_dir, profile, profile_bytes)

    assert generations.read_current(profile_dir, profile_bytes) == generation


def test_manifest_bound_to_another_profile_is_refused(tmp_path: Path) -> None:
    generations = _load("generations")
    profile_dir, profile, profile_bytes = _profile_dir(
        tmp_path, {"one.vcf": _card("one", "Example One")}
    )
    generations.publish(profile_dir, profile, profile_bytes)

    try:
        generations.read_current(profile_dir, profile_bytes + b"changed")
    except generations.UnsafeGenerationState:
        return
    raise AssertionError("expected UnsafeGenerationState")


def test_pointer_to_a_missing_generation_is_refused(tmp_path: Path) -> None:
    generations = _load("generations")
    profile_dir, profile, profile_bytes = _profile_dir(
        tmp_path, {"one.vcf": _card("one", "Example One")}
    )
    generation = generations.publish(profile_dir, profile, profile_bytes)
    (profile_dir / "generations" / generation / "manifest.json").unlink()

    try:
        generations.read_current(profile_dir, profile_bytes)
    except generations.UnsafeGenerationState:
        return
    raise AssertionError("expected UnsafeGenerationState")


ENTRYPOINT = SCRIPTS / "carddav_contacts.py"


def _cli() -> ModuleType:
    spec = importlib.util.spec_from_file_location("carddav_contacts_generations", ENTRYPOINT)
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


def _cli_setup(module: ModuleType, home: Path) -> None:
    os.environ["HERMES_HOME"] = str(home)
    assert (
        module.main(
            [
                "setup",
                "--profile",
                "demo",
                "--namespace",
                "example",
                "--server-url",
                "https://carddav.example.invalid/",
                "--collection",
                "contacts-a",
            ]
        )
        == 0
    )


def test_status_reports_a_null_generation_before_the_first_sync(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _cli()
    _cli_setup(module, tmp_path)

    assert module.main(["status", "--profile", "demo", "--json"]) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    profile_bytes = (
        tmp_path / "carddav-contacts" / "profiles" / "demo" / "profile.json"
    ).read_bytes()
    assert json.loads(captured.out) == {
        "command_schema_version": "carddav-command/1.1",
        "command": "status",
        "profile": "demo",
        "current_generation": None,
        "profile_generation_sha256": _load("generations").hashlib.sha256(
            profile_bytes
        ).hexdigest(),
        "contact_count": None,
        "synced_at": None,
        "freshness": None,
        "cache_invalidated": False,
    }


def test_symlinked_generations_root_is_refused_and_writes_nothing_outside(
    tmp_path: Path,
) -> None:
    generations = _load("generations")
    profile_dir, profile, profile_bytes = _profile_dir(
        tmp_path, {"one.vcf": _card("one", "Example One")}
    )
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    (profile_dir / "generations").symlink_to(outside, target_is_directory=True)

    try:
        generations.publish(profile_dir, profile, profile_bytes)
    except generations.UnsafeGenerationState:
        pass
    else:
        raise AssertionError("expected UnsafeGenerationState")

    assert list(outside.iterdir()) == []
    assert not (profile_dir / "current").exists()
    assert list((profile_dir / "staging").iterdir()) == []


def test_unsafe_current_pointer_is_refused_and_never_repaired(tmp_path: Path) -> None:
    generations = _load("generations")
    profile_dir, profile, profile_bytes = _profile_dir(
        tmp_path, {"one.vcf": _card("one", "Example One")}
    )
    pointer = profile_dir / "current"
    pointer.write_bytes(b"not a pointer\n")
    pointer.chmod(0o644)

    calls: tuple[Callable[[], object], ...] = (
        lambda: generations.publish(profile_dir, profile, profile_bytes),
        lambda: generations.read_current(profile_dir, profile_bytes),
    )
    for call in calls:
        try:
            call()
        except generations.UnsafeGenerationState:
            continue
        raise AssertionError("expected UnsafeGenerationState")

    assert pointer.read_bytes() == b"not a pointer\n"
    assert stat.S_IMODE(pointer.lstat().st_mode) == 0o644


def test_symlinked_current_pointer_is_refused(tmp_path: Path) -> None:
    generations = _load("generations")
    profile_dir, profile, profile_bytes = _profile_dir(
        tmp_path, {"one.vcf": _card("one", "Example One")}
    )
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"{}\n")
    outside.chmod(0o600)
    (profile_dir / "current").symlink_to(outside)

    calls: tuple[Callable[[], object], ...] = (
        lambda: generations.publish(profile_dir, profile, profile_bytes),
        lambda: generations.read_current(profile_dir, profile_bytes),
    )
    for call in calls:
        try:
            call()
        except generations.UnsafeGenerationState:
            continue
        raise AssertionError("expected UnsafeGenerationState")

    assert (profile_dir / "current").is_symlink()
    assert outside.read_bytes() == b"{}\n"


def test_symlinked_published_generation_is_refused(tmp_path: Path) -> None:
    generations = _load("generations")
    profile_dir, profile, profile_bytes = _profile_dir(
        tmp_path, {"one.vcf": _card("one", "Example One")}
    )
    generation = generations.publish(profile_dir, profile, profile_bytes)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    published = profile_dir / "generations" / generation
    shutil.move(str(published), str(outside / generation))
    published.symlink_to(outside / generation, target_is_directory=True)

    calls: tuple[Callable[[], object], ...] = (
        lambda: generations.read_current(profile_dir, profile_bytes),
        lambda: generations.publish(profile_dir, profile, profile_bytes),
    )
    for call in calls:
        try:
            call()
        except generations.UnsafeGenerationState:
            continue
        raise AssertionError("expected UnsafeGenerationState")

    assert published.is_symlink()


def test_unsafe_file_inside_a_published_generation_is_refused(tmp_path: Path) -> None:
    generations = _load("generations")
    profile_dir, profile, profile_bytes = _profile_dir(
        tmp_path, {"one.vcf": _card("one", "Example One")}
    )
    generation = generations.publish(profile_dir, profile, profile_bytes)
    card = profile_dir / "generations" / generation / "mirror" / "contacts-a" / "one.vcf"
    card.chmod(0o644)

    calls: tuple[Callable[[], object], ...] = (
        lambda: generations.read_current(profile_dir, profile_bytes),
        lambda: generations.publish(profile_dir, profile, profile_bytes),
    )
    for call in calls:
        try:
            call()
        except generations.UnsafeGenerationState:
            continue
        raise AssertionError("expected UnsafeGenerationState")

    assert stat.S_IMODE(card.lstat().st_mode) == 0o644


def _second_card(profile_dir: Path) -> None:
    card = profile_dir / "runtime" / "mirror" / "contacts-a" / "two.vcf"
    card.write_text(_card("two", "Example Two"), encoding="utf-8")
    card.chmod(0o600)


def test_failed_directory_rename_keeps_the_previous_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generations = _load("generations")
    profile_dir, profile, profile_bytes = _profile_dir(
        tmp_path, {"one.vcf": _card("one", "Example One")}
    )
    first = generations.publish(profile_dir, profile, profile_bytes)
    pointer = profile_dir / "current"
    before = (pointer.read_bytes(), pointer.lstat().st_mtime_ns)
    _second_card(profile_dir)

    def failing_rename(source: object, target: object) -> None:
        raise OSError("synthetic rename failure")

    monkeypatch.setattr(generations.os, "rename", failing_rename)
    with pytest.raises(OSError, match="synthetic rename failure"):
        generations.publish(profile_dir, profile, profile_bytes)

    assert [path.name for path in (profile_dir / "generations").iterdir()] == [first]
    assert (pointer.read_bytes(), pointer.lstat().st_mtime_ns) == before
    assert list((profile_dir / "staging").iterdir()) == []


def test_failed_pointer_replace_removes_the_unreferenced_new_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generations = _load("generations")
    profile_dir, profile, profile_bytes = _profile_dir(
        tmp_path, {"one.vcf": _card("one", "Example One")}
    )
    first = generations.publish(profile_dir, profile, profile_bytes)
    pointer = profile_dir / "current"
    before = (pointer.read_bytes(), pointer.lstat().st_mtime_ns)
    _second_card(profile_dir)
    real_replace = generations.os.replace

    def failing_replace(source: str, target: str) -> None:
        if Path(target).name == "current":
            raise OSError("synthetic pointer failure")
        real_replace(source, target)

    monkeypatch.setattr(generations.os, "replace", failing_replace)
    with pytest.raises(OSError, match="synthetic pointer failure"):
        generations.publish(profile_dir, profile, profile_bytes)

    assert [path.name for path in (profile_dir / "generations").iterdir()] == [first]
    assert (pointer.read_bytes(), pointer.lstat().st_mtime_ns) == before
    assert list((profile_dir / "staging").iterdir()) == []


def test_durability_failure_after_the_pointer_commit_keeps_the_referenced_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generations = _load("generations")
    profile_dir, profile, profile_bytes = _profile_dir(
        tmp_path, {"one.vcf": _card("one", "Example One")}
    )
    first = generations.publish(profile_dir, profile, profile_bytes)
    _second_card(profile_dir)
    real_replace = generations.os.replace
    real_fsync = generations.os.fsync
    committed = False

    def watched_replace(source: str, target: str) -> None:
        nonlocal committed
        real_replace(source, target)
        if Path(target).name == "current":
            committed = True

    def failing_fsync(descriptor: int) -> None:
        if committed:
            raise OSError("synthetic durability failure")
        real_fsync(descriptor)

    monkeypatch.setattr(generations.os, "replace", watched_replace)
    monkeypatch.setattr(generations.os, "fsync", failing_fsync)
    with pytest.raises(OSError, match="synthetic durability failure"):
        generations.publish(profile_dir, profile, profile_bytes)
    monkeypatch.undo()

    current = generations.read_current(profile_dir, profile_bytes)
    assert current is not None and current != first
    assert sorted(path.name for path in (profile_dir / "generations").iterdir()) == sorted(
        [first, current]
    )
    assert list((profile_dir / "staging").iterdir()) == []


def test_wrong_mode_or_unexpected_type_at_a_root_is_refused_without_repair(
    tmp_path: Path,
) -> None:
    generations = _load("generations")
    profile_dir, profile, profile_bytes = _profile_dir(
        tmp_path, {"one.vcf": _card("one", "Example One")}
    )
    (profile_dir / "staging").mkdir(mode=0o755)

    try:
        generations.publish(profile_dir, profile, profile_bytes)
    except generations.UnsafeGenerationState:
        pass
    else:
        raise AssertionError("expected UnsafeGenerationState")
    assert stat.S_IMODE((profile_dir / "staging").lstat().st_mode) == 0o755

    (profile_dir / "staging").rmdir()
    (profile_dir / "generations").write_bytes(b"not a directory\n")
    (profile_dir / "generations").chmod(0o600)

    try:
        generations.publish(profile_dir, profile, profile_bytes)
    except generations.UnsafeGenerationState:
        pass
    else:
        raise AssertionError("expected UnsafeGenerationState")
    assert (profile_dir / "generations").read_bytes() == b"not a directory\n"
    assert not (profile_dir / "current").exists()


def test_manifest_that_disagrees_with_the_pointer_is_refused(tmp_path: Path) -> None:
    generations = _load("generations")
    profile_dir, profile, profile_bytes = _profile_dir(
        tmp_path, {"one.vcf": _card("one", "Example One")}
    )
    generation = generations.publish(profile_dir, profile, profile_bytes)
    manifest = profile_dir / "generations" / generation / "manifest.json"
    tampered = json.loads(manifest.read_bytes())
    tampered["generation"] = "0" * 64
    manifest.write_bytes(
        json.dumps(tampered, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    )
    manifest.chmod(0o600)

    try:
        generations.read_current(profile_dir, profile_bytes)
    except generations.UnsafeGenerationState:
        return
    raise AssertionError("expected UnsafeGenerationState")


def _watch_publication(
    monkeypatch: pytest.MonkeyPatch, module: ModuleType, order: list[object]
) -> None:
    """Record fsynced inodes interleaved with the rename and pointer commit."""
    real_fsync = module.os.fsync
    real_rename = module.os.rename
    real_replace = module.os.replace

    def watched_fsync(descriptor: int) -> None:
        order.append(os.fstat(descriptor).st_ino)
        real_fsync(descriptor)

    def watched_rename(source: str, target: str) -> None:
        order.append("rename")
        real_rename(source, target)

    def watched_replace(source: str, target: str) -> None:
        order.append(("replace", Path(target).name))
        real_replace(source, target)

    monkeypatch.setattr(module.os, "fsync", watched_fsync)
    monkeypatch.setattr(module.os, "rename", watched_rename)
    monkeypatch.setattr(module.os, "replace", watched_replace)


def test_publication_is_durable_bottom_up_before_the_pointer_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generations = _load("generations")
    profile_dir, profile, profile_bytes = _profile_dir(
        tmp_path, {"one.vcf": _card("one", "Example One")}
    )
    order: list[object] = []
    _watch_publication(monkeypatch, generations, order)

    generation = generations.publish(profile_dir, profile, profile_bytes)
    monkeypatch.undo()

    published = profile_dir / "generations" / generation
    card = published / "mirror" / "contacts-a" / "one.vcf"

    def durable_at(path: Path) -> int:
        """Index of the fsync that finally makes this path durable."""
        inode = path.lstat().st_ino
        assert inode in order, path
        return len(order) - 1 - order[::-1].index(inode)

    rename_at = order.index("rename")
    for path in (card, published / "index.sqlite3", published / "manifest.json", published):
        assert durable_at(path) < rename_at, path
    for child in (card, card.parent, card.parent.parent):
        assert durable_at(child) < durable_at(child.parent), child
    assert rename_at < durable_at(profile_dir / "generations")
    assert durable_at(profile_dir / "generations") < order.index(("replace", "current"))


def test_generations_directory_durability_failure_leaves_the_pointer_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generations = _load("generations")
    profile_dir, profile, profile_bytes = _profile_dir(
        tmp_path, {"one.vcf": _card("one", "Example One")}
    )
    first = generations.publish(profile_dir, profile, profile_bytes)
    pointer = profile_dir / "current"
    before = (pointer.read_bytes(), pointer.lstat().st_mtime_ns)
    _second_card(profile_dir)
    real_fsync = generations.os.fsync
    generations_inode = (profile_dir / "generations").lstat().st_ino

    def failing_fsync(descriptor: int) -> None:
        if os.fstat(descriptor).st_ino == generations_inode:
            raise OSError("synthetic generations durability failure")
        real_fsync(descriptor)

    monkeypatch.setattr(generations.os, "fsync", failing_fsync)
    with pytest.raises(OSError, match="synthetic generations durability failure"):
        generations.publish(profile_dir, profile, profile_bytes)
    monkeypatch.undo()

    assert [path.name for path in (profile_dir / "generations").iterdir()] == [first]
    assert (pointer.read_bytes(), pointer.lstat().st_mtime_ns) == before
    assert list((profile_dir / "staging").iterdir()) == []


def _tamper_manifest(published: Path, key: str, value: str) -> None:
    manifest = published / "manifest.json"
    tampered = json.loads(manifest.read_bytes())
    tampered[key] = value
    manifest.write_bytes(
        json.dumps(tampered, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    )
    manifest.chmod(0o600)


def test_reuse_refuses_a_generation_whose_manifest_lost_its_profile_binding(
    tmp_path: Path,
) -> None:
    generations = _load("generations")
    profile_dir, profile, profile_bytes = _profile_dir(
        tmp_path, {"one.vcf": _card("one", "Example One")}
    )
    generation = generations.publish(profile_dir, profile, profile_bytes)
    published = profile_dir / "generations" / generation
    _tamper_manifest(published, "profile_generation_sha256", "0" * 64)
    tampered = (published / "manifest.json").read_bytes()

    calls: tuple[Callable[[], object], ...] = (
        lambda: generations.publish(profile_dir, profile, profile_bytes),
        lambda: generations.read_current(profile_dir, profile_bytes),
    )
    for call in calls:
        try:
            call()
        except generations.UnsafeGenerationState:
            continue
        raise AssertionError("expected UnsafeGenerationState")

    assert (published / "manifest.json").read_bytes() == tampered
    assert list((profile_dir / "staging").iterdir()) == []


def test_reuse_refuses_a_generation_whose_manifest_names_another_generation(
    tmp_path: Path,
) -> None:
    generations = _load("generations")
    profile_dir, profile, profile_bytes = _profile_dir(
        tmp_path, {"one.vcf": _card("one", "Example One")}
    )
    generation = generations.publish(profile_dir, profile, profile_bytes)
    _tamper_manifest(profile_dir / "generations" / generation, "generation", "0" * 64)

    try:
        generations.publish(profile_dir, profile, profile_bytes)
    except generations.UnsafeGenerationState:
        assert list((profile_dir / "staging").iterdir()) == []
        return
    raise AssertionError("expected UnsafeGenerationState")


def test_reuse_refuses_a_generation_with_unexpected_or_missing_entries(tmp_path: Path) -> None:
    generations = _load("generations")
    profile_dir, profile, profile_bytes = _profile_dir(
        tmp_path, {"one.vcf": _card("one", "Example One")}
    )
    generation = generations.publish(profile_dir, profile, profile_bytes)
    published = profile_dir / "generations" / generation
    stray = published / "stray.json"
    stray.write_bytes(b"{}\n")
    stray.chmod(0o600)

    calls: tuple[Callable[[], object], ...] = (
        lambda: generations.publish(profile_dir, profile, profile_bytes),
        lambda: generations.read_current(profile_dir, profile_bytes),
    )
    for call in calls:
        try:
            call()
        except generations.UnsafeGenerationState:
            continue
        raise AssertionError("expected UnsafeGenerationState for a stray entry")
    stray.unlink()

    (published / "index.sqlite3").unlink()
    calls = (
        lambda: generations.publish(profile_dir, profile, profile_bytes),
        lambda: generations.read_current(profile_dir, profile_bytes),
    )
    for call in calls:
        try:
            call()
        except generations.UnsafeGenerationState:
            continue
        raise AssertionError("expected UnsafeGenerationState for a missing index")
    assert list((profile_dir / "staging").iterdir()) == []


def _tamper_index(published: Path, statement: str, parameters: tuple[str, ...] = ()) -> None:
    database = published / "index.sqlite3"
    connection = sqlite3.connect(database, isolation_level=None)
    try:
        connection.execute(statement, parameters)
    finally:
        connection.close()
    database.chmod(0o600)


def _refuses(generations: ModuleType, profile_dir: Path, profile: object, raw: bytes) -> None:
    calls: tuple[Callable[[], object], ...] = (
        lambda: generations.publish(profile_dir, profile, raw),
        lambda: generations.read_current(profile_dir, raw),
    )
    for call in calls:
        try:
            call()
        except generations.UnsafeGenerationState:
            continue
        raise AssertionError("expected UnsafeGenerationState")
    assert list((profile_dir / "staging").iterdir()) == []


def test_index_metadata_that_disagrees_with_the_generation_is_refused(tmp_path: Path) -> None:
    generations = _load("generations")
    profile_dir, profile, profile_bytes = _profile_dir(
        tmp_path, {"one.vcf": _card("one", "Example One")}
    )
    generation = generations.publish(profile_dir, profile, profile_bytes)
    _tamper_index(
        profile_dir / "generations" / generation,
        "UPDATE source_meta SET value = ? WHERE key = 'generation'",
        ("0" * 64,),
    )

    _refuses(generations, profile_dir, profile, profile_bytes)


def test_index_metadata_bound_to_another_profile_or_scope_is_refused(tmp_path: Path) -> None:
    generations = _load("generations")
    for key, value in (
        ("profile_generation_sha256", "0" * 64),
        ("account_namespace", "other"),
        ("collections", '["contacts-b"]'),
        ("schema_version", "carddav-source/9.9"),
    ):
        profile_dir, profile, profile_bytes = _profile_dir(
            tmp_path / key, {"one.vcf": _card("one", "Example One")}
        )
        generation = generations.publish(profile_dir, profile, profile_bytes)
        _tamper_index(
            profile_dir / "generations" / generation,
            f"UPDATE source_meta SET value = ? WHERE key = '{key}'",
            (value,),
        )

        _refuses(generations, profile_dir, profile, profile_bytes)


def test_truncated_or_malformed_index_fails_closed(tmp_path: Path) -> None:
    generations = _load("generations")
    profile_dir, profile, profile_bytes = _profile_dir(
        tmp_path, {"one.vcf": _card("one", "Example One")}
    )
    generation = generations.publish(profile_dir, profile, profile_bytes)
    database = profile_dir / "generations" / generation / "index.sqlite3"
    intact = database.read_bytes()

    for payload in (b"", intact[: len(intact) // 2], b"not a database\n"):
        database.write_bytes(payload)
        database.chmod(0o600)
        _refuses(generations, profile_dir, profile, profile_bytes)


def test_unexpected_index_schema_or_missing_metadata_is_refused(tmp_path: Path) -> None:
    generations = _load("generations")
    for statement in (
        "CREATE TABLE extra (value TEXT)",
        "DELETE FROM source_meta WHERE key = 'collections'",
    ):
        profile_dir, profile, profile_bytes = _profile_dir(
            tmp_path / str(abs(hash(statement))), {"one.vcf": _card("one", "Example One")}
        )
        generation = generations.publish(profile_dir, profile, profile_bytes)
        _tamper_index(profile_dir / "generations" / generation, statement)
        _refuses(generations, profile_dir, profile, profile_bytes)


def test_reading_the_published_index_creates_no_files_and_writes_nothing(tmp_path: Path) -> None:
    generations = _load("generations")
    profile_dir, profile, profile_bytes = _profile_dir(
        tmp_path, {"one.vcf": _card("one", "Example One")}
    )
    generation = generations.publish(profile_dir, profile, profile_bytes)
    published = profile_dir / "generations" / generation
    before = {
        path: (path.lstat().st_ino, path.lstat().st_size, path.lstat().st_mtime_ns)
        for path in sorted(published.rglob("*"))
    }

    assert generations.read_current(profile_dir, profile_bytes) == generation
    assert generations.publish(profile_dir, profile, profile_bytes) == generation

    after = {
        path: (path.lstat().st_ino, path.lstat().st_size, path.lstat().st_mtime_ns)
        for path in sorted(published.rglob("*"))
    }
    assert after == before
    assert not list(published.glob("*-journal"))
    assert not list(published.glob("*-wal"))
    assert not list(published.glob("*-shm"))


def test_read_current_refuses_an_unsafe_generations_root_before_any_generation(
    tmp_path: Path,
) -> None:
    generations = _load("generations")
    profile_dir, _profile, profile_bytes = _profile_dir(
        tmp_path, {"one.vcf": _card("one", "Example One")}
    )
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    root = profile_dir / "generations"
    root.symlink_to(outside, target_is_directory=True)

    try:
        generations.read_current(profile_dir, profile_bytes)
    except generations.UnsafeGenerationState:
        pass
    else:
        raise AssertionError("expected UnsafeGenerationState")

    assert root.is_symlink()
    assert list(outside.iterdir()) == []
    assert not (profile_dir / "current").exists()


def test_read_current_refuses_a_wrong_type_or_mode_at_a_root_without_repair(
    tmp_path: Path,
) -> None:
    generations = _load("generations")
    profile_dir, _profile, profile_bytes = _profile_dir(
        tmp_path, {"one.vcf": _card("one", "Example One")}
    )
    (profile_dir / "generations").mkdir(mode=0o755)

    try:
        generations.read_current(profile_dir, profile_bytes)
    except generations.UnsafeGenerationState:
        pass
    else:
        raise AssertionError("expected UnsafeGenerationState")
    assert stat.S_IMODE((profile_dir / "generations").lstat().st_mode) == 0o755

    (profile_dir / "generations").rmdir()
    (profile_dir / "generations").write_bytes(b"not a directory\n")
    (profile_dir / "generations").chmod(0o600)

    try:
        generations.read_current(profile_dir, profile_bytes)
    except generations.UnsafeGenerationState:
        pass
    else:
        raise AssertionError("expected UnsafeGenerationState")
    assert (profile_dir / "generations").read_bytes() == b"not a directory\n"

    (profile_dir / "generations").unlink()
    profile_dir.chmod(0o755)
    try:
        generations.read_current(profile_dir, profile_bytes)
    except generations.UnsafeGenerationState:
        profile_dir.chmod(0o700)
        return
    profile_dir.chmod(0o700)
    raise AssertionError("expected UnsafeGenerationState")


def test_status_refuses_an_unsafe_generations_root_before_the_first_sync(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _cli()
    _cli_setup(module, tmp_path)
    profile_dir = tmp_path / "carddav-contacts" / "profiles" / "demo"
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    (profile_dir / "generations").symlink_to(outside, target_is_directory=True)

    assert module.main(["status", "--profile", "demo", "--json"]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: unsafe profile state\n"
    assert list(outside.iterdir()) == []


def test_publish_migrates_an_older_pointer_without_reading_it(tmp_path: Path) -> None:
    generations = _load("generations")
    profile_dir, profile, profile_bytes = _profile_dir(
        tmp_path, {"one.vcf": _card("one", "Example One")}
    )
    first = generations.publish(profile_dir, profile, profile_bytes, now=SYNCED_AT_EPOCH)
    pointer = profile_dir / "current"
    pointer.write_bytes(
        json.dumps(
            {"generation": first, "pointer_schema_version": "carddav-current/1.0"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )
    pointer.chmod(0o600)

    assert generations.publish(profile_dir, profile, profile_bytes, now=LATER_EPOCH) == first

    assert json.loads(pointer.read_bytes()) == {
        "generation": first,
        "pointer_schema_version": "carddav-current/1.1",
        "synced_at": "2026-09-10T01:00:00Z",
    }
