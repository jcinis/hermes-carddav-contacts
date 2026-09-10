# hermes-carddav-contacts

A reusable, distributable Hermes Agent skill for read-only (and later,
approval-gated read/write) access to a standards-compliant CardDAV address
book. It wraps [`vdirsyncer`](https://vdirsyncer.pimutils.org/) for
CardDAV synchronization and [`vobject`](https://eventable.github.io/vobject/)
for vCard parsing, and adds a private local SQLite index, stable opaque
contact IDs, and a small local-only command surface (`status`, `search`,
`show`, `snapshot`, `audit`, ...).

This repository targets any CardDAV server that speaks the standard address
book extension to WebDAV — for example Radicale, Nextcloud, Baïkal, or
Fastmail. It does not implement a generic WebDAV file-storage client.

## Status

Early implementation. `version --json` prints package and dependency metadata.
`setup` creates an immutable local profile, validates private paths, and
serializes cooperating setup processes. Repeating the same settings is a
no-op; changed settings or unsafe existing state are refused. It makes no
network calls and does not read credentials.

`discover --profile NAME` and `sync --profile NAME` use read-only vdirsyncer
transport to maintain a private working mirror. They require explicit runtime
credentials and never write to the remote address book. A successful `sync`
parses the mirror with `vobject`, builds a private SQLite index, and publishes
it as an immutable generation selected by an atomic `current` pointer that also
records the successful sync time; unchanged remote content reuses the existing
generation and only refreshes that time.

`status`, `search`, `show`, `snapshot`, and `audit` (each `--profile NAME
--json`) answer from that generation with no network, no subprocess, and no
credentials. They revalidate the payload and recompute its digest before
printing, report freshness as time since the last successful sync, and fail
closed on damaged state. `search` is a literal case-insensitive substring over
documented text fields, and `audit` reports conservative duplicate *candidates*
only — never a merge, a survivor, or a write. Approval-gated writes (v0.2) and
the cleanup-only deletion ABI (v0.3) are not implemented.

Pure contact-ID, source-schema, and query helpers are implemented separately
from the CLI. They validate contact payloads, produce deterministic source
hashes, and compute search and duplicate-candidate results without any I/O.
See
[`data-model.md`](skills/productivity/carddav-contacts/references/data-model.md).

## Repository ownership boundary

This project is one of three independently owned repositories that together
make up a CardDAV-backed contacts workflow. Each has its own lifecycle and
must not be collapsed into a shared monolith:

1. **This repository (`hermes-carddav-contacts`)** — the reusable, generic
   CardDAV capability. It owns CardDAV discovery/sync, vCard validation,
   stable contact IDs, the local SQLite index, and a local-only
   `status`/`search`/`show`/`snapshot`/`audit` command surface. Later
   releases add approval-gated create/update, and eventually a narrow,
   disabled-by-default deletion ABI intended only for consumption by a
   separate cleanup project.
2. **A private "second brain" / notes repository** — owns any normalized,
   human-curated projection of contact data (for example a generated
   directory of names, organizations, and notes), rendering into a
   note-taking tool, and any explicit contact-to-person identity linking.
   That repository consumes this repository's versioned source snapshot; it
   is not implemented here.
3. **A private, one-time cleanup/migration project** — owns duplicate-review
   ledgers, survivor policy, batching, and approval workflow for merging
   duplicate contact records. It consumes this repository's generic contact
   interface and a private reviewed ledger, but is not a permanent system
   and is not implemented here.

Automation scheduling (for example an hourly sync job), credential mapping,
and operational alerting belong to whatever infrastructure project installs
and runs this skill — not to this repository.

## Explicit non-goals

- No custom HTTP/DAV/XML client and no custom vCard parser. Protocol and
  vCard grammar are owned by `vdirsyncer` and `vobject`.
- No Markdown rendering, note-taking-tool integration, git-carried
  projection, or person-page/identity-link logic. That belongs to a
  downstream consumer.
- No duplicate-contact decision ledgers, survivor policy, or approval
  batching. That belongs to a separate cleanup project.
- No routine, user-facing delete command. A later release may add a narrow,
  disabled-by-default deletion integration point consumed only by an
  explicit cleanup project — never a generic "delete a contact" command.
- No native-export/backup automation.
- No support for arbitrary WebDAV storage as if it were an address book;
  CardDAV only.
- No credentials, contact data, or deployment-specific defaults committed to
  this repository. Configuration uses generic environment variable names and
  example values only (see `skills/productivity/carddav-contacts/references/`).

## Installation

Install the built wheel to get the `hermes-carddav-contacts` console command
on `PATH`. It depends on exactly `vdirsyncer==0.21.0` and `vobject==0.9.9`;
`Radicale`, `pytest`, `ruff`, and `mypy` are development-only and are never
installed at runtime. `khard` is an optional manual-diagnostics extra.

```sh
uv build                      # writes dist/*.whl and dist/*.tar.gz
uv pip install dist/hermes_carddav_contacts-0.1.0-py3-none-any.whl
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

Linux and macOS only (POSIX `fcntl.flock` and `/usr/bin/printenv`). Only Linux
is verified by this repository's test suite; macOS is unverified.

## Repository layout

```text
hermes-carddav-contacts/
  README.md
  LICENSE
  pyproject.toml
  uv.lock
  hermes_carddav_contacts/
    __init__.py            # console-script launcher only
  skills/
    productivity/
      carddav-contacts/
        SKILL.md
        scripts/
          carddav_contacts.py
          generations.py
          ids.py
          index.py
          queries.py
          reads.py
          schemas.py
          transport.py
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

## Configuration

Runtime configuration uses generic `CARDDAV_*` environment variables and a
named profile — see
`skills/productivity/carddav-contacts/references/configuration.md`. No
specific server hostname, address-book name, or credential value is ever
committed to this repository; examples in documentation and tests use
placeholder values such as `https://carddav.example.invalid/`.

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

The `packaging` suite is opt-in because each run performs a real `uv build`
and creates a fresh virtualenv in a temporary directory outside this checkout.
It installs the built wheel and drives the installed console command through
`version`, `setup`, read-only `discover`/`sync` against a disposable localhost
Radicale recorder seeded with synthetic contacts, and every local query with
that server already shut down. It also inspects both artifacts for inclusion
boundaries, deployment-specific hosts, and credential-shaped literals — a
bounded pattern scan, not a universal secret scanner.

## Roadmap

1. **Read-only skill (v0.1):** `version --json`, local `setup`, discovery,
   working-mirror sync, the SQLite index with immutable generations, and the
   local `status`, `search`, `show`, `snapshot`, and `audit` command surface —
   implemented.
2. **Approval-gated writes (v0.2):** plan/apply create and update, with
   pre-sync, conflict detection, and independent read-back verification.
3. **Cleanup-only deletion ABI (v0.3):** a narrow, disabled-by-default
   integration point for the separate cleanup project. Ordinary profiles
   never enable it.

## License

MIT — see [`LICENSE`](./LICENSE).
