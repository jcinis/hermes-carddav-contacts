# Data model — read-only v0.1

`ids.py` owns identities, `schemas.py` owns JSON validation and source
serialization, and `queries.py` owns the pure search and duplicate-candidate
functions; all three are pure and touch neither files nor the network.
`index.py` adapts mirrored vCards into those contracts and reads and writes the
SQLite index, and `reads.py` turns a published generation back into a validated
source payload for the local read commands.
Use synthetic contacts only in examples and tests.

## Identity

`contact_id(uid)` accepts a decoded vObject UID string, calls Python 3.12
`str.strip()` once, encodes strict UTF-8, and returns the first 16 lowercase
hexadecimal characters of SHA-256. Empty/whitespace-only, non-string, and
invalid Unicode inputs are refused. Do not casefold, NFC/NFKC-normalize,
hash serialized vCards, or incorporate display names. IDs are profile-scoped.
Duplicate IDs (including truncated-hash collisions) are errors, not merges.

`contact_ref(namespace, opaque_id)` returns exactly
`carddav:<namespace>:<contact_id>`. The namespace matches
`[a-z0-9][a-z0-9._-]{0,63}`; the ID matches `[0-9a-f]{16}`. Neither part is
silently repaired. References do not establish identity across namespaces.

Frozen examples: `example-uid` → `b687b2e8ceca7c40`, `EXAMPLE-UID` →
`b6533070cf089e7c`, `é` → `4a99557e4033c353`, `e\u0301` →
`bf12767b0f2a56b2` (the last input denotes e followed by U+0301).

## Source payload: `carddav-source/1.0`

All object shapes below are closed: every listed key is required, including
empty/null values; unknown keys are rejected recursively. This v0.1 producer
validator accepts exactly `carddav-source/1.0`, not speculative newer versions.
Downstream compatibility policy belongs to the downstream consumer.

Root keys:

| Key | Value |
| --- | --- |
| `schema_version` | literal `carddav-source/1.0` |
| `account_namespace` | identifier using the grammar above |
| `collections` | non-empty list of unique collection identifiers, same grammar |
| `contacts` | list of contact objects; may be empty |

Each contact has exactly these keys:

| Key | Value |
| --- | --- |
| `contact_id` | 16 lowercase hex characters, unique in the payload |
| `collection_alias` | exact member of `collections`; the vdirsyncer collection name, not an alias mapping |
| `name` | object described below |
| `aliases`, `titles`, `notes` | arrays of strings; duplicates preserved |
| `organizations` | array of non-empty component-string arrays; empty components preserved |
| `emails`, `phones`, `urls` | arrays of typed-value objects |
| `addresses` | array of address objects |
| `birthday` | null or birthday object |

- **Name:** exactly `display`, `prefix`, `given`, `additional`, `family`,
  `suffix`. `display` is a non-empty string; other fields are strings or null.
- **Typed value:** exactly `value`, `types`, `label`, `preference`. `value`
  is a non-empty string; `types` is an array of unique lowercase ASCII tokens
  matching `[a-z0-9-]+`; `label` is a string or null; `preference` is null or a
  positive integer, never Boolean.
- **Address:** exactly `po_box`, `extended`, `street`, `locality`, `region`,
  `postal_code`, `country`, `types`, `label`, `preference`. Address components
  are strings or null, at least one non-null. Metadata follows typed values.
- **Birthday:** exactly `value` (non-empty string) and `kind` (one of `date`,
  `partial-date`, `text`). Values are preserved, not guessed into dates.

Strings preserve Unicode without NFC/NFKC. Normalize CRLF and CR to LF before
serialization. Reject NUL, unpaired surrogates, and C0 controls except tab/LF
(CR is accepted as normalization input). The vCard adapter chooses
display from stripped non-empty FN, then non-empty stripped N components in
prefix/given/additional/family/suffix order, then `(unnamed contact)`; never
from UID, filenames, email, or notes. The pure validator does not infer display.

## Canonical bytes and generation

`canonical_source(data)` validates and creates canonical bytes without mutating
the caller's input. JSON is UTF-8, sorted object keys, `ensure_ascii=False`,
compact separators, no trailing newline. Unlike `profile.json`, source hash
bytes do **not** include a final newline.

