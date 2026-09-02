from django.test import TestCase
import json
from datetime import datetime, timezone as datetime_timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

from accounts.models import Client, ClientContact, User
from admin_panel.models import AuditLog
from edi835.models import EDI835File


class Onboarding835ValidationTestCase(TestCase):
    def setUp(self):
        self.client_record = Client.objects.create(
            name="835 Validation Health",
            client_code="VALIDATE-835",
            email="validation-835@example.com",
            owner="System Admin",
        )
        self.url = (
            f"/admin-panel/api/clients/{self.client_record.id}/"
            "steps/step_7_835_val/validate-uploaded/"
        )

    def test_validator_reads_uploaded_filename_without_name_error(self):
        response = self.client.post(
            self.url,
            data=b"not an X12 document",
            content_type="application/octet-stream",
            HTTP_X_FILENAME="sample.835",
        )

        self.assertEqual(response.status_code, 400)
        self.assertNotIn("name 'os' is not defined", response.json().get("error", ""))


class PermanentClientDeletionTestCase(TestCase):
    def setUp(self):
        self.superadmin = User.objects.create_superuser(
            email="delete-superadmin@example.com",
            name="Delete Superadmin",
            mobile="5550199000",
            password="correct-password",
        )
        self.staff = User.objects.create_user(
            email="delete-staff@example.com",
            name="Delete Staff",
            mobile="5550199001",
            password="staff-password",
            is_staff=True,
        )
        self.client_record = Client.objects.create(
            name="Delete Me Health",
            client_code="DELETE-ME",
            email="delete-me@example.com",
            owner="System Admin",
        )
        self.client_user = User.objects.create_user(
            email="tenant-user@example.com",
            name="Tenant User",
            mobile="5550199002",
            password="tenant-password",
            client=self.client_record,
        )
        EDI835File.objects.create(
            client=self.client_record,
            original_filename="delete-me.835",
            stored_filename="delete-me.835",
        )
        AuditLog.objects.create(
            module="CLIENTS", action="CREATED", details="Tenant data",
            performed_by="Tester", client=self.client_record,
        )
        self.url = f"/admin-panel/api/clients/{self.client_record.id}/delete/"

    def post_delete(self, user, name=None, password="correct-password"):
        self.client.force_login(user)
        return self.client.post(
            self.url,
            data=json.dumps({
                "confirmation_name": name if name is not None else self.client_record.name,
                "password": password,
            }),
            content_type="application/json",
        )

    def test_staff_admin_cannot_delete_client(self):
        response = self.post_delete(self.staff, password="staff-password")
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Client.objects.filter(id=self.client_record.id).exists())

    def test_wrong_name_or_password_does_not_delete_anything(self):
        self.assertEqual(self.post_delete(self.superadmin, name="delete me health").status_code, 400)
        self.assertEqual(self.post_delete(self.superadmin, password="wrong-password").status_code, 403)
        self.assertTrue(Client.objects.filter(id=self.client_record.id).exists())
        self.assertTrue(User.objects.filter(id=self.client_user.id).exists())

    def test_verified_superadmin_deletes_all_tenant_records(self):
        response = self.post_delete(self.superadmin)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Client.objects.filter(id=self.client_record.id).exists())
        self.assertFalse(User.objects.filter(id=self.client_user.id).exists())
        self.assertFalse(EDI835File.objects.filter(client_id=self.client_record.id).exists())
        self.assertFalse(AuditLog.objects.filter(client_id=self.client_record.id).exists())
        self.assertTrue(User.objects.filter(id=self.superadmin.id).exists())


class AuditLogPaginationTestCase(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            email="audit-admin@example.com", name="Audit Admin",
            mobile="5550199200", password="test-password",
        )
        self.client.force_login(self.admin)
        self.client_record = Client.objects.create(
            name="Audit Health", client_code="AUDIT", email="audit-client@example.com",
        )
        for index in range(30):
            AuditLog.objects.create(
                module="AUTH" if index % 2 else "SYSTEM",
                action="LOGIN" if index % 2 else "CONFIG_SAVED",
                details="needle outside first page" if index == 0 else f"Routine event {index}",
                performed_by="Audit Admin" if index % 2 else "System Worker",
                client=self.client_record,
            )

    def test_paginates_after_filtering_the_complete_log(self):
        response = self.client.get(
            "/admin-panel/api/audit-logs/",
            {"page": 1, "page_size": 10, "search": "needle outside first page"},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["pagination"]["total_count"], 1)
        self.assertEqual(len(data["logs"]), 1)
        self.assertIn("needle", data["logs"][0]["details"])

    def test_supports_standard_filters_and_page_metadata(self):
        response = self.client.get(
            "/admin-panel/api/audit-logs/",
            {"page": 2, "page_size": 10, "module": "AUTH", "action": "LOGIN"},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["pagination"]["page"], 2)
        self.assertEqual(data["pagination"]["total_count"], 15)
        self.assertEqual(len(data["logs"]), 5)
        self.assertTrue(all(item["module"] == "AUTH" and item["action"] == "LOGIN" for item in data["logs"]))
        self.assertIn("AUTH", data["filter_options"]["modules"])

    def test_universal_search_includes_displayed_timestamp_values(self):
        target = AuditLog.objects.create(
            module="AUTH", action="LOGIN", details="Timestamp needle 02/03/2042",
            performed_by="tester",
        )
        response = self.client.get(
            "/admin-panel/api/audit-logs/",
            {"search": "Timestamp needle 02/03/2042", "page_size": 10},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["pagination"]["total_count"], 1)
        self.assertEqual(response.json()["logs"][0]["id"], target.id)


