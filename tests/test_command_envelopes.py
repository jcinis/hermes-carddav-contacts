"""Exact `carddav-command/1.1` envelope shapes for the local read commands."""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

SCHEMAS_PATH = (
    Path(__file__).parent.parent
    / "skills"
    / "productivity"
    / "carddav-contacts"
    / "scripts"
    / "schemas.py"
)


def _schemas() -> ModuleType:
    spec = importlib.util.spec_from_file_location("carddav_command_schemas", SCHEMAS_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _contact(contact_id: str = "0123456789abcdef", display: str = "Example Person") -> dict[str, object]:
    return {
        "contact_id": contact_id,
        "collection_alias": "contacts",
        "name": {
            "display": display,
            "prefix": None,
            "given": "Example",
            "additional": None,
            "family": "Person",
            "suffix": None,
        },
        "aliases": [],
        "organizations": [],
        "titles": [],
        "emails": [
            {
                "value": "person@example.invalid",
                "types": [],
                "label": None,
                "preference": None,
            }
        ],
        "phones": [],
        "addresses": [],
        "birthday": None,
        "urls": [],
        "notes": [],
    }


FRESHNESS: dict[str, object] = {
    "age_seconds": 12,
    "stale_after_seconds": 3600,
    "stale": False,
    "clock_skew": False,
}


def _envelope(command: str) -> dict[str, object]:
    return {
        "command_schema_version": "carddav-command/1.1",
        "command": command,
        "profile": "demo",
        "generation": "a" * 64,
        "profile_generation_sha256": "b" * 64,
        "synced_at": "2026-09-10T00:00:00Z",
        "freshness": copy.deepcopy(FRESHNESS),
        "cache_invalidated": False,
    }


def _status(**overrides: object) -> dict[str, object]:
    payload = {
        "command_schema_version": "carddav-command/1.1",
        "command": "status",
        "profile": "demo",
        "current_generation": "a" * 64,
        "profile_generation_sha256": "b" * 64,
        "contact_count": 2,
        "synced_at": "2026-09-10T00:00:00Z",
        "freshness": copy.deepcopy(FRESHNESS),
        "cache_invalidated": False,
    }
    payload.update(overrides)
    return payload


def _show(**overrides: object) -> dict[str, object]:
    payload = _envelope("show")
    payload["contact"] = _contact()
    payload.update(overrides)
    return payload


def _search(**overrides: object) -> dict[str, object]:
    payload = _envelope("search")
    payload.update(
        {"query": "example", "total_matches": 1, "truncated": False, "results": [_contact()]}
    )
    payload.update(overrides)
    return payload


def _audit(**overrides: object) -> dict[str, object]:
    payload = _envelope("audit")
    payload.update(
        {
            "total_candidate_groups": 1,
            "truncated": False,
            "candidates": [
                {
                    "reason": "email",
                    "key": "person@example.invalid",
                    "members": [
                        {"contact_id": "0123456789abcdef", "values": ["Person@example.invalid"]},
                        {"contact_id": "fedcba9876543210", "values": ["person@example.invalid"]},
                    ],
                }
            ],
        }
    )
    payload.update(overrides)
    return payload


def test_valid_read_envelopes_are_accepted() -> None:
    schemas = _schemas()
    for payload in (_status(), _show(), _search(), _audit()):
        schemas.validate_command(payload)


def test_status_without_a_generation_reports_every_derived_field_as_null() -> None:
    schemas = _schemas()
    schemas.validate_command(
        _status(current_generation=None, contact_count=None, synced_at=None, freshness=None)
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"current_generation": None},  # inconsistent with a non-null count
        {"contact_count": None},
        {"synced_at": None},
        {"freshness": None},
        {"contact_count": True},
        {"contact_count": -1},
        {"contact_count": "2"},
        {"current_generation": "A" * 64},
        {"profile_generation_sha256": "b" * 63},
        {"profile": "Demo"},
        {"synced_at": "2026-09-10T00:00:60Z"},
        {"extra": 1},
    ],
)
def test_malformed_status_envelopes_are_refused(overrides: dict[str, object]) -> None:
    schemas = _schemas()
    with pytest.raises(ValueError, match="^invalid command$"):
        schemas.validate_command(_status(**overrides))


@pytest.mark.parametrize(
    "overrides",
    [
        {"contact": None},
        {"contact": {}},
        {"extra": 1},
        {"freshness": {"age_seconds": 1, "stale_after_seconds": 3600, "stale": False}},
        {"generation": "a" * 63},
    ],
)
def test_malformed_show_envelopes_are_refused(overrides: dict[str, object]) -> None:
    schemas = _schemas()
    with pytest.raises(ValueError, match="^invalid command$"):
        schemas.validate_command(_show(**overrides))


@pytest.mark.parametrize(
    "overrides",
    [
        {"total_matches": 2},  # disagrees with the returned results
        {"total_matches": True},
        {"truncated": True},  # v0.1 never truncates
        {"query": ""},
        {"query": "bad\x00query"},
        {"query": "a" * 513},
        {"query": 7},
        {"results": [_contact(), _contact()]},  # duplicate contact IDs
        {"results": {}},
        {"extra": 1},
    ],
)
def test_malformed_search_envelopes_are_refused(overrides: dict[str, object]) -> None:
    schemas = _schemas()
    with pytest.raises(ValueError, match="^invalid command$"):
        schemas.validate_command(_search(**overrides))


@pytest.mark.parametrize(
    "overrides",
    [
        {"total_candidate_groups": 2},
        {"truncated": True},
        {"candidates": [{"reason": "address", "key": "x", "members": []}]},
        {
            "candidates": [
                {
                    "reason": "email",
                    "key": "person@example.invalid",
                    "members": [
                        {"contact_id": "0123456789abcdef", "values": ["only one member"]}
                    ],
                }
            ]
        },
        {
            "candidates": [
                {
                    "reason": "email",
                    "key": "",
                    "members": [
                        {"contact_id": "0123456789abcdef", "values": ["a"]},
                        {"contact_id": "fedcba9876543210", "values": ["b"]},
                    ],
                }
            ]
        },
        {
            "candidates": [
                {
                    "reason": "email",
                    "key": "person@example.invalid",
                    "members": [
                        {"contact_id": "fedcba9876543210", "values": ["b"]},
                        {"contact_id": "0123456789abcdef", "values": ["a"]},
                    ],
                }
            ]
        },  # members must be sorted by contact ID
        {
            "candidates": [
                {
                    "reason": "email",
                    "key": "person@example.invalid",
                    "members": [
                        {"contact_id": "0123456789abcdef", "values": []},
                        {"contact_id": "fedcba9876543210", "values": ["b"]},
                    ],
                }
            ]
        },
        {"extra": 1},
    ],
)
def test_malformed_audit_envelopes_are_refused(overrides: dict[str, object]) -> None:
    schemas = _schemas()
    with pytest.raises(ValueError, match="^invalid command$"):
        schemas.validate_command(_audit(**overrides))