Define `ascii_fold` as A–Z → a–z only, `text_key(s) = (ascii_fold(s), s)`,
and `nullable_text_key(s)` as `(1, "", "")` for null or
`(0, ascii_fold(s), s)` otherwise. Sort normalized arrays as follows:

- collections: `text_key`;
- contacts: `(text_key(name.display), text_key(collection_alias), contact_id)`;
- aliases, titles, notes, and types: `text_key`; preserve owner-visible
  duplicates, reject duplicate types at validation;
- organizations: tuple of component `text_key`s; preserve inner component order;
- email/phone/URL: `(preference_is_null, preference_or_zero, types_tuple,
  nullable_text_key(label), text_key(value), canonical_object_bytes)`;
- addresses: `(preference_is_null, preference_or_zero, types_tuple,
  nullable_text_key(label), text_key(po_box_or_empty), text_key(extended_or_empty),
  text_key(street_or_empty), text_key(locality_or_empty), text_key(region_or_empty),
  text_key(postal_code_or_empty), text_key(country_or_empty), canonical_object_bytes)`.

`canonical_object_bytes` uses the same JSON encoding for the normalized object.
`source_generation(data)` is full lowercase SHA-256 over those canonical source
bytes. It covers the entire source payload, not timestamps, profile display
name, paths, ETags, runtime versions, or envelope metadata. Equivalent array
order and line endings yield the same digest; changed contact data does not.

## Command envelopes: `carddav-command/1.0`

`validate_command(data)` supports exactly the `version`, `status`, `snapshot`,
`show`, `search`, and `audit` shapes below. Unknown commands, versions, or keys
fail, recursively and at every depth. Setup has no JSON envelope, and
`discover`/`sync` print nothing on success.

**Version:** exactly `command_schema_version`, `command` (`version`),
`package_version` (`0.1.0`), `supported_source_schema_versions`
(`["carddav-source/1.0"]`), `capabilities`, `dependency_versions`.
Capabilities are exactly `read_only: true`, `create_update: false`,
`cleanup_delete: false` (actual Booleans). Dependencies are exactly
`vdirsyncer: "0.21.0"`, `vobject: "0.9.9"`.

**Snapshot:** exactly `command_schema_version`, `command` (`snapshot`),
`profile` (identifier), `generation` (source digest), `profile_generation_sha256`
(64 lowercase hex characters), `synced_at` (valid UTC whole-second timestamp
`YYYY-MM-DDTHH:MM:SSZ`), `freshness`, `data` (validated source payload).
Validation checks `generation` against `data`. Freshness has exactly
`age_seconds` (nonnegative integer), `stale_after_seconds` (positive integer),
`stale` and `clock_skew` (Booleans). Integers never accept Booleans.

**Local read envelope:** `status`, `show`, `search`, and `audit` share
`command_schema_version`, `command`, `profile`, `profile_generation_sha256`,
plus a sync time and freshness. `show`/`search`/`audit` add `generation` and
non-null `synced_at`/`freshness`; `status` instead carries `current_generation`,
`contact_count`, `synced_at`, and `freshness`, which are either all null (no
generation yet) or all present. Command-specific keys are exactly:

| Command | Extra keys |
| --- | --- |
| `show` | `contact` — one contact object, validated with the same recursive rules as a source contact |
| `search` | `query` (1–512 characters, no C0/DEL/unpaired surrogate), `total_matches`, `truncated` (always `false`), `results` (contact objects with unique IDs) |
| `audit` | `total_candidate_groups`, `truncated` (always `false`), `candidates` |

`total_matches` and `total_candidate_groups` must equal the length of the list
they describe; integers never accept Booleans. Each candidate group is exactly
`reason` (`email`, `phone`, or `name`), a non-empty `key`, and `members`: at
least two members, each exactly `contact_id` and a non-empty `values` array of
non-empty strings, listed in ascending `contact_id` order. Groups themselves
are ordered by `(reason, key)` with `reason` ordered `email`, `phone`, `name`,
and no `(reason, key)` pair repeats — so one input always validates to one
byte sequence. The command semantics (which fields are searched, how keys are
normalized) live in `configuration.md`.

