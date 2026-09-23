import logging
from datetime import date
from urllib.parse import unquote

from django.core.files.base import ContentFile
from django.http import JsonResponse
from django.utils import timezone

from accounts.models import Client
from validation import validate_step_upload

from .document_registry import document_definition
from .models import AuditLog, ClientDocument, ClientStepStatus, OnboardingStepDefinition
from .views import _offboarded_workflow_lock, update_client_onboarding_stats


# 2026-09-23 - Yash: Removed csrf_exempt decorator for CSRF protection
def api_admin_step_upload(request, client_id, step_key):
    """Upload and validate an onboarding document for a client step.

    ContentFile is intentionally imported at module scope.  The legacy view had
    a function-local ContentFile import in its success path while also using
    ContentFile in its validation-failure path.  Python therefore treated
    ContentFile as a local variable for the whole function and raised
    UnboundLocalError whenever validation failed before that import executed.
    """
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Only POST allowed"}, status=405)

    locked = _offboarded_workflow_lock(request, client_id, "onboarding file upload")
    if locked:
        return locked

    file_bytes = request.body
    filename = unquote(request.headers.get("X-Filename", "uploaded_document.pdf"))
    expiration_value = request.headers.get("X-Expiration-Date", "").strip()
    if not expiration_value:
        return JsonResponse({"success": False, "error": "Expiration date is required."}, status=400)

    try:
        expiration_date = date.fromisoformat(expiration_value)
    except ValueError:
        return JsonResponse({"success": False, "error": "Enter a valid expiration date."}, status=400)

    try:
        parts = step_key.split("_")
        if len(parts) < 2:
            return JsonResponse({"success": False, "error": "Invalid step key."}, status=400)

        step_num = int(parts[1])
        client_obj = Client.objects.get(id=client_id)
        step_def = OnboardingStepDefinition.objects.get(step_number=step_num)

        val_res = validate_step_upload(step_num, file_bytes, filename, client=client_obj)

        if not val_res.get("ok", True):
            checks = val_res.get("checks", [])
            err_msg = val_res.get("error")
            if not err_msg and checks:
                err_msg = next(
                    (check.get("detail") for check in checks if not check.get("ok")),
                    "Validation failed",
                )

            try:
                from admin_panel.email_service import send_client_email

                subject = f"OneSmarter: File Validation Failed - {filename}"
                html = (
                    f"<h3>File Upload Failed</h3>"
                    f"<p>The file <b>{filename}</b> failed validation.</p>"
                    f"<p><b>Reason:</b> {err_msg}</p>"
                )
                send_client_email(client_obj, subject, html)
            except Exception as exc:
                logging.getLogger(__name__).error("Failed to send email: %s", exc)

            failed_doc = None
            if file_bytes:
                failed_definition = document_definition(
                    f"Onboarding Step {step_num}", step_def.title
                )
                failed_doc = ClientDocument.objects.create(
                    client=client_obj,
                    document_name=filename,
                    original_filename=filename,
                    document_type=f"Onboarding Step {step_num}",
                    file_size=len(file_bytes),
                    uploaded_by=(request.user.name or request.user.email)
                    if request.user and request.user.is_authenticated
                    else "Admin User",
                    expiration_date=expiration_date,
                    direction=failed_definition["direction"],
                    state="VALIDATION FAILED",
                    validation_status="INVALID",
                )
                failed_doc.file.save(filename, ContentFile(file_bytes), save=True)

            return JsonResponse(
                {
                    "success": False,
                    "error": err_msg or "Validation failed",
                    "checks": checks,
                    "version": failed_doc.version if failed_doc else None,
                },
                status=400,
            )

        doc = None
        if file_bytes:
            doc_name = f"Step {step_num}: {step_def.title}"
            doc_type = f"Onboarding Step {step_num}"
            definition = document_definition(doc_type, step_def.title)

            doc = ClientDocument.objects.create(
                client=client_obj,
                document_name=doc_name,
                original_filename=filename,
                document_type=doc_type,
                file_size=len(file_bytes),
                uploaded_by=(request.user.name or request.user.email)
                if request.user and request.user.is_authenticated
                else "Admin User",
                signed_or_sent_at=timezone.now(),
                expiration_date=expiration_date,
                direction=definition["direction"],
                state="EXECUTED" if definition["requires_signature"] else "RECEIVED",
                validation_status="VALID",
            )
            doc.file.save(filename, ContentFile(file_bytes), save=True)

        step_status, _ = ClientStepStatus.objects.get_or_create(
            client=client_obj, step=step_def
        )
        step_status.status = "COMPLETED"
        step_status.save()
        update_client_onboarding_stats(client_obj)

        try:
            actor = "System"
            if request.user and getattr(request.user, "name", ""):
                actor = request.user.name
            elif request.user and getattr(request.user, "email", ""):
                actor = request.user.email
            AuditLog.objects.create(
                module="ONBOARDING",
                action="STEP_UPLOAD",
                details=(
                    f"Step {step_num} ('{step_def.title}') document uploaded for "
                    f"client '{client_obj.name}'. File: {filename}."
                ),
                performed_by=actor,
                client=client_obj,
            )
        except Exception:
            pass

        try:
            from admin_panel.email_service import send_client_email

            subject = f"OneSmarter: File Upload Successful - {filename}"
            html = (
                f"<h3>File Upload Successful</h3>"
                f"<p>The file <b>{filename}</b> was successfully uploaded and "
                f"passed all validations.</p>"
            )
            send_client_email(client_obj, subject, html)
        except Exception as exc:
            logging.getLogger(__name__).error("Failed to send email: %s", exc)

        return JsonResponse(
            {
                "success": True,
                "message": "File uploaded and step completed.",
                "checks": val_res.get("checks", []),
                "version": doc.version if doc else None,
            }
        )

    except (Client.DoesNotExist, OnboardingStepDefinition.DoesNotExist):
        return JsonResponse({"success": False, "error": "Client or onboarding step not found."}, status=404)
    except (ValueError, IndexError):
        return JsonResponse({"success": False, "error": "Invalid step key."}, status=400)
    except Exception as exc:
        logging.getLogger(__name__).exception("Onboarding document upload failed")
        return JsonResponse({"success": False, "error": str(exc)}, status=400)
