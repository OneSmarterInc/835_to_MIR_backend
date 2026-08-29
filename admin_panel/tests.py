from django.test import TestCase
import json

from accounts.models import Client, ClientContact, User


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

    def test_contact_save_rejects_duplicates_and_contact_can_be_deleted(self):
        self.client.get(f"/admin-panel/api/clients/{self.client_record.id}/state/")
        url = f"/admin-panel/api/clients/{self.client_record.id}/steps/step_4_contacts/save/"
        payload = {
            "role_name": "Technical Contact", "employee_name": "Jane Doe",
            "email": "jane@example.com", "phone": "+15550102020",
        }
        first = self.client.post(url, data=json.dumps(payload), content_type="application/json")
        duplicate = self.client.post(url, data=json.dumps(payload), content_type="application/json")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(ClientContact.objects.filter(client=self.client_record).count(), 1)

        contact_id = first.json()["contact"]["id"]
        deleted = self.client.post(
            f"/admin-panel/api/clients/{self.client_record.id}/contacts/{contact_id}/delete/"
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertFalse(ClientContact.objects.filter(id=contact_id).exists())

    def test_note_and_client_user_deletes_are_scoped(self):
        note = self.client.post(
            f"/admin-panel/api/clients/{self.client_record.id}/steps/step_5_claim_system_verification/notes/",
            data=json.dumps({"note_text": "Delete me"}), content_type="application/json",
        ).json()["note"]
        wrong_step = self.client.post(
            f"/admin-panel/api/clients/{self.client_record.id}/steps/step_4_contacts/notes/{note['id']}/delete/"
        )
        self.assertEqual(wrong_step.status_code, 404)
        deleted = self.client.post(
            f"/admin-panel/api/clients/{self.client_record.id}/steps/step_5_claim_system_verification/notes/{note['id']}/delete/"
        )
        self.assertEqual(deleted.status_code, 200)

        user = User.objects.create_user(
            email="client-user@example.com", name="Client User", mobile="5550102000",
            password="test-password", client=self.client_record,
        )
        deleted_user = self.client.post(
            f"/admin-panel/api/clients/{self.client_record.id}/users/{user.id}/delete/"
        )
        self.assertEqual(deleted_user.status_code, 200)
        self.assertFalse(User.objects.filter(id=user.id).exists())

    def test_offboarding_requires_sequence_and_revokes_client_users(self):
        user = User.objects.create_user(
            email="offboard-user@example.com", name="Offboard User", mobile="5550103000",
            password="test-password", client=self.client_record,
        )
        step3_url = f"/admin-panel/api/clients/{self.client_record.id}/offboarding/steps/3/complete/"
        self.assertEqual(self.client.post(step3_url).status_code, 409)

        for step_number in (1, 2):
            response = self.client.post(
                f"/admin-panel/api/clients/{self.client_record.id}/offboarding/steps/{step_number}/complete/"
            )
            self.assertEqual(response.status_code, 200)

        response = self.client.post(step3_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["revoked_users"], 1)
        self.client_record.refresh_from_db()
        user.refresh_from_db()
        self.assertEqual(self.client_record.status, "INACTIVE")
        self.assertEqual(self.client_record.stage, "offboarded")
        self.assertFalse(user.is_active)

        # Retrying the request is safe and retains the completed/revoked state.
        repeated = self.client.post(step3_url)
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(repeated.json()["revoked_users"], 0)
        self.assertEqual(repeated.json()["state"]["completed_steps"], 3)
