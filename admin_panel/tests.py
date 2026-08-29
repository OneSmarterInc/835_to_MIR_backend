from django.test import TestCase
import json

from accounts.models import Client, User


class OnboardingSequenceTestCase(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            email="onboarding-admin@example.com",
            name="Onboarding Admin",
            mobile="5550101000",
            password="test-password",
        )
        self.client.force_login(self.admin)
        self.client_record = Client.objects.create(
            name="Sequence Test Client",
            client_code="SEQTEST",
            email="sequence@example.com",
        )

    def test_onboarding_steps_are_grouped_and_sequenced_by_dependency(self):
        response = self.client.get(
            f"/admin-panel/api/clients/{self.client_record.id}/state/"
        )
        self.assertEqual(response.status_code, 200)
        steps = response.json()["state"]["steps"]

        self.assertEqual(len(steps), 16)
        self.assertEqual(
            [step["id"] for step in steps],
            [1, 2, 3, 4, 5, 6, 7, 10, 16, 9, 8, 11, 12, 13, 14, 15],
        )
        self.assertEqual([step["displayNumber"] for step in steps], list(range(1, 17)))

        by_id = {step["id"]: step for step in steps}
        self.assertEqual(by_id[3]["phase"], "DOCUMENTS & COMPLIANCE")
        self.assertEqual(by_id[4]["phase"], "CLIENT DISCOVERY")
        self.assertEqual(by_id[6]["phase"], "SECURE DELIVERY & ACCESS")
        self.assertEqual(by_id[10]["actionType"], "naming_config")
        self.assertEqual(by_id[16]["actionType"], "user_creation")
        self.assertEqual(by_id[9]["phase"], "CONVERSION CONFIGURATION & VALIDATION")
        self.assertEqual(by_id[12]["phase"], "PRODUCTION READINESS")
        self.assertEqual(by_id[14]["phase"], "GO-LIVE & SIGN-OFF")

    def test_step_notes_are_persisted_scoped_and_returned(self):
        create_response = self.client.post(
            f"/admin-panel/api/clients/{self.client_record.id}/steps/step_5_claim_system_verification/notes/",
            data=json.dumps({"note_text": "Persistent onboarding note"}),
            content_type="application/json",
        )
        self.assertEqual(create_response.status_code, 200)

        response = self.client.get(
            f"/admin-panel/api/clients/{self.client_record.id}/steps/step_5_claim_system_verification/notes/"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [note["note_text"] for note in response.json()["notes"]],
            ["Persistent onboarding note"],
        )

    def test_note_namespaces_do_not_mix_workflows(self):
        for step_key, note_text in (
            ("golive_step_5", "Go-live note"),
            ("offboard_step_1", "Offboarding note"),
        ):
            response = self.client.post(
                f"/admin-panel/api/clients/{self.client_record.id}/steps/{step_key}/notes/",
                data=json.dumps({"note_text": note_text}),
                content_type="application/json",
            )
            self.assertEqual(response.status_code, 200)

        golive = self.client.get(
            f"/admin-panel/api/clients/{self.client_record.id}/steps/golive_step_5/notes/"
        ).json()["notes"]
        offboard = self.client.get(
            f"/admin-panel/api/clients/{self.client_record.id}/steps/offboard_step_1/notes/"
        ).json()["notes"]
        self.assertEqual([note["note_text"] for note in golive], ["Go-live note"])
        self.assertEqual([note["note_text"] for note in offboard], ["Offboarding note"])
