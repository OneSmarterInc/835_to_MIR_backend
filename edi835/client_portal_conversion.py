"""Explicit Client User conversion using the existing validated tenant pipeline.

The standard processor pushes a generated MIR to outbound SFTP when configured.
Never trigger it for a Client User without affirmative send authorization.
"""
import logging
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from project835.decorators import authenticated_api_required
from .client_portal_views import only_client_user
from .file_types import has_valid_file_extension, file_extension_error
from .services import process_edi835_file_content


@csrf_exempt
@authenticated_api_required
def convert(request):
    client, denied = only_client_user(request)
    if denied:
        return denied
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "POST only"}, status=405)
    if request.POST.get("confirm_send") != "yes":
        return JsonResponse({
            "success": False,
            "error": "Confirm that conversion may transmit the generated MIR to the configured outbound SFTP.",
        }, status=400)
    upload = request.FILES.get("edi_file")
    if not upload:
        return JsonResponse({"success": False, "error": "Choose an 835 file."}, status=400)
    if upload.size > 10 * 1024 * 1024:
        return JsonResponse({"success": False, "error": "Maximum upload size is 10 MB."}, status=400)
    if not has_valid_file_extension(upload.name, "835"):
        return JsonResponse({"success": False, "error": file_extension_error("835")}, status=400)
    try:
        raw = upload.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        return JsonResponse({"success": False, "error": "835 file is not valid UTF-8 text."}, status=400)
    if not raw.strip():
        return JsonResponse({"success": False, "error": "File is empty."}, status=400)
    if client.stage == "offboarded":
        return JsonResponse({"success": False, "error": "Client is offboarded."}, status=403)
    result = process_edi835_file_content(
        raw, original_filename=upload.name, client=client, ingestion_source="MANUAL",
    )
    source = result.get("db_record")
    mir = getattr(source, "mir_file", None) if source else None
    if not result.get("success"):
        return JsonResponse({
            "success": False,
            "error": ("All claims are held for review." if result.get("code") == "ALL_CLAIMS_HELD"
                      else "Conversion failed. Ask your Client Admin to review the result."),
            "file_id": str(source.pk) if source else None,
            "held_count": source.held_claims_count if source else 0,
        }, status=409 if result.get("code") == "ALL_CLAIMS_HELD" else 400)
    # The processor records actual delivery status. Never imply SFTP success
    # when no outbound connection exists or the remote transfer fails.
    pushed = mir is not None and mir.status == "PUSHED"
    return JsonResponse({
        "success": True,
        "file_id": str(source.pk),
        "source_file": source.original_filename,
        "mir_filename": mir.mir_filename if mir else None,
        "mir_status": mir.status if mir else "NOT_CREATED",
        "sent": pushed,
        "claims_converted": int(source.delivered_claims_count),
        "claims_held": int(source.held_claims_count),
        "message": ("Converted and sent to the configured outbound SFTP." if pushed
                    else "Converted; MIR was not confirmed delivered. Client Admin should review SFTP."),
    })
