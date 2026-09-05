from types import SimpleNamespace
from pathlib import Path
import json
from unittest.mock import patch

from django.http import JsonResponse
from django.test import RequestFactory, SimpleTestCase, TestCase

from accounts.models import Client, User
from .claim_numbers import split_claim_number
from .edi837_service import _837_claim_numbers, export_single_claim, ingest_837, parse_837, split_x12
from .edi837_views import _claim_lifecycle
from .edi837_search_transfer import edi837_sftp_transfer_for_search
from .edi837_transfer import edi837_sftp_transfer
from .models import EDI835File, EDI837Claim, EDI837File, MIRClaim, MIRFile, RECONClaim, RECONFile


SAMPLE_837 = (
    "ISA*00*          *00*          *ZZ*SENDER         *ZZ*RECEIVER       *260904*1200*^*00501*000000001*0*P*:~"
    "GS*HC*SENDER*RECEIVER*20260904*1200*1*X*005010X222A1~ST*837*0001*005010X222A1~BHT*0019*00*1*20260904*1200*CH~"
    "HL*1**20*1~NM1*85*2*PROVIDER*****XX*1234567890~HL*2*1*22*0~SBR*P*18*******CI~"
    "NM1*IL*1*DOE*JANE****MI*MEMBER1~NM1*PR*2*HIGHMARK*****PI*PAYOR~"
    "CLM*123456789QYN071*13.62***11:B:1*Y*A*Y*Y~REF*9C*EXTERNAL1~HI*ABK:M255~"
    "LX*1~SV1*HC:99213*13.62*UN*1***1~DTP*472*D8*20260901~"
    "HL*3**20*1~NM1*85*2*OTHER PROVIDER*****XX*9876543210~HL*4*3*22*0~SBR*P*18*******CI~"
    "NM1*IL*1*SMITH*JOHN****MI*MEMBER2~NM1*PR*2*OTHER PAYER*****PI*OTHER~"
    "CLM*987654321ABC123*20.00***11:B:1*Y*A*Y*Y~REF*9C*EXTERNAL2~"
    "LX*1~SV1*HC:93000*20.00*UN*1***1~SE*20*0001~GE*1*1~IEA*1*000000001~"
)

DEPENDENT_837 = (
    "ISA*00*          *00*          *ZZ*SENDER         *ZZ*RECEIVER       *260904*1200*^*00501*000000001*0*P*:~"
    "GS*HC*SENDER*RECEIVER*20260904*1200*1*X*005010X222A1~ST*837*0001*005010X222A1~BHT*0019*00*1*20260904*1200*CH~"
    "HL*1**20*1~NM1*85*2*LABCORP*****XX*1234567890~HL*2*1*22*1~SBR*P********BL~"
    "NM1*IL*1*MILES*SONIA****MI*MEMBER1~NM1*PR*2*HIGHMARK*****PI*PAYOR~"
    "HL*3*2*23*0~PAT*19~NM1*QC*1*MILES*LAYNA*I~"
    "CLM*08020260371453502*339.15***81:B:6*Y*C*Y*Y~REF*F8*08020260371453501~REF*9C*H03717655302~"
    "NM1*DN*1*UNAVAILABLE*UNAVAILABL****XX*1083661219~LX*1~SV1*HC:85025*36.93*UN*1***1~"
    "SE*16*0001~GE*1*1~IEA*1*000000001~"
)


