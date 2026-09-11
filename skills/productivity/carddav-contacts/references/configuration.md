# Configuration, transport, and command contracts

## Local profile

`setup --profile NAME --namespace NAMESPACE --server-url URL --collection NAME`
creates an immutable profile without networking. Repeat `--collection` for
multiple remote collections. Profile, namespace, and collection names match
`[a-z0-9][a-z0-9._-]{0,63}`. Collections are exact vdirsyncer collection names,
not display labels or a custom href mapping. No wildcard collection selection.

Use an absolute HTTP(S) URL without credentials, query, or fragment, for example
`https://carddav.example.invalid/`. Setup stores exactly one trailing slash.
Never pass secrets in CLI arguments or URLs.

State lives beneath `$HERMES_HOME/carddav-contacts/profiles/NAME/`; if
`HERMES_HOME` is unset, use `~/.hermes/carddav-contacts/profiles/NAME/`.
An explicitly set home must be absolute and non-empty. The home root itself is
operator-owned: `setup` creates it `0700` when it is absent, and every command
— `setup`, `discover`, `sync`, and the local readers — then requires the same
thing of it, that it be a real directory owned by the current user, whatever
mode the operator gave it. Only `setup` may create it, so a read against a
missing home is a refusal, not a fresh home. Below it, skill-owned directories
are `0700`; files are `0600`, owned by the current user. Existing unsafe state
is refused rather than silently chmodded or followed through links.

`setup` refuses to write a fresh profile into a directory that has lost its
`profile.json` but still holds retained private state — operations, receipts,
generations, staging, runtime, or the `current` pointer. Such a directory is
damaged rather than new, and rebinding it would silently re-aim already
reviewed operations at whatever settings the new invocation carries.

`profile.json` is the sole configuration: `carddav-profile/1.1`, namespace,
server URL, sorted collection allowlist, and cadence 3600 seconds. It never
contains credentials. Repeated identical setup is a no-op; changed settings
conflict. A single `profile.lock` serializes cooperating operations.

`carddav-profile/1.1` drops the `capabilities` block that `carddav-profile/1.0`
copied into every profile: what the installed package can do is reported by
`version --json`, not frozen into a file written once at setup time. A `1.0`
profile is still accepted exactly as written — byte-for-byte, including its
capability block — and is **never** rewritten in place; it is only normalized in
memory to the current shape. An existing profile therefore keeps its
identifiers and keeps the `profile_generation_sha256` its published generations
are bound to, and a repeat `setup` with the same settings stays a silent no-op.
Adding a `capabilities` block to a `1.1` profile is an unknown key and is
refused, as is any other non-canonical byte.

## Runtime credentials

Export credentials in the process environment through the operator's secret
manager. This program never reads `.env` files itself and never persists secret
values. Resolution is strict:

1. A complete, non-empty `CARDDAV_USERNAME` / `CARDDAV_PASSWORD` pair wins.
2. Only when **neither** CARDDAV variable is set, use a complete, non-empty
   `DAV_USERNAME` / `DAV_PASSWORD` pair for compatibility.
3. Partial or empty pairs fail. Never mix pairs or fall back past a broken
   explicit CARDDAV configuration.

Only the selected variables enter the subprocess environment, alongside minimal
explicit process settings. Ambient proxy, debug, and unrelated secret variables
are not inherited. A temporary config references the selected variable names
using vdirsyncer's command fetch with absolute `/usr/bin/printenv`; it does not
contain their values. The config is private; cleanup runs after the invocation,
including on timeout or failure. Failure to remove it also fails the operation.
A process crash or filesystem cleanup failure can leave a private temporary
config behind; it still contains no credential values.

## Credentials for writes

`record`, the `prepare-*` commands, `apply`, and `reconcile` resolve
credentials with exactly the same rule. They run the native record transport
in-process rather than through the `vdirsyncer` subprocess, so the temporary
config file above is not involved; nothing is persisted and nothing is echoed.
The native libraries' loggers are silenced at import so a library warning can
never land on the command surface's single fixed error line.

## Discovery and mirror sync

