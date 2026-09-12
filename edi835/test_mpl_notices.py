import json
from datetime import date
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from accounts.models import Client, User
from edi835.models import EDI837Claim, EDI837File, MPLNotice
from edi835.mpl_notices import (
    NoticeValidationError,
    approved_actions_for_claim,
    authoritative_rule_catalog,
    conversion_findings_for_claim,
    extract_claim_identifiers,
    extract_claim_issue_map,
    local_ai_enabled,
    parse_subject,
    process_notice,
    reported_issue_rules,
    unknown_reported_codes,
    unmatched_notice_actions,
    split_latest_message,
)


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


    def test_associates_section_and_inline_issue_codes_per_claim(self):
        body = """
        UE084 - use PR31
        33020262300027000
        RR001 Error - return as sent on the 837
        44320260280007300
        The following Adjustment needs to be processed:
        33020262242261500 - original claim or Recon not finalized
        33020262253998500--UE036, requires CO41
        MP001/MP002
        33020262323004400
        """
        issues = extract_claim_issue_map(body)
        self.assertEqual(issues["33020262300027000"][0]["codes"], ["UE084"])
        self.assertEqual(issues["44320260280007300"][0]["codes"], ["RR001"])
        self.assertEqual(issues["33020262242261500"][0]["category"], "ADJUSTMENT_PENDING")
        self.assertEqual(issues["33020262253998500"][0]["codes"], ["UE036"])
        self.assertEqual(issues["33020262323004400"][0]["codes"], ["MP001", "MP002"])

    def test_unknown_codes_are_disclosed_not_defined(self):
        self.assertEqual(unknown_reported_codes("UE999 and MP003"), ["UE999"])
        self.assertIn("MP003", authoritative_rule_catalog(["MP003", "UE999"]))
        self.assertNotIn("UE999", authoritative_rule_catalog(["MP003", "UE999"]))

    def test_filters_stored_check_findings_to_the_claim(self):
        from types import SimpleNamespace
        source = SimpleNamespace(
            original_filename="input.835",
            conversion_findings=[
                {"rule_code": "MP003", "claim_number": "33020262300027000", "severity": "REFUSE", "reason": "Cross-foot failed.", "evidence": {"line": 2}},
                {"rule_code": "MP013", "claim_number": "33020262091936200", "severity": "REFUSE", "reason": "Group missing."},
            ],
        )
        findings = conversion_findings_for_claim(source, ["33020262300027000"])
        self.assertEqual([item["code"] for item in findings], ["MP003"])
        self.assertEqual(findings[0]["details"], {"line": 2})


    def test_unmatched_actions_are_issue_and_source_specific(self):
        actions = unmatched_notice_actions([
            {
                "claim_number": "33020262300027000",
                "reported_issues": [{"codes": ["MP011", "UE999"], "category": "ADJUSTMENT_PENDING"}],
                "sources": [{"type": "835"}, {"type": "RECON"}],
            }
        ])
        joined = " ".join(actions)
        self.assertIn("timely-filing", joined)
        self.assertIn("original claim processed", joined)
        self.assertIn("835 claim status", joined)
        self.assertIn("reconciliation status", joined)
        self.assertIn("UE999", joined)
        self.assertNotIn("Confirm that the correct client", joined)


class MPLAIEnablementTests(TestCase):
    def test_local_ai_is_disabled_by_default(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(local_ai_enabled())

    def test_local_ai_requires_explicit_opt_in(self):
        with patch.dict("os.environ", {"MPL_AI_ENABLED": "true"}, clear=True):
            self.assertTrue(local_ai_enabled())


class MPLPromptGroundingTests(TestCase):
    def test_only_known_email_issue_codes_receive_approved_definitions(self):
        rules = reported_issue_rules(
            "UE084 needs review. MP013 is present. UE999 is unknown."
        )
        self.assertEqual(list(rules), ["UE084", "MP013"])
        self.assertNotIn("UE999", rules)

    def test_issue_specific_actions_are_added_without_duplicates(self):
        actions = approved_actions_for_claim(
            "UE011 claim already processed.",
            [],
        )
        self.assertIn(
            "Confirm prior processing in 835 and reconciliation history before taking further action.",
            actions,
        )
        self.assertEqual(len(actions), len(set(actions)))


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
        self.assertEqual([item["claim_number"] for item in notice.source_matches], notice.extracted_claim_numbers)
        self.assertTrue(all(not item["sources"] for item in notice.source_matches))
        self.assertNotIn("UE084", notice.extracted_claim_numbers)

    def test_no_claim_is_review_required_not_fabricated(self):
        notice = MPLNotice.objects.create(client=self.client_record, subject="MIR Back to the TPA File -- 9/2 thru 9/8 -- ABC", raw_email_body="Please review.", reporting_year=2026, created_by=self.user)
        process_notice(notice.id)
        notice.refresh_from_db()
        self.assertEqual(notice.status, "REVIEW_REQUIRED")
        self.assertFalse(notice.notice_claims.exists())
