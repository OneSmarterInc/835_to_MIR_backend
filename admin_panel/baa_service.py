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
    _signature_validation_enabled,
    _signed_digital_fields,
    client_nda_address,
)


BAA_TEMPLATE_FILENAME = "OneSmarter_BAA_Template.pdf"
SIGNATURE_REGIONS = {
    "OneSmarter": (86, 266, 335, 310),
    "Client": (356, 266, 575, 310),
}


def _template_bytes():
    path = Path(settings.BASE_DIR) / "sample_docs" / BAA_TEMPLATE_FILENAME
    return path.read_bytes()


def build_client_baa(client, effective_date=None):
    """Overlay client-specific fields on the approved four-page BAA."""
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen.canvas import Canvas

    if not (client.name or "").strip():
        raise ValueError("Client legal name is required before downloading the BAA.")
    full_address = client_nda_address(client)
    if not full_address:
        raise ValueError("Client address is required before downloading the BAA.")

    agreement_date = effective_date or datetime.now(ZoneInfo("America/New_York")).date()
    date_label = agreement_date.strftime("%B %d, %Y").replace(" 0", " ")
    overlay_buffer = io.BytesIO()
    canvas = Canvas(overlay_buffer, pagesize=letter)

    # Page 1: effective date, Covered Entity name, and full address.
    canvas.setFillColorRGB(1, 1, 1)
    canvas.rect(75, 588, 151, 17, fill=1, stroke=0)
    canvas.rect(75, 557, 237, 17, fill=1, stroke=0)
    canvas.rect(75, 542, 235, 17, fill=1, stroke=0)
    _fit_text(canvas, date_label, 78, 593, 144, align="center")
    _fit_text(canvas, client.name, 78, 562, 228, font_name="Helvetica-Bold", align="center")
    _fit_text(canvas, full_address, 78, 547, 225, align="center")
    canvas.showPage()

    # Pages 2 and 3 have no personalized fields.
    canvas.showPage()
    canvas.showPage()

    # Page 4: agreement date and Covered Entity name under its signature line.
    canvas.setFillColorRGB(1, 1, 1)
    canvas.rect(181, 586, 350, 18, fill=1, stroke=0)
    canvas.rect(359, 466, 225, 18, fill=1, stroke=0)
    _fit_text(canvas, date_label, 185, 591, 335, align="center")
    _fit_text(canvas, client.name, 362, 471, 215, font_name="Helvetica-Bold", align="center")
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


def baa_download_filename(client):
    safe_name = re.sub(r"[^A-Za-z0-9]+", "_", client.name or "Client").strip("_")
    return f"OneSmarter_BAA_{safe_name}.pdf"


def _signature_region_has_ink(uploaded_pdf, reference_pdf, region):
    import fitz
    from PIL import Image

    def crop(pdf_bytes):
        document = fitz.open(stream=pdf_bytes, filetype="pdf")
        if len(document) < 4:
            raise ValueError("The signed BAA must contain all four pages.")
        pixmap = document[3].get_pixmap(
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


def validate_signed_baa(pdf_bytes, client):
    """Require the approved terms, complete client identity, date, and signatures."""
    PdfReader, _ = _pdf_classes()
    checks = []
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        extracted = "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception:
        return False, [{
            "ok": False,
            "label": "BAA PDF integrity",
            "detail": "The uploaded BAA is not a readable PDF document.",
        }]

    normalized_text = _normalized(extracted)
    required_legal_text = (
        "BUSINESS ASSOCIATE AGREEMENT",
        "Obligations of the Business Associate",
        "Breach Notification",
        "Governing Law",
    )
    legal_text_present = all(_normalized(phrase) in normalized_text for phrase in required_legal_text)
    checks.append({
        "ok": legal_text_present,
        "label": "Approved BAA content",
        "detail": "Required HIPAA BAA terms are present." if legal_text_present else "The uploaded file is not the approved OneSmarter BAA template.",
    })

    for label, value in (
        ("Client legal name", client.name),
        ("Client address", client_nda_address(client)),
    ):
        present = bool(value and _normalized(value) in normalized_text)
        checks.append({
            "ok": present,
            "label": label,
            "detail": f"{label} is present in the agreement." if present else f"The BAA must contain the selected client's {label.lower()}.",
        })

    date_present = bool(re.search(
        r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}\b|\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b",
        extracted,
        re.IGNORECASE,
    ))
    checks.append({
        "ok": date_present,
        "label": "Agreement date",
        "detail": "Agreement date is present." if date_present else "The BAA must contain a completed agreement date.",
    })

    if _signature_validation_enabled():
        digital_signatures = _signed_digital_fields(pdf_bytes)
        reference = build_client_baa(client)
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
