"""Opt-in real Radicale/vdirsyncer proof; no production server or credentials."""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast
from wsgiref.simple_server import WSGIRequestHandler, make_server

import pytest

from tests import support_reads as support
from tests.test_version import ENTRYPOINT

pytestmark = pytest.mark.integration

USERNAME = "fixture-user"
PASSWORD = "synthetic-test-password"
ALLOWED_METHODS = {"GET", "HEAD", "OPTIONS", "PROPFIND", "REPORT"}


def _card(uid: str, name: str) -> bytes:
    return (f"BEGIN:VCARD\r\nVERSION:3.0\r\nUID:{uid}\r\nFN:{name}\r\n"
            "END:VCARD\r\n").encode()


class _QuietHandler(WSGIRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


class _Recorder:
    def __init__(self, app: Callable[..., Iterable[bytes]]) -> None:
        self.app = app
        self.requests: list[tuple[str, str]] = []
        self.provisioning = False
        self.url = ""
        self.stop: Callable[[], None] = lambda: None

    def __call__(self, environ: dict[str, Any],
                 start_response: Callable[..., Any]) -> Iterable[bytes]:
        method, path = environ["REQUEST_METHOD"], environ["PATH_INFO"]
        if not self.provisioning:
            self.requests.append((method, path))
            if method not in ALLOWED_METHODS:
                start_response("405 Method Not Allowed", [("Content-Length", "0")])
                return [b""]
        expected = "Basic " + base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()
        if environ.get("HTTP_AUTHORIZATION") != expected:
            start_response("401 Unauthorized", [
                ("WWW-Authenticate", 'Basic realm="fixture"'), ("Content-Length", "0"),
            ])
            return [b""]
        return self.app(environ, start_response)

    def seed(self, method: str, path: str, data: bytes | None = None,
             content_type: str = "text/vcard") -> None:
        """Fixture provisioning only; remote writes are rejected during CLI runs."""
        self.provisioning = True
        try:
            headers = {
                "Authorization": "Basic " + base64.b64encode(
                    f"{USERNAME}:{PASSWORD}".encode()).decode(),
                "Content-Type": content_type,
            }
            request = urllib.request.Request(self.url + path, data=data,
                                             headers=headers, method=method)
            with urllib.request.urlopen(request, timeout=5) as response:
                assert response.status in (200, 201, 204)
        finally:
            self.provisioning = False


@pytest.fixture
def dav_server(tmp_path: Path) -> Iterator[_Recorder]:
    radicale = pytest.importorskip("radicale")
    config = pytest.importorskip("radicale.config")

    settings = config.load([])
    settings.update({
        "auth": {"type": "none"},
        "rights": {"type": "owner_only"},
        "storage": {"filesystem_folder": str(tmp_path / "server-storage")},
    })
    recorder = _Recorder(radicale.Application(settings))
    with make_server("127.0.0.1", 0, recorder, handler_class=_QuietHandler) as server:
        recorder.url = f"http://127.0.0.1:{server.server_port}"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop() -> None:
            """Take the address book offline mid-test; further connections are refused."""
            server.shutdown()
            server.server_close()

        recorder.stop = stop
        try:
            # Successful seed requests also prove readiness; no sleep/retry loop.
            for collection in ("contacts", "unselected"):
                recorder.seed("MKCOL", f"/{USERNAME}/{collection}/", (
                    b'<mkcol xmlns="DAV:"><set><prop><resourcetype><collection/>'
                    b'<addressbook xmlns="urn:ietf:params:xml:ns:carddav"/>'
                    b'</resourcetype></prop></set></mkcol>'
                ), "application/xml")
            recorder.seed("PUT", f"/{USERNAME}/contacts/one.vcf", _card("one", "Example One"))
            recorder.seed("PUT", f"/{USERNAME}/contacts/two.vcf", _card("two", "Example Two"))
            recorder.seed("PUT", f"/{USERNAME}/unselected/hidden.vcf", _card("hidden", "Not Selected"))
            yield recorder
        finally:
            server.shutdown()
            thread.join(timeout=5)


@contextmanager
def _clean_env(home: Path) -> Iterator[dict[str, str]]:
    yield {"HERMES_HOME": str(home), "PATH": os.defpath,
           "CARDDAV_USERNAME": USERNAME, "CARDDAV_PASSWORD": PASSWORD}


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(ENTRYPOINT), *args], env=env,
                          capture_output=True, text=True, timeout=45, check=False)