class EDI837ParsingTests(SimpleTestCase):
    def test_ref_9c_is_the_authoritative_internal_claim_number(self):
        self.assertEqual(_837_claim_numbers("123456789QYN071", "H100001346867370"), {
            "highmark_claim_number": "123456789",
            "internal_claim_number": "H100001346867370",
        })

    def test_combined_clm_suffix_is_used_only_without_ref_9c(self):
        self.assertEqual(_837_claim_numbers("123456789QYN071", ""), {
            "highmark_claim_number": "123456789",
            "internal_claim_number": "QYN071",
        })

    def test_claims_and_service_lines_are_normalized(self):
        parsed = parse_837(SAMPLE_837)
        self.assertEqual(len(parsed["claims"]), 2)
        first = parsed["claims"][0]
        self.assertEqual(first["claim_control_number"], "123456789QYN071")
        self.assertEqual(first["reference_9c"], "EXTERNAL1")
        self.assertEqual(first["member_id"], "MEMBER1")
        self.assertEqual(first["services"][0]["procedure_code"], "99213")

    def test_numeric_claim_is_a_highmark_number(self):
        self.assertEqual(split_claim_number("123456789"), {
            "highmark_claim_number": "123456789", "internal_claim_number": "",
        })

    def test_next_hierarchy_does_not_overwrite_previous_claim_context(self):
        parsed = parse_837(SAMPLE_837)
        first, second = parsed["claims"]
        self.assertEqual(first["subscriber_first"], "JANE")
        self.assertEqual(first["member_id"], "MEMBER1")
        self.assertEqual(first["billing_provider"], "PROVIDER")
        self.assertEqual(first["payer"], "HIGHMARK")
        self.assertEqual(second["subscriber_first"], "JOHN")
        self.assertEqual(second["member_id"], "MEMBER2")
        self.assertEqual(second["billing_provider"], "OTHER PROVIDER")

    def test_single_claim_export_has_valid_counts_and_no_sibling_claim(self):
        claim = SimpleNamespace(
            pk=1, claim_control_number="123456789QYN071",
            patient_control_number="123456789QYN071",
            edi_file=SimpleNamespace(file_content=SAMPLE_837),
        )
        output = export_single_claim(claim)
        _, _, _, segments = split_x12(output)
        tags = [segment[0] for segment in segments]
        self.assertEqual(tags.count("CLM"), 1)
        self.assertNotIn("987654321ABC123", output)
        st_index, se_index = tags.index("ST"), tags.index("SE")
        self.assertEqual(int(segments[se_index][1]), se_index - st_index + 1)

    def test_dependent_claim_keeps_subscriber_and_exports_parent_loop(self):
        data = parse_837(DEPENDENT_837)["claims"][0]
        self.assertEqual(data["patient_first"], "LAYNA")
        self.assertEqual(data["subscriber_first"], "SONIA")
        self.assertEqual(data["member_id"], "MEMBER1")
        self.assertEqual(data["referring_provider"], "UNAVAILABL UNAVAILABLE")
        self.assertEqual(data["rendering_provider"], "")
        self.assertEqual(data["claim_frequency_code"], "6")
        self.assertEqual(data["original_claim_number"], "08020260371453501")
        claim = SimpleNamespace(
            pk=2, claim_control_number="08020260371453502",
            patient_control_number="08020260371453502",
            edi_file=SimpleNamespace(file_content=DEPENDENT_837),
        )
        output = export_single_claim(claim)
        self.assertIn("NM1*IL*1*MILES*SONIA****MI*MEMBER1", output)
        self.assertIn("NM1*QC*1*MILES*LAYNA*I", output)
        self.assertIn("NM1*PR*2*HIGHMARK", output)


