"""EDI 835 persistence hooks.

Keep normalized claim rows synchronized and send the same terminal validation
notifications for SFTP-ingested 835 files that manual processing already sends.
"""

import logging

from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.utils.html import escape

from .edi835_claim_service import normalize_835_file
from .models import EDI835File


logger = logging.getLogger(__name__)


@receiver(pre_save, sender=EDI835File)
def remember_previous_sftp_status(sender, instance, **kwargs):
    """Remember the persisted SFTP status so terminal email events fire only once."""
    instance._previous_validation_status = None
    # Manual/API records never use the SFTP terminal-notification comparison,
    # so avoid one database read on every save for those records.
    if not instance.pk or str(instance.ingestion_source or "").upper() != "SFTP":
        return
    previous = sender.objects.filter(pk=instance.pk).values_list("status", flat=True).first()
    instance._previous_validation_status = previous


@receiver(post_save, sender=EDI835File)
def normalize_saved_835_file(sender, instance, created=False, update_fields=None, **kwargs):
    # Most status/progress saves use update_fields. Re-running the normalization
    # existence/count checks for those metadata-only writes creates avoidable DB
    # traffic during conversion. Normalize on creation, full saves, or when the
    # source content itself was explicitly changed.
    if update_fields is not None and "input_file_content" not in update_fields:
        return
    if instance.input_file_content:
        normalize_835_file(instance)


@receiver(post_save, sender=EDI835File)
def notify_sftp_835_validation_result(sender, instance, created=False, **kwargs):
    """Email client users when an SFTP 835 reaches validation success/failure.

    A notification is sent only when the record first reaches a terminal state,
    preventing later metadata saves from producing duplicate emails.
    """
    if str(instance.ingestion_source or "").upper() != "SFTP" or not instance.client_id:
        return

    status = str(instance.status or "").upper()
    if status not in {"ERROR", "ARCHIVED", "COMPLETED"}:
        return

    previous_status = str(getattr(instance, "_previous_validation_status", "") or "").upper()
    if not created and previous_status == status:
        return

    success = status in {"ARCHIVED", "COMPLETED"}
    outcome = "Successful" if success else "Unsuccessful"
    filename = instance.original_filename or instance.stored_filename or "835 file"
    subject = f"OneSmarter: SFTP 835 Validation {outcome} - {filename}"

    mir_filename = ""
    if success:
        try:
            mir = getattr(instance, "mir_file", None)
            mir_filename = getattr(mir, "mir_filename", "") or ""
        except Exception:
            mir_filename = ""

    error_detail = str(instance.error_message or "").strip()
    rows = [
        ("Status", outcome),
        ("Source", "SFTP"),
        ("835 input", filename),
        ("Claims", instance.claims_count or 0),
        ("Services", instance.services_count or 0),
        ("MIR records", instance.records_count or 0),
    ]
    if mir_filename:
        rows.append(("MIR output", mir_filename))
    if not success:
        rows.append(("Validation detail", error_detail or "835 validation failed."))

    table_rows = "".join(
        "<tr>"
        f'<td style="padding:9px 12px;border:1px solid #d7e0ea;background:#f6f9fc;font-weight:700">{escape(str(label))}</td>'
        f'<td style="padding:9px 12px;border:1px solid #d7e0ea">{escape(str(value or "—"))}</td>'
        "</tr>"
        for label, value in rows
    )
    html = (
        f"<h3>SFTP 835 Validation {escape(outcome)}</h3>"
        + (
            "<p>The 835 file received through SFTP passed validation and continued through MIR processing.</p>"
            if success
            else "<p>The 835 file received through SFTP did not pass the processing/validation gate and requires review.</p>"
        )
        + '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;margin:16px 0">'
        + table_rows
        + "</table>"
        + (
            "<p>No action is required.</p>"
            if success
            else "<p><strong>Action required:</strong> Review the validation detail, correct the 835, and place the corrected file back on SFTP.</p>"
        )
    )

    try:
        from admin_panel.email_service import send_client_email

        sent = send_client_email(instance.client, subject, html)
        if not sent:
            logger.warning(
                "SFTP 835 validation email was not sent for %s (%s).",
                filename,
                instance.pk,
            )
    except Exception:
        # Email delivery must never roll back or block the SFTP conversion.
        logger.exception(
            "Failed to send SFTP 835 validation email for %s (%s).",
            filename,
            instance.pk,
        )
