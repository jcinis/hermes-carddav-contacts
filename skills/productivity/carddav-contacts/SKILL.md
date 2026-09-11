---
name: carddav-contacts
description: Read-only CardDAV mirror and local contact queries.
version: 0.1.0
author: jcinis (V), Hermes Agent
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [carddav, contacts, vdirsyncer, vobject, sync]
    related_skills: []
---

# CardDAV Contacts Skill

Read-only discovery and working-mirror synchronization for a standards-compliant
CardDAV address book via `vdirsyncer`, with `vobject` parsing into an immutable,
indexed local generation, plus network-free `status`, `search`, `show`,
`snapshot`, and `audit` queries over that generation. It is read-only: it never
writes to the remote address book, and it makes no merge or identity decisions.

## Status

`version --json` is supported: it prints a single JSON object describing
the command schema version, package version, supported source schema
versions, capabilities, and pinned dependency versions — with no
filesystem, network, or subprocess access. `setup` creates an immutable
local profile with validated identifiers, URL, and private filesystem state.
Identical settings are a no-op; changed settings are refused. A profile lock
serializes cooperating setup processes. No credentials or network access are
needed. Existing unsafe paths or malformed profiles are refused, not repaired.
`discover --profile NAME` and `sync --profile NAME` use the configured
read-only remote and private working mirror. Both are silent on success;
neither has a JSON mode. A successful `sync` also parses the mirror, builds a
SQLite index in private staging, and publishes it as an immutable generation
behind an atomic `current` pointer that also records the successful sync time.
`status`, `search`, `show`, `snapshot`, and `audit` read that generation
locally — no network, no subprocess, no credentials — and each requires
`--json`. `version` alone, no arguments, and any other invocation exit
non-zero. No command in this release creates, updates, merges, or deletes a
contact.

## When to Use

- Look up a person's contact details locally, without touching the network:
  `search` by a literal substring, then `show` the full record by its opaque ID.
- Check how current the local copy is (`status`), export the whole validated
  payload for a downstream consumer (`snapshot`), or list conservative
  duplicate candidates for a human to review (`audit`).
- Refresh the local copy: `discover` once, then `sync` (the only commands that
  need credentials or reach the server).
- Never intended for: arbitrary WebDAV file storage, rendering contacts into
  another tool's format, or making duplicate-contact merge decisions. A
  consumer builds those on the versioned `snapshot` payload.

## Installation and invocation

Python 3.12+ on Linux or macOS. Both forms below run the same code and are
equally supported; every later example shows the installed form.

**Installed (recommended).** Installing the wheel puts one console command on
`PATH`; it pulls in exactly `vdirsyncer==0.21.0` and `vobject==0.9.9`, needs no
checkout, no `PYTHONPATH`, and no particular working directory:

```python
terminal(command="uv pip install hermes_carddav_contacts-0.1.0-py3-none-any.whl")
terminal(command="hermes-carddav-contacts version --json")
```

**From a source checkout.** Run the entry-point script directly from the
repository root:

```python
terminal(command="uv run python skills/productivity/carddav-contacts/scripts/carddav_contacts.py version --json")
```

The `platforms` frontmatter claims Linux and macOS because the code uses only
POSIX primitives (`fcntl.flock`, `/usr/bin/printenv`), but only Linux is
verified by this repository's test suite; macOS is unverified.

`khard` is an optional extra for manual diagnostics only. No command in this
skill invokes it, and it is never required at runtime.

## Prerequisites and local setup

All runtime state lives under `$HERMES_HOME/carddav-contacts/`, outside any
checkout and outside the installed package. Create a profile first:

```python
terminal(command="hermes-carddav-contacts setup --profile demo --namespace example --server-url https://carddav.example.invalid/ --collection contacts")
```

Use the actual collection name, repeated `--collection` flags for multiple
collections, and lowercase identifiers matching `[a-z0-9][a-z0-9._-]{0,63}`.
State lives under `$HERMES_HOME/carddav-contacts/profiles/<profile>/`, or
`~/.hermes/carddav-contacts/profiles/<profile>/` when unset. Setup success is
silent; failure returns exit 2 and one fixed error line. Profiles cannot be
updated in v0.1. Never put credentials in the server URL.

For discovery/sync, export a complete non-empty `CARDDAV_USERNAME` and
`CARDDAV_PASSWORD` pair from the operator's secret manager. A complete DAV pair
is a compatibility fallback only if neither CARDDAV variable is set. Partial
pairs fail; credentials never go in arguments or persisted config. See
`references/configuration.md` for the complete precedence and failure contract.

`discover` and `sync` are the only commands that open a network connection or
read a credential. They talk to the configured server read-only and never write
to the remote address book:

```python
terminal(command="hermes-carddav-contacts discover --profile demo")
terminal(command="hermes-carddav-contacts sync --profile demo")
```

Run discovery explicitly before the first sync. Exact collection names are
required; missing remote collections fail rather than being created.

## Reading contacts locally

Every read below opens no socket, starts no subprocess, and reads no
credential; each prints exactly one JSON object:

