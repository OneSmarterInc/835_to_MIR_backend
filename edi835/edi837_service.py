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
        # An HL after CLM always starts the next claim hierarchy. Finalize first,
        # otherwise the following subscriber/provider names leak into this claim.
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
            # Highmark files commonly carry the adjudication-facing claim key
            # in REF*9C while CLM01 remains the submitter's patient-control ID.
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
        split = split_claim_number(data["claim_control_number"])
        if not split["internal_claim_number"] and data["reference_9c"]:
            split["internal_claim_number"] = data["reference_9c"]
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
        raise ValueError("The stored source is missing the required ISA/GS/ST/HL 837 envelope.") from exc

    matched = None
    for index, seg in enumerate(segments):
        if seg[0] == "CLM" and len(seg) > 1 and seg[1].strip() == claim.patient_control_number:
            matched = index
            break
        if seg[0] == "REF" and len(seg) > 2 and seg[1] == "9C" and seg[2].strip() == claim.claim_control_number:
            matched = index
            break
    if matched is None:
        raise ValueError("The selected claim could not be located in its stored 837 source.")

    provider_start = None
    for index in range(matched, -1, -1):
        seg = segments[index]
        if seg[0] == "HL" and len(seg) > 3 and seg[3] == "20":
            provider_start = index
            break
    if provider_start is None:
        raise ValueError("The selected claim has no enclosing billing-provider HL loop.")
    block_end = next((i for i in range(provider_start + 1, len(segments))
                      if (segments[i][0] == "HL" and len(segments[i]) > 3 and segments[i][3] == "20")
                      or segments[i][0] in {"SE", "GE", "IEA"}), len(segments))
    block = [list(seg) for seg in segments[provider_start:block_end]]

    # A provider block may contain multiple claims. Retain the selected claim's
    # subscriber context and claim segments, excluding sibling subscriber loops.
    subscriber_start = next((i for i in range(matched - provider_start, -1, -1)
                             if block[i][0] == "HL" and len(block[i]) > 3 and block[i][3] in {"22", "23"}), None)
    # A dependent claim begins at HL*23, but its subscriber, SBR, member ID,
    # and payer live in the parent HL*22 loop and must be exported with it.
    if subscriber_start is not None and block[subscriber_start][3] == "23":
        parent_id = block[subscriber_start][2] if len(block[subscriber_start]) > 2 else ""
        parent_start = next((i for i in range(subscriber_start - 1, -1, -1)
                             if block[i][0] == "HL" and len(block[i]) > 3
                             and block[i][3] == "22" and block[i][1] == parent_id), None)
        if parent_start is not None:
            subscriber_start = parent_start
    if subscriber_start is not None:
        first_subscriber = next((i for i, seg in enumerate(block)
                                 if seg[0] == "HL" and len(seg) > 3 and seg[3] in {"22", "23"}), subscriber_start)
        relative_claim = matched - provider_start
        first_claim = next((i for i in range(subscriber_start, len(block)) if block[i][0] == "CLM"), relative_claim)
        claim_end = next((i for i in range(relative_claim + 1, len(block))
                          if block[i][0] == "CLM" or (block[i][0] == "HL" and len(block[i]) > 3 and block[i][3] in {"22", "23"})), len(block))
        block = block[:first_subscriber] + block[subscriber_start:first_claim] + block[relative_claim:claim_end]

    old_ids = [seg[1] for seg in block if seg[0] == "HL" and len(seg) > 1]
    remap = {old: str(index + 1) for index, old in enumerate(old_ids)}
    for seg in block:
        if seg[0] == "HL":
            seg[1] = remap.get(seg[1], seg[1])
            if len(seg) > 2 and seg[2]:
                seg[2] = remap.get(seg[2], seg[2])

    isa = list(segments[isa_index]); gs = list(segments[gs_index]); st = list(segments[st_index])
    isa13, gs06, st02 = _control_number(9, claim.pk), _control_number(9, claim.claim_control_number), "0001"
    if len(isa) > 13: isa[13] = isa13
    if len(gs) > 6: gs[6] = gs06
    if len(st) > 2: st[2] = st02
    header_body = [list(seg) for seg in segments[st_index + 1:first_hl]]
    st_to_se = [st] + header_body + block
    out = [isa, gs] + st_to_se + [["SE", str(len(st_to_se) + 1), st02], ["GE", "1", gs06], ["IEA", "1", isa13]]
    return (separator + "\n").join(element.join(seg) for seg in out) + separator
