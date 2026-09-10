# Write safety

**v0.1 of this skill is read-only. No write path exists.** `capabilities` is
fixed at `read_only=true`, `create_update=false`, `cleanup_delete=false`, and
no command in this release can create, update, merge, or delete a contact —
locally or on the remote address book. `discover` and `sync` use read-only DAV
methods; a local edit to the working mirror is reverted by the next sync, never
uploaded.

This file exists so the read-only boundary is stated where a reader looks for
write behavior. It defines no future contract: any approval-gated write
capability or cleanup deletion ABI will be designed by its own plan, and until
then nothing here is scheduled or promised.
