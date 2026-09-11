# ABOUTME: Pure vCard editing for the carddav-change/1.0 document; no I/O of any kind.
# ABOUTME: Rewrites only named properties and refuses an edit that would disturb the rest.

"""Pure, lossless vCard editing for the `carddav-change/1.0` change document.

Nothing here touches the filesystem, the network, or a credential: one vCard
string and one change document go in, one vCard string comes out. `vobject`
owns all vCard grammar — this module only decides which properties a change
document is allowed to touch, and then proves that every property it did not
touch survived the edit byte-for-byte in its unfolded form. An edit that
`vobject` cannot reproduce is refused rather than silently rewritten, so a
photo, a group prefix, an unknown `X-` extension, or a multi-valued structured
name component can never be dropped by an ordinary field edit.

Field names are the `carddav-source/1.0` names from `references/data-model.md`,
and `replace` values are validated against that same closed contract, so this
module adds no second contact vocabulary.
"""

from __future__ import annotations

import io
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

import vobject  # type: ignore[import-untyped]
from vobject import base as vobject_base
from vobject import vcard as vobject_vcard

_SCRIPT_DIR = Path(__file__).parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import index
import schemas

CHANGE_SCHEMA_VERSION = "carddav-change/1.0"
CHANGE_KEYS = ("change_schema_version", "set", "clear", "replace")

# Scalar contact fields, each mapped to the one vCard property it lives in.
SCALAR_FIELDS: dict[str, str] = {
    "display": "fn",
    "prefix": "n",
    "given": "n",
    "additional": "n",
    "family": "n",
    "suffix": "n",
    "birthday": "bday",
}
# Multi-valued contact fields; `replace` rewrites the whole field at once.
MULTI_FIELDS: dict[str, str] = {
    "aliases": "nickname",
    "organizations": "org",
    "titles": "title",
    "emails": "email",
    "phones": "tel",
    "urls": "url",
    "addresses": "adr",
    "notes": "note",
}
NAME_COMPONENTS = ("prefix", "given", "additional", "family", "suffix")
TYPED_FIELDS = ("emails", "phones", "urls")
TEXT_LIST_FIELDS = ("aliases", "titles", "notes")

# A generated create UID must survive vdirsyncer's href generation unchanged,
# so the operation keeps one stable remote identity across a retry.
UID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")
_PROBE_ID = "0" * 16
_PROBE_ALIAS = "probe"


class InvalidChange(Exception):
    """The change document, the target card, or the result is not acceptable."""


class UnsupportedEdit(InvalidChange):
    """A requested edit cannot be expressed without losing supplied data."""


class LossyEdit(InvalidChange):
    """The edit could not be applied without disturbing untouched properties."""


class LossyRoundTrip(InvalidChange):
    """The card that came back from the server lost something that was sent."""


def _mapping(value: object) -> dict[str, object]:
    if type(value) is not dict:
        raise InvalidChange
    item = cast(dict[object, object], value)
    if not all(type(key) is str for key in item):
        raise InvalidChange
    return cast(dict[str, object], item)


def _probe_contact(name: Mapping[str, object], extra: Mapping[str, object]) -> None:
    """Validate proposed values against the closed `carddav-source/1.0` contract."""
    contact: dict[str, object] = {
        "contact_id": _PROBE_ID,
        "collection_alias": _PROBE_ALIAS,
        "name": {"display": "probe", **{key: None for key in NAME_COMPONENTS}, **name},
        "aliases": [],
        "organizations": [],
        "titles": [],
        "emails": [],
        "phones": [],
        "addresses": [],
        "birthday": None,
        "urls": [],
        "notes": [],
    }
    contact.update(extra)
    try:
        schemas.validate_source(
            {
                "schema_version": index.SOURCE_SCHEMA_VERSION,
                "account_namespace": _PROBE_ALIAS,
                "collections": [_PROBE_ALIAS],
                "contacts": [contact],
            }
        )
    except ValueError as exc:
        raise InvalidChange from exc


