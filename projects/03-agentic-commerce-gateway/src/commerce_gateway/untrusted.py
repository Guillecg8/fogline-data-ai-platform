"""Handling of untrusted text (product text and the search query) before it leaves the gateway."""

import unicodedata

from commerce_gateway.canonical import sha256_hex

# Unicode categories removed: Cc (control) and Cf (format, which includes bidi overrides and
# zero-width characters that can hide text from a human reader).
_REMOVED_CATEGORIES = frozenset({"Cc", "Cf"})


def sanitize(text: str, max_chars: int) -> tuple[str, bool]:
    """Remove control and format characters, then cap the length. Returns (text, was_cut)."""
    clean = "".join(ch for ch in text if unicodedata.category(ch) not in _REMOVED_CATEGORIES)
    if len(clean) > max_chars:
        return clean[:max_chars], True
    return clean, False


def query_audit_fields(query: str, max_chars: int) -> dict[str, object]:
    """Audit payload for an accepted search query (ADR 0004): its hash plus sanitised text.

    Only accepted queries (at most `max_chars`) get here; a rejected query is described by
    `tools.RejectedValue`, which never holds text.
    """
    return {
        "query_sha256": sha256_hex(query.encode("utf-8")),
        "query_text": sanitize(query, max_chars)[0],
        "untrusted": True,
    }
