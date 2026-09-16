# Write safety

v0.2 adds basic CRUD: create, read, update, and delete a contact. Delete is an
ordinary, explicitly supported operation, not a cleanup-only escape hatch.
`capabilities` reports `read_only=false`, `create=true`, `update=true`,
`delete=true`.

What did **not** change: ordinary local reads (`status`, `search`, `show`,
`snapshot`, `audit`) are still offline, and routine `discover`/`sync` still use
a `read_only=true` vdirsyncer storage that never uploads. A write only ever
happens through an operation that was explicitly prepared and then explicitly
applied. No read, no sync, and no audit can mutate anything.

## Prepare, then apply

Every mutation is two steps, and the second one does nothing the first did not
already fix.

1. **Prepare** (`prepare-create`, `prepare-update`, `prepare-delete`, and the
   API-only `prepare_replace`) reads the target fresh from the server and
   writes one private operation document binding:
   the profile, the collection, the exact `contact_id`, the record's `href`,
   the **revision** (`ETag`) the record carried at review time, the complete
   before-image, and the complete proposed card. Its `operation_id` is the
   SHA-256 of that whole binding.
2. **Apply** (`apply --operation <operation_id>`) loads that document, checks
   the digest still matches the binding, and performs exactly that one write.

Applying re-derives nothing. It never re-reads the target to pick up a newer
revision, never re-resolves a name to an identity, and never edits a card
rebuilt from the local snapshot — the snapshot is a normalized projection and
is not a writable record.

## Preconditions, and what a conflict means

| Verb | Precondition sent | On refusal |
| --- | --- | --- |
| create | `If-None-Match: *` | `error: already exists` |
| update / replace | `If-Match: <reviewed revision>` | `error: revision conflict` |
| delete | `If-Match: <reviewed revision>` | `error: revision conflict` |

A precondition failure is final. The record changed between review and
application, so the review is stale: prepare the change again against the
current record and look at it. Nothing retries automatically against whatever
the record happens to say now.

These are vdirsyncer's own `upload`, `update`, and `delete` calls on
`CardDAVStorage`; there is no HTTP, DAV, or XML client in this skill. Callers
never supply a remote URL: a collection address is only ever one the configured
server advertised through native discovery, inside the configured `server_url`,
named by the profile's own `collection_allowlist`.

Containment is decided on whole, decoded path segments before any target
reaches the native library — never on a string prefix, which would accept
`/userother/` as being inside `/user/`. A `.` or `..` segment is refused
outright rather than resolved, in both its literal and percent-encoded
spellings, as is a percent-encoded separator that would hide a segment
boundary. A query or fragment is never part of a collection address, and an
item href must be a path on the already bound origin: a full URL or a
protocol-relative authority is refused.

### Profile binding

An operation records the exact canonical `profile.json` bytes it was prepared
against, and both `apply` and `reconcile` enforce it. If the profile has since
been changed or recreated — different namespace, server, or collections — the
review no longer describes what would happen, so the operation is refused with
`error: profile binding mismatch` rather than being silently re-aimed at the
new configuration. For the same reason, `setup` refuses to write a fresh
profile into a directory that has lost its `profile.json` but still holds
retained state (operations, receipts, generations, staging, runtime, or the
`current` pointer): that directory is damaged, not new.

## Verification

A write is never reported from its own request.

- After a create, an update, or a replace, the exact record is read back and
  checked twice: the modeled projection must match what was intended, **and**
  every raw logical line that was sent must still be present in the returned
  card. A mismatch either way is `error: verification failed`.
- After a delete, the exact address must read back as authoritatively absent.
  A network or authentication failure during that read is **not** proof of
  deletion.

The second check is what catches a server that accepts a write and quietly
drops something the projection does not model — a photo, a `CATEGORIES` line, a
group prefix, an unknown `X-` extension. Only differences that carry no
information are tolerated: property order, parameter order, repeated-parameter
order, parameter-name case, optional parameter quoting, `TYPE` token case, and
line folding or ending style. A server may add lines of its own (`REV`,
`PRODID`); that is not loss. Anything else — a dropped line, a rewritten value,
a changed `VERSION` — fails verification rather than being reported as applied.

### Repeating a settled operation

Applying an operation that already settled does not write again, and it does
not simply repeat the stored receipt back either. A receipt is unauthenticated
private state: a corrupted, stale, or forged one could otherwise make this
program claim an applied, verified mutation it never performed. So the recorded
outcome is re-proved against the server — the record must still be present and
still verify, or for a delete still be absent — before it is reported. It
follows that repeating a settled operation needs the server to be reachable;
when it is not, the answer is a refusal rather than an unverified claim.

Receipts are also validated against their closed contract on load and must
agree with the operation they claim to describe (same operation id, verb,
profile, collection, and contact). A receipt that does not is refused outright.

## Preservation

An edit rewrites only the properties the change document names. Every other
logical line is carried over from the fresh remote card exactly as it arrived,
folded with vobject's own serializer. Photos, group prefixes, `CATEGORIES`,
unknown `X-` extensions, multi-valued structured `N` components, and the `UID`
therefore survive an ordinary field edit unchanged. This matters concretely:
pinned `vobject==0.9.9` does not round-trip every property it can parse — a
multi-component `NICKNAME` collapses to its first component, and parameter
order is not stable — so re-serializing a whole card would silently rewrite
properties nobody asked to change.