def _validate_set(raw: object) -> dict[str, str]:
    values = _mapping(raw)
    if not set(values) <= set(SCALAR_FIELDS):
        raise InvalidChange
    result: dict[str, str] = {}
    name: dict[str, object] = {}
    extra: dict[str, object] = {}
    for field, value in values.items():
        if type(value) is not str or not value.strip():
            raise InvalidChange
        result[field] = value
        if field == "display":
            name["display"] = value
        elif field == "birthday":
            extra["birthday"] = {"value": value, "kind": "text"}
        else:
            name[field] = value
    _probe_contact(name, extra)
    return result


def _validate_clear(raw: object) -> list[str]:
    if type(raw) is not list:
        raise InvalidChange
    fields = cast(list[object], raw)
    if not all(type(field) is str for field in fields):
        raise InvalidChange
    result = cast(list[str], fields)
    if len(set(result)) != len(result):
        raise InvalidChange
    if not set(result) <= (set(SCALAR_FIELDS) | set(MULTI_FIELDS)):
        raise InvalidChange
    if "display" in result:
        # A vCard must carry a formatted name, and the reader derives
        # `name.display` from it; removing it is not an expressible edit.
        raise UnsupportedEdit
    return result


def _validate_replace(raw: object) -> dict[str, list[object]]:
    values = _mapping(raw)
    if not set(values) <= set(MULTI_FIELDS):
        raise InvalidChange
    for field, value in values.items():
        if type(value) is not list:
            raise InvalidChange
    _probe_contact({}, values)
    for field in TYPED_FIELDS + ("addresses",):
        for entry in cast(list[object], values.get(field, [])):
            if cast(dict[str, object], entry).get("label") is not None:
                # `index.py` never reads a vCard LABEL back into the contract,
                # so writing one would be data this skill cannot see again.
                raise UnsupportedEdit
    return cast(dict[str, list[object]], values)


def validate_changes(document: object) -> dict[str, object]:
    """Validate one `carddav-change/1.0` document and return it normalized."""
    value = _mapping(document)
    if set(value) != set(CHANGE_KEYS):
        raise InvalidChange
    if value["change_schema_version"] != CHANGE_SCHEMA_VERSION:
        raise InvalidChange
    changed = _validate_set(value["set"])
    cleared = _validate_clear(value["clear"])
    replaced = _validate_replace(value["replace"])
    if not changed and not cleared and not replaced:
        # An operation that changes nothing is a caller mistake, not a no-op write.
        raise InvalidChange
    targeted = list(changed) + cleared + list(replaced)
    if len(set(targeted)) != len(targeted):
        raise InvalidChange
    return {
        "change_schema_version": CHANGE_SCHEMA_VERSION,
        "set": changed,
        "clear": cleared,
        "replace": replaced,
    }


def touched_properties(changes: Mapping[str, object]) -> set[str]:
    """Name every vCard property the validated change document may rewrite."""
    fields = (
        list(cast(Mapping[str, object], changes["set"]))
        + cast(list[str], changes["clear"])
        + list(cast(Mapping[str, object], changes["replace"]))
    )
    return {(SCALAR_FIELDS | MULTI_FIELDS)[field].upper() for field in fields}


def parse_card(text: object) -> Any:
    """Parse exactly one vCard, refusing anything else."""
    if type(text) is not str:
        raise InvalidChange
    try:
        components = list(vobject.readComponents(text))
    except Exception as exc:  # vobject raises many unrelated parse errors
        raise InvalidChange from exc
    if len(components) != 1:
        raise InvalidChange
    card = components[0]
    if getattr(card, "name", None) != "VCARD" or getattr(card, "uid", None) is None:
        raise InvalidChange
    return card


def _line_items(text: str) -> list[tuple[str, str]]:
    """Read a vCard's unfolded logical lines with vobject's own line reader.

    Each item is `(qualified name, line)`, where the qualified name keeps any
    group prefix (`item1.EMAIL`) so a group can never be silently moved onto
    another property.
    """
    items: list[tuple[str, str]] = []
    try:
        lines = list(vobject_base.getLogicalLines(io.StringIO(text), False))
    except Exception as exc:  # vobject raises many unrelated parse errors
        raise InvalidChange from exc
    for raw_line, _number in lines:
        if not raw_line.strip():
            continue
        name = raw_line.split(":", 1)[0].split(";", 1)[0].strip().upper()
        items.append((name, raw_line))
    return items


