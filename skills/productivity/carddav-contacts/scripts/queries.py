"""Pure, deterministic queries over an already validated source payload.

Nothing here touches the filesystem, the network, or SQL: the caller passes
contact objects that `schemas` has already validated, and these functions only
compare text.
"""

from __future__ import annotations

import re
from typing import cast


def _folded(value: object) -> str:
    return cast(str, value).casefold()


_NAME_FIELDS = ("display", "prefix", "given", "additional", "family", "suffix")
_TEXT_LISTS = ("aliases", "titles", "notes")
_TYPED_LISTS = ("emails", "phones", "urls")
_ADDRESS_FIELDS = (
    "po_box",
    "extended",
    "street",
    "locality",
    "region",
    "postal_code",
    "country",
)


def _strings(values: object) -> list[str]:
    return [value for value in cast(list[object], values) if isinstance(value, str)]


def _searchable(contact: dict[str, object]) -> list[str]:
    """List every documented searchable text field of one validated contact.

    Opaque identifiers and collection metadata are deliberately absent.
    """
    name = cast(dict[str, object], contact["name"])
    values = [cast(str, name[field]) for field in _NAME_FIELDS if name[field] is not None]
    for field in _TEXT_LISTS:
        values.extend(_strings(contact[field]))
    for organization in cast(list[object], contact["organizations"]):
        values.extend(_strings(organization))
    for field in _TYPED_LISTS:
        for entry in cast(list[object], contact[field]):
            values.append(cast(str, cast(dict[str, object], entry)["value"]))
    for entry in cast(list[object], contact["addresses"]):
        address = cast(dict[str, object], entry)
        values.extend(
            cast(str, address[field]) for field in _ADDRESS_FIELDS if address[field] is not None
        )
    birthday = contact["birthday"]
    if isinstance(birthday, dict):
        values.append(cast(str, birthday["value"]))
    return values


def search(contacts: list[dict[str, object]], query: str) -> list[dict[str, object]]:
    """Return every contact holding the query as a literal, case-folded substring."""
    needle = query.casefold()
    return [
        contact
        for contact in contacts
        if any(needle in _folded(value) for value in _searchable(contact))
    ]


# The vCard adapter's placeholder display name (`index.UNNAMED_DISPLAY`), which
# is never evidence that two contacts are the same person.
PLACEHOLDER_DISPLAY = "(unnamed contact)"
REASONS = ("email", "phone", "name")
_NON_DIGITS = re.compile(r"[^0-9]")
_WHITESPACE = re.compile(r"\s+")


def _evidence(contact: dict[str, object]) -> list[tuple[str, str, str]]:
    """List one contact's conservative (reason, shared key, original value) evidence."""
    found: list[tuple[str, str, str]] = []
    for entry in cast(list[object], contact["emails"]):
        value = cast(str, cast(dict[str, object], entry)["value"])
        key = value.strip().casefold()
        if key:
            found.append(("email", key, value))
    for entry in cast(list[object], contact["phones"]):
        value = cast(str, cast(dict[str, object], entry)["value"])
        key = _NON_DIGITS.sub("", value)
        if key:
            found.append(("phone", key, value))
    display = cast(str, cast(dict[str, object], contact["name"])["display"])
    key = _WHITESPACE.sub(" ", display.strip()).casefold()
    if key and display != PLACEHOLDER_DISPLAY:
        found.append(("name", key, display))
    return found


def audit(contacts: list[dict[str, object]]) -> list[dict[str, object]]:
    """Group contacts that share one normalized key; never decide identity.

    Groups are independent: two groups that share a contact are reported
    separately, so no transitive identity is ever inferred.
    """
    grouped: dict[tuple[str, str], dict[str, list[str]]] = {}
    for contact in contacts:
        contact_id = cast(str, contact["contact_id"])
        for reason, key, value in _evidence(contact):
            grouped.setdefault((reason, key), {}).setdefault(contact_id, []).append(value)
    candidates: list[dict[str, object]] = []
    for reason, key in sorted(grouped, key=lambda pair: (REASONS.index(pair[0]), pair[1])):
        members = grouped[(reason, key)]
        if len(members) < 2:
            continue
        candidates.append(
            {
                "reason": reason,
                "key": key,
                "members": [
                    {"contact_id": contact_id, "values": members[contact_id]}
                    for contact_id in sorted(members)
                ],
            }
        )
    return candidates
