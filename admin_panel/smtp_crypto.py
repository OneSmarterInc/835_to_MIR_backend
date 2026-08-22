"""
admin_panel/smtp_crypto.py
Fernet symmetric encryption helpers for SMTP passwords stored in the DB.

How it works:
  encrypt_smtp_password(plain_text)  -> base64 cipher-text stored in DB
  decrypt_smtp_password(cipher_text) -> original plain-text used when sending email

The key (SMTP_FIELD_ENCRYPTION_KEY in settings.py) never touches the DB.
"""

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


def _fernet():
    key = getattr(settings, "SMTP_FIELD_ENCRYPTION_KEY", None)
    if not key:
        raise RuntimeError("SMTP_FIELD_ENCRYPTION_KEY is not set in settings.py")
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_smtp_password(plain_text):
    """Return an encrypted, base64-encoded string safe to store in the DB."""
    if not plain_text:
        return ""
    return _fernet().encrypt(plain_text.encode()).decode()


def decrypt_smtp_password(cipher_text):
    """Return the original plain-text password. Returns empty string on any error."""
    if not cipher_text:
        return ""
    try:
        return _fernet().decrypt(cipher_text.encode()).decode()
    except (InvalidToken, Exception):
        return ""
