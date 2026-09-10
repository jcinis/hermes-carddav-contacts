"""Cross-module proof: decoded UID -> source -> validated snapshot envelope."""

import copy
import runpy
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parents[1] / "skills/productivity/carddav-contacts/scripts"


def test_identity_source_and_envelope_roundtrip() -> None:
    ids = runpy.run_path(str(SCRIPTS / "ids.py"))
    schema = runpy.run_path(str(SCRIPTS / "schemas.py"))
    opaque_id = ids["contact_id"]("example-uid")
    source = {
        "schema_version": "carddav-source/1.0",
        "account_namespace": "example",
        "collections": ["contacts"],
        "contacts": [{
            "contact_id": opaque_id,
            "collection_alias": "contacts",
            "name": {
                "display": "Example Person", "prefix": None, "given": "Example",
                "additional": None, "family": "Person", "suffix": None,
            },
            "aliases": [], "organizations": [], "titles": [], "emails": [],
            "phones": [], "addresses": [], "birthday": None, "urls": [],
            "notes": ["synthetic note\r\nwith Unicode é"],
        }],
    }
    before = copy.deepcopy(source)
    raw = schema["canonical_source"](source)
    parsed = schema["parse_json"](raw)
    generation = schema["source_generation"](parsed)
    assert schema["canonical_source"](parsed) == raw
    assert generation == schema["source_generation"](source)
    assert source == before
    assert b"example-uid" not in raw
    assert ids["contact_ref"]("example", opaque_id) == "carddav:example:b687b2e8ceca7c40"

    envelope = {
        "command_schema_version": "carddav-command/1.0", "command": "snapshot",
        "profile": "demo", "generation": generation,
        "profile_generation_sha256": "a" * 64,
        "synced_at": "2026-01-01T00:00:00Z",
        "freshness": {"age_seconds": 0, "stale_after_seconds": 3600,
                      "stale": False, "clock_skew": False},
        "data": parsed,
    }
    schema["validate_command"](envelope)
    # Envelope metadata isn't part of the source-generation digest.
    envelope["profile"] = "other-local-name"
    schema["validate_command"](envelope)
    # A contact edit without an accompanying generation change is refused.
    parsed["contacts"][0]["notes"].append("changed")
    with pytest.raises(ValueError, match="^invalid command$"):
        schema["validate_command"](envelope)