def _setup(env: dict[str, str], server: _Recorder, collection: str = "contacts",
           profile: str = "demo") -> None:
    result = _run(env, "setup", "--profile", profile, "--namespace", "example",
                  "--server-url", server.url + f"/{USERNAME}/", "--collection", collection)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


def _assert_private_state(home: Path) -> None:
    root = home / "carddav-contacts"
    for path in [root, *root.rglob("*")]:
        info = path.lstat()
        assert stat.S_IMODE(info.st_mode) == (0o700 if path.is_dir() else 0o600)
        assert info.st_uid == os.getuid()
        assert not path.is_symlink()
        if path.is_file():
            assert info.st_nlink == 1
            assert PASSWORD.encode() not in path.read_bytes()
    assert not list(root.rglob("*.conf"))


def test_real_discover_sync_mirrors_only_allowlist_without_remote_writes(
    tmp_path: Path, dav_server: _Recorder
) -> None:
    home = tmp_path / "client"
    with _clean_env(home) as env:
        _setup(env, dav_server)
        for command in ("discover", "sync", "sync"):
            result = _run(env, command, "--profile", "demo")
            assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
        mirror = home / "carddav-contacts/profiles/demo/runtime/mirror"
        cards = sorted(mirror.rglob("*.vcf"))
        assert len(cards) == 2
        assert all(path.parent.name == "contacts" for path in cards)
        assert b"Not Selected" not in b"".join(path.read_bytes() for path in cards)
        assert any(method == "REPORT" for method, _ in dav_server.requests)
        assert all(method in ALLOWED_METHODS for method, _ in dav_server.requests)
        _assert_private_state(home)


def test_missing_remote_collection_fails_without_creating_it(
    tmp_path: Path, dav_server: _Recorder
) -> None:
    with _clean_env(tmp_path / "client") as env:
        _setup(env, dav_server, collection="missing")
        result = _run(env, "discover", "--profile", "demo")
        assert (result.returncode, result.stdout, result.stderr) == (2, "", "error: discover failed\n")
        assert all(method in ALLOWED_METHODS for method, _ in dav_server.requests)
        _assert_private_state(Path(env["HERMES_HOME"]))


def test_sync_requires_explicit_discovery(tmp_path: Path, dav_server: _Recorder) -> None:
    with _clean_env(tmp_path / "client") as env:
        _setup(env, dav_server)
        result = _run(env, "sync", "--profile", "demo")
        assert (result.returncode, result.stdout, result.stderr) == (2, "", "error: sync failed\n")
        assert dav_server.requests == []


def test_failed_auth_is_redacted_and_leaves_no_config(tmp_path: Path, dav_server: _Recorder) -> None:
    with _clean_env(tmp_path / "client") as env:
        _setup(env, dav_server)
        env["CARDDAV_PASSWORD"] = "wrong-synthetic-password"
        result = _run(env, "discover", "--profile", "demo")
        assert (result.returncode, result.stdout, result.stderr) == (2, "", "error: discover failed\n")
        root = Path(env["HERMES_HOME"])
        _assert_private_state(root)
        assert not any(b"wrong-synthetic-password" in path.read_bytes()
                       for path in root.rglob("*") if path.is_file())
        assert dav_server.requests
        assert all(method in ALLOWED_METHODS for method, _ in dav_server.requests)
        # A failed operation must release the profile lock for a corrected retry.
        env["CARDDAV_PASSWORD"] = PASSWORD
        retry = _run(env, "discover", "--profile", "demo")
        assert (retry.returncode, retry.stdout, retry.stderr) == (0, "", "")


