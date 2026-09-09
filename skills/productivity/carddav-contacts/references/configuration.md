# Configuration (planned)

Status: placeholder. Not yet implemented.

This document will define the normative configuration contract for this
skill, including:

- Generic `CARDDAV_*` environment variable names and a documented `DAV_*`
  compatibility mapping (no values ever committed here).
- Profile naming and state-root resolution under an operator-configured
  `$HERMES_HOME`.
- The stable, non-secret account namespace and explicit collection
  allowlist.

All examples in this file must use placeholder values only, for example
`https://carddav.example.invalid/` and profile name `example-profile` —
never a real hostname, address-book name, or credential.
