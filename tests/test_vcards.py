# ABOUTME: Unit tests for the pure vCard change document and lossless card editor.
# ABOUTME: Synthetic cards only; no network, no profile state, no credentials.

"""Pure editing contract for `scripts/vcards.py`.

Every case here is a single vCard string in and a single vCard string out, so
the preservation rules (unknown properties, photos, multi-valued structured
names, UID) are proven without any transport or profile machinery.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests import support_reads as support

vcards = support.load("vcards")
index = support.load("index")

CHANGE_VERSION = "carddav-change/1.0"

RICH_CARD = (
    "BEGIN:VCARD\r\n"
    "VERSION:3.0\r\n"
    "UID:example-uid\r\n"
    "FN:Example Person\r\n"
    "N:Person;Example;Middle,Second;Dr.;PhD\r\n"
    "NICKNAME:Exa,Ex\\,tra\r\n"
    "EMAIL;TYPE=WORK;PREF=1:person@example.invalid\r\n"
    "TEL;TYPE=CELL:+15550100\r\n"
    "ORG:Example Org;Example Unit\r\n"
    "TITLE:Example Title\r\n"
    "ADR;TYPE=HOME:;;1 Example Street;Example City;EX;00000;Example Country\r\n"
    "BDAY:1990-01-02\r\n"
    "URL:https://example.invalid/person\r\n"
    "NOTE:Example note\r\n"
    "CATEGORIES:example-category\r\n"
    "PHOTO;ENCODING=b;TYPE=PNG:aGVsbG8=\r\n"
    "X-EXAMPLE-CUSTOM:keep-me\r\n"
    "END:VCARD\r\n"
)


def _changes(**payload: object) -> dict[str, object]:
    document: dict[str, object] = {
        "change_schema_version": CHANGE_VERSION,
        "set": {},
        "clear": [],
        "replace": {},
    }
    document.update(payload)
    return document


def _contact(raw: str) -> Any:
    """Parse one card through the shipped adapter, as a reader would."""
    return index.parse_vcard(raw, "contacts")


def test_setting_one_scalar_preserves_every_other_property() -> None:
    edited = vcards.apply_changes(RICH_CARD, _changes(set={"display": "Renamed Person"}))

    before, after = _contact(RICH_CARD), _contact(edited)
    assert after["name"]["display"] == "Renamed Person"
    assert after["contact_id"] == before["contact_id"]
    for field in ("aliases", "organizations", "titles", "emails", "phones",
                  "addresses", "birthday", "urls", "notes"):
        assert after[field] == before[field], field
    # Multi-valued structured name members and unknown properties survive verbatim.
    assert after["name"]["additional"] == "Middle, Second"
    assert "X-EXAMPLE-CUSTOM:keep-me" in edited
    assert "CATEGORIES:example-category" in edited
    assert "PHOTO;ENCODING=b;TYPE=PNG:aGVsbG8=" in edited
    assert "UID:example-uid" in edited


def test_clearing_a_scalar_and_replacing_a_multivalued_field() -> None:
    edited = vcards.apply_changes(
        RICH_CARD,
        _changes(
            clear=["birthday", "notes"],
            replace={
                "emails": [
                    {"value": "new@example.invalid", "types": ["home"],
                     "label": None, "preference": 2},
                ]
            },
        ),
    )

    after = _contact(edited)
    assert after["birthday"] is None
    assert after["notes"] == []
    assert after["emails"] == [
        {"value": "new@example.invalid", "types": ["home"], "label": None, "preference": 2}
    ]
    assert after["phones"] == _contact(RICH_CARD)["phones"]
    assert "X-EXAMPLE-CUSTOM:keep-me" in edited


def test_aliases_round_trip_including_an_escaped_comma() -> None:
    edited = vcards.apply_changes(
        RICH_CARD, _changes(replace={"aliases": ["First", "Se,cond"]})
    )

    assert _contact(edited)["aliases"] == ["First", "Se,cond"]


def test_addresses_and_organizations_replace_every_component() -> None:
    edited = vcards.apply_changes(
        RICH_CARD,
        _changes(
            replace={
                "addresses": [
                    {"po_box": None, "extended": None, "street": "2 Example Road",
                     "locality": "Other City", "region": None, "postal_code": "11111",
                     "country": None, "types": ["work"], "label": None, "preference": None}
                ],
                "organizations": [["Other Org", "Other Unit"]],
            },
        ),
    )

    after = _contact(edited)
    assert after["addresses"] == [
        {"po_box": None, "extended": None, "street": "2 Example Road",
         "locality": "Other City", "region": None, "postal_code": "11111",
         "country": None, "types": ["work"], "label": None, "preference": None}
    ]
    assert after["organizations"] == [["Other Org", "Other Unit"]]


def test_build_card_creates_a_minimal_card_with_the_given_uid() -> None:
    raw = vcards.build_card(
        "operation-uid",
        _changes(
            set={"display": "New Person", "given": "New", "family": "Person"},
            replace={"emails": [{"value": "new@example.invalid", "types": [],
                                 "label": None, "preference": None}]},
        ),
    )

    contact = _contact(raw)
    assert contact["name"]["display"] == "New Person"
    assert contact["name"]["given"] == "New"
    assert contact["emails"][0]["value"] == "new@example.invalid"
    assert "UID:operation-uid" in raw
    assert contact["contact_id"] == support.load("ids").contact_id("operation-uid")


@pytest.mark.parametrize(
    "document",
    [
        {},
        {"change_schema_version": "carddav-change/9.9", "set": {}, "clear": [], "replace": {}},
        _changes(),  # nothing to do
        _changes(set={"unknown_field": "x"}),
        _changes(set={"display": ""}),
        _changes(clear=["display"]),
        _changes(clear=["birthday", "birthday"]),
        _changes(set={"given": "A"}, clear=["given"]),
        _changes(replace={"emails": "not-a-list"}),
        _changes(replace={"emails": [{"value": "a@example.invalid", "types": [],
                                      "label": "Work label", "preference": None}]}),
        _changes(replace={"unknown": []}),
        _changes(set={"display": "Has" + chr(0) + "nul"}),
    ],
)
def test_invalid_change_documents_are_refused(document: object) -> None:
    with pytest.raises(vcards.InvalidChange):
        vcards.apply_changes(RICH_CARD, document)


def test_a_card_that_is_not_exactly_one_vcard_is_refused() -> None:
    with pytest.raises(vcards.InvalidChange):
        vcards.apply_changes(RICH_CARD + RICH_CARD, _changes(set={"display": "X"}))


def test_replacement_card_must_keep_the_same_uid() -> None:
    other = RICH_CARD.replace("UID:example-uid", "UID:other-uid")
    assert vcards.validate_replacement(RICH_CARD, RICH_CARD)["contact_id"] == _contact(
        RICH_CARD
    )["contact_id"]
    with pytest.raises(vcards.InvalidChange):
        vcards.validate_replacement(RICH_CARD, other)
