"""`audit --profile NAME --json` duplicate-candidate contract tests."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

from tests.support_reads import (
    ENVIRONMENT_KEYS,
    SYNCED_AT,
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


def _unnamed(uid: str) -> str:
    return f"BEGIN:VCARD\r\nVERSION:3.0\r\nUID:{uid}\r\nEND:VCARD\r\n"


DUPLICATE_CARDS = {
    "a.vcf": card("a", "Shared Name", "EMAIL:Person@Example.invalid"),
    "b.vcf": card("b", "shared  name", "EMAIL:person@example.invalid"),
    "c.vcf": card("c", "Phone One", "TEL:+1 (555) 0100"),
    "d.vcf": card("d", "Phone Two", "TEL:15550100"),
    "e.vcf": _unnamed("e"),
    "f.vcf": _unnamed("f"),
    "g.vcf": card("g", "Unique Person", "EMAIL:unique@example.invalid"),
}


def _candidates(payload: dict[str, object]) -> list[dict[str, object]]:
    candidates = payload["candidates"]
    assert isinstance(candidates, list)
    return candidates


def _audit(cli: ModuleType, capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    assert cli.main(["audit", "--profile", "demo", "--json"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert isinstance(payload, dict)
    load("schemas").validate_command(payload)
    return payload


def test_audit_reports_conservative_grouped_duplicate_candidates(
    tmp_path: Path, cli: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    profile_dir = setup_profile(cli, tmp_path)
    write_mirror(profile_dir, DUPLICATE_CARDS)
    generation = publish(profile_dir)
    ids = load("ids")
    a, b, c, d = (ids.contact_id(uid) for uid in ("a", "b", "c", "d"))

    payload = _audit(cli, capsys)

    assert payload["command"] == "audit"
    assert payload["generation"] == generation
    assert payload["synced_at"] == SYNCED_AT
    assert payload["truncated"] is False
    assert payload["total_candidate_groups"] == 3
    assert payload["candidates"] == [
        {
            "reason": "email",
            "key": "person@example.invalid",
            "members": sorted(
                [
                    {"contact_id": a, "values": ["Person@Example.invalid"]},
                    {"contact_id": b, "values": ["person@example.invalid"]},
                ],
                key=lambda member: member["contact_id"],
            ),
        },
        {
            "reason": "phone",
            "key": "15550100",
            "members": sorted(
                [
                    {"contact_id": c, "values": ["+1 (555) 0100"]},
                    {"contact_id": d, "values": ["15550100"]},
                ],
                key=lambda member: member["contact_id"],
            ),
        },
        {
            "reason": "name",
            "key": "shared name",
            "members": sorted(
                [
                    {"contact_id": a, "values": ["Shared Name"]},
                    {"contact_id": b, "values": ["shared  name"]},
                ],
                key=lambda member: member["contact_id"],
            ),
        },
    ]


def test_the_unnamed_placeholder_is_never_name_evidence(
    tmp_path: Path, cli: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    profile_dir = setup_profile(cli, tmp_path)
    write_mirror(profile_dir, {"e.vcf": _unnamed("e"), "f.vcf": _unnamed("f")})
    publish(profile_dir)

    payload = _audit(cli, capsys)

    assert (payload["candidates"], payload["total_candidate_groups"]) == ([], 0)


def test_the_placeholder_display_name_matches_the_vcard_adapter() -> None:
    assert load("queries").PLACEHOLDER_DISPLAY == load("index").UNNAMED_DISPLAY


def test_shared_contacts_do_not_merge_groups_transitively(
    tmp_path: Path, cli: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    profile_dir = setup_profile(cli, tmp_path)
    write_mirror(
        profile_dir,
        {
            "a.vcf": card("a", "Alpha One", "EMAIL:shared@example.invalid"),
            "b.vcf": card("b", "Beta Two", "EMAIL:shared@example.invalid", "TEL:5550111"),
            "c.vcf": card("c", "Gamma Three", "TEL:555-0111"),
        },
    )
    publish(profile_dir)
    ids = load("ids")

    payload = _audit(cli, capsys)

    candidates = _candidates(payload)
    assert [(group["reason"], group["key"]) for group in candidates] == [
        ("email", "shared@example.invalid"),
        ("phone", "5550111"),
    ]
    assert [
        sorted(member["contact_id"] for member in cast(list[Any], group["members"]))
        for group in candidates
    ] == [
        sorted([ids.contact_id("a"), ids.contact_id("b")]),
        sorted([ids.contact_id("b"), ids.contact_id("c")]),
    ]


def test_audit_output_is_byte_identical_across_runs_and_mirror_order(
    tmp_path: Path, cli: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    first_dir = setup_profile(cli, tmp_path, "demo")
    write_mirror(first_dir, DUPLICATE_CARDS)
    publish(first_dir)
    assert cli.main(["audit", "--profile", "demo", "--json"]) == 0
    first = capsys.readouterr().out
    assert cli.main(["audit", "--profile", "demo", "--json"]) == 0
    repeated = capsys.readouterr().out

    reordered_dir = setup_profile(cli, tmp_path, "reordered")
    write_mirror(
        reordered_dir,
        {name: DUPLICATE_CARDS[name] for name in reversed(list(DUPLICATE_CARDS))},
    )
    publish(reordered_dir)
    assert cli.main(["audit", "--profile", "reordered", "--json"]) == 0
    reordered = capsys.readouterr().out

    assert repeated == first
    assert json.loads(reordered)["candidates"] == json.loads(first)["candidates"]


def test_audit_reports_candidates_only_and_writes_nothing(
    tmp_path: Path, cli: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    profile_dir = setup_profile(cli, tmp_path)
    write_mirror(profile_dir, DUPLICATE_CARDS)
    publish(profile_dir)
    before = {
        path: path.lstat().st_mtime_ns
        for path in sorted((tmp_path / "carddav-contacts").rglob("*"))
    }

    payload = _audit(cli, capsys)

    assert set(payload) == {
        "command_schema_version",
        "command",
        "profile",
        "generation",
        "profile_generation_sha256",
        "synced_at",
        "freshness",
        "cache_invalidated",
        "total_candidate_groups",
        "truncated",
        "candidates",
    }
    for group in _candidates(payload):
        assert set(group) == {"reason", "key", "members"}
    assert {
        path: path.lstat().st_mtime_ns
        for path in sorted((tmp_path / "carddav-contacts").rglob("*"))
    } == before


def test_audit_over_an_empty_address_book_reports_no_candidates(
    tmp_path: Path, cli: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    profile_dir = setup_profile(cli, tmp_path)
    write_mirror(profile_dir, {})
    publish(profile_dir)

    payload = _audit(cli, capsys)

    assert (payload["candidates"], payload["total_candidate_groups"]) == ([], 0)
