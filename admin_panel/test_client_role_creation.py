"""Regression: the real /admin-panel user API must persist the chosen role."""
import json
from django.test import RequestFactory, TestCase
from accounts.models import Client, User
from admin_panel.views import api_admin_create_user, api_admin_update_user, api_admin_users


class ClientRoleCreationTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.client = Client.objects.create(name="Clinic A", client_code="CLINIC-A-T", email="contact@clinic.example")
        self.admin = User.objects.create_superuser(
            email="root@clinic.example", name="Root", mobile="+14155550101",
            password="NotARealPassword123!",
        )

    def post(self, view, data, *args):
        req = self.factory.post(
            "/admin-panel/api/users/create/",
            data=json.dumps(data),
            content_type="application/json",
        )
        req.user = self.admin
        return view(req, *args)

    def payload(self, email, role):
        return {
            "name": "Staff Member", "email": email, "mobile": "+14155550102",
            "password": "AnotherLongPassword!123", "role": role,
            "client_id": str(self.client.pk),
        }

    def test_creating_client_user_stays_client_user(self):
        response = self.post(
            api_admin_create_user,
            self.payload("viewer@clinic.example", "Client User"),
        )
        self.assertEqual(response.status_code, 200, response.content)
        user = User.objects.get(email="viewer@clinic.example")
        self.assertEqual(user.client_role, "CLIENT_USER")
        self.assertFalse(user.is_staff)
        self.assertEqual(user.client_id, self.client.pk)
        self.assertEqual(json.loads(response.content)["user"]["role"], "Client User")

    def test_create_rejects_unknown_role(self):
        response = self.post(
            api_admin_create_user,
            self.payload("viewer@clinic.example", "Overlord"),
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(User.objects.filter(email="viewer@clinic.example").exists())

    def test_existing_client_admin_can_change_to_client_user(self):
        u = User.objects.create_user(
            email="viewer@clinic.example", name="Viewer", mobile="+14155550102",
            password="AnotherLongPassword!123", client=self.client,
        )
        self.assertEqual(u.client_role, "CLIENT_ADMIN")
        response = self.post(api_admin_update_user, {
            "role": "Client User", "client_id": str(self.client.pk),
        }, u.pk)
        self.assertEqual(response.status_code, 200, response.content)
        u.refresh_from_db()
        self.assertEqual(u.client_role, "CLIENT_USER")
        req = self.factory.get("/admin-panel/api/users/")
        req.user = self.admin
        rows = json.loads(api_admin_users(req).content)["users"]
        self.assertEqual(next(row for row in rows if row["id"] == u.pk)["role"], "Client User")
