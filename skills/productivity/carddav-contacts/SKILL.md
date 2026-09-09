---
name: carddav-contacts
description: Version supported; setup in progress; contacts unavailable.
version: 0.1.0
author: V (jcinis), Hermes Agent
license: MIT
platforms: [linux, macos]
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

`version --json` is supported: it prints a single JSON object describing
the command schema version, package version, supported source schema
versions, capabilities, and pinned dependency versions — with no
filesystem, network, or subprocess access. `setup` code exists and
writes a profile file, but it is under development and is **not yet a
supported operation**: identifier, server-URL, and `HERMES_HOME` input
validation now exist, but pre-existing-path handling,
idempotence/conflict handling, and concurrency are not implemented yet
(tracer 2c), so its behavior is not guaranteed until those land.
`version` alone (without `--json`) and no arguments exit non-zero. Every
other command (`sync`, `search`, `show`, `snapshot`, `audit`, ...) also
exits non-zero; none is implemented. There is still no working CardDAV,
`vdirsyncer`, `vobject`, or SQLite behavior here. **Do not wire this
skill into any workflow beyond checking `version --json`.**

## When to Use

- Not yet: this skill has no contact-access capability. Do not select or
  invoke it for any contact-related task today.
- Today: only to check the `version --json` compatibility/version metadata
  contract.
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

- There is no sync, index, search, show, snapshot, or audit behavior. Every
  document under `references/` is a placeholder, not a specification of
  working behavior.
- Do not add projection/rendering or duplicate-decision logic here; see the
  non-goals list in `CLAUDE.md`.

## Verification

Verification at this stage confirms the `version --json` contract and
that no unsupported invocation exits zero:

- This file's YAML frontmatter parses and satisfies the skill-authoring
  contract (`name`/`description`/`version`/`author`/`license`/`platforms`/
  `metadata`).
- `uv run pytest`, `uv run ruff check .`, and `uv run mypy .` pass.
- `scripts/carddav_contacts.py version --json` prints one JSON object and
  exits 0 with no stderr; `version` alone, no arguments, and every other
  unimplemented command exit non-zero with no traceback. `setup` exists
  but is not covered by this contract (see Status) and must not be relied
  on.
