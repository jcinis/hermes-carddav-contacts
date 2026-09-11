---
name: carddav-contacts
description: CardDAV contact mirror, local queries, and basic CRUD.
version: 0.2.0
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
`snapshot`, and `audit` queries over that generation — and basic CRUD: create,
read, update, and delete one explicitly identified contact through a reviewed,
revision-bound operation. Ordinary reads stay offline, routine sync stays
read-only, and the skill still makes no merge or identity decisions.

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
non-zero.

`record` reads one contact fresh from the server. `prepare-create`,
`prepare-update`, and `prepare-delete` each read the target fresh and write one
private, reviewable operation bound to an exact record and the revision it
carried; `apply --operation ID` performs exactly that one write with a
conditional precondition and verifies it by reading the record back;
`reconcile --operation ID` resolves an operation whose outcome is unknown. The
same operations are importable from `hermes_carddav_contacts.api`. No read,
no `sync`, and no `audit` mutates anything. This skill still makes no merge,
survivor, or identity decision.

## When to Use

- Look up a person's contact details locally, without touching the network:
  `search` by a literal substring, then `show` the full record by its opaque ID.
- Check how current the local copy is (`status`), export the whole validated
  payload for a downstream consumer (`snapshot`), or list conservative
  duplicate candidates for a human to review (`audit`).
- Refresh the local copy: `discover` once, then `sync`.
- Add, correct, or remove one specific contact: read it fresh with `record`,
  `prepare-*` the change, show the operation to whoever is deciding, then
  `apply` it.
- Never intended for: arbitrary WebDAV file storage, rendering contacts into
  another tool's format, or making duplicate-contact merge decisions. A
  consumer builds those on the versioned `snapshot` payload and on
  `hermes_carddav_contacts.api`.

## Installation and invocation

Python 3.12+ on Linux or macOS. Both forms below run the same code and are
equally supported; every later example shows the installed form.

**Installed (recommended).** Installing the wheel puts one console command on
`PATH`; it pulls in exactly `vdirsyncer==0.21.0` and `vobject==0.9.9`, needs no
checkout, no `PYTHONPATH`, and no particular working directory:

```python
terminal(command="uv pip install hermes_carddav_contacts-0.2.0-py3-none-any.whl")
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
updated. Never put credentials in the server URL. A profile written by v0.1
(`carddav-profile/1.0`) keeps working untouched and is never rewritten in
place.

For discovery/sync, export a complete non-empty `CARDDAV_USERNAME` and
`CARDDAV_PASSWORD` pair from the operator's secret manager. A complete DAV pair
is a compatibility fallback only if neither CARDDAV variable is set. Partial
pairs fail; credentials never go in arguments or persisted config. See
`references/configuration.md` for the complete precedence and failure contract.

`discover` and `sync` talk to the configured server read-only and never write
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

Each also reports `cache_invalidated`: `true` means a remote write succeeded
but the local generation has not been refreshed yet, so the answer may be out
of date. Run `sync` to clear it.

`--query` is a literal case-insensitive substring, never a pattern: `%`, `_`,
`*`, and quotes match themselves. `--id` is the 16-hex `contact_id` from a
search or snapshot result; a well-formed but absent ID is `error: contact not
found`. `status`, `search`, `show`, `snapshot`, and `audit` report `synced_at`
and a `freshness` object measuring time since the last successful sync against
the profile's 3600-second cadence. See `references/configuration.md` for the
exact envelopes, freshness rule, and error table.

## Changing contacts

Writes need credentials, exactly like `discover`/`sync`. Every change is
prepared first, reviewed, then applied by its own digest:

```python
terminal(command="hermes-carddav-contacts record --profile demo --id 0123456789abcdef --json")
terminal(command="""hermes-carddav-contacts prepare-create --profile demo --collection contacts --changes '{"change_schema_version":"carddav-change/1.0","set":{"display":"Example Person"},"clear":[],"replace":{"emails":[{"value":"person@example.invalid","types":["work"],"label":null,"preference":null}]}}' --json""")
terminal(command="hermes-carddav-contacts apply --profile demo --operation <operation_id> --json")
terminal(command="""hermes-carddav-contacts prepare-update --profile demo --id 0123456789abcdef --changes '{"change_schema_version":"carddav-change/1.0","set":{"display":"Example Person"},"clear":["birthday"],"replace":{}}' --json""")
terminal(command="hermes-carddav-contacts prepare-delete --profile demo --id 0123456789abcdef --json")
terminal(command="hermes-carddav-contacts reconcile --profile demo --operation <operation_id> --json")
```

A change document names only what changes: `set` for scalar fields
(`display`, `prefix`, `given`, `additional`, `family`, `suffix`, `birthday`),
`clear` to remove a field, and `replace` to rewrite one whole multi-valued
field (`aliases`, `organizations`, `titles`, `emails`, `phones`, `urls`,
`addresses`, `notes`). Anything omitted is unchanged. Read the current entries
with `record` first, then supply the whole field — replacing keeps whatever
types, preferences, and values you pass and nothing else.

The same operations are importable, which is how a consumer should build on
this rather than becoming a second CardDAV writer:

```python
from hermes_carddav_contacts import api