Run `discover --profile NAME` explicitly, then `sync --profile NAME`.
Both are silent on success (exit 0); neither accepts `--json` yet. Native
vdirsyncer discovery must resolve every selected remote collection. Missing
collections fail rather than being created remotely. Selected local directories
are precreated, and the subprocess receives no interactive confirmation input.

The tool invokes `vdirsyncer==0.21.0` through its own Python interpreter, not a
possibly different global executable. The CardDAV storage is always
`read_only=true`, with remote-wins conflict resolution and `partial_sync=revert`:
local mirror edits are disposable and must never upload. Request output is
discarded, not copied into logs or error messages.

One wrapper sync runs **two native sync passes** under the same profile lock.
In pinned vdirsyncer 0.21.0, `partial_sync=revert` first updates its internal
status for a local edit, then restores the remote value on the next pass.
Both passes must succeed. This is a bounded native-tool behavior, not a retry
loop or a custom synchronization engine.

Derivative state stays within the selected profile:

- `runtime/status/`: vdirsyncer's own discovery and synchronization state;
- `runtime/mirror/<collection>/`: working vCard mirror;
- `staging/<unique>/`: private in-progress publication, never read by readers;
- `generations/<generation>/`: published, immutable indexed generation;
- `current`: the small pointer file selecting the active generation.

No custom discovery schema or DAV/XML parser is added. The mirror is **mutable
working state**, not a published snapshot: failed synchronization may leave it
partial. Do not read it as a complete contact directory after a failed sync.

## Generations and the `current` pointer

After both native sync passes succeed, `sync` parses the mirror with
`vobject`, builds a SQLite index, and publishes one immutable generation:

1. a private `staging/<unique>/` directory receives a **copy** of the mirror
   vCards plus `index.sqlite3` and `manifest.json`, so a later sync writing to
   the mutable mirror can never change published data;
2. the whole SQLite index is written inside one transaction;
3. the staged contents are made durable bottom-up — every copied file, then
   every directory that holds it — before the staging directory is published
   by a single atomic directory rename to `generations/<generation>/`;
4. the `generations/` directory itself is then fsynced, so the renamed
   generation is durable before anything refers to it;
5. only then is `current` replaced atomically (§ persistence pattern above).

`<generation>` is the 64-character lowercase SHA-256 of the canonical
`carddav-source/1.0` bytes (`data-model.md`). Unchanged remote content
therefore produces the identical generation identifier: the existing
generation is reused, nothing is renamed, no published byte changes, and the
new staging directory is removed. There is no generation churn for identical
content. Reuse is not an existence check: the already published generation is
validated exactly as a reader validates it (below), so a sync never reports
success over a generation the next read would refuse.

A successful sync always commits the pointer, including that no-op case,
because the pointer also carries the last successful sync time (see
*Freshness* below). A no-op sync therefore rewrites `current` with the same
`generation` and a new `synced_at`; it never creates, renames, or mutates a
generation directory.

`generations/<generation>/manifest.json` is canonical JSON with exactly:

```json
{
  "generation": "<64 lowercase hex>",
  "manifest_schema_version": "carddav-generation/1.0",
  "profile_generation_sha256": "<64 lowercase hex>"
}
```

`profile_generation_sha256` is the SHA-256 of the exact canonical
`profile.json` bytes, including its final newline. `current` is canonical JSON
with exactly `generation`, `pointer_schema_version` (`carddav-current/1.1`),
and `synced_at`:

```json
{
  "generation": "<64 lowercase hex>",
  "pointer_schema_version": "carddav-current/1.1",
  "synced_at": "<YYYY-MM-DDTHH:MM:SSZ>"
}
```

Readers resolve `current`, then check that the
generation directory is private and holds exactly `index.sqlite3`,
`manifest.json`, and `mirror/`; that the manifest is canonical, names the same
generation as the pointer, and binds their own profile hash; and that
`index.sqlite3` opens read-only with exactly the published schema and one
`source_meta` row per expected key, agreeing with that generation, that profile
hash, and that profile's namespace and collection scope. A missing, truncated,
or malformed index is a refusal. The index is opened `mode=ro`, so resolving a
generation never creates it, writes to it, or leaves a journal behind.
Re-hashing the whole payload against `generation` is not part of this check.
Any mismatch is a fixed refusal, never a silent fallback.

