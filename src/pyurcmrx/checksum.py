"""32-bit rotating byte-sum used in staged filename tokens (spec §20 §3)."""

from __future__ import annotations


def rotating_checksum(data: bytes) -> int:
    """Sum of `(b << (i % 8)) | (b >> (8 - i % 8))` over all bytes, modulo 2**32.

    Each term is evaluated without 8-bit truncation, so it spans up to 15 bits.
    """
    total = 0
    for i, b in enumerate(data):
        shift = i & 7
        total += (b << shift) | (b >> (8 - shift)) if shift else b
    return total & 0xFFFFFFFF
