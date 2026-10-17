"""Isolated Analysis staging; never reads or writes production claim tables."""
from decimal import Decimal
import hashlib
import json
import logging
import re

from django.core.exceptions import ValidationError
from django.db import DatabaseError, transaction
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods

from accounts.models import Client
from project835.decorators import authenticated_api_required
from .claim_numbers import split_claim_number
from .client_portal_views import only_client_user
from .models import (AnalysisClaim, AnalysisUpload, Analysis835Claim, Analysis837Claim,
                     Analysis837ServiceLine, AnalysisClaimRejection, AnalysisClaimIdentity)
from .edi835_claim_service import normalized_835_claims
from .edi837_service import parse_837, claim_837_values, service_837_values

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
    if not re.fullmatch(r"-?(?:[0-9]{1,16}(?:\.[0-9]{1,2})?|\.[0-9]{1,2})", value):
        raise ValueError(f"Claim occurrence {sequence} has an invalid {element} amount.")
    return Decimal(value)


def analysis_segments(text: str):
    """Read complete interchanges, each using its own fixed-width ISA delimiters."""
    position = 0
    needs_isa = True
    element = terminator = ""
    isa_positions = (3, 6, 17, 20, 31, 34, 50, 53, 69, 76, 81, 83, 89, 99, 101, 103)
    while position < len(text):
        while position < len(text) and text[position].isspace():
            position += 1
        if position == len(text):
            break
        if needs_isa:
            header = text[position:position + 106]
            if len(header) != 106 or not header.startswith("ISA"):
                raise ValueError("Expected a complete ISA envelope; unexpected content outside the X12 envelope.")
            element, terminator = header[3], header[105]
            if (element.isalnum() or terminator.isalnum() or element.isspace()
                    or terminator.isspace() or element == terminator):
                raise ValueError("The ISA delimiters are invalid.")
            if any(header[index] != element for index in isa_positions):
                raise ValueError("The ISA header is incomplete or malformed.")
        end = text.find(terminator, position)
        if end < 0:
            raise ValueError("The final segment terminator is missing.")
        fields = text[position:end].strip().split(element)
        position = end + 1
        needs_isa = fields[0] == "IEA"
        yield fields, element, terminator


