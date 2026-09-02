import io
import os
import zipfile
import json
from types import SimpleNamespace
from unittest.mock import patch
from django.test import TestCase, Client
from converter.services.parser import parse_835_to_mir
from converter.services.validator import PyX12Validator
from converter.views import _send_validation_notice
from admin_panel.email_service import send_conversion_notice

SAMPLE_ONE_LINE = "ISA*00*          *00*          *ZZ*SENDER         *ZZ*RECEIVER       *260813*1200*U*00501*000000001*0*P*:~GS*HP*SENDER*RECEIVER*20260813*1200*1*X*005010X221A1~ST*835*0001~BPR*I*150.00*C*CHK************20260813~TRN*1*123456789*1999999999~N1*PR*PAYER NAME~N1*PE*PROVIDER NAME*XX*1234567890~LX*1~CLP*CLAIM1001*1*200.00*150.00*50.00*MC*REF12345~NM1*QC*1*SMITH*JOHN*M~NM1*IL*1*SMITH*JOHN****MI*SUB123456~REF*1L*GRP999~DTM*036*19850101~DTM*050*20260801~SVC*HC:99213*200.00*150.00**1~DTM*472*20260805~CAS*CO*45*50.00~SE*16*0001~GE*1*1~IEA*1*000000001~"

SAMPLE_CRLF = SAMPLE_ONE_LINE.replace("~", "~\r\n")

class PyX12ValidatorTestSuite(TestCase):

    def setUp(self):
        self.validator = PyX12Validator()

    def test_1_valid_835(self):
        res = self.validator.validate(SAMPLE_ONE_LINE)
        self.assertEqual(res['total_segments'], 20)
        self.assertEqual(res['claims'], 1)
        self.assertEqual(res['validator_engine'], "Validated using PyX12")

    def test_2_malformed_isa(self):
        malformed_isa = "ISA*00*BAD_ISA_HEADER~ST*835*0001~SE*2*0001~"
        res = self.validator.validate(malformed_isa)
        self.assertFalse(res['valid'])
        self.assertGreater(len(res['errors']), 0)

    def test_7_non_835_x12(self):
        edi_270 = SAMPLE_ONE_LINE.replace("ST*835*0001~", "ST*270*0001~")
        res = self.validator.validate(edi_270)
        self.assertFalse(res['valid'])
        self.assertEqual(res['errors'][0]['code'], 'NON_835_TRANSACTION')


