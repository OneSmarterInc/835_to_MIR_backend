import io
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from django.conf import settings

from admin_panel.nda_service import (
    _fit_text,
    _normalized,
    _pdf_classes,
    _signed_digital_fields,
    client_nda_address,
)


SECURITY_REVIEW_TEMPLATE_FILENAME = "OneSmarter_SecurityReview_Template.pdf"
SIGNATURE_REGIONS = {
    "OneSmarter": (86, 414, 335, 468),
    "Client": (356, 414, 575, 468),
}


def _template_bytes():
    path = Path(settings.BASE_DIR) / "sample_docs" / SECURITY_REVIEW_TEMPLATE_FILENAME
    return path.read_bytes()


def build_client_security_review(client, return_date=None):
    """Overlay client-specific fields on the approved three-page report."""
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen.canvas import Canvas

    if not (client.name or "").strip():
        raise ValueError("Client legal name is required before downloading the Security Review.")
    full_address = client_nda_address(client)
    if not full_address:
        raise ValueError("Client address is required before downloading the Security Review.")

    report_date = return_date or datetime.now(ZoneInfo("America/New_York")).date()
    date_label = report_date.strftime("%B %d, %Y").replace(" 0", " ")
    overlay_buffer = io.BytesIO()
    canvas = Canvas(overlay_buffer, pagesize=letter)

    # Page 1: return date, client legal name, and full stored address.
    canvas.setFillColorRGB(1, 1, 1)
    canvas.rect(75, 588, 151, 17, fill=1, stroke=0)
    canvas.rect(75, 557, 237, 17, fill=1, stroke=0)
    canvas.rect(75, 542, 235, 17, fill=1, stroke=0)
    _fit_text(canvas, date_label, 78, 593, 144, preferred_size=9)
    _fit_text(canvas, client.name, 78, 562, 228, preferred_size=9)
    _fit_text(canvas, full_address, 78, 547, 225, preferred_size=8)
    canvas.showPage()

    # Page 2 has no personalized fields.
    canvas.showPage()

    # Page 3: return date and client name beneath its acknowledgment line.
    canvas.setFillColorRGB(1, 1, 1)
    canvas.rect(163, 440, 190, 18, fill=1, stroke=0)
    canvas.rect(359, 326, 225, 18, fill=1, stroke=0)
    _fit_text(canvas, date_label, 166, 445, 181, preferred_size=9)
    _fit_text(canvas, client.name, 362, 331, 215, preferred_size=9)
    canvas.save()
    overlay_buffer.seek(0)

    PdfReader, PdfWriter = _pdf_classes()
    source = PdfReader(io.BytesIO(_template_bytes()))
    overlay = PdfReader(overlay_buffer)
    writer = PdfWriter()
    for page_number, page in enumerate(source.pages):
        page.merge_page(overlay.pages[page_number])
        writer.add_page(page)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def security_review_download_filename(client):
    safe_name = re.sub(r"[^A-Za-z0-9]+", "_", client.name or "Client").strip("_")
    return f"OneSmarter_Security_Review_{safe_name}.pdf"


def _signature_region_has_ink(uploaded_pdf, reference_pdf, region):
    import fitz
    from PIL import Image

    def crop(pdf_bytes):
        document = fitz.open(stream=pdf_bytes, filetype="pdf")
        if len(document) < 3:
            raise ValueError("The signed Security Review must contain all three pages.")
        pixmap = document[2].get_pixmap(
            matrix=fitz.Matrix(2, 2),
            clip=fitz.Rect(*region),
            colorspace=fitz.csGRAY,
            alpha=False,
        )
        return Image.frombytes("L", (pixmap.width, pixmap.height), pixmap.samples)

    uploaded = crop(uploaded_pdf)
    reference = crop(reference_pdf)
    if uploaded.size != reference.size:
        return False
    uploaded_ink = sum(1 for pixel in uploaded.getdata() if pixel < 150)
    reference_ink = sum(1 for pixel in reference.getdata() if pixel < 150)
    return uploaded_ink >= reference_ink + max(120, int(reference_ink * 0.06))


def validate_signed_security_review(pdf_bytes, client):
    """Require approved report terms, client identity, date, and both signatures."""
    PdfReader, _ = _pdf_classes()
    checks = []
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        extracted = "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception:
        return False, [{
            "ok": False,
            "label": "Security Review PDF integrity",
            "detail": "The uploaded Security Review is not a readable PDF document.",
        }]

    normalized_text = _normalized(extracted)
    required_report_text = (
        "SECURITY REVIEW REPORT",
        "Purpose and Scope",
        "Risk Classification",
        "Return and Acknowledgment",
    )
    approved_content = all(_normalized(phrase) in normalized_text for phrase in required_report_text)
    checks.append({
        "ok": approved_content,
        "label": "Approved Security Review content",
        "detail": "Required Security Review terms are present." if approved_content else "The uploaded file is not the approved OneSmarter Security Review template.",
    })

    for label, value in (
        ("Client legal name", client.name),
        ("Client address", client_nda_address(client)),
    ):
        present = bool(value and _normalized(value) in normalized_text)
        checks.append({
            "ok": present,
            "label": label,
            "detail": f"{label} is present in the report." if present else f"The Security Review must contain the selected client's {label.lower()}.",
        })

    date_present = bool(re.search(
        r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}\b|\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b",
        extracted,
        re.IGNORECASE,
    ))
    checks.append({
        "ok": date_present,
        "label": "Return date",
        "detail": "Return date is present." if date_present else "The Security Review must contain a completed return date.",
    })

    digital_signatures = _signed_digital_fields(pdf_bytes)
    reference = build_client_security_review(client)
    for party, region in SIGNATURE_REGIONS.items():
        try:
            signed = digital_signatures >= 2 or _signature_region_has_ink(pdf_bytes, reference, region)
        except Exception:
            signed = False
        checks.append({
            "ok": signed,
            "label": f"{party} signature",
            "detail": f"{party} signature is present." if signed else f"The {party} signature area is blank.",
        })

    return all(check["ok"] for check in checks), checks