def parse_analysis(text: str, kind: str, *, rejected: list | None = None, details: bool = False) -> list[dict]:
    """Validate envelopes and extract identifiers and claim-level comparison amounts.

    This is structural validation, not a payer-specific implementation-guide check.
    Passing rejected enables claim-level recovery; envelope failures always raise.
    Details use the shared main-intake mappings; original bytes remain in staging.
    """
    text = text.strip()
    if kind not in {"837", "835"} or not text.startswith("ISA") or len(text) < 106:
        raise ValueError("Choose a complete X12 837 or 835 file with an ISA envelope.")
    claims: list[dict] = []
    interchange = group = transaction = None
    group_count = transaction_count = segment_count = 0
    complete = False
    current = None
    service = False
    inferred_responsibility: dict[int, Decimal] = {}
    failures = {}
    sequence = 0
    transaction_rows = []
    transaction_raw = []
    isa_raw = gs_raw = ""

    def reject(row, message):
        failures.setdefault(row["sequence"], {"sequence": row["sequence"],
            "claim": row.get("claim", "")[:255], "reason": message[:500]})
    seen_transactions: set[str] = set()
    seen_groups: set[str] = set()
    for index, (fields, element, terminator) in enumerate(analysis_segments(text)):
        if index >= MAX_SEGMENTS:
            raise ValueError("The file contains too many segments.")
        tag = fields[0]
        if not tag or len(tag) > 3 or (complete and tag != "ISA"):
            raise ValueError("Unexpected content outside the X12 envelope.")
        if transaction:
            segment_count += 1
            transaction_raw.append(element.join(fields))
        if tag == "ISA":
            if interchange or len(fields) != 17 or not fields[13]:
                raise ValueError("Each ISA must start a complete ISA/IEA interchange.")
            interchange = fields[13]
            complete = False
            group_count = 0
            seen_groups.clear()
            isa_raw = element.join(fields)
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
            gs_raw = element.join(fields)
        elif tag == "ST":
            if not group or transaction or len(fields) < 3 or fields[1] != kind:
                raise ValueError(f"Expected an ST transaction of type {kind}.")
            transaction = fields[2]
            if not transaction or transaction in seen_transactions:
                raise ValueError("ST control numbers must be present and unique in each group.")
            seen_transactions.add(transaction)
            transaction_count += 1
            segment_count = 1
            transaction_rows = []
            transaction_raw = [isa_raw, gs_raw, element.join(fields)]
            current = None
            service = False
        elif tag == "SE":
            if (not transaction or len(fields) != 3 or fields[2] != transaction
                    or not count_agrees(fields[1], segment_count)):
                raise ValueError("ST/SE control numbers or segment counts do not agree.")
            if details:
                enrich_analysis_claims(transaction_rows, transaction_raw, kind, element, terminator, failures, reject)
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
            interchange = None
        elif not transaction:
            raise ValueError("Claim content must be enclosed by ST and SE segments.")
        elif tag == ("CLM" if kind == "837" else "CLP"):
            sequence += 1
            if sequence > MAX_CLAIMS:
                raise ValueError(f"A file may contain at most {MAX_CLAIMS:,} claim occurrences.")
            current = {"sequence": sequence, "claim": fields[1].strip().upper() if len(fields) > 1 else ""}
            claims.append(current)
            transaction_rows.append(current)
            service = False
            try:
                amount_index = 2 if kind == "837" else 3
                if len(fields) <= amount_index or not current["claim"]:
                    raise ValueError(f"Claim occurrence {sequence} is missing its claim number or billed amount.")
                billed = claim_amount(fields[amount_index], "CLM02" if kind == "837" else "CLP03", sequence)
                control = current["claim"]
                split = split_claim_number(control) if kind == "837" else {}
                internal = (split.get("internal_claim_number", "") if kind == "837"
                            else fields[7].strip().upper() if len(fields) > 7 else "")
                current.update(claim=split.get("highmark_claim_number") or control,
                               internal_number=internal.removeprefix("ZZ:"), billed=format(billed, ".2f"))
                if kind == "835":
                    payment = claim_amount(fields[4] if len(fields) > 4 else "", "CLP04", sequence)
                    responsibility = claim_amount(fields[5] if len(fields) > 5 else "", "CLP05", sequence, optional=True)
                    if responsibility is None:
                        inferred_responsibility[sequence] = Decimal("0")
                    current.update(payment=format(payment, ".2f"),
                        patient_responsibility=format(responsibility, ".2f") if responsibility is not None else None,
                        allowed=format(payment + responsibility, ".2f") if responsibility is not None else None)
            except ValueError as exc:
                reject(current, str(exc))
        elif kind == "835" and tag in {"LX", "PLB"}:
            current = None
        elif kind == "835" and tag == "CAS" and current and current["sequence"] not in failures:
            sequence = current["sequence"]
            try:
                if len(fields) < 4 or not fields[1].strip():
                    raise ValueError(f"Claim occurrence {sequence} has an incomplete CAS adjustment.")
                for reason_index in range(2, len(fields), 3):
                    triple = fields[reason_index:reason_index + 3]
                    if not any(triple):
                        continue
                    if len(triple) < 2 or not triple[0].strip():
                        raise ValueError(f"Claim occurrence {sequence} has an incomplete CAS adjustment.")
                    adjustment = claim_amount(triple[1], "CAS " + fields[1], sequence)
                    if fields[1] == "PR" and sequence in inferred_responsibility:
                        inferred_responsibility[sequence] += adjustment
            except ValueError as exc:
                reject(current, str(exc))
        elif kind == "837":
            if tag == "HL":
                current = None
            elif tag == "LX":
                service = True
            elif current and not service and tag == "REF" and len(fields) > 2 and fields[1] == "9C":
                current["internal_number"] = fields[2].strip().upper().removeprefix("ZZ:")
        if current and current["sequence"] not in failures:
            numeric_fields = ({"SV1": [(2, "charge"), (4, "units")], "SV2": [(3, "charge"), (5, "units")],
                               "SV3": [(2, "charge"), (6, "units")]} if kind == "837"
                              else {"SVC": [(2, "charge"), (3, "payment")]})
            try:
                for offset, label in numeric_fields.get(tag, []):
                    amount = claim_amount(fields[offset] if len(fields) > offset else "", f"{tag} {label}", current["sequence"])
                    if label == "units" and abs(amount) >= Decimal("10000000000"):
                        raise ValueError(f"Claim occurrence {current['sequence']} has out-of-range service units.")
            except ValueError as exc:
                reject(current, str(exc))
    if not complete or not claims:
        raise ValueError("The file is incomplete or contains no claim records.")
    for row in claims:
        if row["sequence"] in failures:
            continue
        try:
            if row["sequence"] in inferred_responsibility:
                responsibility = inferred_responsibility[row["sequence"]]
                row["patient_responsibility"] = format(claim_amount(format(responsibility, ".2f"), "CAS PR total", row["sequence"]), ".2f")
                row["allowed"] = format(Decimal(row["payment"]) + responsibility, ".2f")
            if any(len(row[key]) > 255 for key in ("claim", "internal_number")):
                raise ValueError("Claim identifiers may contain at most 255 characters.")
            if details and kind == "835":
                row["_normalized"].update(patient_responsibility=Decimal(row["patient_responsibility"]))
        except ValueError as exc:
            reject(row, str(exc))
    if rejected is None and failures:
        raise ValueError(next(iter(failures.values()))["reason"])
    if rejected is not None:
        rejected.extend(failures.values())
    return [row for row in claims if row["sequence"] not in failures]


