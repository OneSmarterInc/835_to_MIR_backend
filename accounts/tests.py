import json
from django.test import TestCase, Client as DjangoTestClient
from accounts.models import Client, User


class AdminClientApiTestCase(TestCase):
    def setUp(self):
        self.client_api = DjangoTestClient()
        self.admin_user = User.objects.create_superuser(
            email="admin@example.com",
            name="Admin User",
            mobile="+15551111",
            password="adminpassword"
        )
        self.client_api.login(email="admin@example.com", password="adminpassword")
        self.c1 = Client.objects.create(
            name="Alpha Health",
            client_code="CLT-ALPHA",
            email="alpha@health.com",
            status="ACTIVE"
        )

    def test_list_clients(self):
        res = self.client_api.get("/accounts/api/admin/clients/")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["total_clients"], 1)

    def test_create_client(self):
        payload = {
            "name": "Beta Medical",
            "client_code": "CLT-BETA",
            "email": "beta@med.com",
            "status": "ACTIVE"
        }
        res = self.client_api.post(
            "/accounts/api/admin/clients/create/",
            data=json.dumps(payload),
            content_type="application/json"
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        self.assertTrue(Client.objects.filter(client_code="CLT-BETA").exists())

    def test_update_client(self):
        payload = {"status": "INACTIVE", "name": "Alpha Health Updated"}
        res = self.client_api.post(
            f"/accounts/api/admin/clients/{self.c1.id}/update/",
            data=json.dumps(payload),
            content_type="application/json"
        )
        self.assertEqual(res.status_code, 200)
        self.c1.refresh_from_db()
        self.assertEqual(self.c1.status, "INACTIVE")
        self.assertEqual(self.c1.name, "Alpha Health Updated")

    def test_delete_client(self):
        res = self.client_api.post(
            f"/accounts/api/admin/clients/{self.c1.id}/delete/",
            data=json.dumps({
                "confirmation_name": self.c1.name,
                "password": "adminpassword",
            }),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertFalse(Client.objects.filter(id=self.c1.id).exists())

    def test_delete_client_requires_exact_name_and_password(self):
        url = f"/accounts/api/admin/clients/{self.c1.id}/delete/"
        wrong_name = self.client_api.post(
            url,
            data=json.dumps({"confirmation_name": "Wrong", "password": "adminpassword"}),
            content_type="application/json",
        )
        self.assertEqual(wrong_name.status_code, 400)
        wrong_password = self.client_api.post(
            url,
            data=json.dumps({"confirmation_name": self.c1.name, "password": "wrong"}),
            content_type="application/json",
        )
        self.assertEqual(wrong_password.status_code, 403)
        self.assertTrue(Client.objects.filter(id=self.c1.id).exists())


class OffboardedClientAccessTestCase(TestCase):
    def setUp(self):
        self.http = DjangoTestClient()
        self.client_record = Client.objects.create(
            name="Revoked Health",
            client_code="CLT-REVOKED",
            email="revoked@example.com",
            status="INACTIVE",
            stage="offboarded",
        )
        self.user = User.objects.create_user(
            email="revoked-user@example.com",
            name="Revoked User",
            mobile="+15550199",
            password="correct-password",
            client=self.client_record,
            is_active=False,
        )

    def test_correct_credentials_return_offboarded_lock_state(self):
        response = self.http.post(
            "/accounts/api/login/",
            data=json.dumps({
                "email": self.user.email,
                "password": "correct-password",
                "isAdminRoute": False,
            }),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertTrue(response.json()["offboarded"])
        self.assertEqual(response.json()["code"], "CLIENT_OFFBOARDED")
        self.assertNotIn("sessionid", response.cookies)

    def test_wrong_password_does_not_disclose_offboarding(self):
        response = self.http.post(
            "/accounts/api/login/",
            data=json.dumps({
                "email": self.user.email,
                "password": "wrong-password",
                "isAdminRoute": False,
            }),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json().get("offboarded", False))

    def test_authenticated_offboarded_user_is_blocked_from_client_apis(self):
        self.user.is_active = True
        self.user.save(update_fields=["is_active"])
        self.http.force_login(self.user)

        state = self.http.get("/accounts/api/user/")
        self.assertEqual(state.status_code, 200)
        self.assertTrue(state.json()["offboarded"])

        blocked = self.http.get("/accounts/api/contacts/")
        self.assertEqual(blocked.status_code, 403)
        self.assertEqual(blocked.json()["code"], "CLIENT_OFFBOARDED")


class AdministratorLoginRouteTestCase(TestCase):
    def setUp(self):
        self.http = DjangoTestClient()

    def test_anonymous_visitor_can_load_admin_login_shell(self):
        response = self.http.get("/administrator")
        self.assertEqual(response.status_code, 200)

    def test_anonymous_visitor_cannot_access_admin_api(self):
        response = self.http.get("/admin-panel/api/clients/")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(response.json()["success"])

    def test_authenticated_standard_user_cannot_load_admin_ui(self):
        user = User.objects.create_user(
            email="standard-route-user@example.com",
            name="Standard Route User",
            mobile="+15550200",
            password="correct-password",
        )
        self.http.force_login(user)
        response = self.http.get("/administrator")
        self.assertEqual(response.status_code, 403)
