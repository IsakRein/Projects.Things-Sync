"""Canonical Base58 identifiers, exactly as Things Cloud requires.

Things.app decodes every UUID key in the sync history with
``BSIdentifierFromBase58String()``. Anything that is not a canonical
Bitcoin-alphabet Base58 encoding of exactly 16 bytes — including encodings
that drop the leading-zero padding ('1' per leading 0x00 byte) — poisons
the history permanently and crashes every real client on sync. Always
generate ids with :func:`new_uuid` and validate anything user-supplied
with :func:`validate` before it reaches a commit.
"""

from __future__ import annotations

import uuid as _uuid

ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_INDEX = {c: i for i, c in enumerate(ALPHABET)}


class Base58Error(ValueError):
    """A non-canonical or malformed Things identifier."""


def encode(raw: bytes) -> str:
    """Canonical Base58: big-endian base conversion plus one leading '1'
    per leading zero byte."""
    zeros = 0
    for b in raw:
        if b:
            break
        zeros += 1
    n = int.from_bytes(raw, "big")
    out: list[str] = []
    while n > 0:
        n, rem = divmod(n, 58)
        out.append(ALPHABET[rem])
    return "1" * zeros + "".join(reversed(out))


def decode(s: str) -> bytes:
    """Decode to exactly 16 bytes, rejecting non-canonical input."""
    if not s:
        raise Base58Error("empty identifier")
    zeros = 0
    for c in s:
        if c != "1":
            break
        zeros += 1
    n = 0
    for c in s[zeros:]:
        idx = _INDEX.get(c)
        if idx is None:
            raise Base58Error(f"invalid Base58 character {c!r} in identifier {s!r}")
        n = n * 58 + idx
    value = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    if zeros + len(value) != 16:
        raise Base58Error(
            f"identifier {s!r} decodes to {zeros + len(value)} bytes, want 16"
        )
    return b"\x00" * zeros + value


def validate(s: str) -> str:
    """Return ``s`` if it is a safe Things identifier, else raise."""
    decode(s)
    return s


def new_uuid() -> str:
    """A fresh random identifier in Things' canonical wire format."""
    return encode(_uuid.uuid4().bytes)