def enrich_analysis_claims(rows, raw, kind, element, terminator, failures, reject):
    """Reuse live intake extraction, but persist only validated Analysis rows."""
    if not rows:
        return
    if kind == "837":
        parsed = parse_837(terminator.join(raw) + terminator, allow_missing_claim_number=True)
        extracted = parsed["claims"]
    else:
        extracted = normalized_835_claims("", segments=raw, element=element, include_empty=True)
    if len(extracted) != len(rows):
        raise ValueError("Claim boundaries could not be extracted reliably; no claims were saved.")
    contexts = analysis_claim_contexts(raw, kind, element)
    for row, data, context in zip(rows, extracted, contexts):
        if row["sequence"] in failures:
            continue
        try:
            if kind == "837":
                values = claim_837_values(data, parsed)
                # Use the same authoritative comparison identifiers and validated amounts.
                values.update(highmark_claim_number=row["claim"], internal_claim_number=row["internal_number"],
                              total_charge_amount=Decimal(row["billed"]))
                services = [service_837_values(service, parsed) for service in data["services"]]
                model = Analysis837Claim
            else:
                values = {key: data[key] for key in ("claim_status", "service_count")}
                values.update(highmark_claim_number=row["claim"], internal_claim_number=row["internal_number"],
                    total_charge_amount=Decimal(row["billed"]), paid_amount=Decimal(row["payment"]),
                    patient_responsibility=Decimal(row["patient_responsibility"] or "0"),
                    raw_claim=terminator.join(data["segments"]) + terminator,
                    segment_data={"segments": data["segments"], "element_separator": element, "segment_separator": terminator})
                services = []
                model = Analysis835Claim
            values["claim_sequence"] = row["sequence"]
            instance = model(**values)
            instance.clean_fields(exclude=["edi_file", "analysis_claim", "client", "created_at"])
            for service in services:
                Analysis837ServiceLine(**service).clean_fields(exclude=["claim", "edi_file", "created_at"])
            row["_normalized"], row["_services"] = values, services
            row["_fingerprint"] = analysis_claim_fingerprint(kind, values, services, context, element, terminator)
        except ValidationError as exc:
            fields = ", ".join(sorted(exc.message_dict))
            reject(row, f"Claim occurrence {row['sequence']} has invalid or oversized fields: {fields}.")


