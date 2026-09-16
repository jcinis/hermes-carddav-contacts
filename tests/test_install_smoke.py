# ABOUTME: Opt-in `packaging` proof — real uv build, artifact inspection, and a fresh installed venv.
# ABOUTME: The installed venv holds only pinned runtime deps; Radicale runs in the development venv.

"""Real build and installed-runtime proof.

Selected by `-m packaging` only, because every run performs an actual
`uv build` plus a fresh virtualenv install. It builds the wheel and sdist into
a temporary directory outside this checkout, inspects both for inclusion-
boundary and privacy violations, installs the wheel into a clean virtualenv,
and then drives the installed `hermes-carddav-contacts` console command:
`version`, `setup`, read-only `discover`/`sync` against a disposable localhost
Radicale recorder, and every local query with the server already shut down.

Privacy scan scope (deliberately bounded, not a universal secret scanner):
shipped artifact *text* is checked for hosts and email domains outside an
explicit placeholder allowlist, for a fixed list of credential-shaped token
patterns, and for runtime/private path components. It cannot detect an
arbitrary high-entropy secret that matches none of those shapes.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from wsgiref.simple_server import make_server

import pytest

from tests.test_transport_integration import (
    ALLOWED_METHODS,
    PASSWORD,
    USERNAME,
    _card,
    _QuietHandler,
    _Recorder,
)

pytestmark = pytest.mark.packaging

REPO_ROOT = Path(__file__).parent.parent
VERSION = "0.2.0"
CONSOLE_COMMAND = "hermes-carddav-contacts"
PROFILE = "smoke"

WHEEL_CONTENTS = {
    "hermes_carddav_contacts/__init__.py",
    "hermes_carddav_contacts/api.py",
    "skills/productivity/carddav-contacts/SKILL.md",
    "skills/productivity/carddav-contacts/references/configuration.md",
    "skills/productivity/carddav-contacts/references/data-model.md",
    "skills/productivity/carddav-contacts/references/write-safety.md",
    "skills/productivity/carddav-contacts/scripts/carddav_contacts.py",
    "skills/productivity/carddav-contacts/scripts/generations.py",
    "skills/productivity/carddav-contacts/scripts/ids.py",
    "skills/productivity/carddav-contacts/scripts/index.py",
    "skills/productivity/carddav-contacts/scripts/profiles.py",
    "skills/productivity/carddav-contacts/scripts/queries.py",
    "skills/productivity/carddav-contacts/scripts/reads.py",
    "skills/productivity/carddav-contacts/scripts/records.py",
    "skills/productivity/carddav-contacts/scripts/schemas.py",
    "skills/productivity/carddav-contacts/scripts/transport.py",
    "skills/productivity/carddav-contacts/scripts/vcards.py",
    "skills/productivity/carddav-contacts/scripts/writes.py",
    f"hermes_carddav_contacts-{VERSION}.dist-info/METADATA",
    f"hermes_carddav_contacts-{VERSION}.dist-info/RECORD",
    f"hermes_carddav_contacts-{VERSION}.dist-info/WHEEL",
    f"hermes_carddav_contacts-{VERSION}.dist-info/entry_points.txt",
    f"hermes_carddav_contacts-{VERSION}.dist-info/licenses/LICENSE",
}

# `.gitignore` and `PKG-INFO` are added by hatchling itself, outside the
# configured include list; everything else here is declared in pyproject.toml.
SDIST_TOP_LEVEL = {"PKG-INFO", ".gitignore", "LICENSE", "README.md", "pyproject.toml",
                   "uv.lock", "hermes_carddav_contacts", "skills", "tests"}

# A shipped artifact may only name hosts that cannot be a real deployment:
# RFC 6761/2606/3849 reserved names, the loopback interface, or the upstream
# project/index hosts this repository documents and locks against.
UPSTREAM_HOSTS = {
    "vdirsyncer.pimutils.org", "eventable.github.io", "docs.astral.sh",
    "github.com", "opensource.org", "pypi.org", "files.pythonhosted.org",
    "hermes-agent.nousresearch.com",
}
RESERVED_MARKERS = (".invalid", ".test", ".example", "localhost",
                    "127.0.0.1", "::1", "2001:db8:")
PLACEHOLDER_TLDS = ("invalid", "test", "example", "localhost")
PLACEHOLDER_DOMAINS = ("example.com", "example.org", "example.net")
HOSTNAME_PATTERN = re.compile(r"\A[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\Z")
PRIVATE_PATH_MARKERS = (".env", ".credentials", "secrets/", "/profiles/", "/mirror/",
                        ".vcf", ".ics", ".sqlite3", ".log", "__pycache__", ".venv/")
CREDENTIAL_SHAPES = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{36}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bBasic [A-Za-z0-9+/]{24,}={0,2}"),
)
SYNTHETIC_MARKERS = ("synthetic", "example", "fixture", "placeholder", "test")
TEXT_SUFFIXES = {".py", ".md", ".toml", ".txt", ".cfg", ".json", ""}
HOST_PATTERN = re.compile(r"https?://([^/\s\"')`\],]+)")
EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _is_placeholder_domain(domain: str) -> bool:
    if domain.rsplit(".", 1)[-1] in PLACEHOLDER_TLDS:
        return True
    return any(domain == suffix or domain.endswith(f".{suffix}")
               for suffix in PLACEHOLDER_DOMAINS)


def _cannot_be_a_deployment_host(authority: str) -> bool:
    """True when this URL authority can never address a real deployment.

    Accepts a reserved/loopback name, a documented upstream host, or a
    syntactically impossible hostname — several fixtures are deliberately
    malformed URLs (`[gg::1`, `example.test:80:90`) that `setup` must reject.
    """
    host = authority.rsplit("@", 1)[-1].lower()
    if any(marker in host for marker in RESERVED_MARKERS):
        return True
    bare = "" if host.startswith("[") else host.partition(":")[0]
    return bare in UPSTREAM_HOSTS or not HOSTNAME_PATTERN.match(bare)


@dataclass
class _Installed:
    """Paths of one built-and-installed distribution plus its smoke state."""

    workdir: Path
    wheel: Path
    sdist: Path
    venv: Path
    home: Path
    requests: list[tuple[str, str]]
    crud: dict[str, object]
    crud_requests: list[tuple[str, str]]

    @property
    def command(self) -> Path:
        return self.venv / "bin" / CONSOLE_COMMAND

    @property
    def python(self) -> Path:
        return self.venv / "bin" / "python"

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Run the installed console command with no PYTHONPATH and no repo cwd."""
        env = {"HERMES_HOME": str(self.home), "PATH": os.defpath}
        return subprocess.run([str(self.command), *args], env=env, cwd=self.workdir,
                              capture_output=True, text=True, timeout=90, check=False)

    def run_online(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Run the installed console command with the fixture credentials present."""
        env = {"HERMES_HOME": str(self.home), "PATH": os.defpath,
               "CARDDAV_USERNAME": USERNAME, "CARDDAV_PASSWORD": PASSWORD}
        return subprocess.run([str(self.command), *args], env=env, cwd=self.workdir,
                              capture_output=True, text=True, timeout=180, check=False)

    def json_online(self, *args: str) -> dict[str, object]:
        result = self.run_online(*args)
        assert (result.returncode, result.stderr) == (0, ""), result.stderr
        payload = json.loads(result.stdout)
        assert isinstance(payload, dict)
        return payload


def _uv(*args: str, cwd: Path = REPO_ROOT) -> None:
    result = subprocess.run(["uv", *args], cwd=cwd, capture_output=True, text=True,
                            timeout=600, check=False)
    assert result.returncode == 0, f"uv {' '.join(args)} failed:\n{result.stderr}"


@pytest.fixture(scope="session")
def installed(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_Installed]:
    """Build, install into a clean venv outside the checkout, and drive one real sync."""
    override = os.environ.get("CARDDAV_PACKAGING_WORKDIR")
    workdir = Path(override) if override else Path(tempfile.mkdtemp(prefix="carddav-packaging-"))
    if override and workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    assert REPO_ROOT not in workdir.resolve().parents, "install must not live inside the checkout"

    dist = workdir / "dist"
    _uv("build", "--out-dir", str(dist))
    wheel = next(dist.glob("*.whl"))
    sdist = next(dist.glob("*.tar.gz"))

    venv = workdir / "venv"
    _uv("venv", "--python", "3.12", str(venv))
    _uv("pip", "install", "--python", str(venv / "bin" / "python"), str(wheel))

    home = workdir / "hermes-home"
    state = _Installed(workdir, wheel, sdist, venv, home, [], {}, [])
    server_root = workdir / "server-storage"
    if server_root.exists():
        shutil.rmtree(server_root)

    radicale = pytest.importorskip("radicale")
    config = pytest.importorskip("radicale.config")
    settings = config.load([])
    settings.update({
        "auth": {"type": "none"},
        "rights": {"type": "owner_only"},
        "storage": {"filesystem_folder": str(server_root)},
    })
    recorder = _Recorder(radicale.Application(settings))
    with make_server("127.0.0.1", 0, recorder, handler_class=_QuietHandler) as server:
        recorder.url = f"http://127.0.0.1:{server.server_port}"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            recorder.seed("MKCOL", f"/{USERNAME}/contacts/", (
                b'<mkcol xmlns="DAV:"><set><prop><resourcetype><collection/>'
                b'<addressbook xmlns="urn:ietf:params:xml:ns:carddav"/>'
                b"</resourcetype></prop></set></mkcol>"
            ), "application/xml")
            recorder.seed("PUT", f"/{USERNAME}/contacts/one.vcf",
                          _card("smoke-one", "Example Placeholder One"))
            recorder.seed("PUT", f"/{USERNAME}/contacts/two.vcf",
                          _card("smoke-two", "Example Placeholder Two"))

            setup = state.run("setup", "--profile", PROFILE, "--namespace", "example",
                              "--server-url", f"{recorder.url}/{USERNAME}/",
                              "--collection", "contacts")
            assert (setup.returncode, setup.stdout, setup.stderr) == (0, "", "")
            for command in ("discover", "sync"):
                env = {"HERMES_HOME": str(home), "PATH": os.defpath,
                       "CARDDAV_USERNAME": USERNAME, "CARDDAV_PASSWORD": PASSWORD}
                result = subprocess.run([str(state.command), command, "--profile", PROFILE],
                                        env=env, cwd=workdir, capture_output=True,
                                        text=True, timeout=120, check=False)
                assert (result.returncode, result.stdout, result.stderr) == (0, "", ""), result
            # Captured before any write is permitted, so the read-only claim
            # about discover/sync is about exactly those commands.
            state.requests = list(recorder.requests)
            recorder.requests.clear()
            recorder.writes_allowed = True
            state.crud = _drive_installed_crud(state)
            state.crud_requests = list(recorder.requests)
        finally:
            server.shutdown()
            thread.join(timeout=5)
    # Every later local read runs with the address book already offline.
    yield state
    if not override:
        shutil.rmtree(workdir, ignore_errors=True)


_API_SCRIPT = """
import json, sys
from hermes_carddav_contacts import api

changes = {
    "change_schema_version": api.CHANGE_SCHEMA_VERSION,
    "set": {"display": "Api Placeholder"},
    "clear": [],
    "replace": {"emails": [{"value": "api@example.invalid", "types": ["work"],
                            "label": None, "preference": None}]},
}
operation = api.prepare_create(PROFILE, "contacts", changes)
created = api.apply_operation(PROFILE, operation["operation_id"])
fresh = api.read_record(PROFILE, operation["contact_id"])
removal = api.prepare_delete(PROFILE, operation["contact_id"])
deleted = api.apply_operation(PROFILE, removal["operation_id"])
json.dump({"capabilities": api.CAPABILITIES, "created": created["outcome"],
           "display": fresh["contact"]["name"]["display"],
           "deleted": deleted["outcome"]}, sys.stdout)
"""


def _drive_installed_crud(state: _Installed) -> dict[str, object]:
    """Exercise every CRUD verb through the installed command and the installed API."""
    changes = json.dumps({
        "change_schema_version": "carddav-change/1.0",
        "set": {"display": "Created Placeholder", "given": "Created"},
        "clear": [], "replace": {},
    })
    operation = state.json_online(
        "prepare-create", "--profile", PROFILE, "--collection", "contacts",
        "--changes", changes, "--json")
    created = state.json_online(
        "apply", "--profile", PROFILE, "--operation", str(operation["operation_id"]), "--json")
    contact_id = str(operation["contact_id"])

    renamed = json.dumps({
        "change_schema_version": "carddav-change/1.0",
        "set": {"display": "Renamed Placeholder"}, "clear": ["given"], "replace": {},
    })
    update = state.json_online(
        "prepare-update", "--profile", PROFILE, "--id", contact_id,
        "--changes", renamed, "--json")
    updated = state.json_online(
        "apply", "--profile", PROFILE, "--operation", str(update["operation_id"]), "--json")
    record = state.json_online("record", "--profile", PROFILE, "--id", contact_id, "--json")

    # A stale application of the same reviewed operation must fail closed.
    stale = state.run_online(
        "apply", "--profile", PROFILE, "--operation", str(update["operation_id"]), "--json")

    removal = state.json_online(
        "prepare-delete", "--profile", PROFILE, "--id", contact_id, "--json")
    deleted = state.json_online(
        "apply", "--profile", PROFILE, "--operation", str(removal["operation_id"]), "--json")
    gone = state.run_online("record", "--profile", PROFILE, "--id", contact_id, "--json")

    api_result = subprocess.run(
        [str(state.python), "-c", f"PROFILE = {PROFILE!r}\n" + _API_SCRIPT],
        env={"HERMES_HOME": str(state.home), "PATH": os.defpath,
             "CARDDAV_USERNAME": USERNAME, "CARDDAV_PASSWORD": PASSWORD},
        cwd=state.workdir, capture_output=True, text=True, timeout=240, check=False)
    assert api_result.returncode == 0, api_result.stderr

    return {
        "created": created, "updated": updated, "record": record,
        "repeat_apply": (stale.returncode, stale.stdout, stale.stderr),
        "deleted": deleted,
        "gone": (gone.returncode, gone.stdout, gone.stderr),
        "api": json.loads(api_result.stdout),
    }


def _artifact_text(installed: _Installed) -> dict[str, str]:
    """Every text member of both artifacts, keyed by `<artifact>:<member>`."""
    members: dict[str, str] = {}
    with zipfile.ZipFile(installed.wheel) as wheel:
        for name in wheel.namelist():
            if Path(name).suffix in TEXT_SUFFIXES:
                members[f"wheel:{name}"] = wheel.read(name).decode("utf-8", "replace")
    with tarfile.open(installed.sdist) as sdist:
        for member in sdist.getmembers():
            handle = sdist.extractfile(member) if member.isfile() else None
            if handle is not None and Path(member.name).suffix in TEXT_SUFFIXES:
                members[f"sdist:{member.name}"] = handle.read().decode("utf-8", "replace")
    return members


def test_wheel_contains_exactly_the_declared_files(installed: _Installed) -> None:
    with zipfile.ZipFile(installed.wheel) as wheel:
        assert set(wheel.namelist()) == WHEEL_CONTENTS


def test_sdist_contains_exactly_the_declared_top_level_entries(installed: _Installed) -> None:
    with tarfile.open(installed.sdist) as sdist:
        names = [Path(name).relative_to(f"hermes_carddav_contacts-{VERSION}")
                 for name in sdist.getnames()
                 if name != f"hermes_carddav_contacts-{VERSION}"]
    assert {part.parts[0] for part in names} == SDIST_TOP_LEVEL


def test_artifacts_carry_no_runtime_or_private_artifact_paths(installed: _Installed) -> None:
    with zipfile.ZipFile(installed.wheel) as wheel:
        names = list(wheel.namelist())
    with tarfile.open(installed.sdist) as sdist:
        names += sdist.getnames()
    for name in names:
        lowered = name.lower()
        assert not any(marker in lowered for marker in PRIVATE_PATH_MARKERS), name


def test_artifact_text_names_no_deployment_specific_host_or_address(
    installed: _Installed,
) -> None:
    for member, text in _artifact_text(installed).items():
        for authority in HOST_PATTERN.findall(text):
            assert _cannot_be_a_deployment_host(authority), f"{member}: {authority}"
        for address in EMAIL_PATTERN.findall(text):
            domain = address.rsplit("@", 1)[1].rstrip(".,)`\"'").lower()
            assert _is_placeholder_domain(domain), f"{member}: {address}"


def test_artifact_text_has_no_credential_shaped_literal(installed: _Installed) -> None:
    for member, text in _artifact_text(installed).items():
        for pattern in CREDENTIAL_SHAPES:
            assert not pattern.search(text), f"{member}: {pattern.pattern}"
        for line in text.splitlines():
            if re.search(r"(?i)(password|secret|token)\s*[:=]\s*['\"]", line):
                assert any(marker in line.lower() for marker in SYNTHETIC_MARKERS), (
                    f"{member}: {line.strip()}")


def test_wheel_metadata_is_clean_and_licensed(installed: _Installed) -> None:
    with zipfile.ZipFile(installed.wheel) as wheel:
        metadata = wheel.read(f"hermes_carddav_contacts-{VERSION}.dist-info/METADATA").decode()
        entry_points = wheel.read(
            f"hermes_carddav_contacts-{VERSION}.dist-info/entry_points.txt").decode()
        license_text = wheel.read(
            f"hermes_carddav_contacts-{VERSION}.dist-info/licenses/LICENSE").decode()
    assert "Name: hermes-carddav-contacts" in metadata
    assert f"Version: {VERSION}" in metadata
    assert "Requires-Python: >=3.12" in metadata
    assert "Requires-Dist: vdirsyncer==0.21.0" in metadata
    assert "Requires-Dist: vobject==0.9.9" in metadata
    assert "Requires-Dist: khard==0.19.1; extra == 'khard'" in metadata
    assert "Provides-Extra: khard" in metadata
    # Radicale is named only as an example server in the long description;
    # it must never appear as a dependency of the shipped distribution.
    requires = [line for line in metadata.splitlines() if line.startswith("Requires-Dist:")]
    assert not any("radicale" in line.lower() for line in requires)
    assert len(requires) == 3
    assert "MIT" in license_text
    assert "[console_scripts]" in entry_points
    assert f"{CONSOLE_COMMAND} = hermes_carddav_contacts:main" in entry_points


def test_installed_runtime_holds_exactly_the_pinned_dependencies(installed: _Installed) -> None:
    result = subprocess.run(
        [str(installed.python), "-c",
         ("import json;from importlib.metadata import distributions;"
          "print(json.dumps({d.metadata['Name'].lower(): d.version"
          " for d in distributions()}))")],
        capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr
    found = json.loads(result.stdout)
    assert found["vdirsyncer"] == "0.21.0"
    assert found["vobject"] == "0.9.9"
    assert found["hermes-carddav-contacts"] == VERSION
    for excluded in ("radicale", "pytest", "ruff", "mypy", "khard"):
        assert excluded not in found, excluded


def test_installed_console_command_reports_the_version_contract(installed: _Installed) -> None:
    result = installed.run("version", "--json")
    assert (result.returncode, result.stderr) == (0, "")
    assert json.loads(result.stdout) == {
        "command_schema_version": "carddav-command/1.1",
        "command": "version",
        "package_version": VERSION,
        "supported_source_schema_versions": ["carddav-source/1.0"],
        "capabilities": {"read_only": False, "create": True, "update": True, "delete": True},
        "dependency_versions": {"vdirsyncer": "0.21.0", "vobject": "0.9.9"},
    }


def test_installed_command_runs_from_the_installed_tree_not_the_checkout(
    installed: _Installed,
) -> None:
    result = subprocess.run(
        [str(installed.python), "-c",
         ("import hermes_carddav_contacts as launcher, sys;"
          " sys.path.insert(0, str(launcher.SCRIPTS_DIR)); import carddav_contacts;"
          " print(carddav_contacts.__file__)")],
        cwd=installed.workdir, env={"PATH": os.defpath}, capture_output=True,
        text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr
    loaded = Path(result.stdout.strip()).resolve()
    assert installed.venv in loaded.parents
    assert REPO_ROOT not in loaded.parents


def test_installed_transport_reached_the_server_read_only(installed: _Installed) -> None:
    assert installed.requests, "discover/sync must have issued real HTTP requests"
    assert any(method == "REPORT" for method, _ in installed.requests)
    assert all(method in ALLOWED_METHODS for method, _ in installed.requests)


def test_installed_local_queries_answer_offline_from_the_synthetic_generation(
    installed: _Installed,
) -> None:
    status = installed.run("status", "--profile", PROFILE, "--json")
    assert (status.returncode, status.stderr) == (0, "")
    payload = json.loads(status.stdout)
    assert payload["contact_count"] == 2
    generation = payload["current_generation"]
    assert generation

    snapshot = json.loads(installed.run("snapshot", "--profile", PROFILE, "--json").stdout)
    contacts = snapshot["data"]["contacts"]
    assert {contact["name"]["display"] for contact in contacts} == {
        "Example Placeholder One", "Example Placeholder Two"}

    search = json.loads(installed.run("search", "--profile", PROFILE,
                                      "--query", "Placeholder", "--json").stdout)
    assert search["total_matches"] == 2

    show = json.loads(installed.run("show", "--profile", PROFILE,
                                    "--id", contacts[0]["contact_id"], "--json").stdout)
    assert show["contact"] == contacts[0]

    audit = json.loads(installed.run("audit", "--profile", PROFILE, "--json").stdout)
    assert audit["total_candidate_groups"] == 0
    assert audit["generation"] == generation


def test_installed_crud_runs_through_the_console_command(installed: _Installed) -> None:
    crud = installed.crud
    created = crud["created"]
    assert isinstance(created, dict)
    assert (created["outcome"], created["remote_write"]) == ("applied", True)
    assert created["result_schema_version"] == "carddav-result/1.0"

    updated = crud["updated"]
    assert isinstance(updated, dict)
    assert updated["outcome"] == "applied"

    record = crud["record"]
    assert isinstance(record, dict)
    assert record["record_schema_version"] == "carddav-record/1.0"
    contact = record["contact"]
    assert isinstance(contact, dict)
    name = contact["name"]
    assert isinstance(name, dict)
    assert name["display"] == "Renamed Placeholder"
    assert record["revision"]

    # Re-applying a settled operation reports the recorded outcome, never a second write.
    assert crud["repeat_apply"][0] == 0  # type: ignore[index]

    deleted = crud["deleted"]
    assert isinstance(deleted, dict)
    assert deleted["outcome"] == "applied"
    assert crud["gone"][0] == 2  # type: ignore[index]
    assert crud["gone"][2] == "error: contact not found\n"  # type: ignore[index]


def test_installed_crud_used_real_conditional_write_methods(installed: _Installed) -> None:
    methods = {method for method, _ in installed.crud_requests}
    assert {"PUT", "DELETE"} <= methods
    # The read-only phase recorded no mutation method at all.
    assert all(method in ALLOWED_METHODS for method, _ in installed.requests)


def test_installed_crud_runs_through_the_imported_public_api(installed: _Installed) -> None:
    api = installed.crud["api"]
    assert isinstance(api, dict)
    assert api["capabilities"] == {
        "read_only": False, "create": True, "update": True, "delete": True}
    assert api["created"] == "applied"
    assert api["display"] == "Api Placeholder"
    assert api["deleted"] == "applied"


def test_installed_command_keeps_all_runtime_state_outside_the_checkout(
    installed: _Installed,
) -> None:
    profile = installed.home / "carddav-contacts" / "profiles" / PROFILE / "profile.json"
    assert profile.is_file()
    assert REPO_ROOT not in profile.resolve().parents
    assert PASSWORD not in profile.read_text()
    assert not list(REPO_ROOT.glob("carddav-contacts"))


def test_installed_command_refuses_an_unsupported_invocation(installed: _Installed) -> None:
    for args in (["version"], [], ["delete", "--profile", PROFILE]):
        result = installed.run(*args)
        assert (result.returncode, result.stdout) == (2, "")
        assert result.stderr == "error: invalid operation\n"


if __name__ == "__main__":  # pragma: no cover - convenience for manual runs
    sys.exit(pytest.main([__file__, "-m", "packaging", "-q"]))