Every operation preflights the state it is about to use with `lstat`, never
following links and never repairing what it finds: the `staging/` and
`generations/` roots and the `current` pointer must be a real directory or
regular file owned by the current user with mode `0700`/`0600` (and, for files,
`nlink == 1`), and an already published generation is checked recursively
before it is reused or read. Anything else — a symlink, a wrong mode, a wrong
owner, an unexpected type — is refused as unsafe state. Those roots are
preflighted even when no generation is published yet, so an unsafe
`generations/` root is refused rather than reported as an empty state.

A controlled failure before the pointer replace removes its own staging
directory, and removes a generation directory this call had just renamed into
place while nothing referenced it yet, leaving the previous `current` and every
other generation untouched. The pointer replace itself is the commit point: a
failure after it (for example the final directory fsync) still fails the
command, but the newly referenced generation is never rolled back, so `current`
never dangles. Only an uncatchable crash (SIGKILL, OOM, power loss) can leave an
unreferenced staging or generation directory; readers only ever resolve
`current`, and this release does not prune.

## Freshness

`synced_at` is the UTC whole-second time at which a `sync` last completed
successfully: it is sampled at the final publication step, after the
generation is validated and durable and immediately before the same single
atomic pointer commit that selects the generation writes it. It measures
**time since the last successful sync**, not time since content last changed:
an unchanged address book keeps its generation and still refreshes
`synced_at`. There is no separate last-sync receipt file, so no two-file
consistency problem exists. A failed sync commits no pointer, so
it leaves both the generation and `synced_at` exactly as they were.

Local read commands derive one freshness object from `synced_at` and the
profile's `sync_cadence_seconds` (fixed at `3600`), using the same
rule everywhere:

| Key | Value |
| --- | --- |
| `stale_after_seconds` | the profile's `sync_cadence_seconds` |
| `age_seconds` | `floor(now) - synced_at`, clamped to `0` |
| `clock_skew` | `true` when `synced_at` is in the future (`floor(now) < synced_at`) |
| `stale` | `true` when `age_seconds >= stale_after_seconds`, and always `true` when `clock_skew` is `true` |

A future `synced_at` is never reported as fresh: skew is surfaced explicitly
and marks the data stale rather than being smoothed into an age.

An older `carddav-current/1.0` pointer carries no `synced_at`. Every
freshness-bearing command (`status`, `search`, `show`, `snapshot`, `audit`)
refuses it as unsafe profile state. `sync` tolerates it because it never reads
pointer contents at all: after the mirror, index, and generation validate, it
replaces `current` with a `carddav-current/1.1` pointer. One successful sync is
therefore the whole migration, and a disposable test or development profile can
simply be re-synced.

## Local read commands

`status`, `search`, `show`, `snapshot`, and `audit` are strictly local: no
network, no subprocess, no credential variable is read, and nothing outside
the selected profile is consulted. They work with the CardDAV server
unreachable and with credentials absent or poisoned. Each takes `--profile
NAME` and requires `--json`; there is no human-readable mode. Each
prints exactly one JSON object to stdout and exits 0, or prints nothing to
stdout, one fixed line to stderr, and exits 2. There is no partial-success
output: a payload is validated completely before its first byte is printed.
No command writes a file, a log, or any private data outside stdout.

Each read takes a shared `profile.lock` lock, re-reads and validates
`profile.json`, resolves `current`, and validates the published generation the
same way (`Generations and the current pointer`, above). It then reads every
contact payload row from the index read-only, validates each against the
closed `carddav-source/1.0` contract, and recomputes the canonical source
digest. That digest must equal the generation named by the pointer, the
manifest, and the index metadata, and the indexed `contact_id`,
`collection_alias`, and `display_name` columns must equal the payload they
carry. Any disagreement — an unknown or malformed payload field, an extra or
missing row, a changed profile or digest, a missing or malformed database or
pointer — is `error: unsafe profile state`. A source with zero contacts is
valid, not an error.

