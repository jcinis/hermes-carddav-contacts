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
transport to maintain a private working mirror. `record`, `prepare-*`, `apply`,
and `reconcile` also use the network and credentials; the local query commands
do not. A successful `sync` parses the
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
# Present this operation for approval before making this separate call.
result = api.apply_operation("demo", operation["operation_id"])
```

These are the same objects the console command calls. `api` also exposes
`read_collection` and `prepare_replace`, a validated lossless whole-card
replacement for a consumer that composed the complete card itself.

Nothing deployment-specific lives in this repository: no server hostnames,
address-book names, credential values, or contact data. Documentation and tests
use placeholders such as `https://carddav.example.invalid/`.

## Set up with your Hermes

Give your agent this repository and ask:

> Install the CardDAV contacts command and its Hermes skill for my active
> profile. Help me connect my address book securely, sync it read-only, and
> verify a contact lookup. Do not create, edit, or delete contacts during setup.

This connects a **CardDAV address book**, not arbitrary phone contacts or a
generic WebDAV file share. You need Hermes with terminal access on the machine
where the commands will run, Git, [uv](https://docs.astral.sh/uv/), Python 3.12+,
and a reachable CardDAV account. Linux is tested; macOS is unverified; native
Windows is not supported. No Memex installation or duplicate-cleanup project is
needed. The local mirror and search index are included; scheduled refresh is not.

### 1. Install the executable and the skill

**Source installation:** install the v0.2.0 CRUD implementation from `main`.
This path does not require a GitHub release or PyPI publication; do not assume
a `v0.2.0` tag or an indexed package exists. Use a new checkout, inspect the
source, and record its commit before installation:

```sh
git clone --branch main https://github.com/jcinis/hermes-carddav-contacts.git
cd hermes-carddav-contacts
git rev-parse HEAD
uv tool install --python 3.12 .
hermes-carddav-contacts version --json
```

`uv tool install` creates an isolated runtime environment, rather than modifying
Hermes's Python environment or system Python. If the command is not on `PATH`,
use `uv tool update-shell` and open a new shell; a long-running Hermes process
also needs the updated `PATH`. Do not replace an existing installation blindly.
For a supplied wheel, use `uv tool install --python 3.12 /absolute/path/to/the.whl`
instead. The runtime dependencies are exactly `vdirsyncer==0.21.0` and
`vobject==0.9.9`; test tools such as Radicale and pytest are not runtime dependencies.

Check that `version --json` reports `package_version: "0.2.0"`,
`command_schema_version: "carddav-command/1.1"`, and `create`, `update`, and
`delete` capabilities all `true`.

**Installing the executable does not register the skill with Hermes.** Copy the
complete skill directory from the same checkout into the intended Hermes
profile. Resolve that profile's home first: preserve an existing `HERMES_HOME`;
use `~/.hermes` only for the default profile. A named profile has its own home.
Do not accidentally install into a different profile. In the same shell, from
the checkout root, after confirming the target:

```sh
# Default profile only; for a named profile set its actual absolute home instead.
export HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
mkdir -p "$HERMES_HOME/skills/productivity"
test ! -e "$HERMES_HOME/skills/productivity/carddav-contacts" && \
    test ! -L "$HERMES_HOME/skills/productivity/carddav-contacts" && \
    cp -R skills/productivity/carddav-contacts "$HERMES_HOME/skills/productivity/"
```

If that destination already exists, stop and review it; the command deliberately
does not overwrite an installed skill. Copy `SKILL.md`, `scripts/`, and
`references/` together, not just `SKILL.md`. This manual copy is not a Hub-managed
installation; future updates must keep the executable and skill on the same
revision. See the [Hermes skills documentation](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills).

Open a new conversation in that profile and ask Hermes to load `carddav-contacts`
and run `hermes-carddav-contacts version --json` using its terminal tool. Both
skill discovery and command execution must work before configuring the account.
All subsequent commands must use the same `HERMES_HOME`.

### 2. Select the address book and supply credentials securely

Have the owner identify:

- The CardDAV server URL, without embedded credentials. Use HTTPS for a remote server.
- The exact remote collection identifier, **not its friendly display label**.
  Get it from the provider's address-book settings or administrator. Discovery
  verifies an already selected collection; this CLI has no interactive book picker.
- A local profile name and stable namespace, for example `personal` and `personal`.
  The local CardDAV profile is separate from the Hermes profile chosen above.
- Credentials with the intended access: read permission for sync/search and write
  permission for CRUD. Use a provider-issued app password where required.

Use **one credential pair for all operations**. Full-access credentials enable
reads and CRUD; read-only credentials restrict access at the server. There is
no separate write login or second account. Routine sync remains read-only even
with full-access credentials; explicit CRUD operations perform the writes.

Supply `CARDDAV_USERNAME` and `CARDDAV_PASSWORD` together through the owner's
secret manager or the selected Hermes process's protected credential environment.
Follow [Hermes's secret setup](https://hermes-agent.nousresearch.com/docs/user-guide/secrets).
Do not ask the owner to paste a password into agent chat, put it in a command or
URL, print environment values, or store it in this checkout or `profile.json`.
The contact command reads its environment; **it does not load `.env` itself**.
An export in a separate terminal does not update an already-running gateway or
desktop backend. Confirm credentials reach the agent's terminal subprocess;
do not print their values to check. Restart the relevant process only with the
owner's permission if its environment needs refreshing.

Both variables must be non-empty. Partial pairs fail rather than silently
falling back. See [credential precedence](skills/productivity/carddav-contacts/references/configuration.md#runtime-credentials).

### 3. Create the local profile, sync, and verify a lookup

The following values are examples: replace the URL, collection, profile, and
namespace with the owner's selections. Do not run against the example host.
Profile, namespace, and collection identifiers must match
`[a-z0-9][a-z0-9._-]{0,63}`; if a provider's collection identifier cannot be
represented, stop rather than altering or guessing it. Repeat `--collection`
for multiple explicitly selected books.

```sh
hermes-carddav-contacts setup --profile personal --namespace personal \
    --server-url https://carddav.example.invalid/ --collection contacts
hermes-carddav-contacts discover --profile personal
hermes-carddav-contacts sync --profile personal
hermes-carddav-contacts status --profile personal --json
hermes-carddav-contacts search --profile personal --query 'Example Person' --json
```

`setup`, `discover`, and `sync` are silent on success; check their exit status and
stop on failure. They do not accept `--json`. Confirm `status` reports a current
generation without a stale or invalidated cache, then search for a person the
owner expects. Disambiguate multiple matches and use the returned `contact_id`
with `show --profile personal --id ID --json`; do not invent an ID. A lookup
does not require or authorize a test write.

Profiles are immutable: identical setup is a no-op; changed settings are refused.
For a mistaken server/book selection, choose a new local profile rather than
editing stored profile files. Private state lives under
`$HERMES_HOME/carddav-contacts/`, outside the checkout. The working mirror is a
cache, **not a backup**. No refresh scheduler is installed; run `sync` when fresh
data is needed and check `status` before relying on a cached answer.

### 4. Use authorized create, update, and delete operations

Setup ends with the successful lookup. For later user-requested changes:

1. For update/delete, search, disambiguate, and `record` the exact contact fresh.
   For create, confirm the destination collection and all proposed fields.
2. Run `prepare-create`, `prepare-update`, or `prepare-delete` with that scope.
   Preparation creates a local proposal, not a remote change.
3. Show the target and proposed change to the owner. Creation has no before-image;
   updates/deletions do. Obtain approval for that specific operation.
4. Run `apply` with the returned `operation_id`, then inspect its verified result.
   Do not equate exit success with an unexamined write outcome.

Use the exact change-document examples in
[the skill](skills/productivity/carddav-contacts/SKILL.md#changing-contacts).
A `replace` entry replaces the **whole** named multi-valued field; carry forward
every existing value that should remain. An expired revision requires fresh
preparation and review. An unknown outcome requires `reconcile`, not a blind
retry. If the write succeeded but cache refresh failed, the write still happened;
refresh the cache, do not repeat the write. See
[write safety](skills/productivity/carddav-contacts/references/write-safety.md).

The application enforces revision checks, not human consent: the agent/operator
must enforce the approval step. Installing the skill does not grant blanket
authorization to alter contacts. There is no bulk merge or cleanup command.

## Running from a source checkout

The direct entry point remains supported and uses the same implementation:

```sh
uv run python skills/productivity/carddav-contacts/scripts/carddav_contacts.py version --json
```

The installed executable needs no checkout, no `PYTHONPATH`, and no particular
working directory. Keep the source checkout only if you want it for development
or future manual skill updates.

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