def _grouped_lines(text: str) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for name, line in _line_items(text):
        grouped.setdefault(name, []).append(line)
    return grouped


def _base_name(name: str) -> str:
    return name.rsplit(".", 1)[-1]


def _splice(before: str, edited: str, touched: set[str]) -> str:
    """Rebuild the card, rewriting only the properties the change document names.

    Pinned `vobject==0.9.9` does not round-trip every property it can parse —
    a multi-component `NICKNAME` collapses to its first component, and
    parameter order is not stable — so re-serializing a whole card would
    silently rewrite properties nobody asked to change. Every untouched
    logical line is therefore carried over from the fresh remote card exactly
    as it arrived, and only the touched properties come from the edited card.
    Both sides are produced by vobject's own reader and serializer; no vCard
    grammar is implemented here.
    """
    replacements = _grouped_lines(edited)
    output: list[str] = []
    written: set[str] = set()

    def _emit_touched(base: str) -> None:
        if base in written:
            return
        written.add(base)
        for name, line in _line_items(edited):
            if _base_name(name) == base:
                output.append(line)

    for name, line in _line_items(before):
        base = _base_name(name)
        if base in touched:
            _emit_touched(base)
            continue
        if base == "END":
            for missing in sorted(touched - written):
                if missing in replacements:
                    _emit_touched(missing)
        output.append(line)

    buffer = io.StringIO()
    for line in output:
        vobject_base.foldOneLine(buffer, line)
    return buffer.getvalue()


def _check_preserved(before: str, after: str, touched: set[str]) -> None:
    """Refuse the edit unless every untouched property survived unchanged."""
    original, result = _grouped_lines(before), _grouped_lines(after)
    untouched = {
        name for name in set(original) | set(result) if _base_name(name) not in touched
    }
    for name in untouched:
        if original.get(name) != result.get(name):
            raise LossyEdit


def _set_name(card: Any, changes: Mapping[str, str], cleared: Sequence[str]) -> None:
    components = [field for field in NAME_COMPONENTS if field in changes or field in cleared]
    if not components:
        return
    if getattr(card, "n", None) is None:
        card.add("n").value = vobject_vcard.Name()
    for field in components:
        # Untouched components keep whatever vobject decoded, including a
        # multi-member list, so structured multi-valued names survive.
        setattr(card.n.value, "box" if field == "po_box" else field, changes.get(field, ""))


def _typed_line(card: Any, name: str, entry: Mapping[str, object]) -> None:
    line = card.add(name)
    line.value = entry["value"]
    types = cast(list[str], entry["types"])
    if types:
        line.params["TYPE"] = [token.upper() for token in types]
    preference = entry["preference"]
    if preference is not None:
        line.params["PREF"] = [str(preference)]


_ADDRESS_ATTRIBUTES = (
    ("po_box", "box"),
    ("extended", "extended"),
    ("street", "street"),
    ("locality", "city"),
    ("region", "region"),
    ("postal_code", "code"),
    ("country", "country"),
)


def _escape_component(value: str) -> str:
    """Escape one comma-separated `NICKNAME` component by vCard rules."""
    return value.replace("\\", "\\\\").replace(",", "\\,")


