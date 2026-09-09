---
name: carddav-contacts
description: Sync, index, and search a CardDAV address book locally.
version: 0.1.0
author: V (jcinis), Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [carddav, contacts, vdirsyncer, vobject, sync]
    related_skills: []
---

# CardDAV Contacts Skill

Planned capability, **not yet available**: read-only, later
approval-gated read/write, access to a standards-compliant CardDAV
address book — for example Radicale, Nextcloud, Baïkal, or Fastmail —
via `vdirsyncer` and `vobject`, with a private local SQLite index for
network-free search. None of this exists in this repository yet. It will
not own Markdown rendering, contact-to-person identity links, or
duplicate-merge decisions — see the repository root `README.md`/`CLAUDE.md`
for the full ownership boundary.

## Status

Scaffold only. There is no working CardDAV, `vdirsyncer`, `vobject`, or
SQLite behavior here, and no command is invokable. The file at
`scripts/carddav_contacts.py` is a stub whose `main()` unconditionally
raises `NotImplementedError`; it is not a functioning entry point. **Do
not invoke it and do not wire this skill into any workflow.**

## When to Use

- Not yet: this skill has no implemented capability. Do not select or
  invoke it for any task today.
- Future intent only, once a versioned release implements the read-only
  command surface: local, network-free lookup of a contact stored in a
  standards-compliant CardDAV address book.
- Never intended for: arbitrary WebDAV file storage, projection/rendering
  into a notes tool, or duplicate-contact merge decisions — those belong to
  other repositories in the three-repository system described in
  `README.md`.

## Prerequisites (planned — not yet actionable)

None of the following is wired up; this lists only the intended future
dependency stance, not something to set up today:

- Python 3.12 managed with `uv`.
- `vdirsyncer` and `vobject`, declared in `pyproject.toml`/`uv.lock`.
- A configured `CARDDAV_*` profile. The configuration contract itself does
  not exist yet — `references/configuration.md` is a placeholder.

## Pitfalls

- There is no sync, index, search, or any other runtime behavior. Every
  document under `references/` is a placeholder, not a specification of
  working behavior.
- Do not add projection/rendering or duplicate-decision logic here; see the
  non-goals list in `CLAUDE.md`.

## Verification

Verification at this stage confirms only that the scaffold is well-formed
— it does not confirm any capability works, because none is implemented:

- This file's YAML frontmatter parses and satisfies the skill-authoring
  contract (`name`/`description`/`version`/`author`/`license`/`platforms`/
  `metadata`).
- `uv run pytest`, `uv run ruff check .`, and `uv run mypy .` pass against
  the placeholder test suite and stub script.
- No functional command exists to verify, and none should be attempted.
