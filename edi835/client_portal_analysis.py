"""Isolated Analysis staging; never reads or writes production claim tables."""
from decimal import Decimal
import hashlib
import logging
import re

from django.db import DatabaseError, transaction
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods

from project835.decorators import authenticated_api_required
from .claim_numbers import split_claim_number
from .client_portal_views import only_client_user
from .models import AnalysisClaim, AnalysisUpload

logger = logging.getLogger(__name__)

MAX_BYTES = 10 * 1024 * 1024
MAX_CLAIMS = 20000
MAX_SEGMENTS = 200000


def count_agrees(value: str, expected: int) -> bool:
    return bool(re.fullmatch(r"\d{1,9}", value)) and int(value) == expected


def claim_amount(value: str, element: str, sequence: int, *, optional: bool = False) -> Decimal | None:
    value = value.strip()
    if optional and not value:
        return None
    if not re.fullmatch(r"-?\d{1,16}(?:\.\d{1,2})?", value):
        raise ValueError(f"Claim occurrence {sequence} has an invalid {element} amount.")
    return Decimal(value)


def parse_analysis(text: str, kind: str) -> list[dict]:
    """Validate envelopes and extract identifiers and claim-level comparison amounts.

    This is structural validation, not a payer-specific implementation-guide check.
    Only comparison fields are returned; original bytes are retained in staging.
    """
    text = text.strip()
    if kind not in {"837", "835"} or not text.startswith("ISA") or len(text) < 106:
        raise ValueError("Choose a complete X12 837 or 835 file with an ISA envelope.")
    element, terminator = text[3], text[105]
    if element.isalnum() or terminator.isalnum() or element == terminator:
        raise ValueError("The ISA delimiters are invalid.")
    isa_positions = (3, 6, 17, 20, 31, 34, 50, 53, 69, 76, 81, 83, 89, 99, 101, 103)
    if any(text[index] != element for index in isa_positions) or not text.endswith(terminator):
        raise ValueError("The ISA header or final segment terminator is incomplete.")
    claims: list[dict] = []
    interchange = group = transaction = None
    group_count = transaction_count = segment_count = 0
    complete = False
    current = None
    service = False
    start = 0
    seen_transactions: set[str] = set()
    seen_groups: set[str] = set()
    for index in range(MAX_SEGMENTS + 1):
        end = text.find(terminator, start)
        if end < 0:
            break
        fields = text[start:end].strip().split(element)
        start = end + 1
        tag = fields[0]
        if not tag or len(tag) > 3 or complete:
            raise ValueError("Unexpected content outside the X12 envelope.")
        if transaction:
            segment_count += 1
        if tag == "ISA":
            if interchange or index != 0 or len(fields) != 17:
                raise ValueError("Upload one complete ISA/IEA interchange per file.")
            interchange = fields[13]
        elif tag == "GS":
            if not interchange or group or transaction or len(fields) < 9 or fields[1] != ("HC" if kind == "837" else "HP"):
                raise ValueError(f"The file must contain only {kind} functional groups.")
            group = fields[6]
            if not group or group in seen_groups:
                raise ValueError("GS control numbers must be present and unique.")
            seen_groups.add(group)
            group_count += 1
            transaction_count = 0
            seen_transactions.clear()
        elif tag == "ST":
            if not group or transaction or len(fields) < 3 or fields[1] != kind:
                raise ValueError(f"Expected an ST{element}{kind} transaction.")
            transaction = fields[2]
            if not transaction or transaction in seen_transactions:
                raise ValueError("ST control numbers must be present and unique in each group.")
            seen_transactions.add(transaction)
            transaction_count += 1
            segment_count = 1
            current = None
            service = False
        elif tag == "SE":
            if (not transaction or len(fields) != 3 or fields[2] != transaction
                    or not count_agrees(fields[1], segment_count)):
                raise ValueError("ST/SE control numbers or segment counts do not agree.")
            transaction = None
            current = None
        elif tag == "GE":
            if (not group or transaction or len(fields) != 3 or fields[2] != group
                    or not count_agrees(fields[1], transaction_count) or not transaction_count):
                raise ValueError("GS/GE control numbers or transaction counts do not agree.")
            group = None
        elif tag == "IEA":
            if (not interchange or group or transaction or len(fields) != 3
                    or fields[2] != interchange or not count_agrees(fields[1], group_count) or not group_count):
                raise ValueError("ISA/IEA control numbers or group counts do not agree.")
            complete = True
        elif not transaction:
            raise ValueError("Claim content must be enclosed by ST and SE segments.")
        elif tag == ("CLM" if kind == "837" else "CLP"):
            amount_index = 2 if kind == "837" else 3
            if len(fields) <= amount_index or not fields[1].strip():
                raise ValueError("A claim is missing its claim number or billed amount.")
            sequence = len(claims) + 1
            billed = claim_amount(fields[amount_index], "CLM02" if kind == "837" else "CLP03", sequence)
            control = fields[1].strip().upper()
            split = split_claim_number(control) if kind == "837" else {}
            internal = (split.get("internal_claim_number", "") if kind == "837"
                        else fields[7].strip().upper() if len(fields) > 7 else "")
            current = {
                "claim": split.get("highmark_claim_number") or control,
                "internal_number": internal.removeprefix("ZZ:"),
                "billed": format(billed, ".2f"),
                "sequence": sequence,
            }
            if kind == "835":
                payment = claim_amount(fields[4] if len(fields) > 4 else "", "CLP04", sequence)
                responsibility = claim_amount(fields[5] if len(fields) > 5 else "", "CLP05", sequence, optional=True)
                current.update(
                    payment=format(payment, ".2f"),
                    patient_responsibility=format(responsibility, ".2f") if responsibility is not None else None,
                    allowed=format(payment + responsibility, ".2f") if responsibility is not None else None,
                )
            claims.append(current)
            service = False
            if len(claims) > MAX_CLAIMS:
                raise ValueError(f"A file may contain at most {MAX_CLAIMS:,} claim occurrences.")
        elif kind == "837":
            if tag == "HL":
                current = None
            elif tag == "LX":
                service = True
            elif current and not service and tag == "REF" and len(fields) > 2 and fields[1] == "9C":
                current["internal_number"] = fields[2].strip().upper().removeprefix("ZZ:")
    else:
        raise ValueError("The file contains too many segments.")
    if not complete or not claims:
        raise ValueError("The file is incomplete or contains no claim records.")
    if any(len(row[key]) > 255 for row in claims for key in ("claim", "internal_number")):
        raise ValueError("Claim identifiers may contain at most 255 characters.")
    return claims


