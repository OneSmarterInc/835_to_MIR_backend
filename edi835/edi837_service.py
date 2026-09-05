"""Parse, persist, search, and export client-scoped X12 837 claims."""

from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from datetime import datetime, timezone as datetime_timezone
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from .claim_numbers import split_claim_number
from .models import EDI837Claim, EDI837File, EDI837ServiceLine
from .storage import archive_inbound, client_storage_dirs, relative_media_path, safe_filename, stage_inbound


def _decimal(value):
    try:
        return Decimal(str(value or "0").replace(",", "").replace("$", ""))
    except InvalidOperation:
        return Decimal("0")


def _837_claim_numbers(clm01, reference_9c):
    """Resolve Highmark and internal identifiers from their authoritative fields."""
    split = split_claim_number(clm01)
    return {
        "highmark_claim_number": split["highmark_claim_number"] or str(clm01 or "").strip(),
        # REF*9C is Highmark's internal claim identifier. Only fall back to a
        # combined CLM01 suffix for files that genuinely omit REF*9C.
        "internal_claim_number": str(reference_9c or "").strip() or split["internal_claim_number"],
    }


def split_x12(text):
    content = (text or "").strip()
    if not content:
        raise ValueError("The 837 file is empty.")
    element = content[3] if content.startswith("ISA") and len(content) > 3 else "*"
    segment = content[105] if content.startswith("ISA") and len(content) > 105 else "~"
    raw_segments = [part.strip() for part in content.split(segment) if part.strip()]
    return element, segment, raw_segments, [raw.split(element) for raw in raw_segments]


def _name(seg):
    last = seg[3].strip() if len(seg) > 3 else ""
    first = seg[4].strip() if len(seg) > 4 else ""
    return first, last, " ".join(part for part in (first, last) if part)