An edit that cannot be expressed without losing supplied data is refused rather
than applied: clearing the formatted name, or supplying a `label` on a typed
value or address, which this skill's reader never reads back.

A `UID` is never changed. A whole-card replacement must keep the target's own
identity.

**Server-side normalization is outside this boundary.** A server that
re-serializes what it stores can still lose data no client can protect. The
disposable Radicale used in the tests does exactly that to multi-component
`NICKNAME` on a plain `PUT`. What this skill guarantees is that it never calls
such a write verified: read-back compares both the modeled projection and every
raw line that was sent, so a server that drops any of them produces
`error: verification failed` and an unresolved operation instead of a success.

## Unknown outcomes

There are exactly two classifications, and which one applies depends on *when*
the failure happened, not on what it was.

**The mutation request itself.** A failure raised by `upload`/`update`/`delete`
may be a clean, retryable refusal, but only on positive evidence that nothing
was applied: no connection was ever established, or the server answered with a
definitive client-error refusal (a `4xx` other than `408` or `429`). A `5xx`, a
mid-flight disconnect, or a timeout leaves the outcome **unknown**.

**Everything after that request returned success.** Always unknown. Once the
server has accepted the write, no later evidence says it did not land: a
read-back `401`, `404`, connector failure, timeout, missing `ETag`, or
unparsable card describes the *read*, not the write. Applying the
definitive-refusal rule here would present an applied mutation as cleanly
retryable and let it be replayed, so it is never applied here. The one
apparent exception is not one: a `404` on a delete's confirmation read is the
authoritative proof the record is gone, which is that verb's success condition.

A write that lands but fails verification is likewise not retryable — the
operation stays unresolved. The intent is recorded privately before the request
leaves, so an interrupted apply is always discoverable.

Applying that operation again is refused (`error: reconciliation required`).
`reconcile --operation <operation_id>` decides what actually happened by
reading the server, using **the same complete proof direct apply uses** —
modeled projection *and* every raw line. A projection-only match can never
settle an operation whose photo or unknown extension was lost:

- the proposed card is present and fully proves → `already_applied`;
- the reviewed revision is still in place *and* the record still fully proves
  as the before-image → `not_applied`, and the operation becomes cleanly
  applicable again;
- the record is there but is neither → `error: verification failed`, needing a
  fresh review.

A delete follows the same rules, with absence in place of the proposed card:
the record being gone is `already_applied`, and a record still reporting the
reviewed revision is `not_applied` **only after it proves as the before-image**.
A matching validator token is not on its own proof that the record is
unchanged — a server with a stale or weak `ETag` can report the reviewed
revision over a record that has moved on, and settling on the token alone would
make the reviewed delete applicable again against a record nobody reviewed.

Because a create carries a stable operation `UID` minted at prepare time, a
reconciled retry can neither create a second contact nor delete a record that
was recreated in the meantime.

## Local cache after a write

The published generation may stop describing the server from the moment a write
request is in flight — not only once one is known to have succeeded. The cache
is therefore marked invalid **before the request is dispatched**, and that
marker is written before the operation's receipt; if either write fails, the
mutation is never dispatched at all.

The marker is cleared only when a sync has authoritatively republished the
generation. An unknown outcome and a failed verification both keep it, so a
local read can never report `cache_invalidated: false` over a generation a
possibly-applied write may have outrun. The one withdrawal is narrow and
evidence-based: when the server definitively refused the request itself,
nothing was applied, so a marker *this call* set is removed again — and only if
no other operation is still unresolved.

After a successful write, a read-only sync and a republication are tried.

- Refresh succeeded → `local_cache: "refreshed"`, marker cleared.
- Refresh failed → `local_cache: "stale"`, marker kept, and **every** local read
  reports `cache_invalidated: true` until the next successful `sync`.

The remote write is still reported as `applied`. It is never reported as a
failure, because a failure would invite the caller to perform the mutation
again.

One real case worth knowing: pinned vdirsyncer raises `StorageEmpty` when a
collection it has seen with items becomes empty, and this skill deliberately
does not pass `force_delete` to the routine read-only sync. Deleting the last
contact in a collection therefore succeeds remotely and leaves a stale cache.

## Private state

Operation documents (including before-images) and receipts live under the
profile's own state root, `0700` directories and `0600` files, outside any
checkout. They exist for review and reconciliation and are bounded: the oldest
entries beyond a fixed retention count are removed. No contact data,
credential, or private path ever appears in a repository artifact, a log, or an
error message — every failure is one fixed, content-free line.

## Not in scope

This skill decides *how* to apply one reviewed change safely. It never decides
*which* contacts to change. Survivor policy, duplicate-merge decisions,
batching, scheduling, and approval workflow belong to a consumer, which builds
them on this same API. `audit` still reports candidates only and never decides.
A batch is not a transaction: each operation settles on its own and leaves its
own receipt.
