# ABOUTME: Static packaging contracts — console entry point, shipped data files, and skill metadata.
# ABOUTME: Build-free; the real build/install proof lives in tests/test_install_smoke.py (`packaging` marker).

"""Packaging contracts that need no build.

These assert the declared distribution shape: one console command
(`hermes-carddav-contacts`) delegating to the same script an operator can also
run directly, explicit wheel/sdist inclusion boundaries, exactly the two pinned
runtime dependencies, and honest skill frontmatter.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
SKILL_MD = REPO_ROOT / "skills/productivity/carddav-contacts/SKILL.md"
LAUNCHER = REPO_ROOT / "hermes_carddav_contacts/__init__.py"

CONSOLE_COMMAND = "hermes-carddav-contacts"
RUNTIME_DEPENDENCIES = ["vdirsyncer==0.21.0", "vobject==0.9.9"]


def _pyproject() -> dict[str, object]:
    return tomllib.loads(PYPROJECT.read_text())


def _frontmatter() -> dict[str, str]:
    """Parse the leading `---` block as flat `key: value` lines (no nested keys)."""
    lines = SKILL_MD.read_text().splitlines()
    assert lines[0] == "---"
    fields: dict[str, str] = {}
    for line in lines[1:]:
        if line == "---":
            return fields
        if line.startswith((" ", "\t")) or ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    raise AssertionError("unterminated frontmatter")


def test_pyproject_declares_the_console_entry_point() -> None:
    project = _pyproject()["project"]
    assert isinstance(project, dict)
    scripts = project.get("scripts")
    assert scripts == {CONSOLE_COMMAND: "hermes_carddav_contacts:main"}


def test_launcher_delegates_instead_of_reimplementing_the_command_surface() -> None:
    source = LAUNCHER.read_text()
    assert "import carddav_contacts" in source
    for duplicated in ("argparse", "COMMAND_SCHEMA_VERSION", "json.dumps", "--profile"):
        assert duplicated not in source, f"launcher must not reimplement {duplicated}"


def test_launcher_runs_the_real_command_surface_from_an_unrelated_directory(
    tmp_path: Path,
) -> None:
    """Import by module name only: no PYTHONPATH, no cwd inside the checkout."""
    result = subprocess.run(
        [sys.executable, "-c",
         ("import hermes_carddav_contacts as launcher, sys;"
          " sys.exit(launcher.main(['version', '--json']))")],
        cwd=tmp_path, env={"PATH": "", "PYTHONPATH": str(REPO_ROOT)},
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert (result.returncode, result.stderr) == (0, "")
    assert json.loads(result.stdout)["command"] == "version"


def test_launcher_locates_the_shipped_skill_data_files() -> None:
    import hermes_carddav_contacts as launcher

    skill_dir = launcher.SKILL_DIR
    assert (skill_dir / "SKILL.md").is_file()
    assert (skill_dir / "references/configuration.md").is_file()
    assert (skill_dir / "references/data-model.md").is_file()
    assert (skill_dir / "scripts/carddav_contacts.py").is_file()
    assert launcher.SCRIPTS_DIR == skill_dir / "scripts"


def test_runtime_dependencies_are_exactly_the_two_pins() -> None:
    project = _pyproject()["project"]
    assert isinstance(project, dict)
    assert project["dependencies"] == RUNTIME_DEPENDENCIES
    assert set(project["optional-dependencies"]) == {"khard"}


def test_artifact_inclusion_boundaries_are_declared_explicitly() -> None:
    build = _pyproject()["tool"]
    assert isinstance(build, dict)
    targets = build["hatch"]["build"]["targets"]
    assert targets["wheel"]["packages"] == ["hermes_carddav_contacts", "skills"]
    assert targets["sdist"]["include"] == [
        "/LICENSE", "/README.md", "/pyproject.toml", "/uv.lock",
        "/hermes_carddav_contacts", "/skills", "/tests",
    ]


def test_skill_frontmatter_is_honest_and_within_limits() -> None:
    fields = _frontmatter()
    assert fields["name"] == "carddav-contacts"
    assert len(fields["name"]) <= 60
    assert len(fields["description"]) <= 60
    assert fields["description"].endswith(".")
    assert fields["version"] == "0.2.0"
    assert fields["author"].split(",")[0].strip().startswith("jcinis")
    assert fields["license"] == "MIT"
    assert fields["platforms"] == "[linux, macos]"


def test_skill_documents_both_the_installed_and_source_invocations() -> None:
    text = " ".join(SKILL_MD.read_text().split())
    assert f"{CONSOLE_COMMAND} version --json" in text
    assert "scripts/carddav_contacts.py version --json" in text
    # Honest platform claim: macOS is supported by design but unverified here.
    assert "only Linux is verified" in text


def test_documentation_declares_the_write_boundary_and_external_runtime_state() -> None:
    """Writes exist now, so the docs must say exactly where the boundary is."""
    for path in (SKILL_MD, REPO_ROOT / "README.md"):
        text = path.read_text()
        assert "$HERMES_HOME" in text
        # Ordinary reads are still offline and routine sync is still read-only.
        assert "read-only" in text
        for promised in ("prepare-create", "prepare-update", "prepare-delete", "apply"):
            assert promised in text, f"{path.name}: {promised}"


def test_the_public_api_module_is_declared_and_documented() -> None:
    api_source = (REPO_ROOT / "hermes_carddav_contacts" / "api.py").read_text()
    assert "import writes" in api_source
    assert "__all__" in api_source
    # The consumer-facing API must be named where a consumer will look for it.
    for path in (SKILL_MD, REPO_ROOT / "README.md"):
        assert "hermes_carddav_contacts import api" in path.read_text(), path.name


def test_no_test_module_still_defers_its_subject_to_a_later_task() -> None:
    deferral = "land in a " + "later task"
    for path in sorted((REPO_ROOT / "tests").glob("*.py")):
        if path == Path(__file__):
            continue
        assert deferral not in path.read_text(), path.name
