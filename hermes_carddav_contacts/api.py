# ABOUTME: The supported Python API for record reads and reviewed create/update/delete.
# ABOUTME: A thin re-export of the shipped skill's write implementation — the CLI's own code.

"""Installable public API for CardDAV contact records.

```python
from hermes_carddav_contacts import api

operation = api.prepare_update("demo", "0123456789abcdef", {
    "change_schema_version": api.CHANGE_SCHEMA_VERSION,
    "set": {"display": "Example Person"},
    "clear": ["birthday"],
    "replace": {"emails": [{"value": "person@example.invalid", "types": ["work"],
                            "label": None, "preference": None}]},
})
result = api.apply_operation("demo", operation["operation_id"])
```

Every function here is the same object the `hermes-carddav-contacts` command
calls, so a consumer never becomes a second CardDAV writer with its own
preconditions, its own verification, or its own idea of what a revision means.
Preparing reads the target fresh from the server and binds the operation to
one profile, one collection, one exact contact identity, and the revision seen
at review time; applying consumes that binding by its digest. Nothing here
decides *which* contacts to change: survivor choice, merge policy, batching,
and approval workflow belong to the consumer.

Importing this module loads the record transport. Ordinary local reads do not
go through it and stay offline; use the command surface (`status`, `search`,
`show`, `snapshot`, `audit`) for those.
"""

from __future__ import annotations

import sys

from hermes_carddav_contacts import SCRIPTS_DIR

if str(SCRIPTS_DIR) not in sys.path:
    # The shipped skill tree sits beside this package in both the wheel and a
    # checkout; its directory name is not a valid Python identifier, so the
    # scripts directory is placed on the path exactly as the launcher does.
    sys.path.insert(0, str(SCRIPTS_DIR))

import carddav_contacts
import vcards
import writes

CHANGE_SCHEMA_VERSION = vcards.CHANGE_SCHEMA_VERSION
OPERATION_SCHEMA_VERSION = writes.OPERATION_SCHEMA_VERSION
RESULT_SCHEMA_VERSION = writes.RESULT_SCHEMA_VERSION
RECORD_SCHEMA_VERSION = writes.RECORD_SCHEMA_VERSION
PACKAGE_VERSION = carddav_contacts.PACKAGE_VERSION
CAPABILITIES = dict(carddav_contacts.CAPABILITIES)

read_record = writes.read_record
read_collection = writes.read_collection
prepare_create = writes.prepare_create
prepare_update = writes.prepare_update
prepare_replace = writes.prepare_replace
prepare_delete = writes.prepare_delete
apply_operation = writes.apply_operation
reconcile_operation = writes.reconcile_operation

WriteRefused = writes.WriteRefused
InvalidOperationRequest = writes.InvalidOperationRequest
OperationNotFound = writes.OperationNotFound
ContactNotFound = writes.ContactNotFound
RevisionConflict = writes.RevisionConflict
AlreadyExists = writes.AlreadyExists
VerificationFailed = writes.VerificationFailed
UnknownOutcome = writes.UnknownOutcome
ReconciliationRequired = writes.ReconciliationRequired
RemoteUnavailable = writes.RemoteUnavailable
ProfileBindingMismatch = writes.ProfileBindingMismatch

__all__ = [
    "CAPABILITIES",
    "CHANGE_SCHEMA_VERSION",
    "OPERATION_SCHEMA_VERSION",
    "PACKAGE_VERSION",
    "RECORD_SCHEMA_VERSION",
    "RESULT_SCHEMA_VERSION",
    "AlreadyExists",
    "ContactNotFound",
    "InvalidOperationRequest",
    "OperationNotFound",
    "ProfileBindingMismatch",
    "ReconciliationRequired",
    "RemoteUnavailable",
    "RevisionConflict",
    "UnknownOutcome",
    "VerificationFailed",
    "WriteRefused",
    "apply_operation",
    "prepare_create",
    "prepare_delete",
    "prepare_replace",
    "prepare_update",
    "read_collection",
    "read_record",
    "reconcile_operation",
]
