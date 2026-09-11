"""Shared synthetic-profile helpers for the local read-command tests."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType

SCRIPTS = (
    Path(__file__).parent.parent
    / "skills"
    / "productivity"
    / "carddav-contacts"
    / "scripts"
)
ENTRYPOINT = SCRIPTS / "carddav_contacts.py"

SYNCED_AT_EPOCH = 1788998400  # 2026-09-10T00:00:00Z
SYNCED_AT = "2026-09-10T00:00:00Z"
ENVIRONMENT_KEYS = (
    "HERMES_HOME",
    "CARDDAV_USERNAME",
    "CARDDAV_PASSWORD",
    "DAV_USERNAME",
    "DAV_PASSWORD",
)


def load(name: str) -> ModuleType:
    """Load one skill script as an isolated module."""
    path = ENTRYPOINT if name == "cli" else SCRIPTS / f"{name}.py"
    module_name = f"carddav_reads_{name}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before execution so module-level `dataclass`/`NamedTuple`
    # definitions can resolve their own module, exactly as a normal import does.
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def card(uid: str, name: str, *properties: str) -> str:
    """Render one synthetic vCard."""
    lines = ["BEGIN:VCARD", "VERSION:3.0", f"UID:{uid}", f"FN:{name}", *properties, "END:VCARD"]
    return "\r\n".join(lines) + "\r\n"


def setup_profile(
    cli: ModuleType, home: Path, profile: str = "demo", collection: str = "contacts-a"
) -> Path:
    """Create a real profile through the CLI and return its private directory."""
    os.environ["HERMES_HOME"] = str(home)
    assert (
        cli.main(
            [
                "setup",
                "--profile",
                profile,
                "--namespace",
                "example",
                "--server-url",
                "https://carddav.example.invalid/",
                "--collection",
                collection,
            ]
        )
        == 0
    )
    return home / "carddav-contacts" / "profiles" / profile


def write_mirror(profile_dir: Path, cards: dict[str, str], collection: str = "contacts-a") -> None:
    """Fill the private working mirror with synthetic vCards."""
    directory = profile_dir / "runtime" / "mirror" / collection
    for level in (directory.parent.parent, directory.parent, directory):
        if not level.exists():
            level.mkdir(mode=0o700)
    for name, text in cards.items():
        path = directory / name
        path.write_text(text, encoding="utf-8")
        path.chmod(0o600)


def publish(profile_dir: Path, now: float = SYNCED_AT_EPOCH) -> str:
    """Publish a real generation from the mirror at an injected sync time."""
    generations = load("generations")
    profile_bytes = (profile_dir / "profile.json").read_bytes()
    profile = json.loads(profile_bytes)
    generation: str = generations.publish(profile_dir, profile, profile_bytes, now=now)
    return generation
