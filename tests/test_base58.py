"""Identifier encoding — the one place a bug is unrecoverable.

A malformed identifier is accepted by the server and then permanently
corrupts the account history, so these tests are deliberately paranoid.
"""

from __future__ import annotations

import uuid as _uuid

import pytest

from things_cli import base58


# Captured from real Things.app traffic (via things-cloud-sdk's test vectors).
REAL_IDENTIFIERS = [
    "VJ1edXTP9q3PmFDUuy8EQh",
    "FQxaqvLBkbR5q2Q5oRoknc",
    "BVU8qZ9dNjrdxLvDHPvfDS",
]


@pytest.mark.parametrize("ident", REAL_IDENTIFIERS)
def test_real_identifiers_round_trip_unchanged(ident):
    """Things' own ids must survive a decode/encode cycle byte-for-byte —
    if they don't, we're not speaking canonical Base58."""
    assert base58.encode(base58.decode(ident)) == ident


@pytest.mark.parametrize("ident", REAL_IDENTIFIERS)
def test_real_identifiers_decode_to_16_bytes(ident):
    assert len(base58.decode(ident)) == 16


def test_leading_zero_bytes_are_padded():
    """The bug that poisoned accounts: big-int conversion drops leading
    zero bytes, so a UUID starting 0x00 encodes one character short and
    no longer decodes to 16 bytes. Canonical Base58 emits one '1' per
    leading zero byte."""
    for zeros in range(1, 5):
        raw = b"\x00" * zeros + bytes(range(1, 17 - zeros))
        encoded = base58.encode(raw)
        assert encoded.startswith("1" * zeros)
        assert base58.decode(encoded) == raw


def test_all_zero_identifier_round_trips():
    raw = b"\x00" * 16
    assert base58.encode(raw) == "1" * 16
    assert base58.decode("1" * 16) == raw


def test_generated_identifiers_are_always_canonical():
    """Byte 0 of a v4 UUID is random, so ~1 in 256 draws exercises the
    leading-zero path. 2000 draws makes a regression essentially certain
    to be caught."""
    for _ in range(2000):
        ident = base58.new_uuid()
        assert base58.decode(ident)  # 16 bytes or raises
        assert base58.encode(base58.decode(ident)) == ident


def test_round_trip_from_uuid_bytes():
    for _ in range(200):
        raw = _uuid.uuid4().bytes
        assert base58.decode(base58.encode(raw)) == raw


@pytest.mark.parametrize(
    "bad, reason",
    [
        ("", "empty"),
        ("0OIl", "ambiguous characters are not in the alphabet"),
        # 20 chars is genuinely too short; note 21 is not — a 21-char
        # string still spans 16 bytes, so length alone can't be the check.
        ("VJ1edXTP9q3PmFDUuy8E", "decodes to fewer than 16 bytes"),
        ("zzzzzzzzzzzzzzzzzzzzzz", "value overflows 16 bytes"),
        ("not-a-valid-id!", "invalid characters"),
        (str(_uuid.uuid4()), "a plain UUID string is not Base58"),
    ],
)
def test_invalid_identifiers_are_rejected(bad, reason):
    with pytest.raises(base58.Base58Error):
        base58.decode(bad)


def test_validate_returns_input_when_safe():
    assert base58.validate(REAL_IDENTIFIERS[0]) == REAL_IDENTIFIERS[0]


def test_alphabet_excludes_ambiguous_characters():
    for ch in "0OIl":
        assert ch not in base58.ALPHABET
    assert len(base58.ALPHABET) == 58
