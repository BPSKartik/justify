"""
Private results, sealed: Justify keeps no copy of a private audit that it can read.

The key is 32 random bytes made where the person is — their browser for an upload from the page,
the AI app for code shared in a chat — and sent with the upload. The server holds it in memory
only while that audit runs, writes the result encrypted with AES-256-GCM, and forgets it. What
lands on disk and in Blob Storage is ciphertext; opening it again needs the key, which only the
person has. The page decrypts in the browser with WebCrypto; the server never sees the key again.

Format: b"JSEAL1" + 12-byte nonce + ciphertext and tag. The scan id is the associated data, so a
sealed result cannot be passed off as another scan's.
"""

from __future__ import annotations

import base64
import json
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"JSEAL1"


class SealError(Exception):
    pass


def new_key() -> bytes:
    return os.urandom(32)


def key_text(key: bytes) -> str:
    return base64.urlsafe_b64encode(key).rstrip(b"=").decode()


def parse_key(text: str | None) -> bytes | None:
    """A key as the browser sends it (base64url, no padding), or None if it is not one."""
    if not text or len(text) > 64:
        return None
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (ValueError, TypeError):
        return None
    return raw if len(raw) == 32 else None


def seal(key: bytes, scan_id: str, result: dict) -> bytes:
    nonce = os.urandom(12)
    body = json.dumps(result, separators=(",", ":")).encode()
    return MAGIC + nonce + AESGCM(key).encrypt(nonce, body, scan_id.encode())


def unseal(key: bytes, scan_id: str, data: bytes) -> dict:
    if not data or not data.startswith(MAGIC) or len(data) < len(MAGIC) + 12 + 16:
        raise SealError("not a sealed result")
    nonce, body = data[len(MAGIC):len(MAGIC) + 12], data[len(MAGIC) + 12:]
    try:
        return json.loads(AESGCM(key).decrypt(nonce, body, scan_id.encode()))
    except InvalidTag:
        raise SealError("that key does not open this result") from None