operation = api.prepare_update("demo", "0123456789abcdef", changes)
result = api.apply_operation("demo", operation["operation_id"])
```

`api` also exposes `read_record`, `read_collection`, and `prepare_replace`, a
validated lossless whole-card replacement for a consumer that composed the
complete card itself. See `references/write-safety.md` for preconditions,
verification, unknown outcomes, and cache invalidation, and
`references/configuration.md` for the exact grammar and error table.

## Access and permissions

- **Filesystem.** Only `$HERMES_HOME/carddav-contacts/` (default
  `~/.hermes/carddav-contacts/`), and within it only the named profile's own
  directory. Directories are created `0700` and files `0600`, owned by the
  invoking user; the `$HERMES_HOME` root above them is operator-owned and keeps
  its own mode, checked only for type and owner. No command reads or writes
  another profile, the installed package, or any source checkout.
- **Network.** Only `discover`, `sync`, `record`, the `prepare-*` commands,
  `apply`, and `reconcile`, and only to a collection the profile's configured
  `server_url` itself advertised, named by the profile's own
  `collection_allowlist`. `discover` and `sync` use read-only DAV methods; a
  write happens only inside `apply` or `reconcile`, for exactly one prepared
  operation. `status`, `search`, `show`, `snapshot`, and `audit` are offline.
  A caller can never supply a remote URL.
- **Credentials.** Read at operation time from the environment only: a complete
  `CARDDAV_USERNAME` + `CARDDAV_PASSWORD` pair, else a complete
  `DAV_USERNAME` + `DAV_PASSWORD` pair when neither `CARDDAV_*` variable is
  set. A partial pair fails closed. Nothing is persisted, logged, or echoed.
- **Writes.** `capabilities` reports `read_only=false`, `create=true`,
  `update=true`, `delete=true`. Every write is a separately prepared and
  applied operation bound to one exact contact and one exact revision, with a
  no-overwrite or `If-Match` precondition and a read-back check. There is still
  no merge command and no bulk or wildcard operation — say so rather than
  improvising one.

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
- The sync storage is always read-only: editing a mirror file changes nothing
  remotely and is reverted on the next sync. Writes go through `prepare-*` and
  `apply`, never through the mirror.
- A `revision conflict` means the record changed between review and
  application. Prepare the change again and look at the current record; never
  loop on apply.
- An `unknown outcome` is neither a success nor a failure. Run `reconcile`
  before doing anything else — retrying the write blindly can duplicate a
  contact or delete one that was just recreated.
- After a successful write the local cache is refreshed; if that refresh fails
  the write still happened and every read reports `cache_invalidated: true`.
  Deleting the last contact in a collection always lands in this state, because
  pinned vdirsyncer refuses to sync a newly emptied storage.
- A server that re-serializes what it stores can normalize a card no matter
  what a client sends. Read-back verification reports such a loss for any field
  this skill models rather than claiming success.
- Keep both native sync passes: vdirsyncer 0.21.0 reverts a local edit on the
  following pass, not the first. Wrapper success requires both to finish.
- The transport uses POSIX locking and `/usr/bin/printenv`: Linux/macOS only.
- Do not add rendering/projection or duplicate-decision logic here; see the
  non-goals list in `CLAUDE.md`.

## Verification

Verification covers metadata, local setup, read-only transport, conditional
writes, and refusal of unsupported invocations:

- This file's YAML frontmatter parses and satisfies the skill-authoring
  contract (`name`/`description`/`version`/`author`/`license`/`platforms`/
  `metadata`).
- `uv run pytest`, `uv run ruff check .`, and `uv run mypy .` pass.
- `uv run --group integration pytest -o addopts='' -m packaging -q` builds a
  real wheel and sdist, installs the wheel into a fresh virtualenv outside
  the checkout, and drives the installed `hermes-carddav-contacts` command
  through `version`, `setup`, read-only `discover`/`sync` against a
  disposable localhost Radicale recorder, a full create/update/delete cycle
  through both the installed console command and the installed
  `hermes_carddav_contacts.api`, and every local query with that server
  already shut down.
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
- Write tests cover the change-document contract, lossless preservation of
  untouched properties (photos, unknown extensions, multi-valued structured
  names), preview/revision binding, stale preconditions, duplicate-apply
  refusal, unknown-outcome reconciliation, failed-refresh stale-cache
  reporting, and the fixed content-free error lines.
- `uv run --group integration pytest -m integration -q` exercises real
  vdirsyncer against disposable localhost Radicale: read-only `discover`/`sync`
  with every remote mutation method refused, and real conditional
  create/update/delete with stale `If-Match` preconditions failing closed.
  Localhost Radicale is not evidence of any particular real server's
  behaviour.
