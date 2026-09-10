"""Pure source and command schema contract tests."""

from __future__ import annotations

import copy
import importlib.util
import subprocess
import sys
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


def _load_schemas() -> ModuleType:
    spec = importlib.util.spec_from_file_location("carddav_schemas", SCHEMAS_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source() -> dict[str, object]:
    return {
        "schema_version": "carddav-source/1.0",
        "account_namespace": "example",
        "collections": ["work", "home"],
        "contacts": [
            {
                "contact_id": "0123456789abcdef",
                "collection_alias": "home",
                "name": {
                    "display": "Example Person",
                    "prefix": None,
                    "given": "Example",
                    "additional": None,
                    "family": "Person",
                    "suffix": None,
                },
                "aliases": ["Zed", "Ada"],
                "organizations": [["Example Org", "Team"]],
                "titles": ["Engineer"],
                "emails": [
                    {
                        "value": "person@example.invalid",
                        "types": ["work", "internet"],
                        "label": None,
                        "preference": 1,
                    }
                ],
                "phones": [],
                "addresses": [
                    {
                        "po_box": None,
                        "extended": None,
                        "street": "1 Main St",
                        "locality": "Exampleville",
                        "region": None,
                        "postal_code": "00000",
                        "country": None,
                        "types": ["work"],
                        "label": None,
                        "preference": None,
                    }
                ],
                "birthday": {"value": "2000-01-02", "kind": "date"},
                "urls": [],
                "notes": ["A note"],
            }
        ],
    }


def test_parse_json_rejects_duplicate_keys_and_nonfinite_numbers() -> None:
    schemas = _load_schemas()

    assert schemas.parse_json('{"a": 1}') == {"a": 1}
    for raw in (
        '{"a": 1, "a": 2}',
        '{"a": NaN}',
        '{"a": Infinity}',
        '{"a": 1e999}',
    ):
        with pytest.raises(ValueError, match="^invalid JSON$"):
            schemas.parse_json(raw)


def test_canonical_source_sorts_without_mutating_and_hashes_canonical_bytes() -> None:
    schemas = _load_schemas()
    source = _source()
    before = copy.deepcopy(source)

    canonical = schemas.canonical_source(source)

    expected = (
        b'{"account_namespace":"example","collections":["home","work"],'
        b'"contacts":[{"addresses":[{"country":null,"extended":null,"label":null,'
        b'"locality":"Exampleville","po_box":null,"postal_code":"00000",'
        b'"preference":null,"region":null,"street":"1 Main St","types":["work"]}],'
        b'"aliases":["Ada","Zed"],"birthday":{"kind":"date","value":"2000-01-02"},'
        b'"collection_alias":"home","contact_id":"0123456789abcdef",'
        b'"emails":[{"label":null,"preference":1,"types":["internet","work"],'
        b'"value":"person@example.invalid"}],"name":{"additional":null,"display":'
        b'"Example Person","family":"Person","given":"Example","prefix":null,'
        b'"suffix":null},"notes":["A note"],"organizations":[["Example Org","Team"]],'
        b'"phones":[],"titles":["Engineer"],"urls":[]}],'
        b'"schema_version":"carddav-source/1.0"}'
    )
    assert canonical == expected
    assert source == before
    assert schemas.source_generation(source) == (
        "1de5915a1b2f1abf3a1f9bdc9d6fd7183b97047b4fcd1c504523d99d29f2b80f"
    )


def test_validators_do_not_open_files(monkeypatch: pytest.MonkeyPatch) -> None:
    schemas = _load_schemas()

    def fail_open(*args: object, **kwargs: object) -> object:
        raise AssertionError("validator attempted file I/O")

    monkeypatch.setattr("builtins.open", fail_open)
    schemas.validate_source(_source())
    schemas.canonical_source(_source())
    schemas.source_generation(_source())
    assert schemas.parse_json('{"value": "pure"}') == {"value": "pure"}


def test_source_validator_is_closed_and_enforces_membership_and_uniqueness() -> None:
    schemas = _load_schemas()
    source = _source()

    schemas.validate_source(source)
    for mutation in (
        {**source, "extra": True},
        {**source, "schema_version": "carddav-source/1.1"},
        {**source, "account_namespace": "Bad"},
        {**source, "collections": ["home", "home"]},
    ):
        with pytest.raises(ValueError, match="^invalid source$"):
            schemas.validate_source(mutation)

    unknown_collection = copy.deepcopy(source)
    unknown_collection["contacts"][0]["collection_alias"] = "other"  # type: ignore[index]
    with pytest.raises(ValueError, match="^invalid source$"):
        schemas.validate_source(unknown_collection)

    duplicate_contact = copy.deepcopy(source)
    contacts = duplicate_contact["contacts"]
    assert isinstance(contacts, list)
    contacts.append(copy.deepcopy(contacts[0]))
    with pytest.raises(ValueError, match="^invalid source$"):
        schemas.validate_source(duplicate_contact)


def test_source_validator_rejects_wrong_recursive_shapes_and_strict_scalars() -> None:
    schemas = _load_schemas()
    cases: list[dict[str, object]] = []

    for key in ("aliases", "organizations", "titles", "emails", "phones", "addresses", "urls", "notes"):
        invalid = copy.deepcopy(_source())
        invalid["contacts"][0][key] = {}  # type: ignore[index]
        cases.append(invalid)

    invalid_preference = copy.deepcopy(_source())
    invalid_preference["contacts"][0]["emails"][0]["preference"] = True  # type: ignore[index]
    cases.append(invalid_preference)

    invalid_types = copy.deepcopy(_source())
    invalid_types["contacts"][0]["emails"][0]["types"] = ["Work"]  # type: ignore[index]
    cases.append(invalid_types)

    duplicate_types = copy.deepcopy(_source())
    duplicate_types["contacts"][0]["emails"][0]["types"] = ["work", "work"]  # type: ignore[index]
    cases.append(duplicate_types)

    invalid_address = copy.deepcopy(_source())
    invalid_address["contacts"][0]["addresses"][0] = {  # type: ignore[index]
        "po_box": None,
        "extended": None,
        "street": None,
        "locality": None,
        "region": None,
        "postal_code": None,
        "country": None,
        "types": [],
        "label": None,
        "preference": None,
    }
    cases.append(invalid_address)

    invalid_birthday = copy.deepcopy(_source())
    invalid_birthday["contacts"][0]["birthday"] = {"value": "2000", "kind": "unknown"}  # type: ignore[index]
    cases.append(invalid_birthday)

    for case in cases:
        with pytest.raises(ValueError, match="^invalid source$"):
            schemas.validate_source(case)


def test_source_strings_normalize_line_breaks_and_reject_controls_and_surrogates() -> None:
    schemas = _load_schemas()
    source = _source()
    source["contacts"][0]["name"]["display"] = "A\r\nB\rC"  # type: ignore[index]
    source["contacts"][0]["notes"] = ["tab\tallowed"]  # type: ignore[index]
    canonical = schemas.canonical_source(source)
    assert b'"display":"A\\nB\\nC"' in canonical
    assert b"tab\\tallowed" in canonical

    for bad in ("NUL\x00", "control\x01", "surrogate\ud800"):
        invalid = _source()
        invalid["contacts"][0]["name"]["display"] = bad  # type: ignore[index]
        with pytest.raises(ValueError, match="^invalid source$"):
            schemas.validate_source(invalid)


def test_version_and_snapshot_command_envelopes_are_exact_and_typed() -> None:
    schemas = _load_schemas()
    version = {
        "command_schema_version": "carddav-command/1.0",
        "command": "version",
        "package_version": "0.1.0",
        "supported_source_schema_versions": ["carddav-source/1.0"],
        "capabilities": {"read_only": True, "create_update": False, "cleanup_delete": False},
        "dependency_versions": {"vdirsyncer": "0.21.0", "vobject": "0.9.9"},
    }
    schemas.validate_command(version)

    snapshot_data = _source()
    snapshot = {
        "command_schema_version": "carddav-command/1.0",
        "command": "snapshot",
        "profile": "demo",
        "generation": schemas.source_generation(snapshot_data),
        "profile_generation_sha256": "a" * 64,
        "synced_at": "2026-09-09T12:34:56Z",
        "freshness": {
            "age_seconds": 0,
            "stale_after_seconds": 3600,
            "stale": False,
            "clock_skew": False,
        },
        "data": snapshot_data,
    }
    schemas.validate_command(snapshot)

    invalid_version = copy.deepcopy(version)
    invalid_version["capabilities"]["read_only"] = 1  # type: ignore[index]
    with pytest.raises(ValueError, match="^invalid command$"):
        schemas.validate_command(invalid_version)

    invalid_snapshot = copy.deepcopy(snapshot)
    invalid_snapshot["generation"] = "b" * 64
    with pytest.raises(ValueError, match="^invalid command$"):
        schemas.validate_command(invalid_snapshot)


def test_cli_version_output_is_accepted_by_command_validator() -> None:
    schemas = _load_schemas()
    entrypoint = SCHEMAS_PATH.parent / "carddav_contacts.py"
    result = subprocess.run(
        [sys.executable, str(entrypoint), "version", "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stderr == ""
    schemas.validate_command(schemas.parse_json(result.stdout))


def test_snapshot_requires_generation_digest_and_strict_freshness() -> None:
    schemas = _load_schemas()
    data = _source()
    snapshot: dict[str, object] = {
        "command_schema_version": "carddav-command/1.0",
        "command": "snapshot",
        "profile": "demo",
        "generation": schemas.source_generation(data),
        "profile_generation_sha256": "a" * 64,
        "synced_at": "2026-09-09T12:34:56Z",
        "freshness": {
            "age_seconds": 1,
            "stale_after_seconds": 2,
            "stale": False,
            "clock_skew": False,
        },
        "data": data,
    }
    for key, bad_value in (
        ("freshness", {"age_seconds": True, "stale_after_seconds": 2, "stale": False, "clock_skew": False}),
        ("freshness", {"age_seconds": 1, "stale_after_seconds": 0, "stale": False, "clock_skew": False}),
        ("synced_at", "2026-02-30T12:34:56Z"),
        ("profile_generation_sha256", "A" * 64),
        ("command", "future-command"),
    ):
        invalid = copy.deepcopy(snapshot)
        if key == "freshness":
            invalid[key] = bad_value
        else:
            invalid[key] = bad_value
        with pytest.raises(ValueError, match="^invalid command$"):
            schemas.validate_command(invalid)
