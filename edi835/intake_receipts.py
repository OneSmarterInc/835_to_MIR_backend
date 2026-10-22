"""Public receipt identifiers and explicit duplicate disposition."""


def receipt_summary(receipt):
    receipt.refresh_from_db()
    canonical = receipt.canonical_transfer
    return {
        "receipt_id": str(receipt.pk), "lifecycle_state": receipt.state,
        "status_url": (f"/edi835/api/client-portal/analysis/?view=receipt&receipt_id={receipt.pk}"
                       if receipt.workflow.startswith("ANALYSIS_") else
                       f"/edi835/api/intake/?receipt_id={receipt.pk}"),
        "canonical_receipt_id": str(canonical.pk) if canonical else None,
        "canonical_state": canonical.state if canonical else None,
    }


def receipt_detail(receipt):
    result = receipt_summary(receipt)
    result.update(workflow=receipt.workflow, byte_count=receipt.byte_count,
        received_at=receipt.received_at.isoformat(), state=receipt.dispatch.state,
        validation_outcome=receipt.validation_outcome, safe_error_code=receipt.safe_error_code,
        transaction_set_count=receipt.transaction_set_count, converter_version=receipt.converter_version,
        outputs=list(receipt.dispatch.outputs.values("id", "checksum", "byte_count", "delivery_state")),
        transaction_sets=list(receipt.transaction_sets.order_by("sequence").values(
            "id", "sequence", "transaction_type", "control_number", "version", "first_segment", "last_segment")[:100]),
        attempts=list(receipt.dispatch.attempts.order_by("-sequence").values(
            "id", "sequence", "parent_id", "state", "started_at", "finished_at",
            "heartbeat_at", "lease_expires_at", "retry_reason", "safe_error_code", "converter_version")[:20]),
        result=receipt.dispatch.payload.get("intake_result"))
    return result


def duplicate_result(receipts):
    return {"success": False, "duplicate": True, "code": "DUPLICATE_FILE",
            "error": "Duplicate file: already received for this client and workflow. See its receipt for processing status.",
            "receipts": [receipt_summary(item) for item in receipts]}


class DuplicateSubmission(ValueError):
    """A remote source remains retained; no conversion or delivery is repeated."""

    def __init__(self, receipts):
        self.receipts = receipts
        super().__init__("DUPLICATE_FILE: already received; review the canonical receipt.")
