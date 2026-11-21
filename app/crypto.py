"""Encryption of Plaid access tokens at rest (Fernet: AES-128-CBC + HMAC-SHA256).

Tokens are decrypted only in memory, right before a Plaid API call, and are never
logged, rendered in a template, or returned by any endpoint.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.config import get_settings


def _fernet_for(secret: str) -> Fernet:
    secret = secret.strip()
    try:
        return Fernet(secret.encode())  # already a valid Fernet key
    except (ValueError, TypeError):
        # Derive a 32-byte key from an arbitrary long random string.
        digest = hashlib.sha256(b"rewards-autopilot/token-encryption/v1:" + secret.encode()).digest()
        return Fernet(base64.urlsafe_b64encode(digest))


def _cipher() -> MultiFernet:
    raw = get_settings().token_encryption_key
    keys = [k for k in raw.split(",") if k.strip()]
    if not keys:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY is not set")
    return MultiFernet([_fernet_for(k) for k in keys])


def encrypt_token(plaintext: str) -> str:
    return _cipher().encrypt(plaintext.encode()).decode()


def decrypt_token(ciphertext: str) -> str:
    try:
        return _cipher().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError("Could not decrypt a stored Plaid token: TOKEN_ENCRYPTION_KEY is wrong or was changed") from exc


def generate_secret() -> str:
    """A 43-character URL-safe random secret (256 bits)."""
    import secrets

    return secrets.token_urlsafe(32)