def _write_field(card: Any, field: str, entries: Sequence[object]) -> None:
    """Write one whole multi-valued field, having already removed the old one."""
    if field == "aliases":
        if entries:
            # `NICKNAME` is a comma-separated list property, but pinned vobject
            # gives it the plain single-value text behavior, which would escape
            # the separators away. The components are joined and escaped here
            # by the same vCard rules `index.py` decodes with, and the line is
            # marked already encoded so vobject writes it through untouched.
            line = card.add("nickname")
            line.value = ",".join(_escape_component(cast(str, entry)) for entry in entries)
            line.encoded = True
        return
    if field in TEXT_LIST_FIELDS:
        for entry in entries:
            card.add(MULTI_FIELDS[field]).value = cast(str, entry)
        return
    if field == "organizations":
        for entry in entries:
            card.add("org").value = list(cast(Sequence[str], entry))
        return
    if field == "addresses":
        for entry in entries:
            item = cast(Mapping[str, object], entry)
            line = card.add("adr")
            line.value = vobject_vcard.Address(
                **{
                    attribute: cast(str, item[key]) or ""
                    for key, attribute in _ADDRESS_ATTRIBUTES
                }
            )
            types = cast(list[str], item["types"])
            if types:
                line.params["TYPE"] = [token.upper() for token in types]
            if item["preference"] is not None:
                line.params["PREF"] = [str(item["preference"])]
        return
    for entry in entries:
        _typed_line(card, MULTI_FIELDS[field], cast(Mapping[str, object], entry))


def _edit(card: Any, changes: Mapping[str, object]) -> None:
    """Apply a validated change document to a parsed card in place."""
    changed = cast(dict[str, str], changes["set"])
    cleared = cast(list[str], changes["clear"])
    replaced = cast(dict[str, list[object]], changes["replace"])

    for field in list(replaced) + [name for name in cleared if name in MULTI_FIELDS]:
        card.contents.pop(MULTI_FIELDS[field], None)
    for field, entries in replaced.items():
        _write_field(card, field, entries)

    if "birthday" in cleared:
        card.contents.pop("bday", None)
    if "birthday" in changed:
        card.contents.pop("bday", None)
        card.add("bday").value = changed["birthday"]
    if "display" in changed:
        card.contents.pop("fn", None)
        card.add("fn").value = changed["display"]
    _set_name(card, changed, cleared)
    if getattr(card, "n", None) is not None and all(
        not getattr(card.n.value, field, "") for field in ("prefix", "given", "additional",
                                                           "family", "suffix")
    ):
        card.contents.pop("n", None)


def _verify(before_raw: str, after_raw: str, changes: Mapping[str, object]) -> dict[str, object]:
    """Prove the serialized result says exactly what the change document asked."""
    before = index.parse_vcard(before_raw, _PROBE_ALIAS)
    after = index.parse_vcard(after_raw, _PROBE_ALIAS)
    if after["contact_id"] != before["contact_id"]:
        raise LossyEdit
    changed = cast(dict[str, str], changes["set"])
    cleared = cast(list[str], changes["clear"])
    replaced = cast(dict[str, list[object]], changes["replace"])
    after_name = cast(dict[str, object], after["name"])
    before_name = cast(dict[str, object], before["name"])

    for field in SCALAR_FIELDS:
        target = after_name.get(field) if field != "birthday" else None
        if field == "birthday":
            birthday = cast(dict[str, object] | None, after["birthday"])
            target = None if birthday is None else birthday["value"]
        if field in changed:
            if target != changed[field].strip():
                raise LossyEdit
        elif field in cleared:
            if target is not None:
                raise LossyEdit
        else:
            expected = before_name.get(field)
            if field == "birthday":
                previous = cast(dict[str, object] | None, before["birthday"])
                expected = None if previous is None else previous["value"]
            if field == "display" and "display" not in changed:
                expected = before_name["display"]
            if target != expected:
                raise LossyEdit
    for field in MULTI_FIELDS:
        if field in replaced:
            continue
        if field in cleared:
            if after[field] != []:
                raise LossyEdit
        elif after[field] != before[field]:
            raise LossyEdit
    return after


def apply_changes(raw_vcard: object, document: object) -> str:
    """Return the edited vCard text, or refuse the edit without changing anything."""
    changes = validate_changes(document)
    text = raw_vcard if type(raw_vcard) is str else ""
    card = parse_card(raw_vcard)
    _edit(card, changes)
    touched = touched_properties(changes)
    try:
        edited = _splice(text, cast(str, card.serialize()), touched)
    except InvalidChange:
        raise
    except Exception as exc:  # vobject raises many unrelated serialization errors
        raise LossyEdit from exc
    _check_preserved(text, edited, touched)
    _verify(text, edited, changes)
    return edited


