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

Early scaffolding. `version --json` is supported and prints package and
dependency metadata. `setup` code exists but is under development and is
not yet a supported operation — pre-existing path checks and
idempotence/conflict handling are not implemented yet. `version` alone and
all contact operations (sync, search, show, snapshot, audit) remain
unavailable — see the [roadmap](#roadmap) below.

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

## Repository layout

```text
hermes-carddav-contacts/
  README.md
  LICENSE
  pyproject.toml
  uv.lock
  skills/
    productivity/
      carddav-contacts/
        SKILL.md
        scripts/
          carddav_contacts.py
        references/
          configuration.md
          data-model.md
          write-safety.md
  tests/
    fixtures/
    test_config.py
    test_sync.py
    test_index.py
    test_search.py
    test_show.py
    test_audit.py
    test_real_tools.py
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
uv run pytest
uv run ruff check .
uv run mypy .
```

## Roadmap

1. **Read-only skill (v0.1):** `version --json` metadata command (implemented);
   discovery, sync, local index, `status`, `search`, `show`, `snapshot`,
   `audit` (not yet implemented).
2. **Approval-gated writes (v0.2):** plan/apply create and update, with
   pre-sync, conflict detection, and independent read-back verification.
3. **Cleanup-only deletion ABI (v0.3):** a narrow, disabled-by-default
   integration point for the separate cleanup project. Ordinary profiles
   never enable it.

## License

MIT — see [`LICENSE`](./LICENSE).
