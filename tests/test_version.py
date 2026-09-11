"""Contract test for `version --json`: the only invocation implemented in v0.1.

Invokes the real entry-point script as a subprocess (no imports of internal
helpers), so it exercises exactly what an operator would run.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ENTRYPOINT = (
    Path(__file__).parent.parent
    / "skills"
    / "productivity"
    / "carddav-contacts"
    / "scripts"
    / "carddav_contacts.py"
)

EXPECTED_CAPABILITIES: dict[str, bool] = {
    "read_only": False,
    "create": True,
    "update": True,
    "delete": True,
}
EXPECTED_DEPENDENCY_VERSIONS: dict[str, str] = {
    "vdirsyncer": "0.21.0",
    "vobject": "0.9.9",
}
EXPECTED: dict[str, object] = {
    "command_schema_version": "carddav-command/1.1",
    "command": "version",
    "package_version": "0.2.0",
    "supported_source_schema_versions": ["carddav-source/1.0"],
    "capabilities": EXPECTED_CAPABILITIES,
    "dependency_versions": EXPECTED_DEPENDENCY_VERSIONS,
}


def _run(
    args: list[str], cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ENTRYPOINT), *args],
        cwd=cwd,
        env=env if env is not None else {"PATH": ""},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_version_command_contract_and_all_else_unavailable(tmp_path: Path) -> None:
    before = sorted(tmp_path.rglob("*"))

    result = _run(["version", "--json"], tmp_path)

    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout.endswith("\n")
    assert result.stdout.count("\n") == 1

    payload: dict[str, object] = json.loads(result.stdout)
    assert payload == EXPECTED
    assert set(payload.keys()) == set(EXPECTED.keys())

    capabilities = payload["capabilities"]
    assert isinstance(capabilities, dict)
    assert set(capabilities.keys()) == set(EXPECTED_CAPABILITIES.keys())
    for key, value in EXPECTED_CAPABILITIES.items():
        assert capabilities[key] is value

    dependency_versions = payload["dependency_versions"]
    assert isinstance(dependency_versions, dict)
    assert set(dependency_versions.keys()) == set(EXPECTED_DEPENDENCY_VERSIONS.keys())

    assert isinstance(payload["supported_source_schema_versions"], list)

    for other_args in (
        [],
        ["version"],
        ["version", "--bogus"],
        ["sync"],
        ["apply"],
        ["prepare-create"],
        ["search"],
        ["show"],
        ["snapshot"],
        ["audit"],
    ):
        other = _run(other_args, tmp_path)
        assert other.returncode != 0
        assert "Traceback" not in other.stderr

    after = sorted(tmp_path.rglob("*"))
    assert after == before


def test_version_ignores_invalid_hermes_home(tmp_path: Path) -> None:
    before = sorted(tmp_path.rglob("*"))

    result = _run(
        ["version", "--json"],
        tmp_path,
        env={"HERMES_HOME": "relative/not-absolute", "PATH": ""},
    )

    assert result.returncode == 0
    assert result.stderr == ""

    payload: dict[str, object] = json.loads(result.stdout)
    assert payload == EXPECTED
    assert set(payload.keys()) == set(EXPECTED.keys())

    after = sorted(tmp_path.rglob("*"))
    assert after == before