def build_card(uid: object, document: object) -> str:
    """Build one new vCard for a create operation from a change document."""
    if type(uid) is not str or UID_PATTERN.fullmatch(uid) is None:
        raise InvalidChange
    changes = validate_changes(document)
    if changes["clear"]:
        raise InvalidChange
    if "display" not in cast(Mapping[str, object], changes["set"]):
        # Every contact needs a formatted name; nothing is guessed from a
        # supplied email address, organization, or structured name component.
        raise InvalidChange
    card = vobject.vCard()
    card.add("uid").value = uid
    card.add("fn").value = cast(dict[str, str], changes["set"])["display"]
    _edit(card, {**changes, "set": {k: v for k, v in
                                    cast(dict[str, str], changes["set"]).items()
                                    if k != "display"}})
    try:
        built = cast(str, card.serialize())
    except Exception as exc:  # vobject raises many unrelated serialization errors
        raise LossyEdit from exc
    _verify(built, built, {"set": {}, "clear": [], "replace": {}})
    return built


def validate_replacement(current_raw: object, replacement_raw: object) -> dict[str, object]:
    """Validate a whole-card replacement and return its contact projection.

    This is the lossless path a consumer needs when it has composed a complete
    survivor card itself. It never merges, never chooses fields, and never
    rewrites the supplied bytes: it only refuses a replacement that is not
    exactly one vCard, that would move the record to another identity, or that
    this skill could not read back.
    """
    current = parse_card(current_raw)
    replacement = parse_card(replacement_raw)
    if current.uid.value != replacement.uid.value:
        raise InvalidChange
    try:
        return index.parse_vcard(cast(str, replacement_raw), _PROBE_ALIAS)
    except index.InvalidContact as exc:
        raise InvalidChange from exc


def _unquote_param(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return text[1:-1]
    return text


def _normalized_line(line: str) -> tuple[str, tuple[tuple[str, tuple[str, ...]], ...], str]:
    """Reduce one logical line to what a server may not change without losing data.

    Only differences that carry no information are normalized away: parameter
    order, repeated-parameter order, parameter-name case, optional quoting, and
    the case of `TYPE` tokens (which this skill's reader lowercases anyway).
    The property name, the full parameter set, and the value itself are
    compared as they stand, so a dropped photo, a rewritten value, or a lost
    `X-` extension can never look equal to what was sent.
    """
    head, separator, value = line.partition(":")
    if not separator:
        return line.strip().upper(), (), ""
    pieces = head.split(";")
    name = pieces[0].strip().upper()
    collected: dict[str, list[str]] = {}
    for chunk in pieces[1:]:
        key, _, raw = chunk.partition("=")
        key = key.strip().upper()
        for item in raw.split(","):
            text = _unquote_param(item)
            collected.setdefault(key, []).append(text.casefold() if key == "TYPE" else text)
    params = tuple(sorted((key, tuple(sorted(values))) for key, values in collected.items()))
    return name, params, value


def check_round_trip(intended: object, actual: object) -> None:
    """Refuse to call a write verified if the server dropped or rewrote a line.

    The normalized contact projection deliberately models only the fields this
    skill understands, so comparing projections cannot notice a server that
    silently discarded a photo, a `CATEGORIES` line, a group prefix, or an
    unknown `X-` extension. This compares the actual logical lines instead:
    every line that was sent must still be present, allowing only the
    information-free serialization differences above. A server may add lines of
    its own (`REV`, `PRODID`); that is not loss and is not refused.
    """
    if type(intended) is not str or type(actual) is not str:
        raise LossyRoundTrip
    sent, returned = _grouped_lines(intended), _grouped_lines(actual)
    for name, lines in sent.items():
        if _base_name(name) in ("BEGIN", "END"):
            continue
        present = returned.get(name, [])
        if sorted(lines) == sorted(present):
            continue
        remaining = [_normalized_line(line) for line in present]
        for line in lines:
            candidate = _normalized_line(line)
            if candidate not in remaining:
                raise LossyRoundTrip
            remaining.remove(candidate)
