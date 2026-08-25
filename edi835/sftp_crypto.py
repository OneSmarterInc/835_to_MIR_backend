"""Fernet helpers for SFTP credentials stored in the database.

SFTP and SMTP intentionally use the same SMTP_FIELD_ENCRYPTION_KEY setting.
The key stays in the environment and is never stored in the database.
"""

from admin_panel.smtp_crypto import (
    decrypt_smtp_password,
    encrypt_smtp_password,
)


def encrypt_sftp_secret(plain_text):
    """Encrypt an SFTP password/passphrase for database storage."""
    return encrypt_smtp_password(plain_text)


def decrypt_sftp_secret(cipher_text):
    """Decrypt an SFTP password/passphrase immediately before use."""
    return decrypt_smtp_password(cipher_text)