def test_sync_does_not_consult_or_modify_another_profile(
    tmp_path: Path, dav_server: _Recorder
) -> None:
    home = tmp_path / "client"
    with _clean_env(home) as env:
        _setup(env, dav_server)
        _setup(env, dav_server, collection="unselected", profile="other")
        other = home / "carddav-contacts/profiles/other"
        # Deliberately invalid sibling profile must not affect the selected one.
        (other / "profile.json").write_bytes(b"not json")
        before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns)
                  for path in other.iterdir()}
        # The complete CARDDAV pair must win over a broken DAV fallback pair.
        env["DAV_USERNAME"] = "incorrect-fallback"
        for command in ("discover", "sync"):
            result = _run(env, command, "--profile", "demo")
            assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
        assert {path.name: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in other.iterdir()} == before
        assert all(method in ALLOWED_METHODS for method, _ in dav_server.requests)


def test_local_edits_are_reverted_and_remote_changes_are_pulled(
    tmp_path: Path, dav_server: _Recorder
) -> None:
    home = tmp_path / "client"
    with _clean_env(home) as env:
        _setup(env, dav_server)
        for command in ("discover", "sync"):
            assert _run(env, command, "--profile", "demo").returncode == 0
        mirror = home / "carddav-contacts/profiles/demo/runtime/mirror/contacts"
        first = next(path for path in mirror.glob("*.vcf") if b"UID:one" in path.read_bytes())
        first.write_bytes(_card("one", "Local Edit Must Not Upload"))
        dav_server.seed("PUT", f"/{USERNAME}/contacts/two.vcf", _card("two", "Updated Remotely"))
        result = _run(env, "sync", "--profile", "demo")
        assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
        content = b"".join(path.read_bytes() for path in mirror.glob("*.vcf"))
        assert b"Local Edit Must Not Upload" not in content
        assert b"Example One" in content and b"Updated Remotely" in content
        assert all(method in ALLOWED_METHODS for method, _ in dav_server.requests)
        _assert_private_state(home)


def _status(env: dict[str, str]) -> dict[str, object]:
    result = _run(env, "status", "--profile", "demo", "--json")
    assert (result.returncode, result.stderr) == (0, "")
    parsed = json.loads(result.stdout)
    assert isinstance(parsed, dict)
    return parsed


def test_first_sync_publishes_a_populated_index_and_a_repeat_sync_does_not_churn(
    tmp_path: Path, dav_server: _Recorder
) -> None:
    home = tmp_path / "client"
    with _clean_env(home) as env:
        _setup(env, dav_server)
        assert _status(env)["current_generation"] is None
        for command in ("discover", "sync"):
            assert _run(env, command, "--profile", "demo").returncode == 0

        generation = _status(env)["current_generation"]
        assert isinstance(generation, str)
        profile_dir = home / "carddav-contacts/profiles/demo"
        published = profile_dir / "generations" / generation
        connection = sqlite3.connect(f"file:{published / 'index.sqlite3'}?mode=ro", uri=True)
        try:
            names = connection.execute(
                "SELECT display_name FROM contacts ORDER BY display_name"
            ).fetchall()
        finally:
            connection.close()
        assert names == [("Example One",), ("Example Two",)]

        pointer = profile_dir / "current"
        before = json.loads(pointer.read_bytes())
        published_before = {
            path.relative_to(published): path.read_bytes()
            for path in published.rglob("*")
            if path.is_file()
        }
        assert _run(env, "sync", "--profile", "demo").returncode == 0
        after = json.loads(pointer.read_bytes())
        # A no-op sync creates no generation, but does record that it succeeded.
        assert _status(env)["current_generation"] == generation
        assert after["generation"] == before["generation"]
        assert after["synced_at"] >= before["synced_at"]
        assert {
            path.relative_to(published): path.read_bytes()
            for path in published.rglob("*")
            if path.is_file()
        } == published_before
        assert [path.name for path in (profile_dir / "generations").iterdir()] == [generation]
        assert list((profile_dir / "staging").iterdir()) == []
        assert all(method in ALLOWED_METHODS for method, _ in dav_server.requests)
        _assert_private_state(home)


