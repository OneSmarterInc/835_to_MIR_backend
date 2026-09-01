import io
from pathlib import Path

from django.test import TestCase

from accounts.models import Client, User
from admin_panel.nda_service import build_client_nda, validate_signed_nda


def add_mock_signatures(pdf_bytes):
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:
        from PyPDF2 import PdfReader, PdfWriter
    from reportlab.pdfgen.canvas import Canvas

    overlay_buffer = io.BytesIO()
    canvas = Canvas(overlay_buffer, pagesize=(612, 792))
    canvas.showPage()
    canvas.showPage()
    canvas.setLineWidth(2)
    for x in (95, 350):
        path = canvas.beginPath()
        path.moveTo(x, 580)
        path.curveTo(x + 25, 610, x + 45, 558, x + 80, 594)
        path.curveTo(x + 100, 610, x + 120, 570, x + 145, 592)
        canvas.drawPath(path)
    canvas.save()
    overlay_buffer.seek(0)

    source = PdfReader(io.BytesIO(pdf_bytes))
    overlay = PdfReader(overlay_buffer)
    writer = PdfWriter()
    for page_number, page in enumerate(source.pages):
        page.merge_page(overlay.pages[page_number])
        writer.add_page(page)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class PersonalizedNdaTestCase(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            email="nda-admin@example.com",
            name="NDA Admin",
            mobile="5550199300",
            password="test-password",
        )
        self.client_record = Client.objects.create(
            name="Acme Health Services, LLC",
            client_code="ACME-NDA",
            email="nda@example.com",
            address="123 Main Street, Suite 400, Columbus",
            state="OH",
            zip_code="43215",
        )

    def test_step_one_download_contains_client_identity_date_and_signature_label(self):
        self.client.force_login(self.admin)
        response = self.client.get(
            f"/admin-panel/api/download/{self.client_record.id}/step_1_mutual_nda_signed/"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")

        try:
            from pypdf import PdfReader
        except ImportError:
            from PyPDF2 import PdfReader
        reader = PdfReader(io.BytesIO(response.content))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        self.assertGreaterEqual(len(reader.pages), 3)
        self.assertIn(self.client_record.name, text)
        self.assertIn(f"{self.client_record.address}, {self.client_record.state} {self.client_record.zip_code}", text)
        self.assertGreaterEqual(text.count(self.client_record.name), 2)
        self.assertRegex(text, r"September\s+1,\s+2026|\w+\s+\d{1,2},\s+\d{4}")

    def test_blank_personalized_nda_is_rejected_for_both_missing_signatures(self):
        ok, checks = validate_signed_nda(build_client_nda(self.client_record), self.client_record)
        self.assertFalse(ok)
        failed_labels = {check["label"] for check in checks if not check["ok"]}
        self.assertEqual(failed_labels, {"OneSmarter signature", "Client signature"})

    def test_unpersonalized_template_fails_identity_address_date_and_signatures(self):
        from django.conf import settings
        template = (Path(settings.BASE_DIR) / "sample_docs" / "OneSmarter_MutualNDA_Template.pdf").read_bytes()
        ok, checks = validate_signed_nda(template, self.client_record)
        self.assertFalse(ok)
        failed_labels = {check["label"] for check in checks if not check["ok"]}
        self.assertTrue({
            "Client legal name",
            "Client address",
            "Agreement date",
            "OneSmarter signature",
            "Client signature",
        }.issubset(failed_labels))

    def test_nda_with_both_signatures_is_accepted(self):
        signed_pdf = add_mock_signatures(build_client_nda(self.client_record))
        ok, checks = validate_signed_nda(signed_pdf, self.client_record)
        self.assertTrue(ok, checks)

    def test_download_requires_client_address(self):
        self.client_record.address = ""
        self.client_record.save(update_fields=["address"])
        self.client.force_login(self.admin)
        response = self.client.get(
            f"/admin-panel/api/download/{self.client_record.id}/step_1_mutual_nda_signed/"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("address", response.json()["error"].lower())
