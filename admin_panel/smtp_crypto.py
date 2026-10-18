"""
admin_panel/smtp_crypto.py
Fernet symmetric encryption helpers for SMTP passwords stored in the DB.

How it works:
  encrypt_smtp_password(plain_text)  -> base64 cipher-text stored in DB
  decrypt_smtp_password(cipher_text) -> original plain-text used when sending email

The key (SMTP_FIELD_ENCRYPTION_KEY in settings.py) never touches the DB.
"""

from project835.field_crypto import (
    encrypt_smtp_password,
    decrypt_smtp_password,
)

__all__ = ["encrypt_smtp_password", "decrypt_smtp_password"]

