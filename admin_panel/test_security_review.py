import io

from django.test import TestCase

from accounts.models import Client, User
from admin_panel.security_review_service import (
    build_client_security_review,
    validate_signed_security_review,
)


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
    for x in (105, 375):
        path = canvas.beginPath()
        path.moveTo(x, 345)
        path.curveTo(x + 25, 375, x + 45, 325, x + 80, 361)
        path.curveTo(x + 100, 375, x + 120, 335, x + 145, 357)
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


class PersonalizedSecurityReviewTestCase(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            email="security-review-admin@example.com",
            name="Security Review Admin",
            mobile="5550199500",
            password="test-password",
        )
        self.client_record = Client.objects.create(
            name="Acme Health Services, LLC",
            client_code="ACME-SECURITY",
            email="security@example.com",
            address="123 Main Street, Suite 400, Columbus",
            state="OH",
            zip_code="43215",
        )

    def test_step_three_download_contains_client_identity_date_and_signature_label(self):
        self.client.force_login(self.admin)
        response = self.client.get(
            f"/admin-panel/api/download/{self.client_record.id}/step_3_security_review_returned/"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")

        try:
            from pypdf import PdfReader
        except ImportError:
            from PyPDF2 import PdfReader
        reader = PdfReader(io.BytesIO(response.content))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        self.assertEqual(len(reader.pages), 3)
        self.assertIn(self.client_record.name, text)
        self.assertIn("123 Main Street, Suite 400, Columbus, OH 43215", text)
        self.assertGreaterEqual(text.count(self.client_record.name), 2)
        self.assertRegex(text, r"\w+\s+\d{1,2},\s+\d{4}")

    def test_blank_personalized_security_review_is_accepted_while_signature_validation_is_disabled(self):
        ok, checks = validate_signed_security_review(
            build_client_security_review(self.client_record), self.client_record
        )
        self.assertTrue(ok, checks)
        self.assertFalse(any("signature" in check["label"].lower() for check in checks))

    def test_security_review_with_both_signatures_is_accepted(self):
        signed_pdf = add_mock_signatures(build_client_security_review(self.client_record))
        ok, checks = validate_signed_security_review(signed_pdf, self.client_record)
        self.assertTrue(ok, checks)

    def test_download_requires_client_address(self):
        self.client_record.address = ""
        self.client_record.save(update_fields=["address"])
        self.client.force_login(self.admin)
        response = self.client.get(
            f"/admin-panel/api/download/{self.client_record.id}/step_3_security_review_returned/"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("address", response.json()["error"].lower())
