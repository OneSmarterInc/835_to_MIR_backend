"""
Resolve encrypted SFTP configuration fields for runtime use.

This module must only be used inside the backend. Resolved credentials
must never be returned through an API or written to logs.
"""

from project835.field_crypto import decrypt_sftp_field


class SFTPCredentialError(Exception):
    """Raised when saved SFTP credentials cannot be decrypted."""


def _decrypt_secret(encrypted_value, field_name):
    """
    Decrypt one optional database field.

    Empty database values return an empty string. Decryption failures
    raise a controlled exception without including ciphertext.
    """
    if not encrypted_value:
        return ""

    try:
        return decrypt_sftp_field(encrypted_value)
    except Exception as exc:
        raise SFTPCredentialError(
            f"Saved SFTP {field_name} could not be decrypted. "
            "Verify SFTP_FIELD_ENCRYPTION_KEY."
        ) from exc


def get_sftp_runtime_credentials(config, outbound=False):
    """
    Return decrypted SFTP credentials for temporary backend use.

    outbound=False:
        Resolve inbound/unified server credentials.

    outbound=True:
        If use_same_server is False, resolve separate outbound
        credentials. Otherwise use the unified credentials.

    Never pass the returned dictionary to JsonResponse or logging.
    """
    if config is None:
        raise SFTPCredentialError(
            "SFTP configuration was not found."
        )

    use_separate_outbound = (
        outbound and not config.use_same_server
    )

    if use_separate_outbound:
        return {
            "host": config.outbound_host or "",
            "port": int(config.outbound_port or 22),
            "username": config.outbound_username or "",
            "password": _decrypt_secret(
                config.outbound_password,
                "outbound password",
            ),
            "ssh_key": _decrypt_secret(
                config.outbound_ssh_key,
                "outbound SSH private key",
            ),
            "auth_method": (
                config.outbound_auth_method
                or "Password"
            ),
            "trust_unknown_key": (
                config.outbound_trust_unknown_key
            ),
            "remote_folder": (
                config.outbound_mir_folder or "/"
            ),
        }

    return {
        "host": config.host or "",
        "port": int(config.port or 22),
        "username": config.username or "",
        "password": _decrypt_secret(
            config.password,
            "password",
        ),
        "ssh_key": _decrypt_secret(
            config.ssh_key,
            "SSH private key",
        ),
        "auth_method": (
            config.auth_method or "Password"
        ),
        "trust_unknown_key": (
            config.trust_unknown_key
        ),
        "remote_folder": (
            config.outbound_mir_folder
            if outbound
            else config.inbound_835_folder
        ) or "/",
    }