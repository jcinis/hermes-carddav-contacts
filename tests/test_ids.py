"""Frozen UID identity vectors, independent of contact display data."""

import builtins
import runpy
import socket
import subprocess
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).parents[1] / "skills/productivity/carddav-contacts/scripts"


def _ids() -> dict[str, Any]:
    return runpy.run_path(str(SCRIPTS / "ids.py"))


@pytest.mark.parametrize(("uid", "expected"), [
    ("example-uid", "b687b2e8ceca7c40"),
    ("  example-uid\t\n", "b687b2e8ceca7c40"),
    ("EXAMPLE-UID", "b6533070cf089e7c"),
    ("é", "4a99557e4033c353"),
    ("e\u0301", "bf12767b0f2a56b2"),
    ("\u00a0example-uid\u2003", "b687b2e8ceca7c40"),
])
def test_contact_id_frozen_vectors(uid: str, expected: str) -> None:
    assert _ids()["contact_id"](uid) == expected


@pytest.mark.parametrize("uid", [None, 12, b"uid", "", " \t\n", "\ud800"])
def test_invalid_uid_is_content_free(uid: object) -> None:
    with pytest.raises(ValueError, match="^invalid contact UID$"):
        _ids()["contact_id"](uid)


def test_portable_reference_uses_namespace_and_opaque_id() -> None:
    assert _ids()["contact_ref"]("example", "b687b2e8ceca7c40") == (
        "carddav:example:b687b2e8ceca7c40"
    )


@pytest.mark.parametrize(("namespace", "opaque_id"), [
    ("", "b687b2e8ceca7c40"),
    ("Bad", "b687b2e8ceca7c40"),
    ("bad:namespace", "b687b2e8ceca7c40"),
    ("a" * 65, "b687b2e8ceca7c40"),
    (None, "b687b2e8ceca7c40"),
    ("example", "B687B2E8CECA7C40"),
    ("example", "b687b2e8ceca7c40\n"),
    ("example", "raw-uid"),
    ("example", 1),
])
def test_portable_reference_rejects_invalid_parts(namespace: object, opaque_id: object) -> None:
    with pytest.raises(ValueError, match="^invalid contact reference$"):
        _ids()["contact_ref"](namespace, opaque_id)


def test_identity_helpers_do_no_io(monkeypatch: pytest.MonkeyPatch) -> None:
    functions = _ids()

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("identity helper attempted I/O")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    opaque = functions["contact_id"]("example-uid")
    assert functions["contact_ref"]("example", opaque) == "carddav:example:b687b2e8ceca7c40"
