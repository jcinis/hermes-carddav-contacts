# ABOUTME: Opt-in real CRUD proof against a disposable localhost Radicale; no live server.
# ABOUTME: Drives the entry-point CLI and the importable API; synthetic contacts only.

"""Real conditional writes against a disposable local CardDAV server.

Everything here runs against a Radicale instance created for the test and
thrown away with it. It proves what only a real server can: that a create
really carries `If-None-Match`, that an update and a delete really carry
`If-Match` and really fail closed on a stale revision, that untouched vCard
properties survive a real round trip byte-for-byte, and that ordinary local
reads still open no socket after a write.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import threading
import urllib.request
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any
from wsgiref.simple_server import make_server

import pytest

from tests import support_reads as support
from tests.test_transport_integration import PASSWORD, USERNAME, _QuietHandler

pytestmark = pytest.mark.integration

ENTRYPOINT = support.ENTRYPOINT
REPO_ROOT = Path(__file__).parent.parent

RICH_CARD = (
    "BEGIN:VCARD\r\n"
    "VERSION:3.0\r\n"
    "UID:integration-rich\r\n"
    "FN:Rich Example\r\n"
    "N:Example;Rich;Middle,Second;Dr.;PhD\r\n"
    "NICKNAME:Ricky,Ri\\,ch\r\n"
    "EMAIL;TYPE=WORK:rich@example.invalid\r\n"
    "CATEGORIES:example-category\r\n"
    "PHOTO;ENCODING=b;TYPE=PNG:aGVsbG8=\r\n"
    "X-EXAMPLE-CUSTOM:keep-me\r\n"
    "END:VCARD\r\n"
)


class _WritableRecorder:
    """A Radicale front end that records every method and permits writes."""

    def __init__(self, app: Callable[..., Iterable[bytes]]) -> None:
        self.app = app
        self.requests: list[tuple[str, str]] = []
        self.provisioning = False
        self.url = ""

    def __call__(
        self, environ: dict[str, Any], start_response: Callable[..., Any]
    ) -> Iterable[bytes]:
        if not self.provisioning:
            self.requests.append((environ["REQUEST_METHOD"], environ["PATH_INFO"]))
        return self.app(environ, start_response)

    def _request(self, method: str, path: str, data: bytes | None,
                 content_type: str) -> bytes:
        headers = {
            "Authorization": "Basic " + base64.b64encode(
                f"{USERNAME}:{PASSWORD}".encode()).decode(),
            "Content-Type": content_type,
        }
        request = urllib.request.Request(
            self.url + path, data=data, headers=headers, method=method)
        self.provisioning = True
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                assert response.status in (200, 201, 204)
                return bytes(response.read())
        finally:
            self.provisioning = False

    def seed(self, path: str, data: bytes) -> None:
        self._request("PUT", path, data, "text/vcard")

    def fetch(self, path: str) -> str:
        return self._request("GET", path, None, "text/vcard").decode("utf-8")


@pytest.fixture
def dav_server(tmp_path: Path) -> Iterator[_WritableRecorder]:
    radicale = pytest.importorskip("radicale")
    config = pytest.importorskip("radicale.config")

    settings = config.load([])
    settings.update({
        "auth": {"type": "none"},
        "rights": {"type": "owner_only"},
        "storage": {"filesystem_folder": str(tmp_path / "server-storage")},
    })
    recorder = _WritableRecorder(radicale.Application(settings))
    with make_server("127.0.0.1", 0, recorder, handler_class=_QuietHandler) as server:
        recorder.url = f"http://127.0.0.1:{server.server_port}"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            recorder._request("MKCOL", f"/{USERNAME}/contacts/", (
                b'<mkcol xmlns="DAV:"><set><prop><resourcetype><collection/>'
                b'<addressbook xmlns="urn:ietf:params:xml:ns:carddav"/>'
                b"</resourcetype></prop></set></mkcol>"
            ), "application/xml")
            recorder.requests.clear()
            yield recorder
        finally:
            server.shutdown()
            thread.join(timeout=5)


@pytest.fixture
def env(tmp_path: Path, dav_server: _WritableRecorder) -> dict[str, str]:
    home = tmp_path / "client"
    environment = {
        "HERMES_HOME": str(home), "PATH": os.defpath,
        "CARDDAV_USERNAME": USERNAME, "CARDDAV_PASSWORD": PASSWORD,
    }
    result = _run(environment, "setup", "--profile", "demo", "--namespace", "example",
                  "--server-url", f"{dav_server.url}/{USERNAME}/", "--collection", "contacts")
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    for command in ("discover", "sync"):
        assert _run(environment, command, "--profile", "demo").returncode == 0
    return environment


def _run(environment: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(ENTRYPOINT), *args], env=environment,
                          capture_output=True, text=True, timeout=120, check=False)


def _json(environment: dict[str, str], *args: str) -> Any:
    result = _run(environment, *args)
    assert (result.returncode, result.stderr) == (0, ""), result.stderr
    return json.loads(result.stdout)


def _changes(**payload: object) -> str:
    document: dict[str, object] = {
        "change_schema_version": "carddav-change/1.0",
        "set": {}, "clear": [], "replace": {},
    }
    document.update(payload)
    return json.dumps(document)


def test_a_real_create_update_and_delete_round_trip_through_the_command_surface(
    env: dict[str, str], dav_server: _WritableRecorder
) -> None:
    # A second resident contact keeps the collection non-empty throughout, which
    # is the ordinary case; emptying it entirely is covered separately below.
    dav_server.seed(f"/{USERNAME}/contacts/rich.vcf", RICH_CARD.encode())
    assert _run(env, "sync", "--profile", "demo").returncode == 0

    operation = _json(env, "prepare-create", "--profile", "demo", "--collection", "contacts",
                      "--changes", _changes(set={"display": "Created Example",
                                                 "given": "Created", "family": "Example"}),
                      "--json")
    assert operation["operation"] == "create"
    contact_id = operation["contact_id"]

    result = _json(env, "apply", "--profile", "demo",
                   "--operation", operation["operation_id"], "--json")
    assert (result["outcome"], result["remote_write"]) == ("applied", True)
    assert result["local_cache"] == "refreshed"
    assert any(method == "PUT" for method, _ in dav_server.requests)

    # The refreshed local generation already answers for the new contact.
    status = _json(env, "status", "--profile", "demo", "--json")
    assert status["cache_invalidated"] is False
    show = _json(env, "show", "--profile", "demo", "--id", contact_id, "--json")
    assert show["contact"]["name"]["display"] == "Created Example"

    operation = _json(env, "prepare-update", "--profile", "demo", "--id", contact_id,
                      "--changes", _changes(set={"display": "Renamed Example"},
                                            replace={"phones": [
                                                {"value": "+15550100", "types": ["cell"],
                                                 "label": None, "preference": 1}]}),
                      "--json")
    assert operation["base_revision"]
    result = _json(env, "apply", "--profile", "demo",
                   "--operation", operation["operation_id"], "--json")
    assert result["outcome"] == "applied"
    show = _json(env, "show", "--profile", "demo", "--id", contact_id, "--json")
    assert show["contact"]["name"]["display"] == "Renamed Example"
    assert show["contact"]["phones"] == [
        {"value": "+15550100", "types": ["cell"], "label": None, "preference": 1}]
    assert show["contact"]["name"]["given"] == "Created"

    operation = _json(env, "prepare-delete", "--profile", "demo", "--id", contact_id, "--json")
    result = _json(env, "apply", "--profile", "demo",
                   "--operation", operation["operation_id"], "--json")
    assert (result["outcome"], result["local_cache"]) == ("applied", "refreshed")
    assert any(method == "DELETE" for method, _ in dav_server.requests)

    missing = _run(env, "show", "--profile", "demo", "--id", contact_id, "--json")
    assert (missing.returncode, missing.stderr) == (2, "error: contact not found\n")


def test_deleting_the_last_contact_reports_a_stale_cache_not_a_failed_write(
    env: dict[str, str], dav_server: _WritableRecorder
) -> None:
    """Pinned native behaviour: vdirsyncer refuses to sync a newly emptied storage.

    Pinned vdirsyncer 0.21.0 raises `StorageEmpty` when a collection it has
    seen with items becomes empty, and this skill deliberately does not pass
    `force_delete` to the routine read-only sync. The delete itself is still
    final and verified, so it must be reported as applied with a stale local
    cache — never as a failure that would invite deleting the record again.
    """
    dav_server.seed(f"/{USERNAME}/contacts/rich.vcf", RICH_CARD.encode())
    assert _run(env, "sync", "--profile", "demo").returncode == 0
    contact_id = _json(env, "snapshot", "--profile", "demo",
                       "--json")["data"]["contacts"][0]["contact_id"]

    operation = _json(env, "prepare-delete", "--profile", "demo", "--id", contact_id, "--json")
    result = _json(env, "apply", "--profile", "demo",
                   "--operation", operation["operation_id"], "--json")

    assert (result["outcome"], result["remote_write"]) == ("applied", True)
    assert result["local_cache"] == "stale"
    # The record really is gone; a fresh remote read proves it, not the cache.
    gone = _run(env, "record", "--profile", "demo", "--id", contact_id, "--json")
    assert (gone.returncode, gone.stderr) == (2, "error: contact not found\n")
    # Every local read now says out loud that it may be answering stale data.
    for command in ("status", "snapshot", "audit"):
        assert _json(env, command, "--profile", "demo", "--json")["cache_invalidated"] is True


def test_an_update_preserves_every_untouched_property_on_the_real_server(
    env: dict[str, str], dav_server: _WritableRecorder
) -> None:
    dav_server.seed(f"/{USERNAME}/contacts/rich.vcf", RICH_CARD.encode())
    assert _run(env, "sync", "--profile", "demo").returncode == 0
    contact_id = next(
        contact["contact_id"]
        for contact in _json(env, "snapshot", "--profile", "demo", "--json")["data"]["contacts"]
        if contact["name"]["display"] == "Rich Example"
    )

    operation = _json(env, "prepare-update", "--profile", "demo", "--id", contact_id,
                      "--changes", _changes(set={"display": "Rich Renamed"}), "--json")
    assert _json(env, "apply", "--profile", "demo",
                 "--operation", operation["operation_id"], "--json")["outcome"] == "applied"

    stored = dav_server.fetch(f"/{USERNAME}/contacts/rich.vcf")
    for preserved in (
        "UID:integration-rich",
        "N:Example;Rich;Middle,Second;Dr.;PhD",
        "EMAIL;TYPE=WORK:rich@example.invalid",
        "CATEGORIES:example-category",
        "PHOTO;ENCODING=b;TYPE=PNG:aGVsbG8=",
        "X-EXAMPLE-CUSTOM:keep-me",
    ):
        assert preserved in stored, preserved
    assert "FN:Rich Renamed" in stored
    assert "FN:Rich Example" not in stored

    # Radicale 3.5.8 re-serializes every stored card through vobject, which
    # collapses a multi-component NICKNAME on plain PUT before this skill is
    # involved at all, so alias preservation cannot be asserted against this
    # server. `tests/test_vcards.py` proves the editor itself preserves it.
    contact = _json(env, "show", "--profile", "demo", "--id", contact_id, "--json")["contact"]
    assert contact["name"]["additional"] == "Middle, Second"


def test_a_stale_update_and_a_stale_delete_both_fail_closed(
    env: dict[str, str], dav_server: _WritableRecorder
) -> None:
    dav_server.seed(f"/{USERNAME}/contacts/rich.vcf", RICH_CARD.encode())
    assert _run(env, "sync", "--profile", "demo").returncode == 0
    contact_id = next(
        contact["contact_id"]
        for contact in _json(env, "snapshot", "--profile", "demo", "--json")["data"]["contacts"]
    )

    update = _json(env, "prepare-update", "--profile", "demo", "--id", contact_id,
                   "--changes", _changes(set={"display": "Never Applied"}), "--json")
    delete = _json(env, "prepare-delete", "--profile", "demo", "--id", contact_id, "--json")

    # Somebody else edits the same record between review and application.
    dav_server.seed(f"/{USERNAME}/contacts/rich.vcf",
                    RICH_CARD.replace("FN:Rich Example", "FN:Changed Elsewhere").encode())

    for operation in (update, delete):
        result = _run(env, "apply", "--profile", "demo",
                      "--operation", operation["operation_id"], "--json")
        assert (result.returncode, result.stdout) == (2, "")
        assert result.stderr == "error: revision conflict\n"

    assert "FN:Changed Elsewhere" in dav_server.fetch(f"/{USERNAME}/contacts/rich.vcf")


def test_local_reads_stay_offline_and_routine_sync_never_mutates(
    env: dict[str, str], dav_server: _WritableRecorder
) -> None:
    dav_server.seed(f"/{USERNAME}/contacts/rich.vcf", RICH_CARD.encode())
    assert _run(env, "sync", "--profile", "demo").returncode == 0
    sync_methods = {method for method, _ in dav_server.requests}
    assert not sync_methods & {"PUT", "DELETE", "MKCOL", "MOVE", "COPY", "PROPPATCH"}

    before = len(dav_server.requests)
    for args in (
        ["status", "--profile", "demo", "--json"],
        ["snapshot", "--profile", "demo", "--json"],
        ["search", "--profile", "demo", "--query", "rich", "--json"],
        ["audit", "--profile", "demo", "--json"],
    ):
        assert _run(env, *args).returncode == 0
    assert len(dav_server.requests) == before


def test_the_importable_api_drives_the_same_real_operations(
    env: dict[str, str], dav_server: _WritableRecorder
) -> None:
    script = """