def upload_payload(upload: AnalysisUpload) -> dict:
    claims = []
    for claim in upload.claims.all():
        row = {"claim": claim.claim_number, "internal_number": claim.internal_number,
               "billed": format(claim.billed, ".2f"), "sequence": claim.sequence}
        if upload.kind == "835":
            allowed = (claim.payment + claim.patient_responsibility
                       if claim.payment is not None and claim.patient_responsibility is not None else None)
            row.update(payment=format(claim.payment, ".2f") if claim.payment is not None else None,
                       patient_responsibility=format(claim.patient_responsibility, ".2f") if claim.patient_responsibility is not None else None,
                       allowed=format(allowed, ".2f") if allowed is not None else None)
        claims.append(row)
    return {"id": str(upload.pk), "filename": upload.filename, "kind": upload.kind,
            "uploaded_at": upload.uploaded_at.isoformat(), "claims": claims}


def save_upload(client, user, upload, kind: str, raw: bytes, claims: list[dict]):
    # A content identity makes retries safe, including a lost success response.
    # Both the source and all parsed claims commit together, or neither does.
    with transaction.atomic():
        record, created = AnalysisUpload.objects.get_or_create(
            client=client, kind=kind, filename=upload.name.replace("\\", "/").rsplit("/", 1)[-1],
            content_sha256=hashlib.sha256(raw).hexdigest(),
            defaults={"uploaded_by": user, "raw_content": raw},
        )
        if created:
            AnalysisClaim.objects.bulk_create([
                AnalysisClaim(upload=record, sequence=row["sequence"], claim_number=row["claim"],
                              internal_number=row["internal_number"], billed=row["billed"],
                              payment=row.get("payment"), patient_responsibility=row.get("patient_responsibility"))
                for row in claims
            ], batch_size=500)
    return record, created


@authenticated_api_required
@require_http_methods(["GET", "POST"])
@csrf_protect
def analysis_upload(request):
    client, denied = only_client_user(request)
    if denied:
        return denied
    if request.method == "GET":
        payload = {"success": True, "csrf_token": get_token(request)}
        if request.GET.get("csrf_only") != "1":
            cursor = request.GET.get("after", "0")
            if not re.fullmatch(r"\d{1,18}", cursor):
                return JsonResponse({"success": False, "error": "Invalid upload cursor."}, status=400)
            # One bounded file per response; raw bytes never leave the database.
            uploads = list(AnalysisUpload.objects.filter(client=client, pk__gt=int(cursor))
                           .defer("raw_content").order_by("pk")[:2])
            payload.update(files=[upload_payload(uploads[0])] if uploads else [],
                           next_cursor=str(uploads[0].pk) if len(uploads) > 1 else None)
        response = JsonResponse(payload)
    else:
        upload = request.FILES.get("file")
        kind = request.POST.get("kind")
        if kind not in {"837", "835"} or not upload or len(request.FILES.getlist("file")) != 1 or len(request.FILES) != 1:
            response = JsonResponse({"success": False, "error": "Choose one 837 or 835 file."}, status=400)
        elif upload.size > MAX_BYTES:
            response = JsonResponse({"success": False, "error": "Maximum file size is 10 MB."}, status=413)
        else:
            try:
                raw = upload.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise ValueError("Maximum file size is 10 MB.")
                claims = parse_analysis(raw.decode("utf-8-sig"), kind)
            except UnicodeDecodeError:
                response = JsonResponse({"success": False, "error": "The file must contain UTF-8 X12 text."}, status=400)
            except ValueError as exc:
                response = JsonResponse({"success": False, "error": str(exc)}, status=400)
            else:
                try:
                    record, created = save_upload(client, request.user, upload, kind, raw, claims)
                    response = JsonResponse({"success": True, "saved": True,
                                             "reused": not created, "file": upload_payload(record)})
                except DatabaseError:
                    # Do not log uploaded data or database exception parameters.
                    logger.error("Analysis staging save failed for client_id=%s", client.pk)
                    response = JsonResponse({"success": False, "error": "The file could not be saved. Keep it selected and retry."}, status=503)
    response["Cache-Control"] = "private, no-store, max-age=0"
    response["X-Content-Type-Options"] = "nosniff"
    return response
