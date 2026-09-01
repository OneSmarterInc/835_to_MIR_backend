import io
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from django.conf import settings


NDA_TEMPLATE_FILENAME = "OneSmarter_MutualNDA_Template.pdf"
SIGNATURE_REGIONS = {
    "OneSmarter": (76, 185, 307, 232),
    "Client": (327, 185, 560, 232),
}


def _pdf_classes():
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:
        from PyPDF2 import PdfReader, PdfWriter
    return PdfReader, PdfWriter


def _template_bytes():
    path = Path(settings.BASE_DIR) / "sample_docs" / NDA_TEMPLATE_FILENAME
    return path.read_bytes()


def _fit_text(
    canvas,
    text,
    x,
    y,
    max_width,
    preferred_size=10,
    minimum_size=6.5,
    font_name="Helvetica",
    align="left",
):
    from reportlab.pdfbase.pdfmetrics import stringWidth

    value = " ".join(str(text or "").split())
    size = preferred_size
    while size > minimum_size and stringWidth(value, font_name, size) > max_width:
        size -= 0.25
    canvas.setFont(font_name, size)
    canvas.setFillColorRGB(0.05, 0.05, 0.05)
    text_width = stringWidth(value, font_name, size)
    draw_x = x + max(0, (max_width - text_width) / 2) if align == "center" else x
    canvas.drawString(draw_x, y, value)


def client_nda_address(client):
    """Use the complete stored address without duplicating state or ZIP."""
    address = " ".join(str(getattr(client, "address", "") or "").split())
    normalized_address = _normalized(address)
    extras = []
    for value in (getattr(client, "state", ""), getattr(client, "zip_code", "")):
        value = str(value or "").strip()
        if value and _normalized(value) not in normalized_address:
            extras.append(value)
    return ", ".join([part for part in (address, " ".join(extras)) if part])


def build_client_nda(client, effective_date=None):
    """Overlay client-specific agreement fields on the approved NDA artwork."""
    from reportlab.pdfgen.canvas import Canvas
    from reportlab.lib.pagesizes import letter

    if not (client.name or "").strip():
        raise ValueError("Client legal name is required before downloading the NDA.")
    full_address = client_nda_address(client)
    if not full_address:
        raise ValueError("Client address is required before downloading the NDA.")

    agreement_date = effective_date or datetime.now(ZoneInfo("America/New_York")).date()
    date_label = agreement_date.strftime("%B %d, %Y").replace(" 0", " ")
    overlay_buffer = io.BytesIO()
    canvas = Canvas(overlay_buffer, pagesize=letter)

    # Page 1: effective date, client legal name, and principal address.
    canvas.setFillColorRGB(1, 1, 1)
    canvas.rect(416, 630, 108, 16, fill=1, stroke=0)
    canvas.rect(234, 601, 194, 16, fill=1, stroke=0)
    canvas.rect(126, 587, 191, 16, fill=1, stroke=0)
    _fit_text(canvas, date_label, 418, 635, 104, align="center")
    _fit_text(canvas, client.name, 237, 606, 186, font_name="Helvetica-Bold", align="center")
    _fit_text(canvas, full_address, 129, 592, 183, align="center")
    canvas.showPage()

    # Page 2 has no client-specific fields.
    canvas.showPage()

    # Page 3: agreement date and the client name beneath its signature line.
    canvas.setFillColorRGB(1, 1, 1)
    canvas.rect(166, 636, 150, 17, fill=1, stroke=0)
    canvas.rect(327, 545, 220, 18, fill=1, stroke=0)
    _fit_text(canvas, date_label, 169, 641, 144, align="center")
    _fit_text(canvas, client.name, 330, 550, 205, font_name="Helvetica-Bold", align="center")
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


def nda_download_filename(client):
    safe_name = re.sub(r"[^A-Za-z0-9]+", "_", client.name or "Client").strip("_")
    return f"OneSmarter_Mutual_NDA_{safe_name}.pdf"


def _normalized(value):
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _signed_digital_fields(pdf_bytes):
    PdfReader, _ = _pdf_classes()
    try:
        fields = PdfReader(io.BytesIO(pdf_bytes)).get_fields() or {}
    except Exception:
        return 0
    return sum(
        1 for field in fields.values()
        if str(field.get("/FT", "")) == "/Sig" and field.get("/V")
    )


def _signature_region_has_ink(uploaded_pdf, reference_pdf, region):
    """Compare a signature box with the generated blank agreement."""
    import fitz
    from PIL import Image

    def crop(pdf_bytes):
        document = fitz.open(stream=pdf_bytes, filetype="pdf")
        if len(document) < 3:
            raise ValueError("The signed NDA must contain all three pages.")
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


def validate_signed_nda(pdf_bytes, client):
    """Require client identity, agreement date, and both visible signatures."""
    PdfReader, _ = _pdf_classes()
    checks = []
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        extracted = "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception:
        return False, [{
            "ok": False,
            "label": "NDA PDF integrity",
            "detail": "The uploaded NDA is not a readable PDF document.",
        }]

    normalized_text = _normalized(extracted)
    required_legal_text = (
        "NON-DISCLOSURE AGREEMENT",
        "Definition of Confidential Information",
        "Governing Law",
    )
    legal_text_present = all(_normalized(phrase) in normalized_text for phrase in required_legal_text)
    checks.append({
        "ok": legal_text_present,
        "label": "Approved NDA content",
        "detail": "Required NDA terms are present." if legal_text_present else "The uploaded file is not the approved OneSmarter NDA template.",
    })
    required_values = (
        ("Client legal name", client.name),
        ("Client address", client_nda_address(client)),
    )
    for label, value in required_values:
        present = bool(value and _normalized(value) in normalized_text)
        checks.append({
            "ok": present,
            "label": label,
            "detail": f"{label} is present in the agreement." if present else f"The NDA must contain the selected client's {label.lower()}.",
        })

    date_present = bool(re.search(
        r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}\b|\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b",
        extracted,
        re.IGNORECASE,
    ))
    checks.append({
        "ok": date_present,
        "label": "Agreement date",
        "detail": "Agreement date is present." if date_present else "The NDA must contain a completed agreement date.",
    })

    digital_signatures = _signed_digital_fields(pdf_bytes)
    reference = build_client_nda(client)
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
