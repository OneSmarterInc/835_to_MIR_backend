"""Persist generated fixed-width MIR files as queryable claims and services."""

from __future__ import annotations

import hashlib
from decimal import Decimal, InvalidOperation

from django.db import transaction

from admin_panel.mir_mapper_logic import config
from admin_panel.mir_mapper_logic.mapping_store import get_mappings

from .models import EDI835File, MIRClaim, MIRClaimChunk, MIRFile, MIRServiceLine


def _extract_fields(raw: str, fields: list[dict], scopes: set[str]) -> dict[str, str]:
    values = {}
    for field in fields:
        if field.get("scope") not in scopes:
            continue
        start = int(field["start"]) - 1
        length = int(field["length"])
        key = str(field.get("target") or field.get("name") or field.get("id"))
        values[key] = raw[start:start + length].strip()
    return values


def _integer(value: str, default: int = 0) -> int:
    try:
        return int((value or "").strip())
    except (TypeError, ValueError):
        return default


def _signed_implied_decimal(value: str, decimal_places: int = 2) -> Decimal:
    value = (value or "").strip()
    if not value:
        return Decimal("0")
    sign = Decimal("-1") if value[-1:] == "-" else Decimal("1")
    digits = value[:-1] if value[-1:] in "+-" else value
    try:
        return sign * Decimal(digits or "0").scaleb(-decimal_places)
    except InvalidOperation:
        return Decimal("0")


@transaction.atomic
def store_mir_file(
    *,
    source_835: EDI835File,
    mir_filename: str,
    mir_text: str,
) -> MIRFile:
    """Atomically replace the structured MIR output for one 835 conversion."""
    encoded = mir_text.encode("utf-8")
    rows = mir_text.splitlines()
    fields = get_mappings(source_835.client)

    parsed_rows = []
    total_services = 0
    for row_number, row in enumerate(rows, start=1):
        if len(row) < config.MIR_HEADER_LENGTH:
            raise ValueError(f"MIR row {row_number} is shorter than the {config.MIR_HEADER_LENGTH}-character header")
        service_area_length = len(row) - config.MIR_HEADER_LENGTH
        if service_area_length % config.MIR_SERVICE_BLOCK_LENGTH:
            raise ValueError(
                f"MIR row {row_number} has invalid length {len(row)}; service data is not divisible by "
                f"{config.MIR_SERVICE_BLOCK_LENGTH}"
            )
        actual_count = service_area_length // config.MIR_SERVICE_BLOCK_LENGTH
        declared_count = _integer(row[332:334])
        if actual_count != declared_count:
            raise ValueError(
                f"MIR row {row_number} declares {declared_count} services but contains {actual_count}"
            )
        if actual_count > config.MAX_SERVICE_LINES_PER_RECORD:
            raise ValueError(f"MIR row {row_number} contains more than 50 services")
        sequence = _integer(row[248:250], 1)
        max_sequence = _integer(row[250:252], 1)
        if sequence < 1 or max_sequence < sequence:
            raise ValueError(f"MIR row {row_number} has invalid record sequence {sequence}/{max_sequence}")
        parsed_rows.append((row_number, row, sequence, max_sequence, actual_count))
        total_services += actual_count

    # One source conversion owns one current MIR output. Reprocessing replaces it atomically.
    MIRFile.objects.filter(source_835=source_835).delete()
    mir_file = MIRFile.objects.create(
        source_835=source_835,
        client=source_835.client,
        mir_filename=mir_filename,
        original_835_filename=source_835.original_filename,
        file_content=mir_text,
        file_hash=hashlib.sha256(encoded).hexdigest(),
        file_size=len(encoded),
        physical_row_count=len(rows),
        service_count=total_services,
    )

    current_claim = None
    current_claim_number = ""
    global_service_number = 0
    claim_count = 0

    for row_number, row, sequence, max_sequence, service_count in parsed_rows:
        header = row[:config.MIR_HEADER_LENGTH]
        # The reconciliation identifier is the complete 23-character MIR key:
        # MIR100 (positions 3-19) followed by the six-character cross-reference
        # (positions 20-25). Storing only MIR100 makes distinct claims appear to
        # match and prevents exact matching with reference MIR/RECON files.
        claim_number = header[2:25].strip()
        if sequence == 1:
            claim_count += 1
            global_service_number = 0
            header_data = _extract_fields(header, fields, {"Claim", "Physical record"})
            current_claim = MIRClaim.objects.create(
                mir_file=mir_file,
                claim_sequence=claim_count,
                claim_control_number=claim_number,
                record_type=header[0:2].strip(),
                member_id=header[59:71].strip(),
                patient_last_name=header[85:105].strip(),
                patient_first_name=header[105:115].strip(),
                date_of_birth=header[117:125].strip(),
                claim_status=header[52:53].strip(),
                primary_reason=header[54:59].strip(),
                chunk_count=max_sequence,
                header_raw=header,
                segment_data=header_data,
            )
            current_claim_number = claim_number
        elif current_claim is None or claim_number != current_claim_number:
            raise ValueError(f"MIR row {row_number} is an orphan continuation record")
        elif sequence != current_claim.chunks.count() + 1 or max_sequence != current_claim.chunk_count:
            raise ValueError(f"MIR row {row_number} has an out-of-order continuation sequence")

        service_start = global_service_number + 1 if service_count else 0
        service_end = global_service_number + service_count
        chunk = MIRClaimChunk.objects.create(
            mir_claim=current_claim,
            chunk_number=sequence,
            service_start_number=service_start,
            service_end_number=service_end,
            services_in_chunk=service_count,
            physical_row_number=row_number,
            raw_row=row,
            row_length=len(row),
        )

        service_models = []
        for chunk_position in range(1, service_count + 1):
            start = config.MIR_HEADER_LENGTH + (chunk_position - 1) * config.MIR_SERVICE_BLOCK_LENGTH
            raw_service = row[start:start + config.MIR_SERVICE_BLOCK_LENGTH]
            segment_data = _extract_fields(raw_service, fields, {"Service"})
            global_service_number += 1
            service_models.append(MIRServiceLine(
                mir_claim=current_claim,
                mir_chunk=chunk,
                service_sequence=global_service_number,
                chunk_service_sequence=chunk_position,
                units=_signed_implied_decimal(segment_data.get("service_units", ""), 0),
                # These are canonical MIR positions inside every 303-byte
                # service block. Saved mapping labels must not alter financial
                # reconciliation values.
                charge_amount=_signed_implied_decimal(raw_service[50:61]),
                paid_amount=_signed_implied_decimal(raw_service[94:105]),
                patient_liability=_signed_implied_decimal(raw_service[105:116]),
                reason_code=segment_data.get("service_primary_reason", ""),
                service_raw=raw_service,
                segment_data=segment_data,
            ))
        MIRServiceLine.objects.bulk_create(service_models, batch_size=1000)
        current_claim.service_count = global_service_number
        current_claim.save(update_fields=["service_count"])

    mir_file.claim_count = claim_count
    mir_file.save(update_fields=["claim_count"])
    return mir_file


def set_mir_push_status(mir_file: MIRFile, pushed: bool) -> None:
    mir_file.status = "PUSHED" if pushed else "PUSH_FAILED"
    mir_file.save(update_fields=["status", "updated_at"])
