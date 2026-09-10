# ABOUTME: Asserts the real installed vdirsyncer/vobject match the pins the CLI reports.
# ABOUTME: khard is an optional diagnostics extra, so its absence is never a failure.

"""Installed-tool version regression.

`version --json` reports `dependency_versions` as literals. These tests prove
those literals match the distributions actually resolved in this environment,
so a drifted lockfile fails here rather than silently misreporting.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

import pytest

PINNED_RUNTIME_TOOLS = {"vdirsyncer": "0.21.0", "vobject": "0.9.9"}
OPTIONAL_TOOLS = {"khard": "0.19.1"}


@pytest.mark.parametrize(("distribution", "pin"), sorted(PINNED_RUNTIME_TOOLS.items()))
def test_runtime_tool_matches_its_pin(distribution: str, pin: str) -> None:
    assert version(distribution) == pin


def test_reported_dependency_versions_match_the_installed_distributions() -> None:
    from tests.test_version import EXPECTED_DEPENDENCY_VERSIONS

    assert EXPECTED_DEPENDENCY_VERSIONS == PINNED_RUNTIME_TOOLS
    for distribution, pin in EXPECTED_DEPENDENCY_VERSIONS.items():
        assert version(distribution) == pin


@pytest.mark.parametrize(("distribution", "pin"), sorted(OPTIONAL_TOOLS.items()))
def test_optional_tool_is_absent_or_pinned(distribution: str, pin: str) -> None:
    """khard is a manual diagnostics extra; no command imports or shells out to it."""
    try:
        installed = version(distribution)
    except PackageNotFoundError:
        pytest.skip(f"{distribution} extra is not installed")
    assert installed == pin
