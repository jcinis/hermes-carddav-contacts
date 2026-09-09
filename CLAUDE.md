# CLAUDE.md — hermes-carddav-contacts

Project-specific guidance for working in this repository. This repository is
the **reusable, generic CardDAV capability** in a three-repository system —
see `README.md` for the full ownership boundary. Keep that boundary intact:
do not add projection/rendering/identity-link logic (owned by a downstream
notes repository) or duplicate-decision/survivor/cleanup logic (owned by a
separate cleanup project) here.

## Non-goals (do not implement here)

- Custom HTTP/DAV/XML client or custom vCard parsing — use `vdirsyncer` and
  `vobject`.
- Markdown/note-taking-tool rendering, git-carried human projections, or
  contact-to-person identity links.
- Duplicate-contact ledgers, survivor policy, or merge/delete approval
  batching.
- A routine, user-facing delete command.
- Any deployment-specific default: no hardcoded server hostnames,
  address-book/collection names, filesystem paths outside `$HERMES_HOME`,
  credential values, or real contact data. Use generic `CARDDAV_*`
  environment variable names and `*.example.invalid`-style placeholders in
  code, tests, docs, and fixtures.

## Tech stack

- Python 3.12, managed with `uv` (`uv sync`, `uv run`, `uv lock`).
- `vdirsyncer` for CardDAV sync, `vobject` for vCard parsing, `khard` as an
  optional manual diagnostics tool only (never a runtime dependency of the
  skill commands).
- `pytest` for tests, `ruff` for linting, `mypy` for type checking.

## Testing

- TDD: write a failing test before implementation.
- Tests must not require network access to a real CardDAV server; use
  synthetic fixtures. Disposable local CardDAV server integration (for
  example Radicale) is exercised separately from the default unit test run.
- No real credentials or real contact data in tests or fixtures — synthetic
  values only.

## Secrets and private data

This repository holds no credentials, no contact data, and no per-deployment
state. Private runtime state (mirrors, indexes, plans, receipts) lives
outside this repository entirely, under an operator-configured
`$HERMES_HOME` state root — never inside this checkout.
