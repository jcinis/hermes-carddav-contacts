"""`search --profile NAME --query TEXT --json` contract tests."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

from tests.support_reads import (
    ENVIRONMENT_KEYS,
    SYNCED_AT,
    SYNCED_AT_EPOCH,
    card,
    load,
    publish,
    setup_profile,
    write_mirror,
)


@pytest.fixture(autouse=True)
def _hermetic_environment() -> Iterator[None]:
    original = {key: os.environ[key] for key in ENVIRONMENT_KEYS if key in os.environ}
    for key in ENVIRONMENT_KEYS:
        os.environ.pop(key, None)
    yield
    for key in ENVIRONMENT_KEYS:
        os.environ.pop(key, None)
    os.environ.update(original)


@pytest.fixture
def cli() -> ModuleType:
    return load("cli")


def _search(
    cli: ModuleType, capsys: pytest.CaptureFixture[str], query: str
) -> dict[str, object]:
    assert cli.main(["search", "--profile", "demo", "--query", query, "--json"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert isinstance(payload, dict)
    load("schemas").validate_command(payload)
    return payload


def _displays(payload: dict[str, object]) -> list[str]:
    results = payload["results"]
    assert isinstance(results, list)
    return [result["name"]["display"] for result in results]


def test_search_matches_a_case_insensitive_substring_of_the_display_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cli: ModuleType,
    capsys: pytest.CaptureFixture[str],
) -> None:
    profile_dir = setup_profile(cli, tmp_path)
    write_mirror(
        profile_dir,
        {
            "one.vcf": card("one", "Ada Example"),
            "two.vcf": card("two", "Grace Sample"),
            "three.vcf": card("three", "Another EXAMPLE Person"),
        },
    )
    generation = publish(profile_dir)
    monkeypatch.setattr(cli.reads.time, "time", lambda: SYNCED_AT_EPOCH)

    payload = _search(cli, capsys, "exAMple")

    assert payload["command"] == "search"
    assert payload["profile"] == "demo"
    assert payload["generation"] == generation
    assert payload["synced_at"] == SYNCED_AT
    assert payload["query"] == "exAMple"
    assert payload["total_matches"] == 2
    assert payload["truncated"] is False
    assert _displays(payload) == ["Ada Example", "Another EXAMPLE Person"]


RICH_CARD = card(
    "rich",
    "Marie Example",
    "N:Examplova;Marie;Skłodowska;Dr;PhD",
    "NICKNAME:Manya",
    "ORG:Radium Institute;Research Wing",
    "TITLE:Physicist",
    "EMAIL;TYPE=WORK:marie@example.invalid",
    "TEL;TYPE=WORK:+1-555-0142",
    "ADR;TYPE=WORK:;Building Q;12 Rue Exemple;Parisville;Ile Region;75005;Franceland",
    "BDAY:1867-11-07",
    "URL:https://people.example.invalid/marie",
    "NOTE:Speaks Français and prefers écrit notes",
)


@pytest.fixture
def rich_profile(tmp_path: Path, cli: ModuleType) -> Path:
    profile_dir = setup_profile(cli, tmp_path)
    write_mirror(profile_dir, {"rich.vcf": RICH_CARD, "other.vcf": card("other", "Nobody Here")})
    publish(profile_dir)
    return profile_dir


@pytest.mark.parametrize(
    "query",
    [
        "marie example",  # name.display
        "examplova",  # name.family
        "skłodowska",  # name.additional
        "phd",  # name.suffix
        "manya",  # aliases
        "radium institute",  # organizations component
        "research wing",  # organizations component
        "physicist",  # titles
        "marie@example.invalid",  # emails value
        "555-0142",  # phones value
        "building q",  # addresses extended
        "12 rue exemple",  # addresses street
        "parisville",  # addresses locality
        "ile region",  # addresses region
        "75005",  # addresses postal_code
        "franceland",  # addresses country
        "1867-11-07",  # birthday value
        "people.example.invalid",  # urls value
        "français",  # notes
    ],
)
def test_search_covers_every_documented_source_text_field(
    rich_profile: Path, cli: ModuleType, capsys: pytest.CaptureFixture[str], query: str
) -> None:
    payload = _search(cli, capsys, query)

    assert _displays(payload) == ["Marie Example"]
    assert payload["total_matches"] == 1


@pytest.mark.parametrize("query", ["contacts-a", "carddav-source"])
def test_search_does_not_match_index_or_schema_metadata(
    rich_profile: Path, cli: ModuleType, capsys: pytest.CaptureFixture[str], query: str
) -> None:
    payload = _search(cli, capsys, query)

    assert (payload["results"], payload["total_matches"]) == ([], 0)


def test_search_does_not_match_the_opaque_contact_id(
    rich_profile: Path, cli: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    contact_id = load("ids").contact_id("rich")

    payload = _search(cli, capsys, contact_id)

    assert (payload["results"], payload["total_matches"]) == ([], 0)


@pytest.mark.parametrize(
    "query",
    ["", "ada\x00example", "ada\nexample", "ada\texample", "ada\x7fexample", "a" * 513],
    ids=["empty", "nul", "newline", "tab", "delete", "too-long"],
)
def test_search_refuses_a_malformed_query(
    rich_profile: Path, cli: ModuleType, capsys: pytest.CaptureFixture[str], query: str
) -> None:
    assert cli.main(["search", "--profile", "demo", "--query", query, "--json"]) == 2

    assert capsys.readouterr() == ("", "error: invalid operation\n")


def test_search_accepts_the_longest_documented_query(
    rich_profile: Path, cli: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = _search(cli, capsys, "a" * 512)

    assert (payload["results"], payload["total_matches"]) == ([], 0)


METACHARACTER_CARDS = {
    "pct.vcf": card("pct", "Percent %Sign"),
    "under.vcf": card("under", "Under a_b Score"),
    "axb.vcf": card("axb", "Under axb Score"),
    "quote.vcf": card("quote", "Quote O'Example \"Quoted\""),
    "slash.vcf": card("slash", "Back\\\\slash Path"),
    "star.vcf": card("star", "Star *Glob* Name"),
}


@pytest.fixture
def metacharacter_profile(tmp_path: Path, cli: ModuleType) -> Path:
    profile_dir = setup_profile(cli, tmp_path)
    write_mirror(profile_dir, METACHARACTER_CARDS)
    publish(profile_dir)
    return profile_dir


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("%sign", ["Percent %Sign"]),
        ("%", ["Percent %Sign"]),
        ("a_b", ["Under a_b Score"]),
        ("_", ["Under a_b Score"]),
        ("o'example", ["Quote O'Example \"Quoted\""]),
        ('"quoted"', ["Quote O'Example \"Quoted\""]),
        ("*glob*", ["Star *Glob* Name"]),
        ("%score", []),
        ("under a%score", []),
        (".*", []),
        ("^Percent", []),
    ],
)
def test_search_treats_metacharacters_as_literal_text(
    metacharacter_profile: Path,
    cli: ModuleType,
    capsys: pytest.CaptureFixture[str],
    query: str,
    expected: list[str],
) -> None:
    payload = _search(cli, capsys, query)

    assert _displays(payload) == expected
    assert payload["total_matches"] == len(expected)


def test_a_sql_shaped_query_matches_nothing_and_leaves_the_index_intact(
    metacharacter_profile: Path, cli: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = _search(cli, capsys, "'; DROP TABLE contacts; --")

    assert (payload["results"], payload["total_matches"]) == ([], 0)
    assert cli.main(["status", "--profile", "demo", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["contact_count"] == len(METACHARACTER_CARDS)


PRECOMPOSED = "Ren\u00e9e Precomposed"  # e with acute as one code point
DECOMPOSED = "Rene\u0301e Decomposed"  # e followed by combining acute


def test_search_folds_case_without_unicode_normalization(
    tmp_path: Path, cli: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    profile_dir = setup_profile(cli, tmp_path)
    write_mirror(
        profile_dir,
        {
            "precomposed.vcf": card("precomposed", PRECOMPOSED),
            "decomposed.vcf": card("decomposed", DECOMPOSED),
        },
    )
    publish(profile_dir)

    # Case folds, but the two spellings never match each other.
    assert _displays(_search(cli, capsys, "ren\u00e9e")) == [PRECOMPOSED]
    assert _displays(_search(cli, capsys, "REN\u00c9E")) == [PRECOMPOSED]
    assert _displays(_search(cli, capsys, "rene\u0301e")) == [DECOMPOSED]


def test_search_over_an_empty_address_book_returns_no_results(
    tmp_path: Path, cli: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    profile_dir = setup_profile(cli, tmp_path)
    write_mirror(profile_dir, {})
    publish(profile_dir)

    payload = _search(cli, capsys, "anybody")

    assert (payload["results"], payload["total_matches"], payload["truncated"]) == ([], 0, False)


def test_search_never_reads_another_profile(
    tmp_path: Path, cli: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    demo = setup_profile(cli, tmp_path, "demo")
    beta = setup_profile(cli, tmp_path, "beta")
    write_mirror(demo, {"one.vcf": card("one", "Demo Person")})
    write_mirror(beta, {"two.vcf": card("two", "Beta Person")})
    publish(demo)
    publish(beta)

    assert _displays(_search(cli, capsys, "person")) == ["Demo Person"]
    assert cli.main(["search", "--profile", "beta", "--query", "person", "--json"]) == 0
    assert [
        result["name"]["display"] for result in json.loads(capsys.readouterr().out)["results"]
    ] == ["Beta Person"]


@pytest.mark.parametrize("query", ["skłodowska", "curie", "q, r"])
def test_search_matches_members_of_a_multi_valued_name_component(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cli: ModuleType,
    capsys: pytest.CaptureFixture[str],
    query: str,
) -> None:
    profile_dir = setup_profile(cli, tmp_path)
    write_mirror(
        profile_dir,
        {"one.vcf": card("one", "Marie Example", "N:Example;Marie;Skłodowska,Curie,Q,R;;")},
    )
    publish(profile_dir)
    monkeypatch.setattr(cli.reads.time, "time", lambda: SYNCED_AT_EPOCH)

    assert _displays(_search(cli, capsys, query)) == ["Marie Example"]
