# CLAUDE.md — hermes-carddav-contacts

Project-specific guidance for working in this repository. This is a
standalone, distributable CardDAV contacts skill: it owns CardDAV
discovery/sync, vCard validation, stable contact IDs, the local SQLite index,
a local-only read command surface, and basic per-contact CRUD behind a
reviewed, revision-bound operation. Keep it generic and independently
useful — it must stay usable by any consumer, with no assumptions about who
installs it or what they do with the data.

## Non-goals (do not implement here)

- Custom HTTP/DAV/XML client or custom vCard parsing — use `vdirsyncer` and
  `vobject`.
- Rendering or projection of contact data into another tool's format, and any
  contact-to-person identity linking. Consumers build that on the versioned
  `snapshot` payload.
- Duplicate-contact decision ledgers, survivor policy, or merge approval
  batching. `audit` reports candidates only and never decides. Deciding *which*
  contacts to change stays with the consumer; this skill only decides how to
  apply one reviewed change safely.
- A general approval platform, workflow engine, batching, or scheduling. The
  prepare/apply pair is the whole review boundary.
- Bulk, wildcard, or name-addressed mutation. Every write names one exact
  contact identity and one exact revision.
- Any deployment-specific default: no hardcoded server hostnames,
  address-book/collection names, filesystem paths outside `$HERMES_HOME`,
  credential values, or real contact data. Use generic `CARDDAV_*`
  environment variable names and `*.example.invalid`-style placeholders in
  code, tests, docs, and fixtures.

## Tech stack

- Python 3.12, managed with `uv` (`uv sync`, `uv run`, `uv lock`).
- `vdirsyncer` for CardDAV sync *and* for record-level conditional writes
  through its own `CardDAVStorage` API, `vobject` for vCard parsing and
  serialization, `khard` as an optional manual diagnostics tool only (never a
  runtime dependency of the skill commands). Never add a parallel HTTP, DAV,
  XML, or vCard implementation.
- `pytest` for tests, `ruff` for linting, `mypy` for type checking.

## Testing

- TDD: write a failing test before implementation.
- Tests must not require network access to a real CardDAV server; use
  synthetic fixtures. Disposable local CardDAV server integration (for
  example Radicale) is exercised separately from the default unit test run.
- Write behaviour is exercised only against a disposable local server. Never
  point a test, an example, or a manual check at a live address book.
- No real credentials or real contact data in tests or fixtures — synthetic
  values only.

## Secrets and private data

This repository holds no credentials, no contact data, and no per-deployment
state. Private runtime state (mirrors, indexes, prepared operations,
before-images, receipts) lives outside this repository entirely, under an
operator-configured `$HERMES_HOME` state root — never inside this checkout.
Every failure on the command surface is one fixed, content-free line: no
contact data, path, server response, or credential in stdout, stderr, or logs.
