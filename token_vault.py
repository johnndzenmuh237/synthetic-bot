"""token_vault.py — keeps each user's Deriv API tokens encrypted at rest.

A token can place real trades, so it is never stored (locally or in Firebase) as plain text, and it is
never sent back to the browser. The encryption key comes from TOKEN_ENCRYPTION_KEY (recommended) or,
if that is not set, from SECRET_KEY. Keep that value fixed: if it changes, saved tokens can no longer be
decrypted and users simply have to enter them again.
"""
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from config import TOKEN_ENCRYPTION_KEY, SECRET_KEY


def _fernet():
    secret = (TOKEN_ENCRYPTION_KEY or SECRET_KEY or "dev-secret").encode("utf-8")
    key = base64.urlsafe_b64encode(hashlib.sha256(b"synthbot-token-vault|" + secret).digest())
    return Fernet(key)


def encrypt(token):
    token = (token or "").strip()
    return _fernet().encrypt(token.encode("utf-8")).decode("ascii") if token else ""


def decrypt(blob):
    """Plain token, or "" if there is none / it cannot be decrypted (key changed)."""
    if not blob:
        return ""
    try:
        return _fernet().decrypt(blob.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError):
        return ""


def hint(token):
    """Safe display form: only the last 4 characters."""
    token = (token or "").strip()
    return ("••••" + token[-4:]) if len(token) >= 8 else ("•" * 6 if token else "")