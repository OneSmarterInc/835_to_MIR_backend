"""Authenticated encryption helpers for SMTP and SFTP secrets.

Keys are loaded only from Django settings/environment variables.  Plaintext
credentials returned by the runtime resolver must never be serialized or
logged.
"""

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


class FieldEncryptionError(RuntimeError):
    """Base error for encrypted credential operations."""


class CredentialDecryptionError(FieldEncryptionError):
    """Raised when ciphertext cannot be decrypted with the configured key."""


class SFTPCredentialError(FieldEncryptionError):
    """Raised when saved SFTP runtime credentials cannot be resolved."""


def _fernet(setting_name):
    key = getattr(settings, setting_name, None)
    if not key:
        raise FieldEncryptionError(f"{setting_name} is not configured")
    if isinstance(key, str):
        key = key.encode("utf-8")
    try:
        return Fernet(key)
    except (TypeError, ValueError) as exc:
        raise FieldEncryptionError(
            f"{setting_name} is not a valid Fernet key"
        ) from exc


def encrypt_field(value, setting_name):
    """Encrypt a string and return URL-safe base64 ciphertext for DB storage."""
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        value = str(value)
    return _fernet(setting_name).encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_field(value, setting_name):
    """Decrypt database ciphertext. Empty values remain empty."""
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        value = str(value)
    try:
        return _fernet(setting_name).decrypt(
            value.encode("utf-8")
        ).decode("utf-8")
    except InvalidToken as exc:
        raise CredentialDecryptionError(
            f"Unable to decrypt credential with {setting_name}"
        ) from exc


def encrypt_smtp_password(value):
    return encrypt_field(value, "SMTP_FIELD_ENCRYPTION_KEY")


def decrypt_smtp_password(value):
    return decrypt_field(value, "SMTP_FIELD_ENCRYPTION_KEY")


def encrypt_sftp_field(value):
    return encrypt_field(value, "SFTP_FIELD_ENCRYPTION_KEY")


def decrypt_sftp_field(value):
    return decrypt_field(value, "SFTP_FIELD_ENCRYPTION_KEY")


def _decrypt_sftp_secret(value, label):
    if not value:
        return ""
    try:
        return decrypt_sftp_field(value)
    except (CredentialDecryptionError, FieldEncryptionError) as exc:
        raise SFTPCredentialError(
            f"Saved SFTP {label} could not be decrypted. "
            "Verify SFTP_FIELD_ENCRYPTION_KEY."
        ) from exc


def get_sftp_runtime_credentials(config, outbound=False):
    """Resolve saved SFTP secrets for temporary backend runtime use only."""
    if config is None:
        raise SFTPCredentialError("SFTP configuration was not found.")

    purpose = getattr(config, "purpose", "DEFAULT") or "DEFAULT"
    if purpose != "DEFAULT":
        return {
            "host": config.host or "",
            "port": int(config.port or 22),
            "username": config.username or "",
            "password": _decrypt_sftp_secret(config.password, "password"),
            "ssh_key": _decrypt_sftp_secret(config.ssh_key, "SSH private key"),
            "auth_method": config.auth_method or "Password",
            "trust_unknown_key": config.trust_unknown_key,
            "remote_folder": config.remote_folder or "/",
        }

    separate_outbound = outbound and not config.use_same_server
    if separate_outbound:
        return {
            "host": config.outbound_host or "",
            "port": int(config.outbound_port or 22),
            "username": config.outbound_username or "",
            "password": _decrypt_sftp_secret(
                config.outbound_password, "outbound password"
            ),
            "ssh_key": _decrypt_sftp_secret(
                config.outbound_ssh_key, "outbound SSH private key"
            ),
            "auth_method": config.outbound_auth_method or "Password",
            "trust_unknown_key": config.outbound_trust_unknown_key,
            "remote_folder": config.outbound_mir_folder or "/",
        }

    return {
        "host": config.host or "",
        "port": int(config.port or 22),
        "username": config.username or "",
        "password": _decrypt_sftp_secret(config.password, "password"),
        "ssh_key": _decrypt_sftp_secret(config.ssh_key, "SSH private key"),
        "auth_method": config.auth_method or "Password",
        "trust_unknown_key": config.trust_unknown_key,
        "remote_folder": (
            config.remote_folder
            or (config.outbound_mir_folder if outbound else config.inbound_835_folder)
        ) or "/",
    }
