"""Source selection, tenant boundaries and lossless single-claim exports."""
import json
from types import SimpleNamespace

from django.test import RequestFactory, SimpleTestCase, TestCase

from accounts.models import Client, User
from .client_claim_sources import available_sources, slice_837, slice_mir, slice_recon
from .client_portal_claim import source_preview
from .models import (EDI835File, EDI835Claim, EDI837File, EDI837Claim,
                     MIRFile, MIRClaim, MIRClaimChunk, RECONFile, RECONClaim, RECONServiceLine)
from .test_client_claim_slice import ISA, source, BODY


NUMBER = "12345678901234567"
INTERNAL = "ABC123"


def source837(number=NUMBER):
    return (ISA + "~GS*HC*S*R*20260814*1200*2*X*005010X222A1~"
            "ST*837*0002*005010X222A1~BHT*0019*00*REF*20260814*1200*CH~"
            "NM1*41*2*SUBMITTER~HL*10**20*1~NM1*85*2*PROVIDER~"
            "HL*20*10*22*1~SBR*P*18~NM1*IL*1*SUBSCRIBER~"
            "CLM*SIBLING*100~REF*9C*OTHER1~LX*1~SV1*HC:111*100~"
            "HL*30*20*23*0~PAT*19~NM1*QC*1*DEPENDENT~"
            f"CLM*{number}*200~REF*9C*{INTERNAL}~LX*1~SV1*HC:222*200~"
            "CLM*NEIGHBOR*300~REF*9C*OTHER2~LX*1~SV1*HC:333*300~"
            "SE*23*0002~GE*1*2~IEA*1*000000001~")


class Slice837Tests(SimpleTestCase):
    def record(self, content=None, sequence=2):
        return SimpleNamespace(edi_file=SimpleNamespace(file_content=content or source837()),
                               claim_sequence=sequence, claim_control_number=NUMBER)

    def test_dependent_keeps_ancestors_without_ancestor_claims(self):
        result = slice_837(self.record())
        self.assertNotIn("SIBLING", result)
        self.assertNotIn("NEIGHBOR", result)
        self.assertEqual(result.count("CLM*"), 1)
        self.assertIn("NM1*85*2*PROVIDER", result)
        self.assertIn("NM1*IL*1*SUBSCRIBER", result)
        self.assertIn("HL*1**20*1~", result)
        self.assertIn("HL*2*1*22*1~", result)
        self.assertIn("HL*3*2*23*0~", result)
        rows = result.strip().split("~\n")
        st = next(i for i, row in enumerate(rows) if row.startswith("ST*"))
        se = next(i for i, row in enumerate(rows) if row.startswith("SE*"))
        self.assertEqual(rows[se], f"SE*{se-st+1}*0002")
        self.assertTrue(result.endswith("GE*1*2~\nIEA*1*000000001~\n"))

    def test_duplicate_number_uses_selected_occurrence_and_envelope(self):
        first = source837().replace("DEPENDENT", "FIRSTPATIENT")
        second = source837().replace("000000001", "000000002").replace("DEPENDENT", "SECONDPATIENT")
        result = slice_837(self.record(first + second, 5))
        self.assertIn("SECONDPATIENT", result)
        self.assertNotIn("FIRSTPATIENT", result)
        self.assertEqual(result.count("CLM*"), 1)
        self.assertTrue(result.endswith("IEA*1*000000002~\n"))

    def test_alternate_delimiters(self):
        result = slice_837(self.record(source837().replace("*", "|").replace("~", "!")))
        self.assertIn(f"CLM|{NUMBER}|200!\n", result)
        self.assertTrue(result.endswith("IEA|1|000000001!\n"))

    def test_invalid_occurrence_controls_and_hierarchy_fail_closed(self):
        for content, sequence in ((source837(), 1), (source837(), 0), (source837(), 99),
                (source837().replace("SE*23*0002", "SE*23*WRONG"), 2),
                (source837().replace("HL*30*20", "HL*30*99"), 2),
                (source837().replace("HL*20*10", "HL*20*30"), 2)):
            with self.subTest(sequence=sequence, content=content[-50:]), self.assertRaises(ValueError):
                slice_837(self.record(content, sequence))


