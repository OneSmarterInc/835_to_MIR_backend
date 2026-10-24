"""Synthetic certificates only. Gateway chain/CRL tests run separately on Nginx."""
import io
import json
from datetime import timedelta
from urllib.parse import quote

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command, CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from admin_panel.models import AuditLog
from . import test_intake as fixtures
from .models import InboundTransfer, PartnerCredential, PartnerRoute, SubmissionKey
from .partner_auth import accepted_authorization_valid
from .partner_management import change_partner, create_partner
from .partner_routing import ROUTE_FIELDS, envelope_routes, EnvelopeError


def certificate(*, expired=False, ca=False, client_usage=True):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Synthetic client")])
    now = timezone.now()
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=2))
        .not_valid_after(now + timedelta(days=-1 if expired else 30))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH if client_usage else ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM)


@override_settings(INTAKE_PARTNER_ENABLED=True, MFA_ENFORCEMENT_ENABLED=False,
    INTAKE_ENFORCE_PARTNER_ROUTES=True,
    INTAKE_MTLS_ENABLED=True, INTAKE_MTLS_GATEWAY_SECRET="synthetic-gateway-secret-never-use-in-production",
    INTAKE_MTLS_TRUSTED_PEERS=["127.0.0.1"])
class PartnerSecurityTests(TestCase):
    def setUp(self):
        fixtures.IntakeTests.setUp(self)
        self.raw = (settings.BASE_DIR / "examples/synthetic_claim.837").read_bytes()
        self.pem = certificate()
        self.cert = self.change("CERTIFICATE", {"certificate": self.pem})
        self.change("POLICY", {"auth_mode": "MTLS"})
        self.partner.refresh_from_db()
        route = next(envelope_routes(io.BufferedReader(io.BytesIO(self.raw)), "837"))
        self.route_data = dict(zip(ROUTE_FIELDS, route), workflow="837")
        self.change("ROUTE", self.route_data)

    def change(self, action, data=None):
        return change_partner(self.partner.pk, action=action, data=data, actor="synthetic-operator", reason="Synthetic acceptance test")

    def headers(self, **overrides):
        headers = dict(secure=True, REMOTE_ADDR="127.0.0.1",
            HTTP_X_FILEX_GATEWAY=settings.INTAKE_MTLS_GATEWAY_SECRET,
            HTTP_X_FILEX_CLIENT_VERIFY="SUCCESS", HTTP_X_FILEX_CLIENT_CERT=quote(self.pem.decode(), safe=""))
        headers.update(overrides)
        return headers

    def submit(self, raw=None, path="/api/v1/transfers", key="synthetic-key", **overrides):
        return self.client.post(path, {"workflow": "837", "file": SimpleUploadedFile("synthetic.837", raw or self.raw)},
            HTTP_IDEMPOTENCY_KEY=key, **self.headers(**overrides))

    def test_mtls_receipt_retains_credential_and_route_proof(self):
        response = self.submit()
        self.assertEqual(response.status_code, 202, response.content)
        receipt = InboundTransfer.objects.get()
        self.assertEqual(str(receipt.partner_credential_id), self.cert["credential_id"])
        self.assertEqual(receipt.partner_authorization["routes"][0]["route_id"], self.partner.routes.get().pk)
        self.assertEqual(receipt.partner_id, self.partner.pk)

    def test_legacy_endpoint_enforces_the_same_mtls_policy(self):
        response = self.submit(path="/edi835/api/intake/partner/")
        self.assertEqual(response.status_code, 202, response.content)
        self.assertEqual(self.submit(raw=self.raw.replace(b"SYNTHETIC", b"UNTRUSTED"),
            path="/edi835/api/intake/partner/", key="other").status_code, 409)
        self.assertEqual(InboundTransfer.objects.count(), 1)

    def test_spoofed_headers_never_fall_back_to_staff_session(self):
        self.client.force_login(self.admin)
        for override in ({"REMOTE_ADDR": "203.0.113.5"}, {"HTTP_X_FILEX_GATEWAY": "wrong"},
                {"HTTP_X_FILEX_CLIENT_VERIFY": "FAILED:revoked"}, {"HTTP_X_FILEX_CLIENT_CERT": "invalid"},
                {"secure": False}, {"HTTP_AUTHORIZATION": self.authorization},
                {"HTTP_X_FILEX_CLIENT_CERT": "x" * 24577}):
            with self.subTest(override=list(override)):
                self.assertEqual(self.submit(**override).status_code, 401)
        self.assertFalse(InboundTransfer.objects.exists())

    def test_disabled_gateway_and_empty_secret_fail_closed(self):
        with override_settings(INTAKE_MTLS_ENABLED=False):
            self.assertEqual(self.submit().status_code, 401)
        with override_settings(INTAKE_MTLS_GATEWAY_SECRET="", INTAKE_MTLS_TRUSTED_PEERS=[]):
            self.assertEqual(self.submit().status_code, 401)

    def test_unknown_expired_ca_and_server_only_certificates_rejected(self):
        for pem in (certificate(), certificate(expired=True), certificate(ca=True), certificate(client_usage=False)):
            with self.subTest(pem_length=len(pem)):
                self.assertEqual(self.submit(HTTP_X_FILEX_CLIENT_CERT=quote(pem.decode(), safe="")).status_code, 401)
        for pem in (certificate(expired=True), certificate(ca=True), certificate(client_usage=False)):
            with self.assertRaises(ValueError):
                self.change("CERTIFICATE", {"certificate": pem})

    def test_mtls_required_blocks_bearer_on_both_endpoints(self):
        for path in ("/api/v1/transfers", "/edi835/api/intake/partner/"):
            response = self.client.get(path, secure=True, HTTP_AUTHORIZATION=self.authorization)
            self.assertEqual(response.status_code, 401)

    def test_certificate_rotation_preserves_keys_receipts_and_overlap(self):
        first = self.submit().json()
        old_pem = self.pem
        self.pem = certificate()
        second = self.change("CERTIFICATE", {"certificate": self.pem, "replaces": self.cert["credential_id"]})
        self.assertEqual(self.submit().status_code, 200)
        recovered = self.client.get("/api/v1/transfers/recover", HTTP_IDEMPOTENCY_KEY="synthetic-key", **self.headers())
        self.assertEqual(recovered.json()["receipt_id"], first["receipt_id"])
        self.assertEqual(self.submit(HTTP_X_FILEX_CLIENT_CERT=quote(old_pem.decode(), safe="")).status_code, 200)
        self.change("RETIRE", {"credential_id": self.cert["credential_id"]})
        self.assertEqual(self.submit(HTTP_X_FILEX_CLIENT_CERT=quote(old_pem.decode(), safe="")).status_code, 401)
        self.assertEqual(self.submit().status_code, 200)
        self.assertEqual(SubmissionKey.objects.count(), 1)
        self.assertEqual(PartnerCredential.objects.get(pk=second["credential_id"]).replaces_id,
                         PartnerCredential.objects.get(pk=self.cert["credential_id"]).pk)

    def test_credential_expiry_retirement_and_security_revocation_are_distinct(self):
        self.submit()
        receipt = InboundTransfer.objects.get()
        PartnerCredential.objects.filter(pk=receipt.partner_credential_id).update(
            not_before=timezone.now() - timedelta(days=2), expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.submit().status_code, 401)
        self.assertTrue(accepted_authorization_valid(InboundTransfer.objects.get()))
        self.change("RETIRE", {"credential_id": self.cert["credential_id"]})
        self.assertTrue(accepted_authorization_valid(InboundTransfer.objects.get()))
        self.change("REVOKE", {"credential_id": self.cert["credential_id"]})
        self.assertFalse(accepted_authorization_valid(InboundTransfer.objects.get()))

    def test_suspension_and_permission_removal_stop_queued_work(self):
        self.submit()
        self.change("SUSPEND")
        self.assertFalse(accepted_authorization_valid(InboundTransfer.objects.get()))
        self.assertEqual(self.submit().status_code, 401)
        self.change("ACTIVATE")
        self.change("PERMISSIONS", {"permissions": []})
        self.assertFalse(accepted_authorization_valid(InboundTransfer.objects.get()))
        self.assertEqual(self.submit().status_code, 403)

    def test_read_download_and_submit_are_independent_permissions(self):
        first = self.submit().json()
        self.change("PERMISSIONS", {"permissions": [{"action": "READ", "workflow": "837"}]})
        self.assertEqual(self.client.get(first["status_url"], **self.headers()).status_code, 200)
        self.assertEqual(self.client.get(first["status_url"] + "/source", **self.headers()).status_code, 403)
        self.assertEqual(self.submit().status_code, 403)
        self.change("PERMISSIONS", {"permissions": [{"action": "SUBMIT", "workflow": "837"}]})
        self.assertEqual(self.client.get(first["status_url"], **self.headers()).status_code, 404)
        self.assertEqual(self.client.get("/edi835/api/intake/partner/", {"receipt_id": first["receipt_id"]}, **self.headers()).status_code, 404)

    def test_exact_route_checks_every_interchange_and_rejects_unknown_or_mixed(self):
        bad = self.raw.replace(b"TESTONLY", b"OTHERTEN")
        for raw in (bad, self.raw + bad, self.raw.replace(b"005010X222A1", b"005010X222A2"),
                    self.raw.replace(b"ST*837", b"ST*835"), self.raw + b"garbage", self.raw[:-20]):
            with self.subTest(length=len(raw)):
                self.assertEqual(self.submit(raw=raw).status_code, 400)
        self.assertFalse(InboundTransfer.objects.exists())
        self.assertEqual(self.submit(raw=self.raw + self.raw).status_code, 202)

    def test_envelope_delimiters_can_change_between_interchanges(self):
        alternative = self.raw.replace(b"*", b"|").replace(b"~", b"!")
        self.assertEqual(self.submit(raw=self.raw + alternative).status_code, 202)

    def test_malformed_counts_and_envelope_nesting_rejected(self):
        for raw in (self.raw.replace(b"SE*13", b"SE*12"), self.raw.replace(b"GE*1*1", b"GE*2*1"),
                    self.raw.replace(b"IEA*1*000000001", b"IEA*1*000000002"),
                    self.raw.replace(b"BHT*", b"ST*"), self.raw.replace(b"CLM*", b"X" * 65537 + b"*")):
            self.assertEqual(self.submit(raw=raw).status_code, 400)

    def test_route_administration_prevents_duplicate_and_cross_client_mapping(self):
        with self.assertRaises(ValueError):
            self.change("ROUTE", self.route_data)
        with self.assertRaises(ValueError):
            self.change("ROUTE", {**self.route_data, "client_id": str(self.other.pk)})
        PartnerRoute.objects.filter(partner=self.partner).update(client=self.other)
        self.assertEqual(self.submit().status_code, 400)

    def test_retired_route_blocks_new_submissions(self):
        route = self.partner.routes.get()
        self.change("RETIRE_ROUTE", {"route_id": route.pk})
        self.assertEqual(self.submit().status_code, 400)

    @override_settings(INTAKE_ENFORCE_PARTNER_ROUTES=False)
    def test_informational_routes_do_not_block_either_upload_api_or_workflow(self):
        self.partner.workflows = ["835", "837"]
        self.partner.save(update_fields=["workflows"])
        self.change("PERMISSIONS", {"permissions": [
            {"action": "SUBMIT", "workflow": workflow} for workflow in ("835", "837")
        ]})
        for mode in ("MTLS", "TOKEN"):
            self.change("POLICY", {"auth_mode": mode, "routing_required": True})
            headers = self.headers() if mode == "MTLS" else {
                "secure": True, "HTTP_AUTHORIZATION": "Bearer " + self.change("TOKEN")["token"]}
            for route_state in ("different_client", "retired", "absent"):
                if route_state == "different_client":
                    PartnerRoute.objects.filter(partner=self.partner).update(client=self.other)
                elif route_state == "retired":
                    PartnerRoute.objects.filter(partner=self.partner).update(active=False)
                else:
                    PartnerRoute.objects.filter(partner=self.partner).delete()
                for workflow, filename in (("837", "synthetic_claim.837"), ("835", "synthetic_payment.835")):
                    raw = (settings.BASE_DIR / "examples" / filename).read_bytes()
                    raw = raw.replace(b"SYNTHETIC", b"UNLISTEDX")
                    for path in ("/api/v1/transfers", "/edi835/api/intake/partner/"):
                        with self.subTest(mode=mode, route_state=route_state, workflow=workflow, path=path):
                            response = self.client.post(path, {"workflow": workflow,
                                "file": SimpleUploadedFile(filename, raw)},
                                HTTP_IDEMPOTENCY_KEY=f"{mode}-{route_state}-{workflow}-{path}", **headers)
                            self.assertIn(response.status_code, (200, 202), response.content)
                            receipt = InboundTransfer.objects.get(pk=response.json()["receipt_id"])
                            self.assertEqual(receipt.client_id, self.tenant.pk)
                            self.assertEqual(receipt.partner_authorization["routes"], [])
                            self.assertIsNotNone(receipt.partner_credential_id)

    @override_settings(INTAKE_ENFORCE_PARTNER_ROUTES=False)
    def test_informational_mode_preserves_auth_permissions_scope_and_validation(self):
        self.assertEqual(self.submit(HTTP_X_FILEX_GATEWAY="wrong").status_code, 401)
        malformed = self.submit(raw=self.raw[:-20])
        self.assertEqual(malformed.status_code, 400)
        self.assertNotIn("approved route", malformed.json()["error"])
        self.assertEqual(self.submit(raw=self.raw.replace(b"ST*837", b"ST*835")).status_code, 400)
        response = self.client.post("/api/v1/transfers", {"workflow": "837",
            "client_id": str(self.other.pk), "file": SimpleUploadedFile("claim.837", self.raw)},
            HTTP_IDEMPOTENCY_KEY="wrong-client", **self.headers())
        self.assertEqual(response.status_code, 404)
        self.change("PERMISSIONS", {"permissions": []})
        self.assertEqual(self.submit().status_code, 403)
        self.assertFalse(InboundTransfer.objects.exists())

    @override_settings(INTAKE_ENFORCE_PARTNER_ROUTES=False)
    def test_readiness_allows_missing_informational_routes(self):
        PartnerRoute.objects.filter(partner=self.partner).delete()
        output = io.StringIO()
        call_command("check_partner_security", require_mtls=True, stdout=output)
        self.assertIn("route matching is disabled", output.getvalue())
        with override_settings(INTAKE_ENFORCE_PARTNER_ROUTES=True):
            with self.assertRaises(CommandError):
                call_command("check_partner_security", require_mtls=True, stdout=io.StringIO())

    def test_management_is_audited_and_never_records_tokens_or_certificates(self):
        token = self.change("TOKEN")["token"]
        details = " ".join(AuditLog.objects.filter(module="Partner intake").values_list("details", flat=True))
        self.assertNotIn(token, details)
        self.assertNotIn("BEGIN CERTIFICATE", details)
        self.assertIn("CERTIFICATE", details)
        with self.assertRaises(ValueError):
            change_partner(self.partner.pk, action="SUSPEND", actor="test", reason="")
        self.partner.refresh_from_db()
        self.assertTrue(self.partner.active)

    def test_new_identity_starts_with_no_permissions(self):
        partner = create_partner(client=self.tenant, name="New", workflows=["835"], mode="MTLS",
            actor="synthetic", reason="Create scoped identity")
        self.assertFalse(partner.permissions.exists())
        self.assertTrue(partner.routing_required)

    def test_token_rotation_preserves_stable_partner_identity(self):
        self.change("POLICY", {"auth_mode": "TOKEN", "routing_required": False})
        token = self.change("TOKEN")["token"]
        response = self.client.post("/api/v1/transfers", {"workflow": "837", "file": SimpleUploadedFile("file", self.raw)},
            secure=True, HTTP_AUTHORIZATION="Bearer " + token, HTTP_IDEMPOTENCY_KEY="token-key")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(InboundTransfer.objects.get().partner_id, self.partner.pk)
        self.assertNotEqual(token.split(".")[0], str(self.partner.pk))
        self.assertEqual(self.client.get("/api/v1/transfers", secure=True, HTTP_AUTHORIZATION=self.authorization).status_code, 200)

    def test_cli_status_omits_credential_digests(self):
        output = io.StringIO()
        call_command("partner_security", "status", partner=str(self.partner.pk), stdout=output)
        data = json.loads(output.getvalue())
        self.assertTrue(data["credentials"])
        self.assertNotIn("digest", data["credentials"][0])

    def test_readiness_rejects_unconfigured_gateway(self):
        call_command("check_partner_security", require_mtls=True, stdout=io.StringIO())
        with override_settings(INTAKE_MTLS_GATEWAY_SECRET="", INTAKE_MTLS_TRUSTED_PEERS=["0.0.0.0"]):
            with self.assertRaises(CommandError):
                call_command("check_partner_security", require_mtls=True, stdout=io.StringIO())

    def test_legacy_provisioning_command_issues_working_token_and_revokes_by_token_id(self):
        output = io.StringIO()
        call_command("intake_partner", client_code=self.tenant.client_code, name="Legacy", workflow=["837"],
                     stdout=output, stderr=io.StringIO())
        token = output.getvalue().strip()
        self.assertEqual(self.client.get("/api/v1/transfers", secure=True, HTTP_AUTHORIZATION="Bearer " + token).status_code, 200)
        call_command("intake_partner", revoke=token.split(".")[0], stdout=io.StringIO())
        self.assertEqual(self.client.get("/api/v1/transfers", secure=True, HTTP_AUTHORIZATION="Bearer " + token).status_code, 401)

    def test_real_worker_processes_accepted_837_after_credential_expiry(self):
        from .management.commands.run_batch_worker import Command as Worker
        from .models import EDI837File
        self.submit()
        receipt = InboundTransfer.objects.get()
        PartnerCredential.objects.filter(pk=receipt.partner_credential_id).update(
            not_before=timezone.now() - timedelta(days=2), expires_at=timezone.now() - timedelta(seconds=1))
        Worker()._process(receipt.dispatch.payload)
        receipt.refresh_from_db()
        self.assertEqual(receipt.state, "COMPLETED")
        self.assertEqual(EDI837File.objects.count(), 1)

    def test_835_route_with_absent_st03_is_explicit(self):
        raw = (settings.BASE_DIR / "examples/synthetic_payment.835").read_bytes()
        route = next(envelope_routes(io.BufferedReader(io.BytesIO(raw)), "835"))
        self.partner.workflows = ["835", "837"]
        self.partner.save(update_fields=["workflows"])
        self.change("ROUTE", dict(zip(ROUTE_FIELDS, route), workflow="835"))
        response = self.client.post("/api/v1/transfers", {"workflow": "835", "file": SimpleUploadedFile("synthetic.835", raw)},
            HTTP_IDEMPOTENCY_KEY="835-key", **self.headers())
        self.assertEqual(response.status_code, 202, response.content)
        self.assertEqual(InboundTransfer.objects.get().workflow, "835")

    def test_expired_or_future_credential_and_inactive_client_cannot_authenticate(self):
        credential = PartnerCredential.objects.get(pk=self.cert["credential_id"])
        credential.not_before = timezone.now() + timedelta(days=1)
        credential.save(update_fields=["not_before"])
        self.assertEqual(self.submit().status_code, 401)
        credential.not_before = timezone.now() - timedelta(seconds=1)
        credential.save(update_fields=["not_before"])
        self.tenant.stage = "offboarded"
        self.tenant.save(update_fields=["stage"])
        self.assertEqual(self.submit().status_code, 401)

    def test_permission_channel_and_workflow_cannot_be_expanded_implicitly(self):
        for permission in ({"action": "RETRY", "workflow": "837"}, {"action": "SUBMIT", "workflow": "835"},
                           {"action": "SUBMIT", "workflow": "837", "channel": "SFTP"}):
            with self.assertRaises(ValueError):
                self.change("PERMISSIONS", {"permissions": [permission]})
