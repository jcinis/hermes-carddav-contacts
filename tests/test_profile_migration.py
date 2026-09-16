# ABOUTME: Proves an existing carddav-profile/1.0 profile stays usable after the CRUD release.
# ABOUTME: Uses real CLI setup plus hand-written legacy bytes; no network and no credentials.

"""Profile schema migration contract.

Capabilities are a property of the installed package, not of an immutable
per-profile file, so `carddav-profile/1.1` stops copying them into
`profile.json`. A profile written by the read-only release must keep working
untouched: it is still read, still answers reads, and a repeat `setup` with the
same settings is still a silent no-op that rewrites nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests import support_reads as support

cli = support.load("cli")
profiles = support.load("profiles")

LEGACY_BYTES = (
    b'{"account_namespace":"example",'
    b'"capabilities":{"cleanup_delete":false,"create_update":false,"read_only":true},'
    b'"collection_allowlist":["contacts-a"],'
    b'"profile_schema_version":"carddav-profile/1.0",'
    b'"server_url":"https://carddav.example.invalid/",'
    b'"sync_cadence_seconds":3600}\n'
)


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in support.ENVIRONMENT_KEYS:
        monkeypatch.delenv(key, raising=False)


def _write_legacy(home: Path, profile: str = "demo") -> Path:
    directory = home / "carddav-contacts" / "profiles" / profile
    for level in (home, directory.parent.parent, directory.parent, directory):
        if not level.exists():
            level.mkdir(mode=0o700)
    path = directory / "profile.json"
    path.write_bytes(LEGACY_BYTES)
    path.chmod(0o600)
    lock = directory / "profile.lock"
    lock.touch()
    lock.chmod(0o600)
    return path


def test_new_setup_writes_the_profile_without_a_capability_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    profile_dir = support.setup_profile(cli, tmp_path)

    stored = json.loads((profile_dir / "profile.json").read_bytes())
    assert stored["profile_schema_version"] == "carddav-profile/1.1"
    assert "capabilities" not in stored
    assert stored["account_namespace"] == "example"
    assert stored["collection_allowlist"] == ["contacts-a"]


def test_a_legacy_profile_is_read_normalized_and_never_rewritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = _write_legacy(tmp_path)
    before = (path.read_bytes(), path.lstat().st_mtime_ns)

    normalized = profiles.read_profile(path)
    assert normalized["profile_schema_version"] == "carddav-profile/1.1"
    assert normalized["server_url"] == "https://carddav.example.invalid/"
    assert normalized["sync_cadence_seconds"] == 3600

    # An identical repeat setup stays a silent no-op over the legacy bytes.
    assert support.setup_profile(cli, tmp_path) == path.parent
    assert (path.read_bytes(), path.lstat().st_mtime_ns) == before


def test_a_legacy_profile_still_conflicts_on_changed_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = _write_legacy(tmp_path)
    before = path.read_bytes()

    result = cli.main([
        "setup", "--profile", "demo", "--namespace", "example",
        "--server-url", "https://carddav.example.invalid/", "--collection", "other",
    ])

    assert result == 2
    assert path.read_bytes() == before


def test_a_legacy_profile_answers_a_local_read_after_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = _write_legacy(tmp_path)
    support.write_mirror(path.parent, {"one.vcf": support.card("one", "Example One")})
    support.publish(path.parent)

    assert cli.main(["status", "--profile", "demo", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["contact_count"] == 1
    assert path.read_bytes() == LEGACY_BYTES
