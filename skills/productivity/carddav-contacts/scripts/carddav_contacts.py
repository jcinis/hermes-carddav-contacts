"""CardDAV contacts skill entry point.

Only `version --json` is implemented. It reports command/source schema
versions, this package's version, capabilities, and pinned runtime
dependency versions as a single JSON object, with no filesystem, network, or
subprocess access. `version` without `--json`, no arguments, and every
other command (sync, search, show, snapshot, audit, ...) are not yet
implemented; see SKILL.md and references/ for the planned command and
data-model contracts.
"""

from __future__ import annotations

import json
import sys

COMMAND_SCHEMA_VERSION = "carddav-command/1.0"
PACKAGE_VERSION = "0.1.0"
SUPPORTED_SOURCE_SCHEMA_VERSIONS = ["carddav-source/1.0"]
CAPABILITIES = {
    "read_only": True,
    "create_update": False,
    "cleanup_delete": False,
}
DEPENDENCY_VERSIONS = {
    "vdirsyncer": "0.21.0",
    "vobject": "0.9.9",
}


def _version() -> dict[str, object]:
    return {
        "command_schema_version": COMMAND_SCHEMA_VERSION,
        "command": "version",
        "package_version": PACKAGE_VERSION,
        "supported_source_schema_versions": SUPPORTED_SOURCE_SCHEMA_VERSIONS,
        "capabilities": CAPABILITIES,
        "dependency_versions": DEPENDENCY_VERSIONS,
    }


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args == ["version", "--json"]:
        print(json.dumps(_version()))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
