"""Canonical JSON: the one serialization behind every hash in the gateway (ADR 0004).

Audit row hashes, quote hashes and the mandate content hash all go through `canonical`, so
there is a single rule for field order, encoding and number formatting.
"""

import hashlib
import json

_SCALARS = (str, int, bool, type(None))


class CanonicalError(ValueError):
    """A value cannot be encoded canonically (a float, an unsupported type, bad UTF-8)."""


def _check(value: object, path: str) -> None:
    kind = type(value)
    if kind is float:
        raise CanonicalError(f"float at {path}: money and time are integers")
    if kind in _SCALARS:
        if kind is str:
            try:
                value.encode("utf-8")  # type: ignore[union-attr]
            except UnicodeEncodeError as exc:
                raise CanonicalError(f"string at {path} is not valid UTF-8") from exc
        return
    if kind is list or kind is tuple:
        for index, item in enumerate(value):  # type: ignore[arg-type]
            _check(item, f"{path}[{index}]")
        return
    if kind is dict:
        for key, item in value.items():  # type: ignore[union-attr]
            if type(key) is not str:
                raise CanonicalError(f"non-string key at {path}")
            _check(key, f"{path}.<key>")
            _check(item, f"{path}.{key}")
        return
    raise CanonicalError(f"unsupported type {kind.__name__} at {path}")


def canonical(value: object) -> bytes:
    """Encode `value` as canonical JSON bytes: sorted keys, no whitespace, UTF-8, no floats."""
    _check(value, "$")
    text = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return text.encode("utf-8")


def canonical_text(value: object) -> str:
    """Canonical JSON as a string, for storing in a TEXT column."""
    return canonical(value).decode("utf-8")


def sha256_hex(data: bytes) -> str:
    """Lowercase hex SHA-256 of `data`."""
    return hashlib.sha256(data).hexdigest()


def canonical_hash(value: object) -> str:
    """SHA-256 of the canonical encoding of `value`."""
    return sha256_hex(canonical(value))
