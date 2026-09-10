# ABOUTME: Console-script launcher that delegates to the skill's own entry-point script.
# ABOUTME: Locates the shipped skill tree beside this package, so it works installed or from a checkout.

"""Installed launcher for the `hermes-carddav-contacts` console command.

The skill's command surface lives in `skills/productivity/carddav-contacts/`,
whose directory name is not a valid Python identifier, so it cannot be imported
as a package. Both the wheel and this checkout place that tree next to this
package, so the scripts directory is resolved relative to this file and added
to `sys.path` exactly as running the script directly already does. Nothing
about the command surface is reimplemented here, and the same script stays
runnable standalone.
"""

from __future__ import annotations

import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent / "skills" / "productivity" / "carddav-contacts"
SCRIPTS_DIR = SKILL_DIR / "scripts"

__all__ = ["SCRIPTS_DIR", "SKILL_DIR", "main"]


def main(argv: list[str] | None = None) -> int:
    """Run the skill's entry point; returns its exit code."""
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    import carddav_contacts

    return carddav_contacts.main(argv)
