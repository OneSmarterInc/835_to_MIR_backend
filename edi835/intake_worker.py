"""Dispatch accepted sources through the existing production processors."""
from django.utils import timezone

from admin_panel.access_control import can_access_client
from .intake_service import current_transfer
from .intake_storage import read_verified
from .models import InboundTransfer
from .intake_lifecycle import begin_effects, record_validation


def process_intake(job, user, client):
    receipt = InboundTransfer.objects.select_related("partner").get(dispatch_id=job["id"])
    if receipt.client_id != client.pk or client.stage == "offboarded" or client.status != "ACTIVE":
        raise ValueError("Client is not available for processing.")
    if receipt.partner_id:
        partner = receipt.partner
        if not partner.active or partner.expires_at <= timezone.now() or partner.client_id != client.pk or receipt.workflow not in partner.workflows:
            raise ValueError("Partner authorization has expired or was revoked.")
    elif not (getattr(user, "is_active", False) and getattr(user, "is_staff", False) and can_access_client(user, client.pk)):
        raise ValueError("Staff authorization has expired or was revoked.")
    raw = read_verified(receipt)
    record_validation(receipt, raw)
    token = current_transfer.set(receipt)
    try:
        if receipt.workflow == "837":
            from .edi837_service import ingest_837
            begin_effects()
            record, duplicate = ingest_837(client, user, receipt.original_filename, raw)
            record_validation(receipt, raw, outcome="VALID")
            if duplicate:
                job["receipt_state"] = "DUPLICATE"
            result = {"success": True, "source_id": str(record.pk)}
        elif receipt.workflow == "835":
            from converter.views import _validate_835_for_conversion
            from .services import process_edi835_file_content
            text = raw.decode("utf-8-sig")
            report = _validate_835_for_conversion(text)
            if not report.get("valid", report.get("is_valid", True)):
                record_validation(receipt, raw, outcome="REJECTED", code="VALIDATION_REJECTED")
                job.update(receipt_state="REJECTED", safe_error_code="VALIDATION_REJECTED")
                return {"success": False, "error": "835 validation failed. Review the source file."}
            record_validation(receipt, raw, outcome="VALID")
            begin_effects()
            converted = process_edi835_file_content(text, original_filename=receipt.original_filename, client=client)
            result = {"success": bool(converted.get("success")),
                      "source_id": str(getattr(converted.get("db_record"), "pk", ""))}
            if not result["success"]:
                result["error"] = "835 processing failed. Review the conversion record."
        else:
            raise ValueError("Unsupported intake workflow.")
        return result
    finally:
        current_transfer.reset(token)