class AdministrativeRoleTransitionTestCase(TestCase):
    def setUp(self):
        self.superadmin = User.objects.create_superuser(
            email="role-superadmin@example.com",
            name="Role Superadmin",
            mobile="5550199100",
            password="test-password",
        )
        self.admin = User.objects.create_user(
            email="role-admin@example.com",
            name="Role Admin",
            mobile="5550199101",
            password="test-password",
            is_staff=True,
        )
        self.standard_user = User.objects.create_user(
            email="role-user@example.com",
            name="Role User",
            mobile="5550199102",
            password="test-password",
        )

    def update_role(self, actor, target, role, client_id=None):
        self.client.force_login(actor)
        return self.client.post(
            f"/admin-panel/api/users/{target.id}/update/",
            data=json.dumps({
                "name": target.name,
                "email": target.email,
                "mobile": target.mobile,
                "role": role,
                "is_staff": role in {"Admin", "Super Admin"},
                "is_superuser": role == "Super Admin",
                "client_id": client_id,
            }),
            content_type="application/json",
        )

    def test_superadmin_can_promote_admin_without_client_assignment(self):
        response = self.update_role(self.superadmin, self.admin, "Super Admin")
        self.assertEqual(response.status_code, 200)
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_staff)
        self.assertTrue(self.admin.is_superuser)
        self.assertIsNone(self.admin.client_id)

    def test_superadmin_can_demote_superadmin_to_admin_without_client_assignment(self):
        promoted = User.objects.create_superuser(
            email="role-second-superadmin@example.com",
            name="Second Superadmin",
            mobile="5550199103",
            password="test-password",
        )
        response = self.update_role(self.superadmin, promoted, "Admin")
        self.assertEqual(response.status_code, 200)
        promoted.refresh_from_db()
        self.assertTrue(promoted.is_staff)
        self.assertFalse(promoted.is_superuser)
        self.assertIsNone(promoted.client_id)

    def test_standard_admin_cannot_change_administrative_roles(self):
        response = self.update_role(self.admin, self.standard_user, "Super Admin")
        self.assertEqual(response.status_code, 403)
        self.standard_user.refresh_from_db()
        self.assertFalse(self.standard_user.is_staff)
        self.assertFalse(self.standard_user.is_superuser)

    def test_standard_user_still_requires_client_assignment(self):
        response = self.update_role(self.superadmin, self.admin, "User")
        self.assertEqual(response.status_code, 400)
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_staff)
        self.assertFalse(self.admin.is_superuser)

    def test_access_matrix_keeps_staff_separate_from_offboarded_clients(self):
        offboarded = Client.objects.create(
            name="Former Tenant",
            code="FORMER-TENANT",
            stage="offboarded",
        )
        User.objects.filter(pk=self.admin.pk).update(client=offboarded)
        User.objects.filter(pk=self.superadmin.pk).update(client=offboarded)
        tenant_user = User.objects.create_user(
            email="former-user@example.com",
            name="Former User",
            mobile="5550199199",
            password="test-password",
            client=offboarded,
        )

        self.client.force_login(self.superadmin)
        response = self.client.get("/admin-panel/api/access/info/")

        self.assertEqual(response.status_code, 200)
        rows = {row["email"]: row for row in response.json()["staff"]}
        self.assertIn(self.admin.email, rows)
        self.assertIn(self.superadmin.email, rows)
        self.assertNotIn(tenant_user.email, rows)
        self.assertEqual(rows[self.admin.email]["clients"], ["OneSmarter"])
        self.assertEqual(rows[self.superadmin.email]["clients"], ["OneSmarter"])


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
        from datetime import time
        from edi835.models import SFTPAutomationSchedule
        user = User.objects.create_user(
            email="offboard-user@example.com", name="Offboard User", mobile="5550103000",
            password="test-password", client=self.client_record,
        )
        schedule = SFTPAutomationSchedule.objects.create(
            client=self.client_record, automation_type="835", run_time=time(9, 0),
            enabled=True, created_by=self.admin, updated_by=self.admin,
        )
        step3_url = f"/admin-panel/api/clients/{self.client_record.id}/offboarding/steps/3/complete/"
        self.assertEqual(self.client.post(step3_url).status_code, 409)

        for step_number in (1, 2):
            response = self.client.post(
                f"/admin-panel/api/clients/{self.client_record.id}/offboarding/steps/{step_number}/complete/"
            )
            self.assertEqual(response.status_code, 200)

        with patch(
            "admin_panel.email_service.send_client_offboarding_notice",
            return_value={"attempted": 1, "sent": 1, "failed": []},
        ) as send_notice:
            response = self.client.post(step3_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["revoked_users"], 1)
        self.assertEqual(response.json()["email_notifications"]["sent"], 1)
        send_notice.assert_called_once()
        self.assertEqual(send_notice.call_args.args[1], [user.email])
        self.client_record.refresh_from_db()
        user.refresh_from_db()
        self.assertEqual(self.client_record.status, "INACTIVE")
        self.assertEqual(self.client_record.stage, "offboarded")
        self.assertFalse(user.is_active)
        schedule.refresh_from_db()
        self.assertFalse(schedule.enabled)
        self.assertIsNone(schedule.next_run_at)

        access = self.client.get("/admin-panel/api/access/info/")
        self.assertEqual(access.status_code, 200)
        self.assertNotIn(user.email, [item["email"] for item in access.json()["users"]])

        # Finalization is irreversible: repeat completion and every workflow
        # mutation are rejected while read-only history remains available.
        repeated = self.client.post(step3_url)
        self.assertEqual(repeated.status_code, 409)
        self.assertEqual(repeated.json()["code"], "CLIENT_OFFBOARDING_FINALIZED")

        state = self.client.get(
            f"/admin-panel/api/clients/{self.client_record.id}/offboarding/state/"
        )
        self.assertEqual(state.status_code, 200)
        self.assertTrue(state.json()["state"]["locked"])
        self.assertEqual(state.json()["state"]["completed_steps"], 3)

        for step_number in (1, 2, 3):
            redo = self.client.post(
                f"/admin-panel/api/clients/{self.client_record.id}/offboarding/steps/{step_number}/redo/"
            )
            self.assertEqual(redo.status_code, 409)

        onboarding_redo = self.client.post(
            f"/admin-panel/api/clients/{self.client_record.id}/steps/step_1_mutual_nda_signed/redo/"
        )
        self.assertEqual(onboarding_redo.status_code, 409)

        golive_redo = self.client.post(
            f"/admin-panel/api/clients/{self.client_record.id}/golive/steps/1/redo/"
        )
        self.assertEqual(golive_redo.status_code, 409)

        add_note = self.client.post(
            f"/admin-panel/api/clients/{self.client_record.id}/steps/offboard_step_1/notes/",
            data=json.dumps({"note_text": "Attempt to modify finalized history"}),
            content_type="application/json",
        )
        self.assertEqual(add_note.status_code, 409)

        reactivate = self.client.post(
            f"/admin-panel/api/clients/{self.client_record.id}/update/",
            data=json.dumps({"stage": "onboarding", "status": "ACTIVE"}),
            content_type="application/json",
        )
        self.assertEqual(reactivate.status_code, 409)
        self.client_record.refresh_from_db()
        self.assertEqual(self.client_record.stage, "offboarded")
        self.assertEqual(self.client_record.status, "INACTIVE")

        clients_response = self.client.get("/admin-panel/api/clients/")
        self.assertEqual(clients_response.status_code, 200)
        listed_client = next(
            item for item in clients_response.json()["clients"]
            if item["id"] == str(self.client_record.id)
        )
        self.assertEqual(listed_client["stage"], "offboarded")