```python
terminal(command="hermes-carddav-contacts status --profile demo --json")
terminal(command="hermes-carddav-contacts search --profile demo --query 'example' --json")
terminal(command="hermes-carddav-contacts show --profile demo --id 0123456789abcdef --json")
terminal(command="hermes-carddav-contacts snapshot --profile demo --json")
terminal(command="hermes-carddav-contacts audit --profile demo --json")
```

`--query` is a literal case-insensitive substring, never a pattern: `%`, `_`,
`*`, and quotes match themselves. `--id` is the 16-hex `contact_id` from a
search or snapshot result; a well-formed but absent ID is `error: contact not
found`. `status`, `search`, `show`, `snapshot`, and `audit` report `synced_at`
and a `freshness` object measuring time since the last successful sync against
the profile's 3600-second cadence. See `references/configuration.md` for the
exact envelopes, freshness rule, and error table.

## Access and permissions

- **Filesystem.** Only `$HERMES_HOME/carddav-contacts/` (default
  `~/.hermes/carddav-contacts/`), and within it only the named profile's own
  directory. Directories are created `0700` and files `0600`, owned by the
  invoking user; the `$HERMES_HOME` root above them is operator-owned and keeps
  its own mode, checked only for type and owner. No command reads or writes
  another profile, the installed package, or any source checkout.
- **Network.** Only `discover` and `sync`, only to the profile's configured
  `server_url`, and only with read-only DAV methods. Every other command is
  offline.
- **Credentials.** Read at operation time from the environment only: a complete
  `CARDDAV_USERNAME` + `CARDDAV_PASSWORD` pair, else a complete
  `DAV_USERNAME` + `DAV_PASSWORD` pair when neither `CARDDAV_*` variable is
  set. A partial pair fails closed. Nothing is persisted, logged, or echoed.
- **No writes.** `capabilities` is fixed at `read_only=true`,
  `create_update=false`, `cleanup_delete=false`. There is no create, update,
  merge, or delete command in this release — say so rather than attempting one.

## Pitfalls

- Local reads never contact the server, so they answer from the last
  successful sync. Check `freshness.stale` before treating an answer as
  current; `clock_skew` means the local clock moved backwards and the answer is
  reported stale rather than fresh.
- `audit` reports duplicate *candidates* only — shared email, phone digits, or
  display name. It never merges, never picks a survivor, and never writes.
  Groups are not merged transitively, so one contact can appear in several.
  Reviewing a group is a human's call, not this skill's.
- A generation published before this release carries no sync time; every read
  refuses it until one more successful `sync` refreshes the pointer.
- A remote contact without a usable `UID`, or two contacts sharing one
  `contact_id`, fails the whole sync rather than being dropped; the previous
  generation and `current` stay untouched.
- Every component of every `NICKNAME` property becomes an alias, including
  comma-separated lists; `\,` stays a literal comma in one alias.
- The working mirror is mutable derivative state, not a snapshot. A failed
  sync can leave it partial; never present it as a complete contact directory.
- Remote storage is always read-only. Local edits are reverted, not uploaded.
- Keep both native sync passes: vdirsyncer 0.21.0 reverts a local edit on the
  following pass, not the first. Wrapper success requires both to finish.
- The transport uses POSIX locking and `/usr/bin/printenv`: Linux/macOS only.
- Do not add rendering/projection or duplicate-decision logic here; see the
  non-goals list in `CLAUDE.md`.

## Verification

Verification covers metadata, local setup, read-only transport, and refusal
of unsupported invocations:

- This file's YAML frontmatter parses and satisfies the skill-authoring
  contract (`name`/`description`/`version`/`author`/`license`/`platforms`/
  `metadata`).
- `uv run pytest`, `uv run ruff check .`, and `uv run mypy .` pass.
- `uv run --group integration pytest -o addopts='' -m packaging -q` builds a
  real wheel and sdist, installs the wheel into a fresh virtualenv outside
  the checkout, and drives the installed `hermes-carddav-contacts` command
  through `version`, `setup`, read-only `discover`/`sync` against a
  disposable localhost Radicale recorder, and every local query with that
  server already shut down.
- `scripts/carddav_contacts.py version --json` prints one JSON object and
  exits 0 with no stderr; `version` alone, no arguments, and every other
  unimplemented command exit non-zero with no traceback.
- Read-command tests cover the exact envelopes, literal-substring matching,
  conservative duplicate candidates, freshness and clock-skew edges, and
  fail-closed refusal of damaged pointers, manifests, indexes, and payloads —
  with the network and subprocess seams blocked and credentials poisoned.
- Setup tests cover canonical private state, no network/subprocess access,
  repeat/conflict behavior, unsafe-state refusal, and real process locking.
- Index and generation tests cover vCard parsing, the SQLite index, immutable
  publication, pointer swaps, profile-binding refusal, `status` before any
  generation, and staging cleanup with a retained `current` on failure.
- `uv run --group integration pytest -m integration -q` exercises real
  vdirsyncer against disposable localhost Radicale, records HTTP methods, and
  refuses all remote mutation methods during CLI operations.
