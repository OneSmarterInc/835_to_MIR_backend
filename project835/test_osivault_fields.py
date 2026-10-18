"""
Comprehensive Conformance Test Suite for OSIVault Fields Integration in MIR Portal.
Tests AES-256-GCM envelope encryption at rest, transparent ORM decryption,
legacy Fernet backward compatibility, SFTP/SMTP runtime credential resolution,
TOTP secret protection, and EDI 835 raw claim encryption.
"""

import os
import pyotp
from cryptography.fernet import Fernet
from django.conf import settings
from django.db import connection
from django.test import TestCase

from accounts.models import User
from edi835.models import EDI835File, EDI835Claim, SFTPConfig
from project835.field_crypto import (
    encrypt_field,
    decrypt_field,
    encrypt_sftp_field,
    decrypt_sftp_field,
    encrypt_smtp_password,
    decrypt_smtp_password,
    get_sftp_runtime_credentials,
    CredentialDecryptionError,
)
from osivault.fields import SearchHash, rotate_dek


class OSIVaultFieldsIntegrationTests(TestCase):

    def setUp(self):
        self.plain_password = "P@ssw0rd_Enterprise_2026!"
        self.plain_ssh_key = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA0...\n-----END RSA PRIVATE KEY-----"
        self.totp_seed = "JBSWY3DPEHPK3PXP"
        self.raw_835 = "CLP*CLAIM001*1*1500.00*1200.00*300.00*MC*123456789~"

    def test_01_sftp_field_encrypt_decrypt_roundtrip(self):
        """Verify encrypt_sftp_field produces OSV1 AES-256-GCM envelope and decrypts transparently."""
        envelope = encrypt_sftp_field(self.plain_password)
        self.assertTrue(envelope.startswith("OSV1$AES-256-GCM$default$"))
        self.assertNotEqual(envelope, self.plain_password)

        decrypted = decrypt_sftp_field(envelope)
        self.assertEqual(decrypted, self.plain_password)

    def test_02_smtp_password_encrypt_decrypt_roundtrip(self):
        """Verify encrypt_smtp_password produces OSV1 AES-256-GCM envelope and decrypts transparently."""
        envelope = encrypt_smtp_password(self.plain_password)
        self.assertTrue(envelope.startswith("OSV1$AES-256-GCM$default$"))

        decrypted = decrypt_smtp_password(envelope)
        self.assertEqual(decrypted, self.plain_password)

    def test_03_legacy_fernet_backward_compatibility(self):
        """Verify legacy Fernet tokens existing in database decrypt smoothly during migration."""
        key = getattr(settings, "SFTP_FIELD_ENCRYPTION_KEY", None)
        fernet = Fernet(key.encode("utf-8") if isinstance(key, str) else key)
        legacy_token = fernet.encrypt(b"legacy_unmigrated_sftp_secret").decode("utf-8")

        self.assertFalse(legacy_token.startswith("OSV1$"))
        decrypted = decrypt_sftp_field(legacy_token)
        self.assertEqual(decrypted, "legacy_unmigrated_sftp_secret")

    def test_04_tampered_envelope_rejection(self):
        """Verify tampered ciphertext or altered MAC tag fails closed with CredentialDecryptionError."""
        envelope = encrypt_sftp_field(self.plain_password)
        # Flip a character in the ciphertext payload
        corrupted = envelope[:-2] + ("0" if envelope[-1] != "0" else "1")

        with self.assertRaises(CredentialDecryptionError):
            decrypt_sftp_field(corrupted)

    def test_05_sftp_runtime_credentials_resolver(self):
        """Verify get_sftp_runtime_credentials decrypts SFTPConfig credentials correctly."""
        config = SFTPConfig.objects.create(
            name="Production SFTP",
            host="sftp.hospital.org",
            username="claims_uploader",
            password=encrypt_sftp_field(self.plain_password),
            ssh_key=encrypt_sftp_field(self.plain_ssh_key),
            use_same_server=True,
        )

        creds = get_sftp_runtime_credentials(config)
        self.assertEqual(creds["host"], "sftp.hospital.org")
        self.assertEqual(creds["username"], "claims_uploader")
        self.assertEqual(creds["password"], self.plain_password)
        self.assertEqual(creds["ssh_key"], self.plain_ssh_key)

    def test_06_accounts_user_totp_secret_encrypted_at_rest(self):
        """Verify User.totp_secret is stored as OSV1 envelope in SQL, decrypts via ORM, and verifies TOTP."""
        user = User.objects.create(
            email="osivault_totp_user@test.org",
            name="TOTP Tester",
            mobile="9876543210",
            totp_secret=self.totp_seed,
            totp_enabled=True,
        )

        # 1. Raw SQL query directly from PostgreSQL / SQLite:
        with connection.cursor() as cursor:
            cursor.execute("SELECT totp_secret FROM accounts_user WHERE id = %s", [user.id])
            raw_sql_value = cursor.fetchone()[0]

        self.assertTrue(raw_sql_value.startswith("OSV1$AES-256-GCM$"))
        self.assertNotIn(self.totp_seed, raw_sql_value)

        # 2. ORM transparent access:
        retrieved_user = User.objects.get(id=user.id)
        self.assertEqual(retrieved_user.totp_secret, self.totp_seed)

        # 3. PyOTP operational verification:
        totp = pyotp.TOTP(retrieved_user.totp_secret)
        current_token = totp.now()
        self.assertTrue(totp.verify(current_token, valid_window=0))

    def test_07_edi835_claim_raw_claim_encrypted_at_rest(self):
        """Verify EDI835Claim.raw_claim is encrypted at rest in SQL and transparently decrypted via ORM."""
        edi_file = EDI835File.objects.create(
            status="PROCESSING"
        )
        claim = EDI835Claim.objects.create(
            edi_file=edi_file,
            claim_sequence=1,
            highmark_claim_number="HM-2026-9999",
            raw_claim=self.raw_835,
        )

        # 1. Raw SQL verification:
        with connection.cursor() as cursor:
            cursor.execute('SELECT raw_claim FROM "835_claim" WHERE id = %s', [claim.id])
            raw_sql_value = cursor.fetchone()[0]

        self.assertTrue(raw_sql_value.startswith("OSV1$AES-256-GCM$"))
        self.assertNotIn("CLP*CLAIM001", raw_sql_value)

        # 2. ORM transparent decryption:
        retrieved_claim = EDI835Claim.objects.get(id=claim.id)
        self.assertEqual(retrieved_claim.raw_claim, self.raw_835)

    def test_08_search_hash_blind_indexing(self):
        """Verify SearchHash generates deterministic HMAC-SHA-256 index for querying encrypted fields."""
        ssn = "999-88-7777"
        hash_1 = SearchHash(ssn)
        hash_2 = SearchHash(ssn)

        self.assertEqual(hash_1, hash_2)
        self.assertNotEqual(hash_1, ssn)
        self.assertEqual(len(hash_1), 64)

    def test_09_dek_rotation_capability(self):
        """Verify rotate_dek re-encrypts ciphertext envelope under fresh DEK with zero plaintext leakage."""
        envelope_v1 = encrypt_sftp_field(self.plain_password)
        envelope_v2 = rotate_dek(envelope_v1, context="sftp:secret")

        self.assertNotEqual(envelope_v1, envelope_v2)
        self.assertTrue(envelope_v2.startswith("OSV1$AES-256-GCM$"))
        self.assertEqual(decrypt_sftp_field(envelope_v2), self.plain_password)


    def test_10_empty_and_null_value_safety(self):
        """Verify empty and None inputs return empty strings without raising exceptions."""
        self.assertEqual(encrypt_sftp_field(""), "")
        self.assertEqual(encrypt_sftp_field(None), "")
        self.assertEqual(decrypt_sftp_field(""), "")
        self.assertEqual(decrypt_sftp_field(None), "")