def parse_837(text):
    element_sep, segment_sep, raw_segments, segments = split_x12(text)
    tags = [seg[0].upper() for seg in segments if seg]
    if "ISA" not in tags or "GS" not in tags or not any(seg[0].upper() == "ST" and len(seg) > 1 and seg[1] == "837" for seg in segments):
        raise ValueError("The file is not a supported X12 837: ISA, GS, or ST*837 is missing.")
    claims = []
    current = None
    context_start = 0
    context = {"member_id": "", "patient_first": "", "patient_last": "", "subscriber_first": "", "subscriber_last": "", "billing_provider": "", "rendering_provider": "", "referring_provider": "", "payer": ""}
    current_service = None
    transaction_prefix = []

    for index, seg in enumerate(segments):
        tag = (seg[0] if seg else "").upper()
        if tag == "HL" and current is not None:
            current["end_index"] = index
            claims.append(current)
            current = None
            current_service = None
        if tag in {"ISA", "GS", "ST", "BHT"}:
            transaction_prefix.append(raw_segments[index])
        if tag == "HL" and len(seg) > 3 and seg[3] == "20":
            context.update({"member_id": "", "patient_first": "", "patient_last": "", "subscriber_first": "", "subscriber_last": "", "billing_provider": "", "rendering_provider": "", "referring_provider": "", "payer": ""})
        if tag == "HL" and len(seg) > 3 and seg[3] == "22":
            context_start = index
            context.update({"member_id": "", "patient_first": "", "patient_last": "", "subscriber_first": "", "subscriber_last": "", "rendering_provider": "", "referring_provider": "", "payer": ""})
        elif tag == "HL" and len(seg) > 3 and seg[3] == "23":
            context.update({"patient_first": "", "patient_last": "", "rendering_provider": "", "referring_provider": ""})
        if tag == "NM1":
            entity = seg[1] if len(seg) > 1 else ""
            first, last, display = _name(seg)
            identifier = seg[9].strip() if len(seg) > 9 else ""
            if entity == "IL":
                context.update(member_id=identifier, subscriber_first=first, subscriber_last=last)
            elif entity == "QC":
                context.update(patient_first=first, patient_last=last)
                if identifier:
                    context["member_id"] = identifier
            elif entity == "85":
                context["billing_provider"] = display or last
            elif entity == "82":
                context["rendering_provider"] = display or last
                if current:
                    current["rendering_provider"] = display or last
            elif entity == "DN":
                context["referring_provider"] = display or last
                if current:
                    current["referring_provider"] = display or last
            elif entity == "PR":
                context["payer"] = display or last
        if tag == "CLM":
            if current:
                current["end_index"] = index
                claims.append(current)
            claim_id = seg[1].strip() if len(seg) > 1 else ""
            if not claim_id:
                raise ValueError(f"837 claim {len(claims) + 1} has no CLM01 claim number.")
            facility = (seg[5].split(":") if len(seg) > 5 else [])
            current = {
                "start_index": context_start,
                "claim_index": index,
                "end_index": len(segments),
                "claim_control_number": claim_id,
                "clm01": claim_id,
                "reference_9c": "",
                "patient_control_number": claim_id,
                "total_charge_amount": _decimal(seg[2] if len(seg) > 2 else ""),
                "place_of_service": facility[0] if facility else "",
                "claim_type": facility[1] if len(facility) > 1 else "",
                "claim_frequency_code": facility[2] if len(facility) > 2 else "",
                "original_claim_number": "",
                "diagnosis_codes": [], "service_from_date": "", "service_to_date": "", "services": [],
                **context,
            }
            current_service = None
        elif current and tag in {"SV1", "SV2", "SV3"}:
            composite_index, charge_index, units_index = (1, 2, 4) if tag == "SV1" else (2, 3, 5) if tag == "SV2" else (1, 2, 6)
            composite = seg[composite_index].split(":") if len(seg) > composite_index else []
            current_service = {
                "service_sequence": len(current["services"]) + 1,
                "procedure_qualifier": composite[0] if composite else "",
                "procedure_code": composite[1] if len(composite) > 1 else (composite[0] if composite else ""),
                "modifiers": composite[2:6],
                "revenue_code": seg[1].strip() if tag == "SV2" and len(seg) > 1 else "",
                "charge_amount": _decimal(seg[charge_index] if len(seg) > charge_index else ""),
                "units": _decimal(seg[units_index] if len(seg) > units_index else ""),
                "diagnosis_pointers": (seg[7].split(":") if tag == "SV1" and len(seg) > 7 else []),
                "service_from_date": "", "service_to_date": "", "raw": [raw_segments[index]], "segment_data": {tag: seg},
            }
            current["services"].append(current_service)
        elif current and tag == "HI":
            for value in seg[1:]:
                parts = value.split(":")
                if len(parts) > 1 and parts[1]:
                    current["diagnosis_codes"].append(parts[1])
        elif current and tag == "REF" and len(seg) > 2 and seg[1].upper() == "9C":
            current["reference_9c"] = seg[2].strip()
        elif current and tag == "REF" and len(seg) > 2 and seg[1].upper() == "F8":
            current["original_claim_number"] = seg[2].strip()
        elif current and tag == "DTP" and len(seg) > 3:
            qualifier, value = seg[1], seg[3]
            dates = value.split("-")
            if qualifier in {"472", "434"}:
                target = current_service if current_service else current
                target["service_from_date"] = dates[0]
                target["service_to_date"] = dates[-1]
            if current_service:
                current_service["raw"].append(raw_segments[index])
        elif current_service and tag in {"REF", "NTE", "LIN", "CTP"}:
            current_service["raw"].append(raw_segments[index])

    if current:
        trailer_index = next((i for i in range(current["claim_index"] + 1, len(segments)) if segments[i][0].upper() in {"SE", "GE", "IEA"}), len(segments))
        current["end_index"] = trailer_index
        claims.append(current)
    if not claims:
        raise ValueError("No CLM claim segments were found in the 837 file.")
    for claim in claims:
        if not claim["patient_first"] and not claim["patient_last"]:
            claim["patient_first"], claim["patient_last"] = claim["subscriber_first"], claim["subscriber_last"]
        claim["raw_claim"] = segment_sep.join(raw_segments[claim["start_index"]:claim["end_index"]]) + segment_sep
    return {"element_sep": element_sep, "segment_sep": segment_sep, "prefix": transaction_prefix, "claims": claims}