def test_unusable_remote_contact_fails_sync_and_retains_the_current_generation(
    tmp_path: Path, dav_server: _Recorder
) -> None:
    home = tmp_path / "client"
    with _clean_env(home) as env:
        _setup(env, dav_server)
        for command in ("discover", "sync"):
            assert _run(env, command, "--profile", "demo").returncode == 0
        profile_dir = home / "carddav-contacts/profiles/demo"
        pointer = profile_dir / "current"
        before = (pointer.read_bytes(), pointer.lstat().st_mtime_ns)
        generation = _status(env)["current_generation"]
        dav_server.seed("PUT", f"/{USERNAME}/contacts/blank.vcf",
                        _card("   ", "Blank Identifier"))

        result = _run(env, "sync", "--profile", "demo")

        assert (result.returncode, result.stdout, result.stderr) == (2, "", "error: sync failed\n")
        assert (pointer.read_bytes(), pointer.lstat().st_mtime_ns) == before
        assert _status(env)["current_generation"] == generation
        assert [path.name for path in (profile_dir / "generations").iterdir()] == [generation]
        assert list((profile_dir / "staging").iterdir()) == []
        assert all(method in ALLOWED_METHODS for method, _ in dav_server.requests)
        _assert_private_state(home)


def test_a_corrupted_published_index_fails_a_repeat_sync_and_status_closed(
    tmp_path: Path, dav_server: _Recorder
) -> None:
    home = tmp_path / "client"
    with _clean_env(home) as env:
        _setup(env, dav_server)
        for command in ("discover", "sync"):
            assert _run(env, command, "--profile", "demo").returncode == 0
        generation = _status(env)["current_generation"]
        assert isinstance(generation, str)
        profile_dir = home / "carddav-contacts/profiles/demo"
        pointer = profile_dir / "current"
        before = (pointer.read_bytes(), pointer.lstat().st_mtime_ns)
        database = profile_dir / "generations" / generation / "index.sqlite3"
        connection = sqlite3.connect(database, isolation_level=None)
        try:
            connection.execute(
                "UPDATE source_meta SET value = ? WHERE key = 'generation'", ("0" * 64,)
            )
        finally:
            connection.close()

        unsafe = (2, "", "error: unsafe profile state\n")
        result = _run(env, "sync", "--profile", "demo")
        status = _run(env, "status", "--profile", "demo", "--json")

        assert (result.returncode, result.stdout, result.stderr) == unsafe
        assert (status.returncode, status.stdout, status.stderr) == unsafe
        assert (pointer.read_bytes(), pointer.lstat().st_mtime_ns) == before
        assert [path.name for path in (profile_dir / "generations").iterdir()] == [generation]
        assert list((profile_dir / "staging").iterdir()) == []
        assert all(method in ALLOWED_METHODS for method, _ in dav_server.requests)
        _assert_private_state(home)


DUPLICATE_CARDS = {
    "dup-one.vcf": (
        b"BEGIN:VCARD\r\nVERSION:3.0\r\nUID:dup-one\r\nFN:Duplicate Person\r\n"
        b"EMAIL:Shared@Example.invalid\r\nEND:VCARD\r\n"
    ),
    "dup-two.vcf": (
        b"BEGIN:VCARD\r\nVERSION:3.0\r\nUID:dup-two\r\nFN:duplicate  person\r\n"
        b"EMAIL:shared@example.invalid\r\nEND:VCARD\r\n"
    ),
}


def _read(env: dict[str, str], *args: str) -> dict[str, object]:
    """Run one local read command and validate its envelope."""
    result = _run(env, *args)
    assert (result.returncode, result.stderr) == (0, ""), result.stderr
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict)
    support.load("schemas").validate_command(payload)
    return payload


def _wait_for_the_next_whole_second() -> None:
    start = int(time.time())
    while int(time.time()) == start:
        time.sleep(0.05)