class ClaimSourceTests(TestCase):
    def setUp(self):
        self.client_a = Client.objects.create(name="A", client_code="SOURCE_A", email="a@example.org")
        self.client_b = Client.objects.create(name="B", client_code="SOURCE_B", email="b@example.org")
        self.viewer = User.objects.create_user(email="sources@example.org", name="Viewer", mobile="15555550123",
                                               password="TestPassword123!", client=self.client_a, client_role="CLIENT_USER")
        self.file = EDI835File.objects.create(client=self.client_a, original_filename="held.835", stored_filename="held.835",
                                             input_file_content=source(BODY.replace("CLP*FIRST", "CLP*" + NUMBER)))
        self.claim = self.file.claims.get(claim_sequence=1)
        self.claim.internal_claim_number = INTERNAL
        self.claim.save(update_fields=["internal_claim_number"])

    def request(self, kind, record_id, download=False, claim_id=None):
        request = RequestFactory().get("/source/", {"record_id": record_id, "download": "1" if download else "0"})
        request.user = self.viewer
        return source_preview(request, claim_id or self.claim.pk, kind)

    def add837(self, client=None, number=NUMBER, internal=INTERNAL):
        client = client or self.client_a
        file = EDI837File.objects.create(client=client, original_filename="matched.837", stored_filename="matched.837",
                                        file_content=source837(number), file_hash="test")
        return EDI837Claim.objects.create(edi_file=file, client=client, claim_sequence=2,
                    claim_control_number=number, highmark_claim_number=number, internal_claim_number=internal)

    def addmir(self, source_file=None):
        row = "01" + NUMBER + INTERNAL + " " * 307 + "00"
        self.assertEqual(len(row), 334)
        other = row.replace(NUMBER, "99999999999999999")
        file = MIRFile.objects.create(source_835=source_file or self.file, client=self.client_a,
                    mir_filename="matched.mir", file_content=other + "\r\n" + row + "\r\n", file_hash="test")
        claim = MIRClaim.objects.create(mir_file=file, claim_sequence=2, claim_control_number=NUMBER + INTERNAL,
                                        header_raw=row, chunk_count=1)
        MIRClaimChunk.objects.create(mir_claim=claim, chunk_number=1, physical_row_number=2, raw_row=row, row_length=len(row))
        return claim, row

    def addrecon(self):
        text = 'claim_number,paid_amount,note\r\nOTHER,99,other\r\n' + NUMBER + ',80,"quoted, value\r\ncontinued"\r\n\x1a'
        file = RECONFile.objects.create(client=self.client_a, original_filename="matched.csv", stored_filename="matched.csv",
                                        file_content=text, file_hash="test")
        claim = RECONClaim.objects.create(client=self.client_a, recon_file=file, claim_sequence=2, claim_control_number=NUMBER)
        RECONServiceLine.objects.create(recon_claim=claim, recon_file=file, service_sequence=1, source_row_number=3,
                                       raw_service=NUMBER + ',80,quoted, value\r\ncontinued')
        return claim

    def test_only_present_sources_and_exact_related_mir_are_listed(self):
        self.assertEqual([s["kind"] for s in available_sources(self.claim)], ["835"])
        unrelated = EDI835File.objects.create(client=self.client_a, original_filename="other.835", stored_filename="other.835")
        wrong_mir, _ = self.addmir(unrelated)
        self.assertEqual(self.request("mir", wrong_mir.pk).status_code, 404)
        mir, _ = self.addmir()
        first, second = self.add837(), self.add837()
        # Fast test inserts can share a timestamp; UUID tie ordering is random.
        # Establish the chronological ordering this assertion intends to test.
        from datetime import timedelta
        from django.utils import timezone
        EDI837File.objects.filter(pk=first.edi_file_id).update(
            uploaded_at=timezone.now() - timedelta(days=1))
        recon = self.addrecon()
        sources = available_sources(self.claim)
        self.assertEqual([s["kind"] for s in sources], ["835", "mir", "837", "recon"])
        self.assertEqual([r["record_id"] for r in sources[1]["records"]], [mir.pk])
        self.assertEqual([r["record_id"] for r in sources[2]["records"]], [first.pk, second.pk])
        self.assertFalse(sources[2]["linked"])
        self.assertEqual(sources[3]["records"][0]["record_id"], recon.pk)

    def test_tenant_and_identity_checks_apply_to_preview_and_download(self):
        foreign = self.add837(self.client_b)
        other = self.add837(number="DIFFERENT")
        different_internal = self.add837(internal="OTHER1")
        for row in (foreign, other, different_internal):
            for download in (False, True):
                self.assertEqual(self.request("837", row.pk, download).status_code, 404)
        self.assertEqual(self.request("837", "").status_code, 404)
        self.assertEqual(self.request("invalid", 1).status_code, 404)
        self.assertEqual(self.request("835", self.claim.pk, claim_id=999999).status_code, 404)

    def test_all_downloads_equal_preview_with_claim_filename(self):
        mir, _ = self.addmir()
        recon = self.addrecon()
        for kind, record in (("835", self.claim), ("837", self.add837()), ("mir", mir), ("recon", recon)):
            with self.subTest(kind=kind):
                preview = self.request(kind, record.pk)
                self.assertEqual(preview.status_code, 200, preview.content)
                data = json.loads(preview.content)
                download = self.request(kind, record.pk, True)
                self.assertEqual(download.content.decode(), data["content"])
                self.assertEqual(data["download_name"], NUMBER + "." + kind)
                self.assertIn(NUMBER + "." + kind, download["Content-Disposition"])
                self.assertIn("no-store", preview["Cache-Control"])
                self.assertIn("no-store", download["Cache-Control"])

    def test_mir_preserves_width_and_rejects_changed_archive(self):
        claim, row = self.addmir()
        self.assertEqual(slice_mir(claim), row + "\r\n")
        claim.mir_file.file_content = "different"
        with self.assertRaises(ValueError):
            slice_mir(claim)

    def test_recon_retains_quotes_multiline_header_and_eof(self):
        claim = self.addrecon()
        self.assertEqual(slice_recon(claim), 'claim_number,paid_amount,note\r\n' + NUMBER + ',80,"quoted, value\r\ncontinued"\r\n\x1a')

    def test_recon_fixed_width_uses_original_rows_once(self):
        claim = self.addrecon()
        claim.recon_file.file_content = "OTHER    \r\n\r\n" + NUMBER + "  80.00     \r\n\x1a"
        claim.recon_file.save()
        claim.service_lines.update(source_row_number=2, raw_service=NUMBER + "  80.00     ")
        RECONServiceLine.objects.create(recon_claim=claim, recon_file=claim.recon_file,
                                        service_sequence=2, source_row_number=2, raw_service=NUMBER + "  80.00     ")
        self.assertEqual(slice_recon(claim), NUMBER + "  80.00     \r\n\x1a")

    def test_changed_recon_archive_fails_closed(self):
        claim = self.addrecon()
        claim.recon_file.file_content = claim.recon_file.file_content.replace(NUMBER, "DIFFERENT")
        with self.assertRaises(ValueError):
            slice_recon(claim)

    def test_invalid_slice_returns_422_not_another_claim(self):
        row = self.add837()
        row.claim_sequence = 1
        row.save()
        self.assertEqual(self.request("837", row.pk).status_code, 422)
