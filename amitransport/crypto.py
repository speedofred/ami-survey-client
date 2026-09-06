"""Sealing submissions so the public tier cannot read what it holds.

One primitive, chosen rather than assembled: libsodium's sealed box. It
generates an ephemeral keypair per message, does the X25519 exchange against the
recipient's public key, encrypts with XSalsa20-Poly1305 and throws the ephemeral
private key away. The sender is anonymous and cannot decrypt its own message
afterwards, which is exactly right here - a collector has no business reading
back what it submitted.

The private key exists only on the AMI server. The landing server holds the
public half, which is what lets it seal a plaintext arrival on receipt without
ever being able to open one.
"""

from __future__ import annotations

import base64
import hashlib
import os
import stat
from pathlib import Path

from nacl.public import PrivateKey, PublicKey, SealedBox
from nacl.exceptions import CryptoError

KEY_FILE_MODE = 0o600


class SealError(RuntimeError):
    """A submission could not be sealed or opened."""


def generate_keypair() -> tuple[bytes, bytes]:
    """A new (private, public) pair, raw 32-byte values."""
    private = PrivateKey.generate()
    return bytes(private), bytes(private.public_key)


def key_id(public_key: bytes) -> str:
    """A short, stable name for a public key.

    Travels with every envelope so a submission sealed to a retired key is
    recognised as such rather than failing as corruption.
    """
    return hashlib.sha256(public_key).hexdigest()[:16]


def encode_key(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def decode_key(text: str) -> bytes:
    try:
        raw = base64.b64decode(text.strip(), validate=True)
    except (ValueError, TypeError) as exc:
        raise SealError(f"Key is not valid base64: {exc}") from exc
    if len(raw) != 32:
        raise SealError(f"Key must be 32 bytes, got {len(raw)}.")
    return raw


def write_private_key(path: Path, private_key: bytes) -> None:
    """Write a private key readable only by its owner.

    Created with the right mode rather than chmod'ed afterwards: between the two
    there is a window where the key is on disk and world-readable.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, KEY_FILE_MODE)
    with os.fdopen(fd, "w", encoding="ascii") as handle:
        handle.write(encode_key(private_key) + "\n")


def read_private_key(path: Path) -> bytes:
    if not path.exists():
        raise SealError(f"No private key at {path}.")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise SealError(
            f"Private key at {path} is readable by others (mode {mode:o}). "
            "Refusing to use it; run chmod 600 on it first."
        )
    return decode_key(path.read_text(encoding="ascii"))


def seal(payload: bytes, public_key: bytes) -> bytes:
    try:
        return SealedBox(PublicKey(public_key)).encrypt(payload)
    except (CryptoError, TypeError, ValueError) as exc:
        raise SealError(f"Could not seal payload: {exc}") from exc


def unseal(sealed: bytes, private_key: bytes) -> bytes:
    """Open a sealed payload.

    A failure here is deliberately not distinguished into "wrong key" and
    "tampered": both mean this server cannot vouch for the bytes, and reporting
    which would tell an attacker whether they had guessed the right key.
    """
    try:
        return SealedBox(PrivateKey(private_key)).decrypt(sealed)
    except (CryptoError, TypeError, ValueError) as exc:
        raise SealError("Sealed payload could not be opened with this key.") from exc


def public_key_of(private_key: bytes) -> bytes:
    return bytes(PrivateKey(private_key).public_key)