`profile_generation_sha256` binds the exact canonical `profile.json` bytes,
including its newline. It is **not** proof of remote collection identity.
The pure envelope validator checks its shape; future readers compare it to
their local profile. No custom discovery document or href fingerprint is
introduced. A snapshot requires an existing valid generation; there is no
null-generation snapshot. Other command envelopes will be specified when built.

## vCard adapter and SQLite index

`index.py` parses each mirrored `.vcf` with `vobject==0.9.9` — there is no
custom vCard grammar here — and maps one vCard to one `carddav-source/1.0`
contact:

| vCard property | Contact field |
| --- | --- |
| `UID` | `contact_id` via `ids.contact_id` |
| `FN` / `N` | `name.display` and the `prefix`/`given`/`additional`/`family`/`suffix` components; a component carrying several comma-separated members is flattened into one string by joining the members with `, ` in vCard order, keeping repeats, empty members, and decoded escaping; a component whose members are all empty stays null |
| `NICKNAME` | `aliases`, one alias per comma-separated component of every `NICKNAME` property |
| `ORG` | `organizations`, one component list per property, inner order preserved |
| `TITLE` | `titles` |
| `EMAIL`, `TEL`, `URL` | `emails`, `phones`, `urls` as typed values |
| `ADR` | `addresses`, seven structured components preserved |
| `BDAY` | `birthday`; `kind` is `date` for `YYYY-MM-DD`, `partial-date` for a vCard 4 `--MMDD` form, otherwise `text` |
| `NOTE` | `notes`, preserved verbatim apart from CRLF/CR normalization |

`NICKNAME` is a comma-separated list property, but pinned `vobject==0.9.9` has
no registered behavior for it and its default single-value text decode keeps
only the first component. The adapter therefore re-reads the same source text
with vobject's own `getLogicalLines`/`textLineToContentLine` reader and applies
vobject's `MultiTextBehavior` decode to those raw lines, so every component of
every `NICKNAME` property survives and vCard escaping still holds: `Ex\,tra`
stays the single alias `Ex,tra`. No comma or vCard grammar is implemented here.
Empty components are dropped, exactly as empty single-value properties are.

`TYPE` parameters become lowercase `types` tokens; a token that does not match
`[a-z0-9-]+` is dropped rather than failing the contact, and duplicates are
collapsed. A `PREF` parameter that is a positive integer becomes `preference`;
the vCard 3 `TYPE=PREF` form becomes `preference` 1. Empty structured
components stay `null`. A vCard with a missing, empty, or non-string `UID`, a
file that is not exactly one parsable vCard, or two vCards whose `contact_id`
collides is an error for the whole sync — contacts are never silently dropped.

The index is one SQLite database, `index.sqlite3`, written inside a single
transaction in private staging and never modified after publication:

```sql
CREATE TABLE source_meta (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL);
CREATE TABLE contacts (
    contact_id TEXT PRIMARY KEY NOT NULL,
    collection_alias TEXT NOT NULL,
    display_name TEXT NOT NULL,
    payload TEXT NOT NULL
);
```

`source_meta` holds `schema_version`, `account_namespace`, `collections`
(canonical JSON array), `generation`, and `profile_generation_sha256`.
`payload` is the canonical JSON of that contact exactly as it appears in the
hashed canonical source, so the index cannot drift from `generation`. Resolving
or reusing a generation re-reads `source_meta` read-only and refuses an index
whose schema or metadata disagrees with the manifest, the pointer, or the
profile. A local read goes further: it re-reads every `contacts` row read-only,
validates each payload against the closed contract, checks the row's
`contact_id`/`collection_alias`/`display_name` columns against that payload,
and recomputes the canonical source digest, which must equal the generation. Generation publication and the `current` pointer are specified in
`configuration.md`.

## JSON parsing and failures

`parse_json(raw)` detects duplicate keys at every object depth and rejects
nonfinite numbers before schema validation. Never feed a last-key-wins parse
into validation or hashing. Public pure validators raise content-free
`ValueError`s; CLI commands are responsible for their fixed stderr/exit mapping.