def analysis_claim_contexts(raw, kind, element):
    """Retain transaction and ancestor provider elements absent from CLM loops."""
    header, provider, contexts = [], [], []
    in_header = True
    in_provider = False
    claim_tag = "CLM" if kind == "837" else "CLP"
    for segment in raw:
        fields = segment.split(element)
        tag = fields[0]
        if tag == "HL":
            in_header = False
            in_provider = len(fields) > 3 and fields[3] == "20"
            if in_provider:
                provider = [fields]
            continue
        if tag == claim_tag:
            in_header = in_provider = False
            contexts.append(header + provider)
        elif in_header:
            header.append(fields)
        elif in_provider:
            provider.append(fields)
    return contexts


def analysis_claim_fingerprint(kind, values, services, context, element, terminator):
    """Hash all business elements, excluding envelope controls and HL ordinals.

    Dates, references, people, providers, services, adjustments and amounts remain
    part of the identity. File names and upload timestamps are transport metadata.
    """
    skip = {"ISA", "GS", "ST", "SE", "GE", "IEA", "BHT", "LX", "TS2", "TS3"}
    prefix = [([fields[0], *fields[3:]] if fields[0] == "HL" else fields)
              for fields in context if fields[0] not in skip]
    body = []
    for segment in values["raw_claim"].split(terminator):
        if not segment.strip():
            continue
        fields = segment.strip().split(element)
        if fields[0] in skip or fields[0] == "BHT":
            continue
        if fields[0] == "HL":
            fields = [fields[0], *fields[3:]]
        body.append(fields)
    ignored = {"claim_sequence", "service_sequence", "raw_claim", "raw_segments", "segment_data"}
    columns = {key: value for key, value in values.items() if key not in ignored}
    lines = [{key: value for key, value in line.items() if key not in ignored} for line in services]
    return hashlib.sha256(json.dumps([kind, prefix, body, columns, lines], sort_keys=True,
                                    separators=(",", ":"), ensure_ascii=True, default=str).encode()).hexdigest()


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
            "uploaded_at": upload.uploaded_at.isoformat(), "claims": claims,
            "status": upload.status, "accepted_count": len(claims),
            "rejected_count": upload.rejected_count,
            "rejections": list(upload.rejections.values("sequence", "claim_number", "reason"))}