class ViewsTestCase(TestCase):

    def setUp(self):
        from accounts.models import User
        self.client = Client()
        self.user = User.objects.create_user(
            email="testuser@example.com",
            name="Test User",
            mobile="+15550000",
            password="testpassword"
        )
        # Log user in
        self.client.login(email="testuser@example.com", password="testpassword")

    def test_api_convert(self):
        response = self.client.post('/api/convert/', data={'edi_text': SAMPLE_ONE_LINE})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])

    def test_api_validate_endpoint(self):
        response = self.client.post('/api/validate/', data={'edi_text': SAMPLE_ONE_LINE})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['report']['total_segments'], 20)

    @patch("admin_panel.email_service.send_client_email", return_value=True)
    def test_invalid_validation_email_is_labeled_failed_and_lists_file(self, send_email):
        from accounts.models import Client as AccountClient
        tenant = AccountClient.objects.create(
            name="Validation Tenant", client_code="VALIDATION", email="tenant@example.com"
        )
        request = SimpleNamespace(user=self.user)
        self.assertTrue(_send_validation_notice(
            tenant, request, ["bad-claim.835"], False, 0, ["Invalid transaction structure"]
        ))
        subject = send_email.call_args.args[1]
        html = send_email.call_args.args[2]
        self.assertIn("Validation Failed", subject)
        self.assertNotIn("Validation Completed", subject)
        self.assertIn("bad-claim.835", html)
        self.assertIn("Invalid transaction structure", html)

    @patch("admin_panel.email_service.send_client_email", return_value=True)
    def test_conversion_success_email_contains_files_counts_and_est_time(self, send_email):
        from accounts.models import Client as AccountClient
        tenant = AccountClient.objects.create(
            name="Conversion Email Tenant", client_code="CONVERSION-EMAIL", email="tenant@example.com"
        )
        request = SimpleNamespace(user=self.user)
        self.assertTrue(send_conversion_notice(
            tenant, request, success=True,
            input_files=["first.835", "second.x12"], output_files=["combined.MIR"],
            claims=12, services=18, records=30, batch=True,
        ))
        subject = send_email.call_args.args[1]
        html = send_email.call_args.args[2]
        self.assertIn("Conversion Successful", subject)
        for expected in ("first.835", "second.x12", "combined.MIR", "12", "18", "30", "Completed at (EST)"):
            self.assertIn(expected, html)

    def test_wrong_835_extension_is_rejected_before_processing(self):
        response = self.client.post(
            '/api/validate/',
            data=json.dumps({'edi_text': SAMPLE_ONE_LINE, 'original_filename': 'claim.pdf'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('Wrong file format for 835', response.json()['error'])

    def test_standard_client_user_can_convert_and_is_tenant_scoped(self):
        from accounts.models import Client as AccountClient
        from edi835.models import EDI835File
        tenant = AccountClient.objects.create(name='Conversion Tenant', client_code='CONVERSION', email='tenant@example.com')
        other = AccountClient.objects.create(name='Other Tenant', client_code='OTHER-CONVERSION', email='other-tenant@example.com')
        self.user.client = tenant
        self.user.save(update_fields=['client'])
        response = self.client.post(
            '/api/convert/',
            data=json.dumps({'edi_text': SAMPLE_ONE_LINE, 'original_filename': 'tenant-upload.835', 'client_id': str(other.id)}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(EDI835File.objects.get(id=response.json()['file_id']).client, tenant)

    def test_offboarded_client_cannot_validate_or_convert_new_files(self):
        from accounts.models import Client as AccountClient
        tenant = AccountClient.objects.create(
            name='Offboarded Conversion Tenant', client_code='OFF-CONVERSION',
            email='offboarded-conversion@example.com', stage='offboarded', status='INACTIVE',
        )
        self.user.is_staff = True
        self.user.save(update_fields=['is_staff'])
        payload = json.dumps({
            'edi_text': SAMPLE_ONE_LINE, 'original_filename': 'blocked.835',
            'client_id': str(tenant.id),
        })
        for endpoint in ('/api/validate/', '/api/convert/'):
            with self.subTest(endpoint=endpoint):
                response = self.client.post(endpoint, data=payload, content_type='application/json')
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json()['code'], 'CLIENT_OFFBOARDED')

    def test_health_check_endpoint(self):
        response = self.client.get('/health/')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['status'], 'healthy')
        self.assertEqual(data['database'], 'connected')
        self.assertEqual(data['database_schema'], 'current')

    def test_file_viewer_uses_only_database_content_and_has_empty_fallbacks(self):
        from edi835.models import EDI835File, MIRFile

        source = EDI835File.objects.create(
            original_filename="database-source.835",
            stored_filename="missing-on-disk.835",
            input_file_content="DATABASE 835 CONTENT",
            status="ARCHIVED",
            archive_path="media/edi835/archive/does-not-exist.835",
            output_path="media/edi835/output/does-not-exist.MIR",
        )
        MIRFile.objects.create(
            source_835=source,
            mir_filename="database-output.MIR",
            original_835_filename=source.original_filename,
            file_content="DATABASE MIR CONTENT",
            file_hash="a" * 64,
        )

        response = self.client.get(f"/api/file-content/{source.id}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["edi_text"], "DATABASE 835 CONTENT")
        self.assertEqual(response.json()["mir_text"], "DATABASE MIR CONTENT")

        empty = EDI835File.objects.create(
            original_filename="empty.835", stored_filename="empty.835"
        )
        empty_response = self.client.get(f"/api/file-content/{empty.id}/")
        self.assertEqual(empty_response.status_code, 200)
        self.assertEqual(empty_response.json()["edi_text"], "")
        self.assertEqual(empty_response.json()["mir_text"], "")

    def test_client_archive_zip_contains_database_835_mir_and_recon_folders(self):
        from accounts.models import Client as AccountClient
        from edi835.models import EDI835File, MIRFile, RECONFile

        selected = AccountClient.objects.create(
            name="Selected Client",
            client_code="SELECTED",
            email="selected@example.com",
        )
        other = AccountClient.objects.create(
            name="Other Client",
            client_code="OTHER",
            email="other@example.com",
        )
        source = EDI835File.objects.create(
            client=selected,
            original_filename="selected.835",
            stored_filename="selected.835",
            input_file_content="SELECTED 835",
        )
        MIRFile.objects.create(
            source_835=source,
            client=selected,
            mir_filename="selected.MIR",
            file_content="SELECTED MIR",
            file_hash="b" * 64,
        )
        RECONFile.objects.create(
            client=selected,
            original_filename="selected.P7A",
            stored_filename="selected.P7A",
            file_content="SELECTED RECON",
            file_hash="c" * 64,
        )
        EDI835File.objects.create(
            client=other,
            original_filename="other.835",
            stored_filename="other.835",
            input_file_content="OTHER 835",
        )

        response = self.client.get(
            f"/api/download-zip/?type=all&client={selected.id}"
        )

        self.assertEqual(response.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            self.assertEqual(
                set(archive.namelist()),
                {"835/selected.835", "MIR/selected.MIR", "RECON/selected.P7A"},
            )
            self.assertEqual(archive.read("835/selected.835"), b"SELECTED 835")
            self.assertEqual(archive.read("MIR/selected.MIR"), b"SELECTED MIR")
            self.assertEqual(archive.read("RECON/selected.P7A"), b"SELECTED RECON")
