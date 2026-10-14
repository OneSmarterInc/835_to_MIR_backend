"""Claim-scoped activity, complete attention pagination, and history paging."""
import json
from datetime import timedelta

from django.test import RequestFactory, TestCase
from django.utils import timezone

from accounts.models import Client, User
from edi835.client_portal_actions import actions
from edi835.client_portal_activity import attach_claim_activity
from edi835.client_portal_holds import attention_claims
from edi835.client_portal_views import history
from edi835.models import ClientPortalAction, EDI835Claim, EDI835File, MIRFile


class ClientClaimQueueTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.client_a = Client.objects.create(name="Alpha", client_code="QUEUE_A", email="a@example.org")
        self.client_b = Client.objects.create(name="Beta", client_code="QUEUE_B", email="b@example.org")
        self.user = User.objects.create_user(
            email="queue@example.org", name="Reviewer", mobile="+15555551011",
            client=self.client_a, client_role="CLIENT_USER",
        )
        self.file = EDI835File.objects.create(
            client=self.client_a, original_filename="source.835", stored_filename="source.835",
            held_claims_count=12,
            conversion_findings=[{"severity": "HOLD", "claim_index": i, "reason": "Plan ID required"} for i in range(1, 13)],
        )
        self.claims = [EDI835Claim.objects.create(
            edi_file=self.file, claim_sequence=i, highmark_claim_number=f"CLAIM-{i:06d}",
            total_charge_amount=100, paid_amount=50,
        ) for i in range(1, 13)]
        self.other_file = EDI835File.objects.create(
            client=self.client_b, original_filename="private.835", stored_filename="private.835",
            held_claims_count=1, conversion_findings=[{"severity": "HOLD", "claim_index": 1}],
        )
        self.other_claim = EDI835Claim.objects.create(
            edi_file=self.other_file, claim_sequence=1, highmark_claim_number="PRIVATE",
            total_charge_amount=100,
        )

    def get(self, view, query=None):
        request = self.factory.get("/", query or {})
        request.user = self.user
        response = view(request)
        self.assertEqual(response.status_code, 200, response.content)
        return json.loads(response.content)

    def action(self, claim=None, kind="NOTE", message="Claim note", **extra):
        return ClientPortalAction.objects.create(
            client=self.client_a, file=self.file, claim=claim, created_by=self.user,
            kind=kind, message=message, **extra,
        )

    def test_attention_pages_include_every_claim_without_other_tenants(self):
        first = self.get(attention_claims)
        second = self.get(attention_claims, {"page": 2})
        self.assertEqual((first["count"], first["pages"]), (12, 2))
        ids = [row["id"] for row in first["claims"] + second["claims"]]
        self.assertCountEqual(ids, [claim.pk for claim in self.claims])
        self.assertEqual(len(ids), len(set(ids)))
        self.assertNotIn(self.other_claim.pk, ids)

    def test_old_held_claims_are_not_cut_off_at_90_days(self):
        EDI835File.objects.filter(pk=self.file.pk).update(uploaded_at=timezone.now() - timedelta(days=120))
        result = self.get(attention_claims)
        self.assertEqual(result["count"], 12)
        self.assertGreaterEqual(result["claims"][0]["days"], 119)

    def test_source_findings_are_deduplicated_without_fabricating_claim_ids(self):
        self.file.conversion_findings += [
            {"severity": "HOLD", "claim_number": "UNMATCHED", "reason": "First issue"},
            {"severity": "HOLD", "claim_number": "UNMATCHED", "reason": "Second issue"},
        ]
        self.file.save(update_fields=["conversion_findings"])
        result = self.get(attention_claims, {"page_size": 50})
        unmatched = [row for row in result["claims"] if row["id"] is None]
        self.assertEqual(len(unmatched), 1)
        self.assertIn("Second issue", unmatched[0]["reason"])
        self.assertEqual(result["unmatched_count"], 1)

    def test_activity_is_claim_scoped_and_not_limited_by_recent_200(self):
        wanted = self.action(self.claims[0], message="Only claim one")
        self.action(message="Legacy file note")
        for i in range(205):
            self.action(self.claims[1], message=f"Other claim note {i}")
        rows = [{"id": claim.pk} for claim in self.claims]
        with self.assertNumQueries(1):
            attach_claim_activity(self.client_a, rows)
        self.assertEqual([a["id"] for a in rows[0]["review_activity"]], [wanted.pk])
        self.assertEqual(rows[0]["review_activity"][0]["record_id"], self.claims[0].pk)
        self.assertEqual(rows[2]["review_activity"], [])
        self.assertEqual(len(rows[1]["review_activity"]), 1)

    def test_pending_request_survives_newer_note_and_reviewed_request(self):
        pending = self.action(self.claims[0], kind="REQUEST_RESEND", message="")
        self.action(self.claims[0], kind="REQUEST_RESEND", status="REVIEWED", message="")
        self.action(self.claims[0], message="New note")
        rows = [{"id": self.claims[0].pk}]
        attach_claim_activity(self.client_a, rows)
        self.assertIn(pending.pk, [row["id"] for row in rows[0]["review_activity"]])

    def test_activity_lookup_rejects_other_tenant_even_with_known_id(self):
        rows = [{"id": self.other_claim.pk}]
        attach_claim_activity(self.client_a, rows)
        self.assertEqual(rows[0]["review_activity"], [])
        request = self.factory.get("/", {"claim_id": self.other_claim.pk})
        request.user = self.user
        self.assertEqual(actions(request).status_code, 404)

    def test_claim_activity_paginates_without_file_or_sibling_notes(self):
        for i in range(13):
            self.action(self.claims[0], message=f"Claim note {i}")
        self.action(self.claims[1], message="Sibling note")
        self.action(message="Legacy note")
        result = self.get(actions, {"claim_id": self.claims[0].pk, "page": 2})
        self.assertEqual(result["count"], 13)
        self.assertEqual(len(result["actions"]), 3)
        self.assertTrue(all(a["claim_id"] == self.claims[0].pk for a in result["actions"]))

    def test_history_17_files_have_two_pages_and_stable_order(self):
        for i in range(17):
            source = EDI835File.objects.create(client=self.client_a, original_filename=f"{i}.835", stored_filename=f"{i}.835")
            MIRFile.objects.create(client=self.client_a, source_835=source, mir_filename=f"{i}.MIR")
        first = self.get(history)
        second = self.get(history, {"page": 2})
        self.assertEqual((first["pages"], len(first["files"]), len(second["files"])), (2, 10, 7))
        self.assertFalse({f["id"] for f in first["files"]} & {f["id"] for f in second["files"]})
        self.assertEqual(self.get(history, {"page_size": 25})["pages"], 1)

    def test_invalid_pagination_is_rejected(self):
        for view in (attention_claims, history):
            request = self.factory.get("/", {"page_size": "invalid"})
            request.user = self.user
            self.assertEqual(view(request).status_code, 400)

    def test_attention_has_no_queries_per_claim(self):
        with self.assertNumQueries(3):
            self.get(attention_claims)
