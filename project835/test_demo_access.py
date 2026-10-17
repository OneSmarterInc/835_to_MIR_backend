"""Demo permissions exercised through sessions, middleware and the real views."""
from datetime import timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client as HttpClient, TestCase, override_settings
from django.urls import path
from django.utils import timezone

from accounts import views as account_views
from accounts.models import Client, User
from admin_panel import views as admin_views
from admin_panel.models import AdminClientAccessGrant
from edi835.client_portal_analysis import analysis_upload
from edi835.models import AnalysisUpload
from edi835.test_client_portal_analysis import envelope
from project835.drf_compat import admin_api, public_api

ANALYSIS = "/edi835/api/client-portal/analysis/"
urlpatterns = [path(ANALYSIS.lstrip("/"), analysis_upload),
               path("accounts/api/user/", public_api(account_views.api_user_info)),
               path("admin-panel/api/access/", admin_api(admin_views.api_admin_access_info))]
for prefix, module in (("admin-panel", admin_views), ("accounts/api/admin", account_views)):
    base = "admin-panel/api/users/" if prefix == "admin-panel" else prefix + "/users/"
    urlpatterns += [
        path(base, admin_api(module.api_admin_users)),
        path(base + "create/", admin_api(module.api_admin_create_user)),
        path(base + "<int:user_id>/update/", admin_api(module.api_admin_update_user)),
    ]


