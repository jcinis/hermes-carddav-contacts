# ABOUTME: Closed-schema tests for the change, operation, result, and record documents.
# ABOUTME: Pure validation only — no profile state, no network, no credentials.

"""The write documents are closed contracts, exactly like the read envelopes.

A consumer validates what it receives before acting on it, so every key is
required, unknown keys are refused at every depth, and the three document
versions are literals rather than anything a producer may vary.
"""

from __future__ import annotations

import copy

import pytest

from tests import support_reads as support

schemas = support.load("schemas")

CONTACT: dict[str, object] = {
    "contact_id": "0123456789abcdef",
    "collection_alias": "contacts",
    "name": {"display": "Example Person", "prefix": None, "given": "Example",
             "additional": None, "family": "Person", "suffix": None},
    "aliases": [], "organizations": [], "titles": [], "emails": [], "phones": [],
    "addresses": [], "birthday": None, "urls": [], "notes": [],
}
VCARD = "BEGIN:VCARD\r\nVERSION:3.0\r\nUID:example-uid\r\nFN:Example Person\r\nEND:VCARD\r\n"

OPERATION: dict[str, object] = {
    "operation_schema_version": "carddav-operation/1.0",
    "operation_id": "a" * 64,
    "operation": "update",
    "profile": "demo",
    "account_namespace": "example",
    "collection_alias": "contacts",
    "contact_id": "0123456789abcdef",
    "href": "/dav/contacts/one.vcf",
    "base_revision": '"etag-1"',
    "before_vcard": VCARD,
    "after_vcard": VCARD,
    "before_contact": copy.deepcopy(CONTACT),
    "after_contact": copy.deepcopy(CONTACT),
    "profile_generation_sha256": "b" * 64,
    "prepared_at": "2026-09-11T00:00:00Z",
}
RESULT: dict[str, object] = {
    "result_schema_version": "carddav-result/1.0",
    "operation_id": "a" * 64,
    "operation": "update",
    "profile": "demo",
    "collection_alias": "contacts",
    "contact_id": "0123456789abcdef",
    "outcome": "applied",
    "remote_write": True,
    "result_revision": '"etag-2"',
    "verified": True,
    "local_cache": "refreshed",
    "generation": "c" * 64,
}
RECORD: dict[str, object] = {
    "record_schema_version": "carddav-record/1.0",
    "profile": "demo",
    "collection_alias": "contacts",
    "contact_id": "0123456789abcdef",
    "revision": '"etag-1"',
    "raw_vcard": VCARD,
    "contact": copy.deepcopy(CONTACT),
}


@pytest.mark.parametrize("document", [OPERATION, RESULT, RECORD])
def test_valid_write_documents_are_accepted(document: dict[str, object]) -> None:
    schemas.validate_write_document(copy.deepcopy(document))


@pytest.mark.parametrize("document", [OPERATION, RESULT, RECORD])
def test_an_unknown_or_missing_key_is_refused_at_every_depth(
    document: dict[str, object]
) -> None:
    extra = copy.deepcopy(document)
    extra["unexpected"] = "value"
    with pytest.raises(ValueError, match="^invalid command$"):
        schemas.validate_write_document(extra)

    for key in document:
        missing = copy.deepcopy(document)
        del missing[key]
        with pytest.raises(ValueError, match="^invalid command$"):
            schemas.validate_write_document(missing)


@pytest.mark.parametrize("mutation", [
    {"operation": "merge"},
    {"operation_id": "not-a-digest"},
    {"contact_id": "NOTHEX0123456789"},
    {"profile": "Demo"},
    {"prepared_at": "2026-02-30T00:00:00Z"},
    {"base_revision": ""},
    {"after_vcard": 3},
    {"operation_schema_version": "carddav-operation/9.9"},
])
def test_malformed_operation_fields_are_refused(mutation: dict[str, object]) -> None:
    document = copy.deepcopy(OPERATION)
    document.update(mutation)
    with pytest.raises(ValueError, match="^invalid command$"):
        schemas.validate_write_document(document)


@pytest.mark.parametrize("mutation", [
    {"outcome": "probably"},
    {"remote_write": "yes"},
    {"verified": False},
    {"local_cache": "warm"},
    {"generation": "short"},
])
def test_malformed_result_fields_are_refused(mutation: dict[str, object]) -> None:
    document = copy.deepcopy(RESULT)
    document.update(mutation)
    with pytest.raises(ValueError, match="^invalid command$"):
        schemas.validate_write_document(document)


def test_a_delete_operation_carries_no_after_state() -> None:
    document = copy.deepcopy(OPERATION)
    document.update({"operation": "delete", "after_vcard": None, "after_contact": None})
    schemas.validate_write_document(document)

    document["after_vcard"] = VCARD
    with pytest.raises(ValueError, match="^invalid command$"):
        schemas.validate_write_document(document)


def test_a_create_operation_carries_no_before_state_or_revision() -> None:
    document = copy.deepcopy(OPERATION)
    document.update({
        "operation": "create", "before_vcard": None, "before_contact": None,
        "href": None, "base_revision": None,
    })
    schemas.validate_write_document(document)

    document["base_revision"] = '"etag-1"'
    with pytest.raises(ValueError, match="^invalid command$"):
        schemas.validate_write_document(document)


def test_an_unknown_document_kind_is_refused() -> None:
    unknown: list[object] = [{}, {"result_schema_version": "carddav-result/9.9"}, [], "text"]
    for document in unknown:
        with pytest.raises(ValueError, match="^invalid command$"):
            schemas.validate_write_document(document)