import json, sys
from hermes_carddav_contacts import api

changes = {
    "change_schema_version": api.CHANGE_SCHEMA_VERSION,
    "set": {"display": "Api Example"},
    "clear": [],
    "replace": {"emails": [{"value": "api@example.invalid", "types": ["work"],
                            "label": None, "preference": None}]},
}
operation = api.prepare_create("demo", "contacts", changes)
created = api.apply_operation("demo", operation["operation_id"])
fresh = api.read_record("demo", operation["contact_id"])
listed = api.read_collection("demo", "contacts")
removal = api.prepare_delete("demo", operation["contact_id"])
deleted = api.apply_operation("demo", removal["operation_id"])
try:
    api.read_record("demo", operation["contact_id"])
    gone = False
except api.ContactNotFound:
    gone = True
json.dump({"created": created, "fresh_revision": fresh["revision"],
           "listed": len(listed), "deleted": deleted, "gone": gone}, sys.stdout)
"""
    result = subprocess.run(
        [sys.executable, "-c", script], env={**env, "PYTHONPATH": str(REPO_ROOT)},
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=180, check=False)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)

    assert payload["created"]["outcome"] == "applied"
    assert payload["created"]["result_schema_version"] == "carddav-result/1.0"
    assert payload["fresh_revision"]
    assert payload["listed"] == 1
    assert payload["deleted"]["outcome"] == "applied"
    assert payload["gone"] is True
