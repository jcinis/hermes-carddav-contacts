# ABOUTME: Offline unit tests for the record transport's target-boundary and identity rules.
# ABOUTME: No socket is opened here; live conditional writes are proven in the integration suite.

"""Boundary contract for `scripts/records.py`.

The record transport may only ever address a collection the configured server
itself advertised, under the configured `server_url`, named by the profile's
own allowlist. These tests pin that rule without a server, so a regression in
it cannot hide behind a passing integration run.
"""

from __future__ import annotations

import pytest

from tests import support_reads as support

records = support.load("records")

SERVER = "https://carddav.example.invalid/user/"


@pytest.mark.parametrize(
    "candidate",
    [
        "https://carddav.example.invalid/user/contacts/",
        "https://carddav.example.invalid/user/nested/contacts/",
    ],
)
def test_a_collection_under_the_configured_server_is_accepted(candidate: str) -> None:
    assert records.check_collection_url(SERVER, candidate) == candidate


@pytest.mark.parametrize(
    "candidate",
    [
        "https://other.example.invalid/user/contacts/",  # different host
        "http://carddav.example.invalid/user/contacts/",  # different scheme
        "https://carddav.example.invalid:8443/user/contacts/",  # different port
        "https://carddav.example.invalid/other/contacts/",  # outside the configured path
        "https://carddav.example.invalid/userother/contacts/",  # prefix-only lookalike
        "https://user:pass@carddav.example.invalid/user/contacts/",  # credentials in URL
        "/user/contacts/",  # not absolute
        "",
    ],
)
def test_a_collection_outside_the_configured_server_is_refused(candidate: str) -> None:
    with pytest.raises(records.UnsafeRemoteTarget):
        records.check_collection_url(SERVER, candidate)


def test_a_collection_must_be_in_the_profile_allowlist() -> None:
    profile = {
        "server_url": SERVER,
        "collection_allowlist": ["contacts"],
        "account_namespace": "example",
    }
    with pytest.raises(records.UnsafeRemoteTarget):
        records.check_collection_alias(profile, "not-allowed")
    assert records.check_collection_alias(profile, "contacts") == "contacts"


def test_a_response_href_must_stay_inside_the_resolved_collection() -> None:
    collection = "/user/contacts/"
    assert records.check_href(collection, "/user/contacts/one.vcf") == "/user/contacts/one.vcf"
    for outside in ("/user/other/one.vcf", "/user/contacts/", "", "one.vcf"):
        with pytest.raises(records.UnsafeRemoteTarget):
            records.check_href(collection, outside)


def test_a_generated_operation_uid_is_stable_and_href_safe() -> None:
    uid = records.new_operation_uid()
    assert support.load("vcards").UID_PATTERN.fullmatch(uid)
    assert uid != records.new_operation_uid()


# --- Regression: component-aware target boundary (review blocker 2) ---------

@pytest.mark.parametrize(
    "candidate",
    [
        # Dot segments that escape the configured path, plain and percent-encoded.
        "https://carddav.example.invalid/user/../private/",
        "https://carddav.example.invalid/user/%2e%2e/private/",
        "https://carddav.example.invalid/user/%2E%2E/private/",
        "https://carddav.example.invalid/user/./../private/",
        # A dot segment anywhere at all, even one that happens to stay inside.
        "https://carddav.example.invalid/user/contacts/../contacts/",
        "https://carddav.example.invalid/user/./contacts/",
        # Query and fragment components are never part of a collection address.
        "https://carddav.example.invalid/user/contacts/?a=1",
        "https://carddav.example.invalid/user/contacts/#frag",
        # An encoded separator hides a segment boundary from a lexical check.
        "https://carddav.example.invalid/user%2f../private/",
    ],
)
def test_a_traversing_or_decorated_collection_url_is_refused(candidate: str) -> None:
    with pytest.raises(records.UnsafeRemoteTarget):
        records.check_collection_url(SERVER, candidate)


@pytest.mark.parametrize(
    "candidate",
    [
        "/user/contacts/../private.vcf",
        "/user/contacts/%2e%2e/private.vcf",
        "/user/contacts/./one.vcf",
        "/user/contacts/one.vcf?a=1",
        "/user/contacts/one.vcf#frag",
        "/user/contacts/sub/%2f../one.vcf",
        # An href is a path on the bound origin, never a full URL.
        "https://carddav.example.invalid/user/contacts/one.vcf",
        "//evil.example.invalid/user/contacts/one.vcf",
    ],
)
def test_a_traversing_or_decorated_href_is_refused(candidate: str) -> None:
    with pytest.raises(records.UnsafeRemoteTarget):
        records.check_href("/user/contacts/", candidate)


def test_boundary_checks_compare_whole_path_segments_not_string_prefixes() -> None:
    # `/userother/` shares a string prefix with `/user/` but no path segment.
    with pytest.raises(records.UnsafeRemoteTarget):
        records.check_collection_url(SERVER, "https://carddav.example.invalid/userother/c/")
    with pytest.raises(records.UnsafeRemoteTarget):
        records.check_href("/user/contacts/", "/user/contactsother/one.vcf")
    assert records.check_href("/user/contacts/", "/user/contacts/one.vcf")