def persist_analysis_result(record: AnalysisUpload, claims: list[dict], rejected: list[dict]):
    """Commit the comparison index, normalized columns, lines, and findings together."""
    with transaction.atomic():
        # Serialize intake/backfill per tenant so concurrent files cannot accept
        # the same fingerprint twice. The unique constraint is the final guard.
        Client.objects.select_for_update().get(pk=record.client_id)
        record = AnalysisUpload.objects.select_for_update().get(pk=record.pk)
        if record.processing_version >= 2:
            return record
        record.claims.all().delete()
        record.rejections.all().delete()
        rejected = list(rejected)
        fingerprints = [row["_fingerprint"] for row in claims]
        existing = set()
        for start in range(0, len(fingerprints), 500):
            existing.update(AnalysisClaimIdentity.objects.filter(client_id=record.client_id, kind=record.kind,
                fingerprint__in=fingerprints[start:start + 500]).values_list("fingerprint", flat=True))
        unique_claims = []
        for row in claims:
            fingerprint = row["_fingerprint"]
            if fingerprint in existing:
                rejected.append({"sequence": row["sequence"], "claim": row["claim"],
                    "reason": f"Claim occurrence {row['sequence']} is an exact duplicate of an already accepted {record.kind} claim."})
            else:
                existing.add(fingerprint)
                unique_claims.append(row)
        claims = unique_claims
        projections = AnalysisClaim.objects.bulk_create([
            AnalysisClaim(upload=record, sequence=row["sequence"], claim_number=row["claim"],
                          internal_number=row["internal_number"], billed=row["billed"],
                          payment=row.get("payment"), patient_responsibility=row.get("patient_responsibility"))
            for row in claims
        ], batch_size=500)
        AnalysisClaimIdentity.objects.bulk_create([
            AnalysisClaimIdentity(client_id=record.client_id, kind=record.kind,
                                  fingerprint=row["_fingerprint"], claim=projection)
            for projection, row in zip(projections, claims)
        ], batch_size=500)
        if record.kind == "835":
            Analysis835Claim.objects.bulk_create([
                Analysis835Claim(edi_file=record, analysis_claim=projection, **row["_normalized"])
                for projection, row in zip(projections, claims)
            ], batch_size=500)
        else:
            normalized = Analysis837Claim.objects.bulk_create([
                Analysis837Claim(edi_file=record, analysis_claim=projection, client_id=record.client_id, **row["_normalized"])
                for projection, row in zip(projections, claims)
            ], batch_size=500)
            Analysis837ServiceLine.objects.bulk_create([
                Analysis837ServiceLine(edi_file=record, claim=claim, **service)
                for claim, row in zip(normalized, claims) for service in row["_services"]
            ], batch_size=500)
        AnalysisClaimRejection.objects.bulk_create([
            AnalysisClaimRejection(upload=record, sequence=row["sequence"], claim_number=row["claim"], reason=row["reason"])
            for row in rejected
        ], batch_size=500)
        record.accepted_count, record.rejected_count = len(claims), len(rejected)
        record.status = "PARTIAL" if claims and rejected else "SAVED" if claims else "REJECTED"
        record.processing_version = 2
        record.save(update_fields=["accepted_count", "rejected_count", "status", "processing_version"])
    return record


def save_upload(client, user, upload, kind: str, raw: bytes, claims: list[dict], rejected: list[dict]):
    # An identical retry returns the same persisted outcome, including rejections.
    with transaction.atomic():
        Client.objects.select_for_update().get(pk=client.pk)
        record, created = AnalysisUpload.objects.get_or_create(
            client=client, kind=kind, filename=upload.name.replace("\\", "/").rsplit("/", 1)[-1],
            content_sha256=hashlib.sha256(raw).hexdigest(),
            defaults={"uploaded_by": user, "raw_content": raw},
        )
        record = persist_analysis_result(record, claims, rejected)
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
                rejected = []
                claims = parse_analysis(raw.decode("utf-8-sig"), kind, rejected=rejected, details=True)
            except UnicodeDecodeError:
                response = JsonResponse({"success": False, "error": "The file must contain UTF-8 X12 text."}, status=400)
            except ValueError as exc:
                response = JsonResponse({"success": False, "error": str(exc)}, status=400)
            else:
                try:
                    record, created = save_upload(client, request.user, upload, kind, raw, claims, rejected)
                    response = JsonResponse({"success": True, "saved": True,
                                             "reused": not created, "file": upload_payload(record)})
                except ValueError as exc:
                    response = JsonResponse({"success": False, "error": str(exc)}, status=409)
                except DatabaseError:
                    # Do not log uploaded data or database exception parameters.
                    logger.error("Analysis staging save failed for client_id=%s", client.pk)
                    response = JsonResponse({"success": False, "error": "The file could not be saved. Keep it selected and retry."}, status=503)
    response["Cache-Control"] = "private, no-store, max-age=0"
    response["X-Content-Type-Options"] = "nosniff"
    return response
