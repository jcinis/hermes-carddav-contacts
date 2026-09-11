"""Pure validators and canonicalization for CardDAV source data."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import UTC, datetime
from typing import NoReturn, cast

_COMMAND_SCHEMA_VERSION = "carddav-command/1.0"
_SOURCE_SCHEMA_VERSION = "carddav-source/1.0"
_IDENTIFIER_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
_CONTACT_ID_RE = re.compile(r"[0-9a-f]{16}\Z")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_TYPE_RE = re.compile(r"[a-z0-9-]+\Z")
_TIMESTAMP_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")

_INVALID_JSON = "invalid JSON"
_INVALID_SOURCE = "invalid source"
_INVALID_COMMAND = "invalid command"

_SOURCE_KEYS = ("schema_version", "account_namespace", "collections", "contacts")
_CONTACT_KEYS = (
    "contact_id",
    "collection_alias",
    "name",
    "aliases",
    "organizations",
    "titles",
    "emails",
    "phones",
    "addresses",
    "birthday",
    "urls",
    "notes",
)
_NAME_KEYS = ("display", "prefix", "given", "additional", "family", "suffix")
_REPEATED_KEYS = ("value", "types", "label", "preference")
_ADDRESS_KEYS = (
    "po_box",
    "extended",
    "street",
    "locality",
    "region",
    "postal_code",
    "country",
    "types",
    "label",
    "preference",
)
_BIRTHDAY_KEYS = ("value", "kind")


def _raise(message: str) -> NoReturn:
    raise ValueError(message)


def _object(value: object, message: str) -> dict[str, object]:
    if type(value) is not dict:
        _raise(message)
    result = cast(dict[object, object], value)
    if not all(type(key) is str for key in result):
        _raise(message)
    return cast(dict[str, object], result)


def _exact_keys(value: dict[str, object], keys: tuple[str, ...], message: str) -> None:
    if set(value) != set(keys):
        _raise(message)


def _normalise_string(value: object, message: str) -> str:
    if type(value) is not str:
        _raise(message)
    text = value
    for character in text:
        codepoint = ord(character)
        if codepoint < 0x20 and codepoint not in (9, 10, 13):
            _raise(message)
        if 0xD800 <= codepoint <= 0xDFFF:
            _raise(message)
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _nonempty_string(value: object, message: str) -> str:
    text = _normalise_string(value, message)
    if not text:
        _raise(message)
    return text


def _nullable_string(value: object, message: str) -> str | None:
    if value is None:
        return None
    return _normalise_string(value, message)


def _array(value: object, message: str) -> list[object]:
    if type(value) is not list:
        _raise(message)
    return cast(list[object], value)


def _text_key(value: str) -> tuple[str, str]:
    folded = value.translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"))
    return folded, value


def _nullable_text_key(value: str | None) -> tuple[int, str, str]:
    if value is None:
        return 1, "", ""
    folded, exact = _text_key(value)
    return 0, folded, exact


def _identifier(value: object) -> str:
    text = _nonempty_string(value, _INVALID_SOURCE)
    if _IDENTIFIER_RE.fullmatch(text) is None:
        _raise(_INVALID_SOURCE)
    return text


def _types(value: object) -> list[str]:
    values = _array(value, _INVALID_SOURCE)
    result: list[str] = []
    for item in values:
        text = _normalise_string(item, _INVALID_SOURCE)
        if _TYPE_RE.fullmatch(text) is None:
            _raise(_INVALID_SOURCE)
        result.append(text)
    if len(set(result)) != len(result):
        _raise(_INVALID_SOURCE)
    return sorted(result, key=_text_key)


def _preference(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value <= 0:
        _raise(_INVALID_SOURCE)
    return value


def _repeated_object(value: object) -> dict[str, object]:
    item = _object(value, _INVALID_SOURCE)
    _exact_keys(item, _REPEATED_KEYS, _INVALID_SOURCE)
    result: dict[str, object] = {
        "value": _nonempty_string(item["value"], _INVALID_SOURCE),
        "types": _types(item["types"]),
        "label": _nullable_string(item["label"], _INVALID_SOURCE),
        "preference": _preference(item["preference"]),
    }
    return result


def _repeated_sort_key(item: dict[str, object]) -> tuple[object, ...]:
    preference = cast(int | None, item["preference"])
    label = cast(str | None, item["label"])
    types = tuple(_text_key(cast(str, value)) for value in cast(list[object], item["types"]))
    return (
        1 if preference is None else 0,
        0 if preference is None else preference,
        types,
        _nullable_text_key(label),
        _text_key(cast(str, item["value"])),
        _compact_bytes(item),
    )


def _address(value: object) -> dict[str, object]:
    item = _object(value, _INVALID_SOURCE)
    _exact_keys(item, _ADDRESS_KEYS, _INVALID_SOURCE)
    component_names = _ADDRESS_KEYS[:7]
    components: dict[str, object] = {}
    has_value = False
    for name in component_names:
        component = _nullable_string(item[name], _INVALID_SOURCE)
        if component is not None:
            has_value = True
        components[name] = component
    if not has_value:
        _raise(_INVALID_SOURCE)
    components["types"] = _types(item["types"])
    components["label"] = _nullable_string(item["label"], _INVALID_SOURCE)
    components["preference"] = _preference(item["preference"])
    return components


def _address_sort_key(item: dict[str, object]) -> tuple[object, ...]:
    preference = cast(int | None, item["preference"])
    label = cast(str | None, item["label"])
    types = tuple(_text_key(cast(str, value)) for value in cast(list[object], item["types"]))
    components = tuple(
        _text_key(cast(str, item[name]) or "")
        for name in _ADDRESS_KEYS[:7]
    )
    return (
        1 if preference is None else 0,
        0 if preference is None else preference,
        types,
        _nullable_text_key(label),
        *components,
        _compact_bytes(item),
    )


def _name(value: object) -> dict[str, object]:
    item = _object(value, _INVALID_SOURCE)
    _exact_keys(item, _NAME_KEYS, _INVALID_SOURCE)
    result: dict[str, object] = {
        "display": _nonempty_string(item["display"], _INVALID_SOURCE),
    }
    for key in _NAME_KEYS[1:]:
        result[key] = _nullable_string(item[key], _INVALID_SOURCE)
    return result


def _organization(value: object) -> list[str]:
    components = _array(value, _INVALID_SOURCE)
    if not components:
        _raise(_INVALID_SOURCE)
    return [_normalise_string(component, _INVALID_SOURCE) for component in components]


def _birthday(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    item = _object(value, _INVALID_SOURCE)
    _exact_keys(item, _BIRTHDAY_KEYS, _INVALID_SOURCE)
    birthday_kind = _nonempty_string(item["kind"], _INVALID_SOURCE)
    if birthday_kind not in {"date", "partial-date", "text"}:
        _raise(_INVALID_SOURCE)
    return {
        "value": _nonempty_string(item["value"], _INVALID_SOURCE),
        "kind": birthday_kind,
    }


def _contact(value: object, aliases: set[str]) -> dict[str, object]:
    item = _object(value, _INVALID_SOURCE)
    _exact_keys(item, _CONTACT_KEYS, _INVALID_SOURCE)
    contact_id = _normalise_string(item["contact_id"], _INVALID_SOURCE)
    if _CONTACT_ID_RE.fullmatch(contact_id) is None:
        _raise(_INVALID_SOURCE)
    collection_alias = _normalise_string(item["collection_alias"], _INVALID_SOURCE)
    if collection_alias not in aliases:
        _raise(_INVALID_SOURCE)

    name = _name(item["name"])
    alias_values = [
        _normalise_string(value, _INVALID_SOURCE)
        for value in _array(item["aliases"], _INVALID_SOURCE)
    ]
    title_values = [
        _normalise_string(value, _INVALID_SOURCE)
        for value in _array(item["titles"], _INVALID_SOURCE)
    ]
    note_values = [
        _normalise_string(value, _INVALID_SOURCE)
        for value in _array(item["notes"], _INVALID_SOURCE)
    ]
    organizations = [
        _organization(value)
        for value in _array(item["organizations"], _INVALID_SOURCE)
    ]
    emails = [_repeated_object(value) for value in _array(item["emails"], _INVALID_SOURCE)]
    phones = [_repeated_object(value) for value in _array(item["phones"], _INVALID_SOURCE)]
    urls = [_repeated_object(value) for value in _array(item["urls"], _INVALID_SOURCE)]
    addresses = [_address(value) for value in _array(item["addresses"], _INVALID_SOURCE)]

    organizations.sort(key=lambda value: tuple(_text_key(component) for component in value))
    emails.sort(key=_repeated_sort_key)
    phones.sort(key=_repeated_sort_key)
    urls.sort(key=_repeated_sort_key)
    addresses.sort(key=_address_sort_key)

    return {
        "contact_id": contact_id,
        "collection_alias": collection_alias,
        "name": name,
        "aliases": sorted(alias_values, key=_text_key),
        "organizations": organizations,
        "titles": sorted(title_values, key=_text_key),
        "emails": emails,
        "phones": phones,
        "addresses": addresses,
        "birthday": _birthday(item["birthday"]),
        "urls": urls,
        "notes": sorted(note_values, key=_text_key),
    }


def _normalise_source(data: object) -> dict[str, object]:
    source = _object(data, _INVALID_SOURCE)
    _exact_keys(source, _SOURCE_KEYS, _INVALID_SOURCE)
    schema_version = _normalise_string(source["schema_version"], _INVALID_SOURCE)
    if schema_version != _SOURCE_SCHEMA_VERSION:
        _raise(_INVALID_SOURCE)
    account_namespace = _identifier(source["account_namespace"])

    collection_values = [
        _identifier(value) for value in _array(source["collections"], _INVALID_SOURCE)
    ]
    if not collection_values or len(set(collection_values)) != len(collection_values):
        _raise(_INVALID_SOURCE)
    collection_values.sort(key=_text_key)
    allowed_aliases = set(collection_values)

    contacts = [_contact(value, allowed_aliases) for value in _array(source["contacts"], _INVALID_SOURCE)]
    contact_ids = [cast(str, value["contact_id"]) for value in contacts]
    if len(set(contact_ids)) != len(contact_ids):
        _raise(_INVALID_SOURCE)
    contacts.sort(
        key=lambda value: (
            _text_key(cast(str, cast(dict[str, object], value["name"])["display"])),
            _text_key(cast(str, value["collection_alias"])),
            cast(str, value["contact_id"]),
        )
    )
    return {
        "schema_version": schema_version,
        "account_namespace": account_namespace,
        "collections": collection_values,
        "contacts": contacts,
    }


def _compact_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", "strict")
    except (TypeError, UnicodeError, ValueError):
        _raise(_INVALID_SOURCE)


def validate_source(data: object) -> None:
    """Validate a complete, closed ``carddav-source/1.0`` payload."""
    _normalise_source(data)


def canonical_source(data: object) -> bytes:
    """Validate and return deterministic UTF-8 bytes for a source payload."""
    return _compact_bytes(_normalise_source(data))


def source_generation(data: object) -> str:
    """Return the lowercase SHA-256 generation of canonical source bytes."""
    return hashlib.sha256(canonical_source(data)).hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _raise(_INVALID_JSON)
        result[key] = value
    return result


def _reject_constant(value: str) -> NoReturn:
    _raise(_INVALID_JSON)


def _parse_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        _raise(_INVALID_JSON)
    return parsed


def parse_json(raw: str | bytes) -> object:
    """Parse JSON while rejecting duplicate object keys and nonfinite numbers."""
    if type(raw) not in (str, bytes):
        _raise(_INVALID_JSON)
    try:
        return json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
            parse_float=_parse_float,
        )
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _raise(_INVALID_JSON)


def _validate_version(data: object) -> None:
    value = _object(data, _INVALID_COMMAND)
    expected_keys = (
        "command_schema_version",
        "command",
        "package_version",
        "supported_source_schema_versions",
        "capabilities",
        "dependency_versions",
    )
    _exact_keys(value, expected_keys, _INVALID_COMMAND)
    string_values = {
        "command_schema_version": _COMMAND_SCHEMA_VERSION,
        "command": "version",
        "package_version": "0.1.0",
    }
    for key, expected in string_values.items():
        if type(value[key]) is not str or value[key] != expected:
            _raise(_INVALID_COMMAND)

    supported = _array(value["supported_source_schema_versions"], _INVALID_COMMAND)
    if len(supported) != 1 or type(supported[0]) is not str or supported[0] != _SOURCE_SCHEMA_VERSION:
        _raise(_INVALID_COMMAND)

    capabilities = _object(value["capabilities"], _INVALID_COMMAND)
    _exact_keys(capabilities, ("read_only", "create_update", "cleanup_delete"), _INVALID_COMMAND)
    expected_capabilities = {"read_only": True, "create_update": False, "cleanup_delete": False}
    for key, expected_bool in expected_capabilities.items():
        if type(capabilities[key]) is not bool or capabilities[key] is not expected_bool:
            _raise(_INVALID_COMMAND)

    dependencies = _object(value["dependency_versions"], _INVALID_COMMAND)
    _exact_keys(dependencies, ("vdirsyncer", "vobject"), _INVALID_COMMAND)
    for key, expected in {"vdirsyncer": "0.21.0", "vobject": "0.9.9"}.items():
        if type(dependencies[key]) is not str or dependencies[key] != expected:
            _raise(_INVALID_COMMAND)


def _validate_timestamp(value: object) -> None:
    if type(value) is not str or _TIMESTAMP_RE.fullmatch(value) is None:
        _raise(_INVALID_COMMAND)
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        _raise(_INVALID_COMMAND)


def _validate_digest(value: object) -> None:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        _raise(_INVALID_COMMAND)


_READ_ENVELOPE_KEYS = (
    "command_schema_version",
    "command",
    "profile",
    "generation",
    "profile_generation_sha256",
    "synced_at",
    "freshness",
)
_STATUS_KEYS = (
    "command_schema_version",
    "command",
    "profile",
    "current_generation",
    "profile_generation_sha256",
    "contact_count",
    "synced_at",
    "freshness",
)
_CANDIDATE_KEYS = ("reason", "key", "members")
_MEMBER_KEYS = ("contact_id", "values")
_AUDIT_REASONS = ("email", "phone", "name")
_MAX_QUERY_CHARACTERS = 512


def _validate_freshness(data: object) -> None:
    freshness = _object(data, _INVALID_COMMAND)
    _exact_keys(
        freshness, ("age_seconds", "stale_after_seconds", "stale", "clock_skew"), _INVALID_COMMAND
    )
    for key in ("age_seconds", "stale_after_seconds"):
        if type(freshness[key]) is not int or cast(int, freshness[key]) < 0:
            _raise(_INVALID_COMMAND)
    if cast(int, freshness["stale_after_seconds"]) <= 0:
        _raise(_INVALID_COMMAND)
    for key in ("stale", "clock_skew"):
        if type(freshness[key]) is not bool:
            _raise(_INVALID_COMMAND)


def _validate_profile_name(value: object) -> None:
    if type(value) is not str or _IDENTIFIER_RE.fullmatch(value) is None:
        _raise(_INVALID_COMMAND)


def _count(value: object) -> int:
    if type(value) is not int or value < 0:
        _raise(_INVALID_COMMAND)
    return value


def _read_envelope(data: object, command: str, extra: tuple[str, ...]) -> dict[str, object]:
    """Validate the envelope every local read command shares."""
    value = _object(data, _INVALID_COMMAND)
    _exact_keys(value, _READ_ENVELOPE_KEYS + extra, _INVALID_COMMAND)
    if value["command_schema_version"] != _COMMAND_SCHEMA_VERSION or value["command"] != command:
        _raise(_INVALID_COMMAND)
    _validate_profile_name(value["profile"])
    _validate_digest(value["generation"])
    _validate_digest(value["profile_generation_sha256"])
    _validate_timestamp(value["synced_at"])
    _validate_freshness(value["freshness"])
    return value


def _contact_object(value: object) -> dict[str, object]:
    """Validate one contact with the same recursive rules the source uses."""
    item = _object(value, _INVALID_COMMAND)
    alias = item.get("collection_alias")
    if type(alias) is not str:
        _raise(_INVALID_COMMAND)
    return _contact(item, {_identifier(alias)})


def _validate_query(value: object) -> None:
    if type(value) is not str or not 1 <= len(value) <= _MAX_QUERY_CHARACTERS:
        _raise(_INVALID_COMMAND)
    for character in value:
        codepoint = ord(character)
        if codepoint < 0x20 or codepoint == 0x7F or 0xD800 <= codepoint <= 0xDFFF:
            _raise(_INVALID_COMMAND)


def _validate_status(data: object) -> None:
    value = _object(data, _INVALID_COMMAND)
    _exact_keys(value, _STATUS_KEYS, _INVALID_COMMAND)
    if value["command_schema_version"] != _COMMAND_SCHEMA_VERSION or value["command"] != "status":
        _raise(_INVALID_COMMAND)
    _validate_profile_name(value["profile"])
    _validate_digest(value["profile_generation_sha256"])
    derived = ("current_generation", "contact_count", "synced_at", "freshness")
    missing = [value[key] is None for key in derived]
    if any(missing) != all(missing):
        # Before the first successful sync every derived field is null together.
        _raise(_INVALID_COMMAND)
    if all(missing):
        return
    _validate_digest(value["current_generation"])
    _count(value["contact_count"])
    _validate_timestamp(value["synced_at"])
    _validate_freshness(value["freshness"])


def _validate_show(data: object) -> None:
    value = _read_envelope(data, "show", ("contact",))
    _contact_object(value["contact"])


def _validate_search(data: object) -> None:
    value = _read_envelope(data, "search", ("query", "total_matches", "truncated", "results"))
    _validate_query(value["query"])
    if value["truncated"] is not False:
        _raise(_INVALID_COMMAND)
    results = [_contact_object(item) for item in _array(value["results"], _INVALID_COMMAND)]
    identifiers = [cast(str, item["contact_id"]) for item in results]
    if len(set(identifiers)) != len(identifiers):
        _raise(_INVALID_COMMAND)
    if _count(value["total_matches"]) != len(results):
        _raise(_INVALID_COMMAND)


def _validate_member(value: object) -> str:
    member = _object(value, _INVALID_COMMAND)
    _exact_keys(member, _MEMBER_KEYS, _INVALID_COMMAND)
    contact_id = member["contact_id"]
    if type(contact_id) is not str or _CONTACT_ID_RE.fullmatch(contact_id) is None:
        _raise(_INVALID_COMMAND)
    values = _array(member["values"], _INVALID_COMMAND)
    if not values:
        _raise(_INVALID_COMMAND)
    for item in values:
        _nonempty_string(item, _INVALID_COMMAND)
    return contact_id


def _validate_candidate(value: object) -> tuple[int, str]:
    group = _object(value, _INVALID_COMMAND)
    _exact_keys(group, _CANDIDATE_KEYS, _INVALID_COMMAND)
    reason = group["reason"]
    if type(reason) is not str or reason not in _AUDIT_REASONS:
        _raise(_INVALID_COMMAND)
    key = _nonempty_string(group["key"], _INVALID_COMMAND)
    members = [_validate_member(item) for item in _array(group["members"], _INVALID_COMMAND)]
    # A candidate needs at least two distinct contacts, listed in identifier order.
    if len(members) < 2 or members != sorted(set(members)):
        _raise(_INVALID_COMMAND)
    return _AUDIT_REASONS.index(reason), key


def _validate_audit(data: object) -> None:
    value = _read_envelope(data, "audit", ("total_candidate_groups", "truncated", "candidates"))
    if value["truncated"] is not False:
        _raise(_INVALID_COMMAND)
    candidates = _array(value["candidates"], _INVALID_COMMAND)
    keys = [_validate_candidate(item) for item in candidates]
    if keys != sorted(set(keys)):
        _raise(_INVALID_COMMAND)
    if _count(value["total_candidate_groups"]) != len(candidates):
        _raise(_INVALID_COMMAND)


def _validate_snapshot(data: object) -> None:
    value = _object(data, _INVALID_COMMAND)
    _exact_keys(
        value,
        (
            "command_schema_version",
            "command",
            "profile",
            "generation",
            "profile_generation_sha256",
            "synced_at",
            "freshness",
            "data",
        ),
        _INVALID_COMMAND,
    )
    if type(value["command_schema_version"]) is not str or value["command_schema_version"] != _COMMAND_SCHEMA_VERSION:
        _raise(_INVALID_COMMAND)
    if type(value["command"]) is not str or value["command"] != "snapshot":
        _raise(_INVALID_COMMAND)
    if type(value["profile"]) is not str or _IDENTIFIER_RE.fullmatch(value["profile"]) is None:
        _raise(_INVALID_COMMAND)
    _validate_digest(value["generation"])
    _validate_digest(value["profile_generation_sha256"])
    _validate_timestamp(value["synced_at"])

    _validate_freshness(value["freshness"])

    source = value["data"]
    try:
        expected_generation = source_generation(source)
    except ValueError:
        _raise(_INVALID_COMMAND)
    if value["generation"] != expected_generation:
        _raise(_INVALID_COMMAND)


_COMMAND_VALIDATORS = {
    "version": _validate_version,
    "snapshot": _validate_snapshot,
    "status": _validate_status,
    "show": _validate_show,
    "search": _validate_search,
    "audit": _validate_audit,
}


def validate_command(data: object) -> None:
    """Validate one implemented `carddav-command/1.0` envelope."""
    value = _object(data, _INVALID_COMMAND)
    command = value.get("command")
    if type(command) is not str or command not in _COMMAND_VALIDATORS:
        _raise(_INVALID_COMMAND)
    try:
        _COMMAND_VALIDATORS[command](value)
    except ValueError:
        _raise(_INVALID_COMMAND)
