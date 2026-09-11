"""vCard adapter and private SQLite index for one profile's mirrored contacts."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sqlite3
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import vobject  # type: ignore[import-untyped]
from vobject import base as vobject_base
from vobject.icalendar import MultiTextBehavior  # type: ignore[import-untyped]

_SCRIPT_DIR = Path(__file__).parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import ids
import schemas

SOURCE_SCHEMA_VERSION = "carddav-source/1.0"
MANAGED_FILE_MODE = 0o600
UNNAMED_DISPLAY = "(unnamed contact)"
_NAME_COMPONENTS = ("prefix", "given", "additional", "family", "suffix")


class InvalidContact(Exception):
    """A mirrored vCard cannot be represented as a source contact."""


class UnreadableIndex(Exception):
    """A published index is missing, malformed, or not the index this program writes."""


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise InvalidContact
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _display(card: Any, components: dict[str, str | None]) -> str:
    formatted = getattr(card, "fn", None)
    if formatted is not None:
        text = _text(formatted.value).strip()
        if text:
            return text
    parts = [
        stripped
        for component in _NAME_COMPONENTS
        if (value := components[component]) is not None and (stripped := value.strip())
    ]
    if parts:
        return " ".join(parts)
    return UNNAMED_DISPLAY


_TYPE_TOKEN = re.compile(r"[a-z0-9-]+\Z")


def _parameters(line: Any) -> tuple[list[str], int | None]:
    """Map vCard 3 and 4 TYPE/PREF parameters onto the source contract."""
    params = getattr(line, "params", {}) or {}
    tokens: list[str] = []
    preference: int | None = None
    for raw in params.get("TYPE", []):
        token = _text(raw).strip().lower()
        if token == "pref":
            preference = 1
            continue
        if _TYPE_TOKEN.fullmatch(token) and token not in tokens:
            tokens.append(token)
    for raw in params.get("PREF", []):
        text = _text(raw).strip()
        if text.isdigit() and int(text) > 0:
            preference = int(text)
    return sorted(tokens), preference


def _typed_values(card: Any, name: str) -> list[dict[str, object]]:
    values: list[dict[str, object]] = []
    for line in card.contents.get(name, []):
        value = _text(line.value).strip()
        if not value:
            continue
        types, preference = _parameters(line)
        values.append(
            {"value": value, "types": types, "label": None, "preference": preference}
        )
    return values


_ADDRESS_COMPONENTS = (
    ("po_box", "box"),
    ("extended", "extended"),
    ("street", "street"),
    ("locality", "city"),
    ("region", "region"),
    ("postal_code", "code"),
    ("country", "country"),
)


def _addresses(card: Any) -> list[dict[str, object]]:
    addresses: list[dict[str, object]] = []
    for line in card.contents.get("adr", []):
        components: dict[str, object] = {}
        for field, attribute in _ADDRESS_COMPONENTS:
            raw = getattr(line.value, attribute, "")
            text = _text(raw if isinstance(raw, str) else " ".join(raw))
            components[field] = text or None
        if all(components[field] is None for field, _ in _ADDRESS_COMPONENTS):
            continue
        types, preference = _parameters(line)
        components["types"] = types
        components["label"] = None
        components["preference"] = preference
        addresses.append(components)
    return addresses


_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_PARTIAL_DATE = re.compile(r"--[0-9]{2}(-?[0-9]{2})?\Z")


def _plain_values(card: Any, name: str, *, strip: bool = True) -> list[str]:
    values: list[str] = []
    for line in card.contents.get(name, []):
        raw = line.value
        parts = [raw] if isinstance(raw, str) else list(raw)
        for part in parts:
            text = _text(part)
            if not text.strip():
                continue
            values.append(text.strip() if strip else text)
    return values


def _multi_text_values(text: str, name: str) -> list[str]:
    """Decode a comma-separated text property losslessly.

    vobject's default single-value text decode keeps only the first component
    of `NICKNAME:a,b`, so the same source text is re-read with vobject's own
    line reader and its multi-value behavior transform, which applies vCard
    escaping rules (`\\,` stays one literal comma).
    """
    values: list[str] = []
    try:
        lines = list(vobject_base.getLogicalLines(io.StringIO(text), False))
    except Exception as exc:  # vobject raises many unrelated parse errors
        raise InvalidContact from exc
    for raw_line, number in lines:
        try:
            line = vobject_base.textLineToContentLine(raw_line, number)
        except Exception as exc:  # vobject raises many unrelated parse errors
            raise InvalidContact from exc
        if line.name != name:
            continue
        MultiTextBehavior.decode(line)
        if not isinstance(line.value, list):
            raise InvalidContact
        for component in line.value:
            value = _text(component).strip()
            if value:
                values.append(value)
    return values


def _organizations(card: Any) -> list[list[str]]:
    organizations: list[list[str]] = []
    for line in card.contents.get("org", []):
        raw = line.value
        parts = [raw] if isinstance(raw, str) else list(raw)
        components = [_text(part) for part in parts]
        if any(component.strip() for component in components):
            organizations.append(components)
    return organizations


def _birthday(card: Any) -> dict[str, object] | None:
    line = getattr(card, "bday", None)
    if line is None:
        return None
    value = _text(line.value).strip()
    if not value:
        return None
    if _DATE.fullmatch(value):
        kind = "date"
    elif _PARTIAL_DATE.fullmatch(value):
        kind = "partial-date"
    else:
        kind = "text"
    return {"value": value, "kind": kind}


_NAME_MEMBER_SEPARATOR = ", "


def _name_component(raw: object) -> str:
    """Flatten one structured `N` component into the source contract's string.

    A vCard `N` component may carry several comma-separated members, which
    vobject decodes as a list. The source schema stores each component as one
    string or null, so members are joined with `", "` in their vCard order,
    keeping repeats, empty members, and vobject's decoded escaping (an escaped
    `\\,` is already one literal comma inside a member and is not a separator).
    Members that are all empty carry no name text, so they flatten to the empty
    component the caller stores as null rather than to separators alone.
    Anything that is neither text nor a list of text is refused rather than
    stringified.
    """
    if isinstance(raw, str):
        return _text(raw)
    if not isinstance(raw, list):
        raise InvalidContact
    members = [_text(member) for member in raw]
    if not any(members):
        return ""
    return _NAME_MEMBER_SEPARATOR.join(members)


def _name_components(card: Any) -> dict[str, str | None]:
    structured = getattr(card, "n", None)
    components: dict[str, str | None] = dict.fromkeys(_NAME_COMPONENTS)
    if structured is None:
        return components
    for component in _NAME_COMPONENTS:
        raw = getattr(structured.value, component, "")
        text = _name_component(raw)
        components[component] = text or None
    return components


def parse_vcard(text: str, collection_alias: str) -> dict[str, object]:
    """Adapt exactly one vCard into a `carddav-source/1.0` contact object."""
    try:
        components = list(vobject.readComponents(text))
    except Exception as exc:  # vobject raises many unrelated parse errors
        raise InvalidContact from exc
    if len(components) != 1:
        raise InvalidContact
    card = components[0]
    uid = getattr(card, "uid", None)
    if uid is None:
        raise InvalidContact
    try:
        contact_id = ids.contact_id(uid.value)
    except ValueError as exc:
        raise InvalidContact from exc
    name_components = _name_components(card)
    return {
        "contact_id": contact_id,
        "collection_alias": collection_alias,
        "name": {"display": _display(card, name_components), **name_components},
        "aliases": _multi_text_values(text, "NICKNAME"),
        "organizations": _organizations(card),
        "titles": _plain_values(card, "title"),
        "emails": _typed_values(card, "email"),
        "phones": _typed_values(card, "tel"),
        "addresses": _addresses(card),
        "birthday": _birthday(card),
        "urls": _typed_values(card, "url"),
        "notes": _plain_values(card, "note", strip=False),
    }


def build_source(mirror: Path, profile: Mapping[str, object]) -> dict[str, object]:
    """Adapt every mirrored vCard in the selected collections into one payload."""
    namespace = profile["account_namespace"]
    collections = profile["collection_allowlist"]
    if not isinstance(namespace, str) or not isinstance(collections, list):
        raise InvalidContact
    contacts: list[dict[str, object]] = []
    seen: set[object] = set()
    for collection in sorted(collections):
        if not isinstance(collection, str):
            raise InvalidContact
        directory = mirror / collection
        for path in sorted(directory.glob("*.vcf")):
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise InvalidContact from exc
            contact = parse_vcard(text, collection)
            if contact["contact_id"] in seen:
                raise InvalidContact
            seen.add(contact["contact_id"])
            contacts.append(contact)
    return {
        "schema_version": SOURCE_SCHEMA_VERSION,
        "account_namespace": namespace,
        "collections": sorted(collections),
        "contacts": contacts,
    }


_SCHEMA = (
    "CREATE TABLE source_meta (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL)",
    (
        "CREATE TABLE contacts ("
        "contact_id TEXT PRIMARY KEY NOT NULL, "
        "collection_alias TEXT NOT NULL, "
        "display_name TEXT NOT NULL, "
        "payload TEXT NOT NULL)"
    ),
)


META_KEYS = (
    "account_namespace",
    "collections",
    "generation",
    "profile_generation_sha256",
    "schema_version",
)


def read_meta(database: Path) -> dict[str, object]:
    """Read a published index read-only and return its validated source metadata.

    Opened `mode=ro` so resolving a generation never creates the database, writes
    to it, or leaves a journal behind. The schema must be exactly the one
    `write_index` creates, and `source_meta` exactly the keys it stores.
    """
    uri = f"{Path(os.path.abspath(database)).as_uri()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, isolation_level=None)
    except (sqlite3.Error, OSError, ValueError) as exc:
        raise UnreadableIndex from exc
    try:
        entries = connection.execute("SELECT type, name, sql FROM sqlite_master").fetchall()
        rows = connection.execute("SELECT key, value FROM source_meta").fetchall()
    except (sqlite3.Error, OSError) as exc:
        raise UnreadableIndex from exc
    finally:
        connection.close()
    tables = sorted(sql for kind, _, sql in entries if kind == "table")
    implicit = [
        entry
        for entry in entries
        if entry[0] == "index" and entry[2] is None and entry[1].startswith("sqlite_autoindex_")
    ]
    if tables != sorted(_SCHEMA) or len(entries) != len(tables) + len(implicit):
        raise UnreadableIndex
    meta = dict(rows)
    if len(rows) != len(META_KEYS) or sorted(meta) != sorted(META_KEYS):
        raise UnreadableIndex
    if not all(isinstance(value, str) for value in meta.values()):
        raise UnreadableIndex
    try:
        collections = json.loads(meta["collections"])
    except ValueError as exc:
        raise UnreadableIndex from exc
    if (
        not isinstance(collections, list)
        or not all(isinstance(value, str) for value in collections)
        or _canonical_json(collections) != meta["collections"]
    ):
        raise UnreadableIndex
    return {**meta, "collections": collections}


def read_contacts(database: Path) -> list[tuple[str, str, str, str]]:
    """Read every indexed contact row read-only, in stored identifier order."""
    uri = f"{Path(os.path.abspath(database)).as_uri()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, isolation_level=None)
    except (sqlite3.Error, OSError, ValueError) as exc:
        raise UnreadableIndex from exc
    try:
        rows = connection.execute(
            "SELECT contact_id, collection_alias, display_name, payload "
            "FROM contacts ORDER BY contact_id"
        ).fetchall()
    except (sqlite3.Error, OSError) as exc:
        raise UnreadableIndex from exc
    finally:
        connection.close()
    for row in rows:
        if len(row) != 4 or not all(isinstance(column, str) for column in row):
            raise UnreadableIndex
    return cast(list[tuple[str, str, str, str]], rows)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def write_index(database: Path, source: Mapping[str, object], profile_hash: str) -> str:
    """Write the whole index in one transaction and return its generation."""
    canonical = schemas.canonical_source(source)
    generation = hashlib.sha256(canonical).hexdigest()
    normalized = json.loads(canonical.decode("utf-8"))
    os.close(os.open(database, os.O_WRONLY | os.O_CREAT | os.O_EXCL, MANAGED_FILE_MODE))
    connection = sqlite3.connect(database, isolation_level=None)
    try:
        connection.execute("BEGIN")
        for statement in _SCHEMA:
            connection.execute(statement)
        connection.executemany(
            "INSERT INTO source_meta (key, value) VALUES (?, ?)",
            (
                ("schema_version", normalized["schema_version"]),
                ("account_namespace", normalized["account_namespace"]),
                ("collections", _canonical_json(normalized["collections"])),
                ("generation", generation),
                ("profile_generation_sha256", profile_hash),
            ),
        )
        connection.executemany(
            "INSERT INTO contacts (contact_id, collection_alias, display_name, payload) "
            "VALUES (?, ?, ?, ?)",
            (
                (
                    contact["contact_id"],
                    contact["collection_alias"],
                    contact["name"]["display"],
                    _canonical_json(contact),
                )
                for contact in normalized["contacts"]
            ),
        )
        connection.execute("COMMIT")
    finally:
        connection.close()
    return generation
