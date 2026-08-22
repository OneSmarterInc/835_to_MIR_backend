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
        res = self.client_api.post(f"/accounts/api/admin/clients/{self.c1.id}/delete/")
        self.assertEqual(res.status_code, 200)
        self.assertFalse(Client.objects.filter(id=self.c1.id).exists())
