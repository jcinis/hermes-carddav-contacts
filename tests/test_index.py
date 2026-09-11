"""vCard adapter and SQLite index contract tests; synthetic contacts only."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import stat
from pathlib import Path
from types import ModuleType

SCRIPTS = (
    Path(__file__).parent.parent
    / "skills"
    / "productivity"
    / "carddav-contacts"
    / "scripts"
)


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"carddav_{name}", SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _card(*lines: str) -> str:
    return "\r\n".join(["BEGIN:VCARD", "VERSION:3.0", *lines, "END:VCARD"]) + "\r\n"


def test_parse_vcard_uses_fn_as_display_and_hashes_the_uid() -> None:
    index = _load("index")
    ids = _load("ids")

    contact = index.parse_vcard(_card("UID:example-uid", "FN:Example Person"), "contacts-a")

    assert contact["contact_id"] == ids.contact_id("example-uid")
    assert contact["collection_alias"] == "contacts-a"
    assert contact["name"]["display"] == "Example Person"


def test_parse_vcard_preserves_structured_name_components() -> None:
    index = _load("index")

    contact = index.parse_vcard(
        _card("UID:example-uid", "FN:Example Person", "N:Doe;Jane;Q;Dr.;Jr."), "contacts-a"
    )

    assert contact["name"] == {
        "display": "Example Person",
        "prefix": "Dr.",
        "given": "Jane",
        "additional": "Q",
        "family": "Doe",
        "suffix": "Jr.",
    }


def test_display_falls_back_to_name_components_in_specified_order() -> None:
    index = _load("index")

    contact = index.parse_vcard(_card("UID:example-uid", "N:Doe;Jane;Q;Dr.;Jr."), "contacts-a")

    assert contact["name"]["display"] == "Dr. Jane Q Doe Jr."


def test_display_falls_back_to_placeholder_without_usable_names() -> None:
    index = _load("index")

    contact = index.parse_vcard(_card("UID:example-uid", "FN: ", "N:;;;;"), "contacts-a")

    assert contact["name"]["display"] == "(unnamed contact)"


def test_missing_empty_or_unparsable_vcards_are_rejected() -> None:
    index = _load("index")

    for text in (
        _card("FN:No Identifier"),
        _card("UID:   ", "FN:Blank Identifier"),
        "BEGIN:VCARD\r\nVERSION:3.0\r\nUID:truncated\r\n",
        _card("UID:one", "FN:One") + _card("UID:two", "FN:Two"),
    ):
        try:
            index.parse_vcard(text, "contacts-a")
        except index.InvalidContact:
            continue
        raise AssertionError("expected InvalidContact")


def test_typed_values_keep_type_tokens_and_preference() -> None:
    index = _load("index")

    contact = index.parse_vcard(
        _card(
            "UID:example-uid",
            "FN:Example Person",
            "EMAIL;TYPE=WORK,INTERNET;PREF=1:person@example.invalid",
            "EMAIL;TYPE=PREF:other@example.invalid",
            "TEL;TYPE=cell:+15550000000",
            "URL:https://example.invalid/person",
        ),
        "contacts-a",
    )

    assert contact["emails"] == [
        {
            "value": "person@example.invalid",
            "types": ["internet", "work"],
            "label": None,
            "preference": 1,
        },
        {"value": "other@example.invalid", "types": [], "label": None, "preference": 1},
    ]
    assert contact["phones"] == [
        {"value": "+15550000000", "types": ["cell"], "label": None, "preference": None}
    ]
    assert contact["urls"] == [
        {
            "value": "https://example.invalid/person",
            "types": [],
            "label": None,
            "preference": None,
        }
    ]


def test_addresses_keep_every_structured_component() -> None:
    index = _load("index")

    contact = index.parse_vcard(
        _card(
            "UID:example-uid",
            "FN:Example Person",
            "ADR;TYPE=home:PO 1;Suite 2;1 Example Street;Exampleton;EX;00000;Exampleland",
        ),
        "contacts-a",
    )

    assert contact["addresses"] == [
        {
            "po_box": "PO 1",
            "extended": "Suite 2",
            "street": "1 Example Street",
            "locality": "Exampleton",
            "region": "EX",
            "postal_code": "00000",
            "country": "Exampleland",
            "types": ["home"],
            "label": None,
            "preference": None,
        }
    ]


def test_notes_organizations_titles_aliases_and_birthday_are_preserved() -> None:
    index = _load("index")

    contact = index.parse_vcard(
        _card(
            "UID:example-uid",
            "FN:Example Person",
            "NICKNAME:Ex",
            "NICKNAME:Exa",
            "ORG:Example Org;Example Unit",
            "TITLE:Example Title",
            "BDAY:1970-01-02",
            "NOTE:First note line\\nsecond note line",
            "NOTE:Another note",
        ),
        "contacts-a",
    )

    assert contact["aliases"] == ["Ex", "Exa"]
    assert contact["organizations"] == [["Example Org", "Example Unit"]]
    assert contact["titles"] == ["Example Title"]
    assert contact["birthday"] == {"value": "1970-01-02", "kind": "date"}
    assert contact["notes"] == ["First note line\nsecond note line", "Another note"]


def _profile(collections: list[str]) -> dict[str, object]:
    return {
        "profile_schema_version": "carddav-profile/1.0",
        "account_namespace": "example",
        "server_url": "https://carddav.example.invalid/",
        "collection_allowlist": collections,
        "sync_cadence_seconds": 3600,
        "capabilities": {"read_only": True, "create_update": False, "cleanup_delete": False},
    }


def _mirror(root: Path, collection: str, cards: dict[str, str]) -> Path:
    mirror = root / "mirror"
    (mirror / collection).mkdir(mode=0o700, parents=True)
    for name, text in cards.items():
        (mirror / collection / name).write_text(text, encoding="utf-8")
    return mirror


def test_build_source_reads_every_selected_collection_into_a_valid_payload(
    tmp_path: Path,
) -> None:
    index = _load("index")
    schemas = _load("schemas")
    mirror = _mirror(
        tmp_path,
        "contacts-a",
        {
            "one.vcf": _card("UID:one", "FN:Example One"),
            "two.vcf": _card("UID:two", "FN:Example Two"),
        },
    )
    (mirror / "contacts-b").mkdir(mode=0o700)

    source = index.build_source(mirror, _profile(["contacts-a", "contacts-b"]))

    schemas.validate_source(source)
    assert source["schema_version"] == "carddav-source/1.0"
    assert source["account_namespace"] == "example"
    assert source["collections"] == ["contacts-a", "contacts-b"]
    assert [contact["name"]["display"] for contact in source["contacts"]] == [
        "Example One",
        "Example Two",
    ]


def test_duplicate_contact_ids_are_rejected_rather_than_dropped(tmp_path: Path) -> None:
    index = _load("index")
    mirror = _mirror(
        tmp_path,
        "contacts-a",
        {
            "one.vcf": _card("UID:shared", "FN:Example One"),
            "copy.vcf": _card("UID:shared", "FN:Example Copy"),
        },
    )

    try:
        index.build_source(mirror, _profile(["contacts-a"]))
    except index.InvalidContact:
        return
    raise AssertionError("expected InvalidContact")


def test_write_index_stores_canonical_contacts_and_binding_metadata(tmp_path: Path) -> None:
    index = _load("index")
    schemas = _load("schemas")
    mirror = _mirror(tmp_path, "contacts-a", {"one.vcf": _card("UID:one", "FN:Example One")})
    source = index.build_source(mirror, _profile(["contacts-a"]))
    profile_hash = "a" * 64
    database = tmp_path / "index.sqlite3"

    generation = index.write_index(database, source, profile_hash)

    assert generation == schemas.source_generation(source)
    assert stat.S_IMODE(database.lstat().st_mode) == 0o600
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT contact_id, collection_alias, display_name, payload FROM contacts"
        ).fetchall()
        meta = dict(connection.execute("SELECT key, value FROM source_meta").fetchall())
    finally:
        connection.close()
    assert len(rows) == 1
    assert rows[0][:3] == (source["contacts"][0]["contact_id"], "contacts-a", "Example One")
    assert json.loads(rows[0][3])["name"]["display"] == "Example One"
    assert meta == {
        "schema_version": "carddav-source/1.0",
        "account_namespace": "example",
        "collections": '["contacts-a"]',
        "generation": generation,
        "profile_generation_sha256": profile_hash,
    }


def test_notes_keep_their_surrounding_whitespace() -> None:
    index = _load("index")

    contact = index.parse_vcard(
        _card("UID:example-uid", "FN:Example Person", "NOTE:  padded note  "), "contacts-a"
    )

    assert contact["notes"] == ["  padded note  "]


def test_comma_separated_and_repeated_nicknames_are_all_preserved() -> None:
    index = _load("index")

    contact = index.parse_vcard(
        _card(
            "UID:example-uid",
            "FN:Example Person",
            "NICKNAME:Ex,Exa",
            "NICKNAME:",
            "NICKNAME:Third,,Fourth",
        ),
        "contacts-a",
    )

    assert contact["aliases"] == ["Ex", "Exa", "Third", "Fourth"]


def test_escaped_commas_stay_inside_one_nickname_value() -> None:
    index = _load("index")

    contact = index.parse_vcard(
        _card("UID:example-uid", "FN:Example Person", "NICKNAME:Ex\\,tra,Second"),
        "contacts-a",
    )

    assert contact["aliases"] == ["Ex,tra", "Second"]


def test_multi_valued_additional_name_component_is_flattened() -> None:
    index = _load("index")

    contact = index.parse_vcard(
        _card("UID:example-uid", "FN:Example Person", "N:Doe;Jane;Q,R;Dr.;Jr."), "contacts-a"
    )

    assert contact["name"] == {
        "display": "Example Person",
        "prefix": "Dr.",
        "given": "Jane",
        "additional": "Q, R",
        "family": "Doe",
        "suffix": "Jr.",
    }


def test_every_structured_name_component_accepts_several_members() -> None:
    index = _load("index")

    contact = index.parse_vcard(
        _card(
            "UID:example-uid",
            "FN:Example Person",
            "N:Doe,Roe;Jane,Janet;Q,R;Dr.,Prof.;Jr.,III",
        ),
        "contacts-a",
    )

    assert contact["name"] == {
        "display": "Example Person",
        "prefix": "Dr., Prof.",
        "given": "Jane, Janet",
        "additional": "Q, R",
        "family": "Doe, Roe",
        "suffix": "Jr., III",
    }


def test_name_component_members_keep_order_repeats_empties_and_escaped_commas() -> None:
    index = _load("index")

    contact = index.parse_vcard(
        _card("UID:example-uid", "FN:Example Person", "N:Doe;Jane;A\\,B,C,,C;;"), "contacts-a"
    )

    assert contact["name"]["additional"] == "A,B, C, , C"


def test_display_falls_back_to_flattened_name_components_in_order() -> None:
    index = _load("index")

    contact = index.parse_vcard(
        _card("UID:example-uid", "N:Doe;Jane,Janet;Q,R;Dr.,Prof.;Jr.,III"), "contacts-a"
    )

    assert contact["name"]["display"] == "Dr., Prof. Jane, Janet Q, R Doe Jr., III"


def test_name_component_members_that_are_not_text_are_rejected() -> None:
    index = _load("index")

    for members in ([b"Q"], [None], [["Q"]], object()):
        try:
            index._name_component(members)
        except index.InvalidContact:
            continue
        raise AssertionError("expected InvalidContact")


def test_a_generation_refuses_control_characters_inside_a_name_component(
    tmp_path: Path,
) -> None:
    index = _load("index")
    mirror = _mirror(
        tmp_path,
        "contacts-a",
        {"one.vcf": _card("UID:one", "FN:Example One", "N:Doe;Jane;Q\x01,R;;")},
    )
    source = index.build_source(mirror, _profile(["contacts-a"]))

    assert source["contacts"][0]["name"]["additional"] == "Q\x01, R"
    try:
        index.write_index(tmp_path / "index.sqlite3", source, "a" * 64)
    except ValueError as error:
        assert str(error) == "invalid source"
        return
    raise AssertionError("expected invalid source")


def test_all_empty_multi_valued_name_components_are_null() -> None:
    index = _load("index")

    contact = index.parse_vcard(
        _card("UID:example-uid", "FN:Example Person", "N:,,;,,;,,;,,;,,"), "contacts-a"
    )

    assert contact["name"] == {
        "display": "Example Person",
        "prefix": None,
        "given": None,
        "additional": None,
        "family": None,
        "suffix": None,
    }


def test_an_all_empty_component_never_puts_punctuation_in_the_display_fallback() -> None:
    index = _load("index")

    contact = index.parse_vcard(_card("UID:example-uid", "N:Doe;Jane;,,;;"), "contacts-a")
    empty = index.parse_vcard(_card("UID:other-uid", "N:,,;,,;,,;,,;,,"), "contacts-a")

    assert contact["name"]["additional"] is None
    assert contact["name"]["display"] == "Jane Doe"
    assert empty["name"]["display"] == "(unnamed contact)"


def test_empty_members_beside_text_are_still_preserved() -> None:
    index = _load("index")

    contact = index.parse_vcard(
        _card("UID:example-uid", "FN:Example Person", "N:Doe;Jane;,Q,R;;"), "contacts-a"
    )

    assert contact["name"]["additional"] == ", Q, R"


def test_whitespace_only_name_members_stay_text_like_scalar_components() -> None:
    index = _load("index")

    scalar = index.parse_vcard(
        _card("UID:example-uid", "FN:Example Person", "N:Doe;Jane; ;;"), "contacts-a"
    )
    members = index.parse_vcard(
        _card("UID:other-uid", "FN:Example Person", "N:Doe;Jane; , ;;"), "contacts-a"
    )

    assert scalar["name"]["additional"] == " "
    assert members["name"]["additional"] == " ,  "


def test_empty_members_mixed_with_non_text_members_are_rejected() -> None:
    index = _load("index")

    for members in (["", b"Q"], [None, ""], ["", ["Q"], ""]):
        try:
            index._name_component(members)
        except index.InvalidContact:
            continue
        raise AssertionError("expected InvalidContact")
