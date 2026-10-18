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


import osivault.fields
from osivault.fields.crypto import FieldEncryptionError as OSIVaultFieldEncryptionError


def encrypt_field(value, setting_name="OSIVAULT_FIELD_KEY", context=None):
    """Encrypt a string and return OSIVault AES-256-GCM self-describing envelope."""
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        value = str(value)
    return osivault.fields.encrypt(value, context=context)


def decrypt_field(value, setting_name=None, context=None):
    """Decrypt database ciphertext. Empty values remain empty.
    
    Supports dual-mode:
    1. OSIVault AES-256-GCM envelope (starting with 'OSV1$AES-256-GCM$').
    2. Legacy Fernet tokens (for backward compatibility during migration window).
    """
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        value = str(value)

    # 1. Primary: OSIVault AES-256-GCM envelope
    if value.startswith("OSV1$AES-256-GCM$"):
        try:
            return osivault.fields.decrypt(value, context=context)
        except OSIVaultFieldEncryptionError:
            # Fallback retry without context if context mismatch
            if context is not None:
                try:
                    return osivault.fields.decrypt(value, context=None)
                except OSIVaultFieldEncryptionError as exc:
                    raise CredentialDecryptionError(
                        f"OSIVault envelope decryption failed: {str(exc)}"
                    ) from exc
            raise

    # 2. Backward compatibility: Legacy Fernet decryption
    if setting_name:
        try:
            return _fernet(setting_name).decrypt(
                value.encode("utf-8")
            ).decode("utf-8")
        except InvalidToken as exc:
            raise CredentialDecryptionError(
                f"Unable to decrypt credential with {setting_name}"
            ) from exc

    # Try default SFTP / SMTP Fernet keys if setting_name was not provided
    for fallback_setting in ("SFTP_FIELD_ENCRYPTION_KEY", "SMTP_FIELD_ENCRYPTION_KEY"):
        try:
            return _fernet(fallback_setting).decrypt(
                value.encode("utf-8")
            ).decode("utf-8")
        except Exception:
            continue

    raise CredentialDecryptionError(
        "Unable to decrypt credential: unrecognized ciphertext format or invalid key"
    )


def encrypt_smtp_password(value):
    return encrypt_field(value, "SMTP_FIELD_ENCRYPTION_KEY", context="smtp:password")


def decrypt_smtp_password(value):
    return decrypt_field(value, "SMTP_FIELD_ENCRYPTION_KEY", context="smtp:password")


def encrypt_sftp_field(value):
    return encrypt_field(value, "SFTP_FIELD_ENCRYPTION_KEY", context="sftp:secret")


def decrypt_sftp_field(value):
    return decrypt_field(value, "SFTP_FIELD_ENCRYPTION_KEY", context="sftp:secret")



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
