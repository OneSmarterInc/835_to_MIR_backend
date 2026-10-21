"""Durable MIR checkpoints. External SFTP acknowledgement is a separate concern."""
from __future__ import annotations

import hashlib
import uuid

from django.db import transaction

from .intake_lifecycle import converter_version, current_attempt, owned_attempt
from .intake_storage import read_verified, store_stream
from .models import BatchDispatch, IntakeOutput


def conversion_identity(fallback):
    from .intake_service import current_transfer
    receipt = current_transfer.get()
    return uuid.uuid5(receipt.dispatch_id, "mir-source") if receipt else fallback


def checkpoint_output(source, mir, archive_path, out_path) -> None:
    from .intake_service import current_transfer
    receipt = current_transfer.get()
    if receipt is None:
        return
    if source.client_id != receipt.client_id or mir.client_id != receipt.client_id:
        raise ValueError("Output tenant does not match intake.")
    with open(archive_path, "rb") as handle:
        stored = store_stream(handle, receipt.client_id, max_bytes=max(1, mir.file_size))
    if stored.size != mir.file_size or stored.checksum != mir.file_hash:
        raise ValueError("MIR archive differs from its normalized database record.")
    digest = hashlib.sha256()
    with open(out_path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != stored.checksum:
        raise ValueError("MIR outbound copy differs from the archive.")
    with transaction.atomic():
        dispatch = BatchDispatch.objects.select_for_update().get(pk=receipt.dispatch_id)
        job = current_attempt.get()
        attempt = owned_attempt(job, lock=True) if job else None
        existing = IntakeOutput.objects.filter(dispatch=dispatch, source_835=source).first()
        if existing:
            if existing.checksum != stored.checksum or existing.mir_file_id != mir.pk:
                raise ValueError("Output checkpoint conflict; explicit regeneration is required.")
            verify_output(existing)
            # The redundant random object remains for the reconciler. Never
            # delete an object on a transaction outcome that may be uncertain.
            return
        IntakeOutput.objects.create(
            dispatch=dispatch, attempt=attempt, source_835=source, mir_file=mir,
            storage_key=stored.key, checksum=stored.checksum, byte_count=stored.size,
            converter_version=converter_version(),
        )


def verify_output(output: IntakeOutput) -> None:
    raw = read_verified(output)
    mir = output.mir_file
    if (mir is None or mir.file_hash != output.checksum or mir.file_size != len(raw)
            or hashlib.sha256(mir.file_content.encode("utf-8")).hexdigest() != output.checksum):
        raise ValueError("Output checkpoint is missing or inconsistent.")


def verify_dispatch_outputs(dispatch_id) -> None:
    for output in IntakeOutput.objects.filter(dispatch_id=dispatch_id).select_related("mir_file"):
        verify_output(output)


def replay_output():
    """Return the committed conversion without regenerating or sending it again."""
    from .intake_service import current_transfer
    receipt = current_transfer.get()
    if receipt is None:
        return None
    output = IntakeOutput.objects.filter(dispatch_id=receipt.dispatch_id).select_related("mir_file", "source_835").first()
    if output is None:
        return None
    verify_output(output)
    source, mir = output.source_835, output.mir_file
    return {"success": True, "replayed": True, "db_record": source, "mir_text": mir.file_content,
            "claims_count": source.claims_count, "services_count": mir.service_count,
            "records_count": mir.physical_row_count, "partial": bool(source.held_claims_count),
            "findings": source.conversion_findings, "filename": mir.mir_filename,
            "sftp_uploaded": output.delivery_state == "CONFIRMED", "delivery_state": output.delivery_state}


def delivery_checkpoint(mir, state: str) -> None:
    if state not in {"SENDING", "CONFIRMED", "UNCONFIRMED"}:
        raise ValueError("Invalid output delivery state.")
    job = current_attempt.get()
    if job:
        owned_attempt(job)
    IntakeOutput.objects.filter(mir_file=mir).update(delivery_state=state)