`status` reports `null` for an absent generation. Every other read command
requires a current generation and fails closed with
`error: no current generation`.

Every read also reports `cache_invalidated`. It is `true` from the moment a
remote write succeeds until the next successful `sync` republishes the
generation, and it is present and Boolean even before the first sync. A reader
that treats a local answer as current must check it as well as `freshness`.

This release never truncates and never paginates: `search` and `audit` return every
deterministic result, their totals count exactly what is returned, and
`truncated` is always `false`. It is reserved so a later release can page
without changing the envelope shape.

### `status --profile NAME --json`

```json
{
  "command_schema_version": "carddav-command/1.1",
  "command": "status",
  "profile": "demo",
  "current_generation": null,
  "profile_generation_sha256": "<64 lowercase hex>",
  "contact_count": null,
  "synced_at": null,
  "freshness": null,
  "cache_invalidated": false
}
```

`current_generation` is `null` before the first successful `sync` publishes a
generation, and the 64-character generation identifier afterwards. `null` is
reported only after the profile's managed roots pass their preflight; an unsafe
`generations/` root fails closed instead. `profile_generation_sha256` is always
the SHA-256 of this profile's canonical `profile.json` bytes.

With a current generation, `contact_count` is the number of contacts in the
validated payload (`0` is a valid count), `synced_at` is the pointer timestamp,
and `freshness` is the object described under *Freshness*. Without one, those
three keys are `null` together.

### `snapshot --profile NAME --json`

Prints the whole validated source payload inside the fixed
`carddav-command/1.1` snapshot envelope specified in `data-model.md`: exactly
`command_schema_version`, `command` (`snapshot`), `profile`, `generation`,
`profile_generation_sha256`, `synced_at`, `freshness`, and `data`. `data` is
the canonical `carddav-source/1.0` payload, and `generation` is its digest.
This envelope is unchanged from the version `schemas.validate_command` already
accepts.

### `show --profile NAME --id ID --json`

`ID` is exactly 16 lowercase hexadecimal characters — the opaque
`contact_id` of `data-model.md`. Any other spelling (uppercase, wrong length,
non-hex, empty) is `error: invalid operation` and is refused before any state
is read. A well-formed ID that is not in the current generation is
`error: contact not found`.

```json
{
  "command_schema_version": "carddav-command/1.1",
  "command": "show",
  "profile": "demo",
  "generation": "<64 lowercase hex>",
  "profile_generation_sha256": "<64 lowercase hex>",
  "synced_at": "<YYYY-MM-DDTHH:MM:SSZ>",
  "freshness": { "age_seconds": 0, "stale_after_seconds": 3600, "stale": false, "clock_skew": false },
  "cache_invalidated": false,
  "contact": { "contact_id": "<16 lowercase hex>", "...": "the whole validated contact object" }
}
```

### `search --profile NAME --query TEXT --json`

