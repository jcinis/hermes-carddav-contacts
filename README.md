# hermes-carddav-contacts

A distributable, self-contained Hermes Agent skill for a standards-compliant
CardDAV address book: offline local queries over a synchronized mirror, plus
basic CRUD on individual contacts. It wraps
[`vdirsyncer`](https://vdirsyncer.pimutils.org/) for CardDAV synchronization
and [`vobject`](https://eventable.github.io/vobject/) for vCard parsing, and
adds a private local SQLite index, stable opaque contact IDs, a small
local-only read surface (`status`, `search`, `show`, `snapshot`, `audit`), and
a reviewed, revision-bound write surface (`record`, `prepare-create`,
`prepare-update`, `prepare-delete`, `apply`, `reconcile`) that is also
importable as `hermes_carddav_contacts.api`.

It targets any CardDAV server that speaks the standard address book extension
to WebDAV — for example Radicale, Nextcloud, Baïkal, or Fastmail. It is not a
generic WebDAV file-storage client, and it implements no HTTP/DAV/XML or vCard
grammar of its own: protocol and vCard parsing stay with `vdirsyncer` and
`vobject`.

## What it does today

`setup` creates an immutable local profile, validates private paths, and
serializes cooperating setup processes. Repeating the same settings is a no-op;
changed settings or unsafe existing state are refused. It makes no network
calls and reads no credentials.

`discover --profile NAME` and `sync --profile NAME` use read-only vdirsyncer
transport to maintain a private working mirror. They are the only commands that
open a network connection or read a credential. A successful `sync` parses the
mirror with `vobject`, builds a private SQLite index, and publishes it as an
immutable generation selected by an atomic `current` pointer that also records
the successful sync time; unchanged remote content reuses the existing
generation and only refreshes that time.

`status`, `search`, `show`, `snapshot`, and `audit` (each `--profile NAME
--json`) answer from that generation with no network, no subprocess, and no
credentials. They revalidate the payload and recompute its digest before
printing, report freshness as time since the last successful sync, and fail
closed on damaged state. `search` is a literal case-insensitive substring over
documented text fields. `snapshot` prints the whole validated payload, so a
downstream consumer can build on a versioned, digest-identified source.
`version --json` prints package, schema, capability, and dependency metadata.

`record --profile NAME --id ID --json` reads one contact fresh from the server.
`prepare-create`, `prepare-update`, and `prepare-delete` each read the target
fresh and emit one private, reviewable operation bound to an exact contact and
the revision it carried; `apply --operation ID` performs exactly that one write
and verifies it by reading the record back; `reconcile --operation ID` resolves
an operation whose outcome is unknown.

### Write scope

`capabilities` reports `read_only=false`, `create=true`, `update=true`,
`delete=true`. Delete is an ordinary supported operation.

What that does and does not mean:

- Ordinary local reads stay offline, and routine `discover`/`sync` still use a
  `read_only=true` vdirsyncer storage — a local edit to the working mirror is
  reverted, never uploaded.
- A write only happens inside `apply` or `reconcile`, for exactly one already
  prepared operation, with `If-None-Match: *` on a create and `If-Match` on an
  update or delete. A precondition failure is final and needs a fresh review.
- Every applied write is verified by reading the exact record back; a delete
  requires an authoritative not-found.
- An edit rewrites only the properties it names. Photos, unknown `X-`
  extensions, group prefixes, multi-valued structured names, and the `UID`
  survive untouched, and an edit that cannot be expressed without losing
  supplied data is refused.
- A timeout is an unknown outcome, resolved by `reconcile` reading the server —
  never by replaying the write.
- `audit` still reports conservative duplicate *candidates* only — never a
  merge, a survivor, or a write. Survivor policy, batching, and approval
  workflow belong to a consumer.

See
[`write-safety.md`](skills/productivity/carddav-contacts/references/write-safety.md).

### Using it from Python

A consumer should build on the shared API rather than becoming a second CardDAV
writer:

```python
from hermes_carddav_contacts import api

current = api.read_record("demo", "0123456789abcdef")
operation = api.prepare_update("demo", current["contact_id"], {
    "change_schema_version": api.CHANGE_SCHEMA_VERSION,
    "set": {"display": "Example Person"},
    "clear": ["birthday"],
    "replace": {"emails": [{"value": "person@example.invalid", "types": ["work"],
                            "label": None, "preference": None}]},
})
result = api.apply_operation("demo", operation["operation_id"])
```

These are the same objects the console command calls. `api` also exposes
`read_collection` and `prepare_replace`, a validated lossless whole-card
replacement for a consumer that composed the complete card itself.

Nothing deployment-specific lives in this repository: no server hostnames,
address-book names, credential values, or contact data. Documentation and tests
use placeholders such as `https://carddav.example.invalid/`.

## Installation

Install the built wheel to get the `hermes-carddav-contacts` console command on
`PATH`. It depends on exactly `vdirsyncer==0.21.0` and `vobject==0.9.9`;
`Radicale`, `pytest`, `ruff`, and `mypy` are development-only and are never
installed at runtime. `khard` is an optional manual-diagnostics extra, never
invoked by any command.

```sh
uv build                      # writes dist/*.whl and dist/*.tar.gz
uv pip install dist/hermes_carddav_contacts-0.2.0-py3-none-any.whl
hermes-carddav-contacts version --json
```

The installed command needs no checkout, no `PYTHONPATH`, and no particular
working directory. All runtime state (profiles, mirrors, indexes, generations)
lives under `$HERMES_HOME/carddav-contacts/` — outside this repository and
outside the installed package. Nothing is ever written into either tree.

Running from a source checkout stays supported and runs the same code:

```sh
uv run python skills/productivity/carddav-contacts/scripts/carddav_contacts.py version --json
```

## Usage

```sh
hermes-carddav-contacts setup --profile demo --namespace example \
    --server-url https://carddav.example.invalid/ --collection contacts
export CARDDAV_USERNAME=... CARDDAV_PASSWORD=...   # from your secret manager
hermes-carddav-contacts discover --profile demo
hermes-carddav-contacts sync --profile demo
hermes-carddav-contacts search --profile demo --query 'example' --json
hermes-carddav-contacts show --profile demo --id 0123456789abcdef --json

# Changing a contact: read it fresh, prepare the change, review it, apply it.
hermes-carddav-contacts record --profile demo --id 0123456789abcdef --json
hermes-carddav-contacts prepare-update --profile demo --id 0123456789abcdef \
    --changes '{"change_schema_version":"carddav-change/1.0","set":{"display":"Example Person"},"clear":[],"replace":{}}' --json
hermes-carddav-contacts apply --profile demo --operation "$OPERATION_ID" --json
hermes-carddav-contacts prepare-delete --profile demo --id 0123456789abcdef --json
```

`$OPERATION_ID` is the `operation_id` printed by the matching `prepare-*`
command. Writes need the same credentials as `discover`/`sync`.

Credentials are read from the process environment at operation time only, never
from CLI arguments, a URL, or persisted config, and are never logged or echoed.

Full documentation lives with the skill:

- [`SKILL.md`](skills/productivity/carddav-contacts/SKILL.md) — the agent-facing
  skill description, command walkthrough, and pitfalls.
- [`references/configuration.md`](skills/productivity/carddav-contacts/references/configuration.md)
  — `CARDDAV_*` environment variables and credential precedence, profile
  layout, generations and the `current` pointer, freshness, the exact JSON
  envelopes, and the fixed error table.
- [`references/data-model.md`](skills/productivity/carddav-contacts/references/data-model.md)
  — contact IDs, the closed `carddav-source/1.0` payload, canonical bytes and
  the generation digest, the vCard adapter, and the SQLite index schema.
- [`references/write-safety.md`](skills/productivity/carddav-contacts/references/write-safety.md)
  — preconditions, read-back verification, lossless preservation, unknown
  outcomes, reconciliation, and cache invalidation.

Pure contact-ID, source-schema, and query helpers are implemented separately
from the CLI. They validate contact payloads, produce deterministic source
hashes, and compute search and duplicate-candidate results without any I/O.

## Platform support

Linux and macOS only: the code uses POSIX primitives (`fcntl.flock`,
`/usr/bin/printenv`). Only Linux is verified by this repository's test suite;
macOS is unverified. Python 3.12 or newer is required.

Automation scheduling (for example an hourly sync job), credential mapping, and
operational alerting belong to whatever infrastructure installs and runs this
skill, not to this repository.

## Development

This project uses [`uv`](https://docs.astral.sh/uv/) for dependency and
environment management.

```sh
uv sync
uv run pytest                                              # unit suite
uv run --group integration pytest -m integration -q        # disposable localhost Radicale
uv run --group integration pytest -o addopts='' -m packaging -q  # build + fresh installed venv
uv run ruff check .
uv run mypy .
uv build
```

The `packaging` suite is opt-in because each run performs a real `uv build` and
creates a fresh virtualenv in a temporary directory outside this checkout. It
installs the built wheel and drives the installed console command through
`version`, `setup`, read-only `discover`/`sync` against a disposable localhost
Radicale recorder seeded with synthetic contacts, a full create/update/delete
cycle through both the installed console command and the installed
`hermes_carddav_contacts.api`, and every local query with that server already
shut down. It also inspects both artifacts for inclusion
boundaries, deployment-specific hosts, and credential-shaped literals — a
bounded pattern scan, not a universal secret scanner.

Tests never reach a real CardDAV server and use synthetic fixtures only.
Passing against disposable localhost Radicale is not evidence about any
particular real server: Radicale re-serializes what it stores, and other
servers normalize differently.

### Repository layout

```text
hermes-carddav-contacts/
  README.md
  LICENSE
  pyproject.toml
  uv.lock
  hermes_carddav_contacts/
    __init__.py            # console-script launcher only
    api.py                 # the supported Python CRUD API
  skills/
    productivity/
      carddav-contacts/
        SKILL.md
        scripts/
          carddav_contacts.py
          generations.py
          ids.py
          index.py
          profiles.py
          queries.py
          reads.py
          records.py
          schemas.py
          transport.py
          vcards.py
          writes.py
        references/
          configuration.md
          data-model.md
          write-safety.md
  tests/
    fixtures/
    test_generations.py
    test_index.py
    test_setup.py
    test_sync.py
    test_transport_integration.py
    ...
```

## License

MIT — see [`LICENSE`](./LICENSE).
