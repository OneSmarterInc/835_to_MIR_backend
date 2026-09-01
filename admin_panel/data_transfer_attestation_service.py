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


DATA_TRANSFER_TEMPLATE_FILENAME = "OneSmarter_ProductionBaseline_Template.pdf"
SIGNATURE_REGIONS = {
    "OneSmarter": (86, 388, 335, 443),
    "Client": (356, 388, 575, 443),
}


def _template_bytes():
    path = Path(settings.BASE_DIR) / "sample_docs" / DATA_TRANSFER_TEMPLATE_FILENAME
    return path.read_bytes()


def build_client_data_transfer_attestation(client, attestation_date=None):
    """Overlay client-specific fields on the approved three-page attestation."""
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen.canvas import Canvas

    if not (client.name or "").strip():
        raise ValueError("Client legal name is required before downloading the Data Transfer Attestation.")
    full_address = client_nda_address(client)
    if not full_address:
        raise ValueError("Client address is required before downloading the Data Transfer Attestation.")

    signed_date = attestation_date or datetime.now(ZoneInfo("America/New_York")).date()
    date_label = signed_date.strftime("%B %d, %Y").replace(" 0", " ")
    overlay_buffer = io.BytesIO()
    canvas = Canvas(overlay_buffer, pagesize=letter)

    # Page 1: attestation date, client legal name, and full stored address.
    canvas.setFillColorRGB(1, 1, 1)
    canvas.rect(75, 568, 151, 17, fill=1, stroke=0)
    canvas.rect(75, 537, 237, 17, fill=1, stroke=0)
    canvas.rect(75, 522, 235, 17, fill=1, stroke=0)
    _fit_text(canvas, date_label, 78, 573, 144, align="center")
    _fit_text(canvas, client.name, 78, 542, 228, font_name="Helvetica-Bold", align="center")
    _fit_text(canvas, full_address, 78, 527, 225, align="center")
    canvas.showPage()

    # Page 2 has no personalized fields.
    canvas.showPage()

    # Page 3: attestation date and client name beneath its countersignature line.
    canvas.setFillColorRGB(1, 1, 1)
    canvas.rect(171, 466, 190, 18, fill=1, stroke=0)
    canvas.rect(359, 348, 225, 18, fill=1, stroke=0)
    _fit_text(canvas, date_label, 174, 471, 181, align="center")
    _fit_text(canvas, client.name, 362, 353, 215, font_name="Helvetica-Bold", align="center")
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


def data_transfer_attestation_download_filename(client):
    safe_name = re.sub(r"[^A-Za-z0-9]+", "_", client.name or "Client").strip("_")
    return f"OneSmarter_Data_Transfer_Attestation_{safe_name}.pdf"


def _signature_region_has_ink(uploaded_pdf, reference_pdf, region):
    import fitz
    from PIL import Image

    def crop(pdf_bytes):
        document = fitz.open(stream=pdf_bytes, filetype="pdf")
        if len(document) < 3:
            raise ValueError("The signed Data Transfer Attestation must contain all three pages.")
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


def validate_signed_data_transfer_attestation(pdf_bytes, client):
    """Require approved attestation terms, client identity, date, and signatures."""
    PdfReader, _ = _pdf_classes()
    checks = []
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        extracted = "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception:
        return False, [{
            "ok": False,
            "label": "Data Transfer Attestation PDF integrity",
            "detail": "The uploaded Data Transfer Attestation is not a readable PDF document.",
        }]

    normalized_text = _normalized(extracted)
    required_attestation_text = (
        "PRODUCTION DATA TRANSFER SECURITY ATTESTATION",
        "Identification of the Transfer",
        "Protection in Transit",
        "Attestation and Reliance",
    )
    approved_content = all(_normalized(phrase) in normalized_text for phrase in required_attestation_text)
    checks.append({
        "ok": approved_content,
        "label": "Approved Data Transfer Attestation content",
        "detail": "Required Data Transfer Attestation terms are present." if approved_content else "The uploaded file is not the approved OneSmarter Data Transfer Attestation template.",
    })

    for label, value in (
        ("Client legal name", client.name),
        ("Client address", client_nda_address(client)),
    ):
        present = bool(value and _normalized(value) in normalized_text)
        checks.append({
            "ok": present,
            "label": label,
            "detail": f"{label} is present in the attestation." if present else f"The Data Transfer Attestation must contain the selected client's {label.lower()}.",
        })

    date_present = bool(re.search(
        r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}\b|\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b",
        extracted,
        re.IGNORECASE,
    ))
    checks.append({
        "ok": date_present,
        "label": "Attestation date",
        "detail": "Attestation date is present." if date_present else "The Data Transfer Attestation must contain a completed attestation date.",
    })

    if _signature_validation_enabled():
        digital_signatures = _signed_digital_fields(pdf_bytes)
        reference = build_client_data_transfer_attestation(client)
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
