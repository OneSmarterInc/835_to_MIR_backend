import json
from datetime import date

from django.test import TestCase
from django.utils import timezone

from accounts.models import Client, User
from edi835.models import EDI837Claim, EDI837File, MPLNotice
from edi835.mpl_notices import (\n    NoticeValidationError,\n    extract_claim_identifiers,\n    parse_subject,\n    process_notice,\n    split_latest_message,\n)


class MPLSubjectTests(TestCase):
    def test_accepts_actual_mpl_subject_variants(self):
        cases = [
            ("MIR Back to the TPA File -- 8/21 thru 8/26  -- ABC", "ABC", "MIR_RESULTS"),
            ("Fw: MIR Back to the TPA File -- 9/2 thru 9/8 -- ABC_CPT", "ABC_CPT", "MIR_RESULTS"),
            ("Re: MIR Back to the TPA File -- 8/18 thru 8/20 -- ABC_CPT -Acknowledged", "ABC_CPT", "ACKNOWLEDGEMENT"),
        ]
        for subject, program, notice_type in cases:
            parsed = parse_subject(subject, 2026)
            self.assertEqual(parsed["program"], program)
            self.assertEqual(parsed["notice_type"], notice_type)

    def test_rejects_obsolete_synthetic_notice_format(self):
        with self.assertRaises(NoticeValidationError):
            parse_subject("MPL-RTN-20250806-114", 2026)

    def test_handles_reporting_period_across_new_year(self):
        parsed = parse_subject("MIR Back to the TPA File -- 12/29 thru 1/4 -- ABC", 2026)
        self.assertEqual(parsed["period_start"], date(2026, 12, 29))
        self.assertEqual(parsed["period_end"], date(2027, 1, 4))

    def test_splits_latest_outlook_message_from_thread(self):
        latest, history = split_latest_message("Please review this claim.\n\nFrom: Scott\nSent: Tuesday\nOlder email")
        self.assertEqual(latest, "Please review this claim.")
        self.assertIn("From: Scott", history)


class MPLClaimExtractionTests(TestCase):
    def test_extracts_real_mpl_claim_numbers_and_excludes_issue_codes(self):
        body = """
        UE084 - use PR31
        MP013 - missing group
        RR001 Error
        33020262300027000
        33020262091936200
        33020262253998500--UE036
        MP001/MP002
        """
        self.assertEqual(
            extract_claim_identifiers(body),
            [
                "33020262300027000",
                "33020262091936200",
                "33020262253998500",
            ],
        )

    def test_accepts_explicitly_labeled_legacy_alphanumeric_claim(self):
        self.assertEqual(
            extract_claim_identifiers("Please review claim CLM12345."),
            ["CLM12345"],
        )

    def test_does_not_extract_dates_mir_fields_or_ordinary_words(self):
        body = "Period 2026-08-21. Check MIR1019, CON89, HEADER, DIRECT and UE115."
        self.assertEqual(extract_claim_identifiers(body), [])


class MPLNoticeAPITests(TestCase):
    def setUp(self):
        self.client_record = Client.objects.create(name="ABC Health", client_code="ABC", email="abc@example.com")
        self.user = User.objects.create_user(email="mpl@example.com", name="MPL User", mobile="5550102000", password="password", client=self.client_record)
        self.client.force_login(self.user)

    def test_client_can_create_and_list_actual_email(self):
        response = self.client.post("/edi835/api/mpl-notices/", data=json.dumps({
            "subject": "MIR Back to the TPA File -- 9/2 thru 9/8 -- ABC",
            "email_body": "Hi Everyone!\nMIR Results:\nPlease review claim CLM12345.",
            "reporting_year": 2026,
        }), content_type="application/json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["notice"]["program"], "ABC")
        listed = self.client.get("/edi835/api/mpl-notices/")
        self.assertEqual(len(listed.json()["notices"]), 1)

    def test_client_cannot_create_notice_for_other_client(self):
        other = Client.objects.create(name="Other", client_code="OTHER", email="other@example.com")
        response = self.client.post("/edi835/api/mpl-notices/", data=json.dumps({
            "client_id": str(other.id),
            "subject": "MIR Back to the TPA File -- 9/2 thru 9/8 -- ABC",
            "email_body": "Claim CLM12345",
            "reporting_year": 2026,
        }), content_type="application/json")
        # A client identity is always pinned to its own tenant, even if another id is supplied.
        self.assertEqual(response.status_code, 201)
        self.assertEqual(MPLNotice.objects.get().client, self.client_record)

    def test_processes_exact_claim_with_deterministic_fallback(self):
        edi_file = EDI837File.objects.create(client=self.client_record, uploaded_by=self.user, original_filename="abc.837", stored_filename="abc.837", file_content="x", file_hash="a" * 64, status="PROCESSED", processed_at=timezone.now())
        claim = EDI837Claim.objects.create(edi_file=edi_file, client=self.client_record, claim_sequence=1, claim_control_number="CLM12345", service_count=1, total_charge_amount="100.00")
        notice = MPLNotice.objects.create(client=self.client_record, subject="MIR Back to the TPA File -- 9/2 thru 9/8 -- ABC", raw_email_body="Hi Everyone! MIR Results: claim CLM12345 returned with NO PREFIX. Please review.", reporting_year=2026, created_by=self.user)
        process_notice(notice.id)
        notice.refresh_from_db()
        self.assertEqual(notice.status, "COMPLETED")
        link = notice.notice_claims.get(claim=claim)
        codes = {item["code"] for item in link.analysis.findings}
        self.assertIn("MIR_CLAIM_MISSING", codes)
        self.assertIn("NO_PREFIX_NOTICE", codes)
        self.assertEqual(link.analysis.model_id, "deterministic-fallback")

    def test_unmatched_email_claims_remain_visible(self):
        notice = MPLNotice.objects.create(
            client=self.client_record,
            subject="MIR Back to the TPA File -- 9/2 thru 9/8 -- ABC",
            raw_email_body=(
                "UE084 - use PR31. Claims 33020262300027000 and "
                "33020262091936200 require review."
            ),
            reporting_year=2026,
            created_by=self.user,
        )
        process_notice(notice.id)
        notice.refresh_from_db()
        self.assertEqual(
            notice.extracted_claim_numbers,
            ["33020262300027000", "33020262091936200"],
        )
        self.assertEqual(notice.source_matches, [])
        self.assertNotIn("UE084", notice.extracted_claim_numbers)

    def test_no_claim_is_review_required_not_fabricated(self):
        notice = MPLNotice.objects.create(client=self.client_record, subject="MIR Back to the TPA File -- 9/2 thru 9/8 -- ABC", raw_email_body="Please review.", reporting_year=2026, created_by=self.user)
        process_notice(notice.id)
        notice.refresh_from_db()
        self.assertEqual(notice.status, "REVIEW_REQUIRED")
        self.assertFalse(notice.notice_claims.exists())