class EDI837LifecycleTests(TestCase):
    def test_client_837_transfer_never_promotes_user_to_staff(self):
        client = Client.objects.create(
            name="Role Safe Client", client_code="ROLE837", email="role@example.com"
        )
        user = User.objects.create_user(
            email="role-user@example.com",
            name="Role User",
            mobile="5550008370",
            password="test-password",
            client=client,
        )
        request = RequestFactory().post(
            "/edi835/api/837/sftp-rename/",
            data=json.dumps({"filename": "YYYYMMDDhhmmss.837"}),
            content_type="application/json",
        )
        request.user = user

        def assert_original_role(inner_request):
            self.assertFalse(inner_request.user.is_staff)
            return JsonResponse({"success": True})

        with patch(
            "edi835.edi837_search_transfer.edi837_sftp_transfer_named",
            side_effect=assert_original_role,
        ):
            response = edi837_sftp_transfer_for_search(request)

        self.assertEqual(response.status_code, 200)
        user.refresh_from_db()
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)

    def test_client_user_reaches_tenant_scoped_837_transfer_without_staff_role(self):
        client = Client.objects.create(
            name="Tenant Transfer Client", client_code="TEN837", email="tenant@example.com"
        )
        user = User.objects.create_user(
            email="tenant-user@example.com",
            name="Tenant User",
            mobile="5550008371",
            password="test-password",
            client=client,
        )
        request = RequestFactory().post(
            "/edi835/api/837/sftp-rename/",
            data=json.dumps({}),
            content_type="application/json",
        )
        request.user = user

        response = edi837_sftp_transfer(request)

        self.assertEqual(response.status_code, 400)
        self.assertIn("filename", json.loads(response.content)["error"].lower())
        user.refresh_from_db()
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)

    def test_identical_837_arrivals_are_each_persisted_and_indexed(self):
        client = Client.objects.create(
            name="Duplicate Intake Client", client_code="DUP837", email="duplicate@example.com"
        )

        first, first_duplicate = ingest_837(client, None, "claims", SAMPLE_837.encode())
        second, second_duplicate = ingest_837(client, None, "claims", SAMPLE_837.encode())

        self.assertNotEqual(first.pk, second.pk)
        self.assertFalse(first_duplicate)
        self.assertFalse(second_duplicate)
        self.assertEqual(EDI837File.objects.filter(client=client).count(), 2)
        self.assertEqual(EDI837Claim.objects.filter(client=client).count(), 4)
        self.assertEqual(first.file_hash, second.file_hash)
        self.assertNotEqual(first.archive_path, second.archive_path)
        self.assertEqual(first.stored_filename, "claims.837")
        self.assertEqual(second.stored_filename, "claims_002.837")
        self.assertEqual(Path(first.archive_path).name, "claims.837")
        self.assertEqual(Path(second.archive_path).name, "claims_002.837")
        self.assertNotRegex(first.stored_filename, r"^[0-9a-f]{32}_")

    def test_requested_rename_is_used_for_archive_and_local_outbound(self):
        client = Client.objects.create(
            name="Named Intake Client", client_code="NAME837", email="named@example.com"
        )

        edi_file, _ = ingest_837(
            client,
            None,
            "IP7A260904P",
            SAMPLE_837.encode(),
            import_mode="SFTP",
            remote_path="/in/IP7A260904P",
            storage_filename="2026_09_05.837",
        )

        self.assertEqual(edi_file.original_filename, "IP7A260904P")
        self.assertEqual(edi_file.stored_filename, "2026_09_05.837")
        self.assertEqual(Path(edi_file.archive_path).name, "2026_09_05.837")
        self.assertEqual(Path(edi_file.outbound_path).name, "2026_09_05.837")

    def test_835_lifecycle_is_resolved_through_linked_mir_source(self):
        client = Client.objects.create(
            name="Lifecycle Client", client_code="LIFE837", email="life@example.com"
        )
        source_835 = EDI835File.objects.create(
            client=client, original_filename="payment.835", stored_filename="payment.835",
            status="ARCHIVED", ingestion_source="SFTP",
        )
        mir_file = MIRFile.objects.create(
            source_835=source_835, client=client, mir_filename="payment.MIR",
            original_835_filename="payment.835", file_content="", file_hash="a" * 64,
        )
        MIRClaim.objects.create(
            mir_file=mir_file, claim_sequence=1, claim_control_number="123456789QYN071",
            header_raw=" " * 334,
        )
        recon_file = RECONFile.objects.create(
            client=client, original_filename="payment.recon", stored_filename="payment.recon",
            file_content="", file_hash="c" * 64,
        )
        RECONClaim.objects.create(
            recon_file=recon_file, client=client, claim_sequence=1,
            claim_control_number="123456789QYN071",
        )
        edi_file = EDI837File.objects.create(
            client=client, original_filename="claim.837", stored_filename="claim.837",
            file_content="", file_hash="b" * 64,
        )
        claim = EDI837Claim.objects.create(
            edi_file=edi_file, client=client, claim_sequence=1,
            claim_control_number="123456789", highmark_claim_number="123456789",
            internal_claim_number="H100001346867370", reference_9c="H100001346867370",
        )

        lifecycle = _claim_lifecycle(claim)

        self.assertTrue(lifecycle["835"]["exists"])
        self.assertEqual(lifecycle["835"]["file_name"], "payment.835")
        self.assertEqual(lifecycle["835"]["source"], "SFTP")
        self.assertEqual(lifecycle["835"]["status"], "ARCHIVED")
        self.assertTrue(lifecycle["mir"]["exists"])
        self.assertTrue(lifecycle["recon"]["exists"])