@override_settings(ROOT_URLCONF=__name__, MFA_ENFORCEMENT_ENABLED=False)
class DemoAccessTests(TestCase):
    def setUp(self):
        self.a = Client.objects.create(name="Demo A", client_code="DEMO-A", email="a@example.test")
        self.b = Client.objects.create(name="Demo B", client_code="DEMO-B", email="b@example.test")
        self.demo = User.objects.create_user(email="demo@example.test", name="Demo", mobile="+14155550101",
                                             client=self.a, client_role="DEMO", password="TestPassword!123")
        self.admin = User.objects.create_user(email="admin@example.test", name="Admin", mobile="+14155550102",
                                              is_staff=True, admin_screens=["access"], password="TestPassword!123")
        self.root = User.objects.create_superuser(email="root@example.test", name="Root", mobile="+14155550103",
                                                  password="TestPassword!123")
        self.client.force_login(self.demo)

    def upload(self, kind="837", segments=None, **extra):
        return self.client.post(ANALYSIS + "?compact=1", {
            "kind": kind, "file": SimpleUploadedFile("source." + kind, envelope(kind, segments).encode()), **extra,
        })

    def test_all_analysis_operations_and_shared_client_data(self):
        # Data already uploaded by another user of the assigned client is visible.
        viewer = User.objects.create_user(email="viewer@example.test", name="Viewer", mobile="+14155550104",
                                          client=self.a, client_role="CLIENT_USER")
        self.client.force_login(viewer)
        self.assertEqual(self.upload(segments=["CLM*EXISTING*100"]).status_code, 200)
        self.client.force_login(self.demo)
        self.assertEqual(self.client.get(ANALYSIS, {"csrf_only": "1"}).status_code, 200)
        uploaded = self.upload("835", ["CLP*EXISTING*1*100*80*20*ZZ*INTERNAL", "CLP*INVALID*1*NaN*0*0"])
        self.assertEqual(uploaded.status_code, 200, uploaded.content)
        report = uploaded.json()["file"]
        self.assertEqual((report["accepted_count"], report["rejected_count"]), (1, 1))
        response = self.client.get(ANALYSIS, {"view": "comparison", "category": "matched"})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual([row["claim"] for row in response.json()["rows"]], ["EXISTING"])
        self.assertEqual(self.client.get(ANALYSIS, {"view": "claim", "claim": "EXISTING"}).status_code, 200)
        for view in ("export", "rejection-export", "rejections"):
            result = self.client.get(ANALYSIS, {"view": view, "category": "matched", "upload": report["id"]})
            self.assertEqual(result.status_code, 200)
            if result.streaming:
                self.assertTrue(b"".join(result.streaming_content))
        self.assertEqual(self.upload(segments=["CLM*NEW*25"]).status_code, 200)
        self.assertEqual(self.upload(segments=["CLM*NEW*25"]).status_code, 409)
        self.assertEqual(self.client.get(ANALYSIS, {"view": "comparison", "category": "837-only"}).json()["total"], 1)

    def test_demo_cannot_access_other_operations_or_manage_users(self):
        for target in ("/edi835/api/client-portal/overview/", "/edi835/api/client-portal/actions/",
                       "/edi835/api/client-portal/comparison-835-837/", "/edi835/api/tracked-files/",
                       "/api/validate/", "/api/convert/", "/api/start-batch-conversion/",
                       "/edi835/api/sftp/push/", "/admin-panel/api/users/create/",
                       "/accounts/api/admin/users/create/", "/accounts/api/signup/",
                       "/home/", "/admin/", "/edi835/api/new-unlisted-operation/"):
            for method in ("get", "post"):
                with self.subTest(target=target, method=method):
                    self.assertEqual(getattr(self.client, method)(target).status_code, 403)

    def test_client_reassignment_changes_reads_and_writes_on_existing_session(self):
        self.assertEqual(self.upload(segments=["CLM*OLDCLIENT*100"]).status_code, 200)
        old_upload = AnalysisUpload.objects.get(client=self.a)
        admin_session = HttpClient()
        for administrator, base in ((self.admin, "/admin-panel/api/users/"),
                                    (self.root, "/accounts/api/admin/users/")):
            with self.subTest(administrator=administrator.name):
                admin_session.force_login(administrator)
                response = admin_session.post(base + f"{self.demo.pk}/update/",
                    {"role": "Demo", "client_id": str(self.b.pk)}, content_type="application/json")
                self.assertEqual(response.status_code, 200, response.content)
                self.demo.refresh_from_db()
                self.assertEqual(self.demo.client_id, self.b.pk)
                self.assertEqual(self.demo.client_role, "DEMO")
        # The Demo browser retains its session; the next request gets the new tenant.
        self.assertEqual(self.client.get(ANALYSIS, {"view": "comparison"}).json()["total"], 0)
        self.assertEqual(self.client.get(ANALYSIS, {"view": "claim", "claim": "OLDCLIENT"}).status_code, 404)
        for view in ("rejections", "rejection-export"):
            self.assertEqual(self.client.get(ANALYSIS, {"view": view, "upload": old_upload.pk}).status_code, 404)
        exported = self.client.get(ANALYSIS, {"view": "export", "category": "837-only"})
        self.assertNotIn(b"OLDCLIENT", b"".join(exported.streaming_content))
        self.assertEqual(self.upload(segments=["CLM*NEWCLIENT*50"]).status_code, 200)
        self.assertEqual(AnalysisUpload.objects.filter(client=self.a).count(), 1)
        self.assertEqual(AnalysisUpload.objects.filter(client=self.b).count(), 1)
        self.assertEqual(self.client.get(ANALYSIS, {"view": "comparison", "client_id": str(self.a.pk)}).status_code, 403)
        self.assertEqual(self.upload(client_id=str(self.a.pk)).status_code, 403)

    def test_admin_and_super_admin_can_create_demo_in_both_user_apis(self):
        for index, (actor, base) in enumerate((
            (self.admin, "/admin-panel/api/users/"), (self.root, "/admin-panel/api/users/"),
            (self.admin, "/accounts/api/admin/users/"), (self.root, "/accounts/api/admin/users/"),
        )):
            self.client.force_login(actor)
            email = f"created{index}@example.test"
            payload = {"name": "Created demo", "email": email, "mobile": f"+1415555020{index}",
                       "password": "TestPassword!123", "role": "Demo", "client_id": str(self.a.pk)}
            result = self.client.post(base + "create/", payload, content_type="application/json")
            self.assertEqual(result.status_code, 200, result.content)
            account = User.objects.get(email=email)
            self.assertEqual(account.client_id, self.a.pk)
            self.assertEqual(account.client_role, "DEMO")
            self.assertFalse(account.is_staff or account.is_superuser)
            self.assertEqual(result.json()["user"]["role"], "Demo")
            rows = self.client.get(base).json()["users"]
            self.assertEqual(next(row for row in rows if row["id"] == account.pk)["role"], "Demo")

    def test_assignment_required_and_failed_edit_preserves_client(self):
        self.client.force_login(self.admin)
        for base in ("/admin-panel/api/users/", "/accounts/api/admin/users/"):
            for client_id in (None, "not-a-client"):
                payload = {"name": "Missing client", "email": "missing@example.test", "mobile": "+14155550300",
                           "password": "TestPassword!123", "role": "Demo", "client_id": client_id}
                self.assertEqual(self.client.post(base + "create/", payload, content_type="application/json").status_code, 400)
                self.assertEqual(self.client.post(base + f"{self.demo.pk}/update/", {"client_id": client_id},
                                                 content_type="application/json").status_code, 400)
                self.demo.refresh_from_db()
                self.assertEqual(self.demo.client_id, self.a.pk)
        self.assertFalse(User.objects.filter(email="missing@example.test").exists())

    def test_authentication_csrf_mfa_and_offboarding_still_apply(self):
        self.assertEqual(self.client.get("/accounts/api/user/").json()["user"]["role"], "Demo")
        strict = HttpClient(enforce_csrf_checks=True)
        strict.force_login(self.demo)
        self.assertEqual(strict.post(ANALYSIS, {}).status_code, 403)
        with override_settings(MFA_ENFORCEMENT_ENABLED=True):
            self.assertEqual(self.client.get(ANALYSIS).status_code, 403)
        self.a.stage = "offboarded"
        self.a.save(update_fields=["stage"])
        self.assertEqual(self.client.get(ANALYSIS).status_code, 403)

    def test_unassigned_demo_cannot_read_analysis(self):
        self.demo.client = None
        self.demo.save(update_fields=["client"])
        self.assertEqual(self.client.get(ANALYSIS).status_code, 403)

    def test_access_screen_lists_demo_and_current_client_for_editing(self):
        for tenant in (self.a, self.b):
            AdminClientAccessGrant.objects.create(administrator=self.admin, client=tenant, approved_by=self.root,
                                                 reason="Test account management", expires_at=timezone.now() + timedelta(hours=1))
        for administrator in (self.admin, self.root):
            self.client.force_login(administrator)
            rows = self.client.get("/admin-panel/api/access/").json()["staff"]
            row = next(item for item in rows if item["id"] == self.demo.pk)
            self.assertEqual(row["role"], "Demo")
            self.assertEqual(row["person"], "Demo")
            self.assertEqual(row["client_id"], str(self.a.pk))
            self.assertEqual(row["access"], "Analysis only")
        self.client.force_login(self.admin)
        self.assertEqual(self.client.post(f"/admin-panel/api/users/{self.demo.pk}/update/",
            {"client_id": str(self.b.pk)}, content_type="application/json").status_code, 200)
        row = next(item for item in self.client.get("/admin-panel/api/access/").json()["staff"] if item["id"] == self.demo.pk)
        self.assertEqual(row["role"], "Demo")
        self.assertEqual(row["client_id"], str(self.b.pk))
        self.assertEqual(row["clients"], [self.b.name])
