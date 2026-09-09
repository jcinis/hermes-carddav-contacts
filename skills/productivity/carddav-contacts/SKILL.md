# CardDAV Contacts Skill

Status: scaffolding only. No commands are implemented yet.

## Purpose

Read-only (and later, approval-gated read/write) access to a
standards-compliant CardDAV address book, backed by `vdirsyncer` for sync
and `vobject` for vCard parsing, with a private local SQLite index for
fast, network-free `search`/`show`.

This skill targets standards-compliant CardDAV servers (for example
Radicale, Nextcloud, Baïkal, Fastmail). It is not a generic WebDAV client.

## Invocation contract

```sh
python3 ${HERMES_SKILL_DIR}/scripts/carddav_contacts.py <command>
```

The script may rely on a project-managed environment internally, but this
invocation path is the stable, external contract.

## Commands (planned)

See `references/configuration.md`, `references/data-model.md`, and
`references/write-safety.md` for the normative configuration, source data
model, and write-safety contracts as they are implemented. No command is
functional in this scaffolding commit.

## Non-goals

See the repository root `README.md` and `CLAUDE.md` for the full
ownership-boundary and non-goals list. In short: this skill does not own
projection/rendering/identity-linking, duplicate-decision/cleanup policy,
or a routine delete command.