class ScheduleTimezoneTestCase(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            email="timezone-admin@example.com",
            name="Timezone Admin",
            mobile="5550104000",
            password="test-password",
        )
        self.client.force_login(self.admin)
        self.client_record = Client.objects.create(
            name="Timezone Test Client",
            client_code="TZTEST",
            email="timezone@example.com",
        )

    def test_golive_schedule_persists_selected_iana_timezone(self):
        response = self.client.post(
            f"/admin-panel/api/clients/{self.client_record.id}/golive/steps/4/schedule/",
            data=json.dumps({
                "production_date": "01-15-2026",
                "production_time": "10:00",
                "timezone": "America/New_York",
                "notes": "Timezone regression",
            }),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.client_record.refresh_from_db()
        self.assertEqual(self.client_record.timezone, "America/New_York")
        self.assertEqual(
            self.client_record.live_since.astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M"),
            "2026-01-15 10:00",
        )
        step4 = next(item for item in response.json()["state"]["steps"] if item["step_number"] == 4)
        self.assertEqual(step4["extra"]["schedule"]["timezone"], "America/New_York")
        self.assertIn("scheduled_at", step4["extra"]["schedule"])

    def test_invalid_timezone_falls_back_to_eastern(self):
        response = self.client.post(
            f"/admin-panel/api/clients/{self.client_record.id}/golive/steps/4/schedule/",
            data=json.dumps({
                "production_date": "01-15-2026",
                "production_time": "10:00",
                "timezone": "Not/A_Zone",
            }),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.client_record.refresh_from_db()
        self.assertEqual(self.client_record.timezone, "America/New_York")