def _write_outbound_copy(client, archived_path):
    out_path = client_storage_dirs(client)["837_out"] / archived_path.name
    if out_path.exists():
        out_path = out_path.with_name(f"{out_path.stem}_{uuid.uuid4().hex[:12]}{out_path.suffix}")
    temporary = out_path.with_name(f".{out_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copy2(archived_path, temporary)
        os.replace(temporary, out_path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return out_path


@transaction.atomic
def ingest_837(client, actor, filename, raw, text=None, import_mode="MANUAL", remote_path=""):
    if client is None:
        raise ValueError("Select a client before processing an 837 file.")
    raw = bytes(raw or b"")
    if not raw:
        raise ValueError("The 837 file is empty.")
    text = text if text is not None else raw.decode("utf-8-sig", errors="replace")
    digest = hashlib.sha256(raw).hexdigest()
    existing = EDI837File.objects.filter(client=client, file_hash=digest).first()
    if existing:
        # A file may have been indexed earlier by a manual/admin workflow and
        # later actually arrive through the configured 837_IN SFTP route. The
        # Search table should reflect the real inbound transport. Promote the
        # existing record to SFTP instead of leaving a stale MANUAL source.
        if str(import_mode or "").upper() == "SFTP":
            update_fields = []
            if existing.import_mode != "SFTP":
                existing.import_mode = "SFTP"
                update_fields.append("import_mode")
            normalized_remote = str(remote_path or "").strip()
            if normalized_remote and existing.remote_path != normalized_remote:
                existing.remote_path = normalized_remote
                update_fields.append("remote_path")
            if update_fields:
                existing.save(update_fields=update_fields)
        return existing, True
    parsed = parse_837(text)
    original_name = safe_filename(filename)[:255]
    stored_name = f"{uuid.uuid4().hex}_{original_name}"[:255]
    edi_file = EDI837File.objects.create(
        client=client, uploaded_by=actor if getattr(actor, "is_authenticated", False) else None,
        original_filename=original_name, stored_filename=stored_name, file_content=text,
        file_hash=digest, file_size=len(raw), import_mode=import_mode, remote_path=remote_path,
        status="PROCESSING",
    )
    total_charge = Decimal("0")
    service_total = 0
    claim_models = []
    for sequence, data in enumerate(parsed["claims"], start=1):
        split = _837_claim_numbers(data["claim_control_number"], data["reference_9c"])
        claim_models.append(EDI837Claim(
            edi_file=edi_file, client=client, claim_sequence=sequence,
            claim_control_number=data["claim_control_number"], **split,
            reference_9c=data["reference_9c"],
            patient_control_number=data["clm01"], member_id=data["member_id"],
            patient_first_name=data["patient_first"], patient_last_name=data["patient_last"],
            subscriber_first_name=data["subscriber_first"], subscriber_last_name=data["subscriber_last"],
            billing_provider_name=data["billing_provider"], rendering_provider_name=data["rendering_provider"],
            referring_provider_name=data["referring_provider"],
            payer_name=data["payer"], claim_type=data["claim_type"], place_of_service=data["place_of_service"],
            claim_frequency_code=data["claim_frequency_code"], original_claim_number=data["original_claim_number"],
            service_from_date=data["service_from_date"], service_to_date=data["service_to_date"],
            diagnosis_codes=data["diagnosis_codes"], service_count=len(data["services"]),
            total_charge_amount=data["total_charge_amount"], raw_claim=data["raw_claim"],
            segment_data={"prefix": parsed["prefix"], "element_separator": parsed["element_sep"], "segment_separator": parsed["segment_sep"]},
        ))
        total_charge += data["total_charge_amount"]
        service_total += len(data["services"])
    EDI837Claim.objects.bulk_create(claim_models, batch_size=1000)
    service_models = []
    for claim, data in zip(claim_models, parsed["claims"]):
        service_models.extend([
            EDI837ServiceLine(
                claim=claim, edi_file=edi_file, service_sequence=service["service_sequence"],
                procedure_code=service["procedure_code"], procedure_qualifier=service["procedure_qualifier"],
                modifiers=service["modifiers"], revenue_code=service["revenue_code"],
                service_from_date=service["service_from_date"], service_to_date=service["service_to_date"],
                units=service["units"], charge_amount=service["charge_amount"],
                diagnosis_pointers=service["diagnosis_pointers"],
                raw_segments=parsed["segment_sep"].join(service["raw"]) + parsed["segment_sep"],
                segment_data=service["segment_data"],
            ) for service in data["services"]
        ])
    EDI837ServiceLine.objects.bulk_create(service_models, batch_size=2000)
    inbound = stage_inbound(client, "837", stored_name, raw, binary=True)
    archived = archive_inbound(client, "837", inbound)
    outbound = _write_outbound_copy(client, archived)
    edi_file.archive_path = relative_media_path(archived)
    edi_file.outbound_path = relative_media_path(outbound)
    edi_file.claim_count = len(parsed["claims"])
    edi_file.service_count = service_total
    edi_file.total_charge_amount = total_charge
    edi_file.status = "PROCESSED"
    edi_file.processed_at = timezone.now()
    edi_file.save()
    return edi_file, False


def _control_number(length, salt):
    now = datetime.now(datetime_timezone.utc).strftime("%Y%m%d%H%M%S%f")
    digest = hashlib.sha256(f"{now}-{uuid.uuid4()}-{salt}".encode()).hexdigest()
    numeric = "".join(str(int(char, 16)) for char in digest)
    return numeric[:length].zfill(length)


def export_single_claim(claim):
    """Slice one provider loop into a standalone 837 with fresh control numbers."""
    element, separator, raw, segments = split_x12(claim.edi_file.file_content)
    try:
        isa_index = next(i for i, seg in enumerate(segments) if seg[0] == "ISA")
        gs_index = next(i for i, seg in enumerate(segments) if seg[0] == "GS")
        st_index = next(i for i, seg in enumerate(segments) if seg[0] == "ST" and len(seg) > 1 and seg[1] == "837")
        first_hl = next(i for i in range(st_index, len(segments)) if segments[i][0] == "HL")
    except StopIteration as exc:
        raise ValueError("The source 837 is missing required transaction envelope segments.") from exc
    prefix = raw[isa_index:first_hl]
    claim_parts = [part for part in claim.raw_claim.split(separator) if part.strip()]
    isa = prefix[0].split(element)
    gs = prefix[1].split(element)
    st = next((segment.split(element) for segment in prefix if segment.startswith("ST" + element)), None)
    if st is None:
        raise ValueError("The source 837 is missing ST.")
    isa13, gs06, st02 = _control_number(9, claim.id), _control_number(9, f"gs-{claim.id}"), _control_number(4, f"st-{claim.id}")
    if len(isa) > 13: isa[13] = isa13
    if len(gs) > 6: gs[6] = gs06
    if len(st) > 2: st[2] = st02
    prefix[0], prefix[1] = element.join(isa), element.join(gs)
    for index, value in enumerate(prefix):
        if value.startswith("ST" + element):
            prefix[index] = element.join(st)
            break
    body = [value for value in claim_parts if not value.startswith(("ISA" + element, "GS" + element, "ST" + element, "SE" + element, "GE" + element, "IEA" + element))]
    se_count = 1 + len(body) + 1
    output = prefix + body + [f"SE{element}{se_count}{element}{st02}", f"GE{element}1{element}{gs06}", f"IEA{element}1{element}{isa13}"]
    return separator.join(output) + separator
