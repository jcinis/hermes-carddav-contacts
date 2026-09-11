"""Pure, frozen contact identity functions; never expose the raw UID."""

import hashlib
import re


def contact_id(uid: object) -> str:
    """Hash a decoded vCard UID after one Python str.strip(), without NFC."""
    if not isinstance(uid, str):
        raise ValueError("invalid contact UID")  # noqa: TRY004 — uniform validation boundary
    stripped = uid.strip()
    if not stripped:
        raise ValueError("invalid contact UID")
    try:
        payload = stripped.encode("utf-8", "strict")
    except UnicodeError:
        raise ValueError("invalid contact UID") from None
    return hashlib.sha256(payload).hexdigest()[:16]


def contact_ref(namespace: object, opaque_id: object) -> str:
    """Qualify an already opaque profile-scoped ID, without rehashing it."""
    if (
        not isinstance(namespace, str)
        or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", namespace)
        or not isinstance(opaque_id, str)
        or not re.fullmatch(r"[0-9a-f]{16}", opaque_id)
    ):
        raise ValueError("invalid contact reference")
    return f"carddav:{namespace}:{opaque_id}"