`TEXT` is a literal search string: 1 to 512 characters, with no NUL, no other
C0 control character, no DEL, and no unpaired surrogate. Anything else is
`error: invalid operation`. There is no SQL, glob, or regular-expression
input anywhere in this command: the query is never interpolated into SQL, and
`%`, `_`, `*`, `\`, quotes, and regex metacharacters are matched literally.

Matching is a case-insensitive literal substring test. Both sides are folded
with Python `str.casefold()`; no Unicode normalization is applied, so
precomposed `é` does not match `e` followed by U+0301. A contact matches when
any of these validated payload fields contains the query:

- `name.display` and the `prefix`, `given`, `additional`, `family`, `suffix`
  components;
- `aliases`, `titles`, `notes`;
- every component string of every entry in `organizations`;
- the `value` of every `emails`, `phones`, and `urls` entry;
- the seven address component strings of every `addresses` entry;
- `birthday.value`.

`contact_id`, `collection_alias`, type tokens, and preferences are not
searched.

```json
{
  "command_schema_version": "carddav-command/1.1",
  "command": "search",
  "profile": "demo",
  "generation": "<64 lowercase hex>",
  "profile_generation_sha256": "<64 lowercase hex>",
  "synced_at": "<YYYY-MM-DDTHH:MM:SSZ>",
  "freshness": { "age_seconds": 0, "stale_after_seconds": 3600, "stale": false, "clock_skew": false },
  "cache_invalidated": false,
  "query": "<the query exactly as given>",
  "total_matches": 1,
  "truncated": false,
  "results": [ { "contact_id": "<16 lowercase hex>", "...": "the whole validated contact object" } ]
}
```

`results` holds whole contact objects in canonical source order (the
`data-model.md` contact sort), `total_matches` equals `len(results)`, and
`truncated` is always `false`.

### `audit --profile NAME --json`

Reports **duplicate candidates only**. It makes no identity decision, picks no
survivor, merges nothing, writes nothing, and never rewrites the index. A
group is evidence that a human should look, and nothing more.

A candidate group is a single shared normalized key held by two or more
distinct contacts. Groups are never merged transitively: two groups that share
a contact stay two groups, so no implicit identity is ever inferred. The three
conservative reasons are:

| `reason` | Key | Normalization |
| --- | --- | --- |
| `email` | shared email address | `str.casefold()` of the stripped `value`; must be non-empty |
| `phone` | shared phone number | the ASCII digits of `value` in order, everything else dropped; must be non-empty |
| `name` | shared display name | `str.casefold()` of `name.display` with runs of whitespace collapsed to one space; must be non-empty |

The `(unnamed contact)` placeholder display name is never name evidence, so
contacts that merely lack a name are not reported as duplicates of each other.
No other field (address, organization, birthday, URL) is evidence.

```json
{
  "command_schema_version": "carddav-command/1.1",
  "command": "audit",
  "profile": "demo",
  "generation": "<64 lowercase hex>",
  "profile_generation_sha256": "<64 lowercase hex>",
  "synced_at": "<YYYY-MM-DDTHH:MM:SSZ>",
  "freshness": { "age_seconds": 0, "stale_after_seconds": 3600, "stale": false, "clock_skew": false },
  "cache_invalidated": false,
  "total_candidate_groups": 1,
  "truncated": false,
  "candidates": [
    {
      "reason": "email",
      "key": "person@example.invalid",
      "members": [
        { "contact_id": "<16 lowercase hex>", "values": ["Person@Example.invalid"] },
        { "contact_id": "<16 lowercase hex>", "values": ["person@example.invalid"] }
      ]
    }
  ]
}
```

`values` preserves the full matching values exactly as they appear in the
payload — this is private local output, never a redacted digest. Members are
sorted by `contact_id`, each member's `values` keeps canonical payload order,
and groups are sorted by `(reason, key, member contact_ids)` with `reason`
ordered `email`, `phone`, `name`. The same input always produces the same
bytes.

## Write commands

Writes are two explicit steps: prepare an operation, then apply it by digest.
Every write command takes `--profile NAME`, requires `--json`, prints exactly
one JSON object, and accepts only the exact flag sequence below — no
abbreviations, no reordering, no extra flags.

| Command | Flags after `--profile NAME` |
| --- | --- |
| `record` | `--id ID --json` |
| `prepare-create` | `--collection NAME --changes JSON --json` |
| `prepare-update` | `--id ID --changes JSON --json` |
| `prepare-delete` | `--id ID --json` |
| `apply` | `--operation ID --json` |
| `reconcile` | `--operation ID --json` |

`--id` is the 16-hex `contact_id`; `--operation` is the 64-hex `operation_id`
from a `prepare-*` result; `--collection` must be a member of the profile's own
`collection_allowlist`. `--changes` is one `carddav-change/1.0` document as a
single JSON argument, parsed with duplicate-key rejection. A target is always
an exact identity — never a name, a pattern, a wildcard, or a remote URL. There
is no bulk or multi-target form.

`prepare-create` requires `--collection` explicitly, including when the profile
selects only one collection, so a destination is always chosen rather than
defaulted.

Whole-card replacement (`prepare_replace`) is API-only, deliberately: it takes
a complete vCard, and the command surface reads no file outside
`$HERMES_HOME`.

The documents these commands print — `carddav-operation/1.0`,
`carddav-result/1.0`, `carddav-record/1.0` — are specified in
`data-model.md` and validated by `schemas.validate_write_document`. The safety
rules behind them (preconditions, verification, unknown outcomes,
reconciliation, cache invalidation, private receipts) are in
`write-safety.md`.

### Private write state

Inside the selected profile, alongside the read-side directories:

- `operations/<operation_id>.json`: one reviewed operation, including the
  complete before-image;
- `receipts/<operation_id>.json`: one `carddav-receipt/1.0` outcome record;
- `cache-invalid`: written before a mutation is dispatched and cleared only
  when a sync has authoritatively republished the generation, so it is present
  whenever a write may have outrun it — including while an outcome is unknown.

Directories are `0700` and files `0600`, and both sets are bounded: the oldest
entries beyond a fixed retention count are removed as new ones are written.

Both documents are validated against their closed contracts every time they are
loaded, and a receipt must additionally agree with the operation it claims to
describe. Neither is treated as evidence on its own: a recorded success is
re-proved against the server before it is reported (`write-safety.md`).

## Fixed failures

Failures write one content-free stderr line and exit 2:

| Failure | stderr |
| --- | --- |
| malformed flags, profile identifier, contact ID, or search query | `error: invalid operation` |
| missing, partial, or empty credential pair | `error: invalid credentials` |
| unsafe paths, malformed profile, malformed pointer, manifest, or index, or a generation bound to a different profile | `error: unsafe profile state` |
| native discovery or unexpected discovery failure | `error: discover failed` |
| native sync failure, including missing discovery state, unparsable or duplicate-UID vCards, and index or publication failure | `error: sync failed` |
| unexpected status failure | `error: status failed` |
| `search`/`show`/`snapshot`/`audit` with no current generation | `error: no current generation` |
| `show` with a well-formed contact ID that is not in the current generation | `error: contact not found` |
| unexpected search/show/snapshot/audit failure | `error: search failed`, `error: show failed`, `error: snapshot failed`, `error: audit failed` |
| malformed write flags, contact ID, operation ID, collection, or change document | `error: invalid operation` |
| no prepared operation with that identifier | `error: operation not found` |
| `record`/`prepare-*` target absent on the server | `error: contact not found` |
| create precondition refused: something is already there | `error: already exists` |
| update/delete precondition refused: the record changed since review | `error: revision conflict` |
| the operation was reviewed against different profile settings than the current ones | `error: profile binding mismatch` |
| the write was accepted but does not read back as intended, or reconciliation finds a record that is neither the reviewed nor the intended card | `error: verification failed` |
| the outcome is unknown; run `reconcile` | `error: unknown outcome` |
| applying an operation whose outcome is still unresolved | `error: reconciliation required` |
| the server could not be reached or answered unusably; nothing was written | `error: remote unavailable` |
| unexpected write failure | `error: <command> failed` |

Setup retains its separate fixed error classes. No failure includes server
response bodies, paths, contact contents, or credential values.

## Integration verification

`uv run --group integration pytest -m integration -q` installs the optional
`Radicale==3.5.8` test dependency and runs disposable localhost fixtures.
Fixture provisioning happens before transport checks. For the read-only
transport tests the server records every method/path and rejects all methods
except GET, HEAD, OPTIONS, PROPFIND, and REPORT, so `discover`/`sync` are proven
never to attempt a mutation. The CRUD tests use a server that permits writes and
assert that real `PUT`/`DELETE` requests happen, that a stale `If-Match` fails
closed, and that local reads still open no socket afterwards. These tests never
connect to a live address book. The default unit suite excludes tests marked
`integration`.

Localhost Radicale is a disposable test double, not evidence about any
particular real server. Radicale 3.5.8 re-serializes every card it stores
through vobject — which, among other things, collapses a multi-component
`NICKNAME` on a plain `PUT`, before this skill is involved at all. Lossless
editing is therefore proven at the pure level in `tests/test_vcards.py`, and
read-back verification is what protects a real deployment from a server that
normalizes a field this skill models.