def test_local_reads_answer_the_synced_generation_without_any_remote_write(
    tmp_path: Path, dav_server: _Recorder
) -> None:
    home = tmp_path / "client"
    for name, content in DUPLICATE_CARDS.items():
        dav_server.seed("PUT", f"/{USERNAME}/contacts/{name}", content)
    with _clean_env(home) as env:
        _setup(env, dav_server)
        for command in ("discover", "sync"):
            assert _run(env, command, "--profile", "demo").returncode == 0

        status = _read(env, "status", "--profile", "demo", "--json")
        generation = status["current_generation"]
        assert isinstance(generation, str)
        assert status["contact_count"] == 4

        snapshot = _read(env, "snapshot", "--profile", "demo", "--json")
        contacts = snapshot["data"]["contacts"]  # type: ignore[index]
        # Canonical contact order folds A-Z to a-z, then compares exactly.
        assert [contact["name"]["display"] for contact in contacts] == [
            "duplicate  person",
            "Duplicate Person",
            "Example One",
            "Example Two",
        ]
        assert snapshot["generation"] == generation
        assert snapshot["synced_at"] == status["synced_at"]

        search = _read(env, "search", "--profile", "demo", "--query", "duplicate", "--json")
        assert search["total_matches"] == 2

        contact_id = next(
            contact["contact_id"]
            for contact in contacts
            if contact["name"]["display"] == "Example One"
        )
        show = _read(env, "show", "--profile", "demo", "--id", contact_id, "--json")
        assert show["contact"]["name"]["display"] == "Example One"  # type: ignore[index]

        audit = _read(env, "audit", "--profile", "demo", "--json")
        candidates = cast(list[dict[str, object]], audit["candidates"])
        assert [(group["reason"], group["key"]) for group in candidates] == [
            ("email", "shared@example.invalid"),
            ("name", "duplicate person"),
        ]

        assert all(method in ALLOWED_METHODS for method, _ in dav_server.requests)
        _assert_private_state(home)


def test_reads_survive_the_server_going_down_and_a_failed_sync_keeps_freshness(
    tmp_path: Path, dav_server: _Recorder
) -> None:
    home = tmp_path / "client"
    with _clean_env(home) as env:
        _setup(env, dav_server)
        for command in ("discover", "sync"):
            assert _run(env, command, "--profile", "demo").returncode == 0
        first = _read(env, "status", "--profile", "demo", "--json")
        profile_dir = home / "carddav-contacts/profiles/demo"
        published = profile_dir / "generations" / str(first["current_generation"])
        published_before = {
            path.relative_to(published): path.read_bytes()
            for path in published.rglob("*")
            if path.is_file()
        }

        # An unchanged address book records the new sync time and nothing else.
        _wait_for_the_next_whole_second()
        assert _run(env, "sync", "--profile", "demo").returncode == 0
        refreshed = _read(env, "status", "--profile", "demo", "--json")
        assert refreshed["current_generation"] == first["current_generation"]
        assert str(refreshed["synced_at"]) > str(first["synced_at"])
        assert [path.name for path in (profile_dir / "generations").iterdir()] == [
            first["current_generation"]
        ]
        assert {
            path.relative_to(published): path.read_bytes()
            for path in published.rglob("*")
            if path.is_file()
        } == published_before

        dav_server.stop()

        failed = _run(env, "sync", "--profile", "demo")
        assert (failed.returncode, failed.stdout, failed.stderr) == (
            2,
            "",
            "error: sync failed\n",
        )

        offline = _read(env, "status", "--profile", "demo", "--json")
        assert offline["current_generation"] == refreshed["current_generation"]
        assert offline["synced_at"] == refreshed["synced_at"]
        snapshot = _read(env, "snapshot", "--profile", "demo", "--json")
        assert [
            contact["name"]["display"]
            for contact in snapshot["data"]["contacts"]  # type: ignore[index]
        ] == ["Example One", "Example Two"]
        assert _read(env, "search", "--profile", "demo", "--query", "example", "--json")[
            "total_matches"
        ] == 2
        assert _read(env, "audit", "--profile", "demo", "--json")["candidates"] == []
        _assert_private_state(home)
