import io
import json
import os
import posixpath
import stat
import uuid

from django.conf import settings
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from accounts.models import Client
from project835.decorators import authenticated_api_required, json_api_errors
from project835.field_crypto import SFTPCredentialError, get_sftp_runtime_credentials

from .claim_numbers import split_claim_number
from .edi837_service import export_single_claim, ingest_837
from .file_types import has_valid_file_extension
from .models import EDI837Claim, EDI837File, MIRClaim, RECONClaim
from .services import resolve_sftp_config


def _client_for_request(request, supplied_id=None):
    if getattr(request.user, "client_id", None):
        return request.user.client
    if not request.user.is_staff or not supplied_id:
        return None
    try:
        client = Client.objects.get(id=supplied_id)
    except (Client.DoesNotExist, ValueError):
        return None
    if request.user.is_superuser:
        return client
    from admin_panel.access_control import has_active_client_grant
    return client if has_active_client_grant(request.user, client.id) else None


def _visible_claim(request, claim_id):
    queryset = EDI837Claim.objects.select_related("edi_file", "client")
    if getattr(request.user, "client_id", None):
        queryset = queryset.filter(client_id=request.user.client_id)
    elif not request.user.is_staff:
        return None
    elif not request.user.is_superuser:
        visible = request.user.client_access_grants.filter(
            revoked_at__isnull=True, expires_at__gt=timezone.now()
        ).values_list("client_id", flat=True)
        queryset = queryset.filter(client_id__in=visible)
    try:
        return queryset.get(id=claim_id)
    except (EDI837Claim.DoesNotExist, ValueError):
        return None


def _claim_row(claim):
    patient = " ".join(part for part in (claim.patient_first_name, claim.patient_last_name) if part)
    identifiers = split_claim_number(claim.claim_control_number)
    identifiers["highmark_claim_number"] = (
        claim.highmark_claim_number or identifiers["highmark_claim_number"]
    )
    identifiers["internal_claim_number"] = (
        claim.reference_9c or claim.internal_claim_number or identifiers["internal_claim_number"]
    )
    return {
        "id": claim.id,
        "claim_number": claim.claim_control_number,
        **identifiers,
        "reference_9c": claim.reference_9c,
        "patient_control_number": claim.patient_control_number,
        "member_id": claim.member_id,
        "patient_name": patient,
        "total_charge_amount": str(claim.total_charge_amount),
        "service_count": claim.service_count,
        "service_from_date": claim.service_from_date,
        "service_to_date": claim.service_to_date,
        "file_name": claim.edi_file.original_filename,
        "processed_at": claim.edi_file.processed_at.isoformat() if claim.edi_file.processed_at else None,
        "import_mode": claim.edi_file.import_mode,
    }


def _claim_lifecycle(claim):
    identifiers = {str(value or "").strip() for value in (
        claim.claim_control_number, claim.highmark_claim_number,
        claim.internal_claim_number, claim.reference_9c, claim.patient_control_number,
    ) if str(value or "").strip()}
    lookup = Q()
    for identifier in identifiers:
        lookup |= Q(claim_control_number__iexact=identifier)
    highmark = str(claim.highmark_claim_number or "").strip()
    if not highmark:
        highmark = split_claim_number(claim.claim_control_number)["highmark_claim_number"]
    # MIR and RECON commonly persist the same Highmark number with the legacy
    # internal suffix appended. Match that combined representation as well as
    # the separated 837 columns.
    if highmark:
        lookup |= Q(claim_control_number__istartswith=highmark)
    mir = recon = None
    if lookup:
        mir = (MIRClaim.objects.select_related("mir_file", "mir_file__source_835")
               .filter(lookup, mir_file__client=claim.client)
               .order_by("mir_file__converted_at").first())
        recon = (RECONClaim.objects.select_related("recon_file")
                 .filter(lookup, client=claim.client, recon_file__file_kind="RECON")
                 .order_by("recon_file__uploaded_at").first())
    source_835 = mir.mir_file.source_835 if mir else None
    return {
        "835": {
            "exists": bool(source_835),
            "arrived_at": source_835.uploaded_at.isoformat() if source_835 else None,
            "file_name": source_835.original_filename if source_835 else "",
            "status": source_835.status if source_835 else "",
            "source": source_835.ingestion_source if source_835 else "",
        },
        "mir": {"exists": bool(mir), "arrived_at": mir.mir_file.converted_at.isoformat() if mir else None,
                "file_name": mir.mir_file.mir_filename if mir else ""},
        "recon": {"exists": bool(recon), "arrived_at": recon.recon_file.uploaded_at.isoformat() if recon else None,
                  "file_name": recon.recon_file.original_filename if recon else ""},
    }


def _load_private_key(paramiko, key_text, password=None):
    if not key_text:
        return None
    last_error = None
    key_classes = [paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey]
    dss_key = getattr(paramiko, "DSSKey", None)
    if dss_key:
        key_classes.append(dss_key)
    for key_class in key_classes:
        try:
            return key_class.from_private_key(io.StringIO(key_text), password=password or None)
        except Exception as exc:
            last_error = exc
    raise ValueError(f"Unable to load the configured SFTP private key: {last_error}")


def _safe_837_filename(value, fallback="837.837"):
    raw = os.path.basename(str(value or "").strip())
    if not raw:
        return fallback
    safe = "".join(char if char.isalnum() or char in "-_." else "_" for char in raw)
    if not safe:
        return fallback
    if not safe.lower().endswith(".837"):
        safe = f"{safe}.837"
    return safe[:120]


@csrf_exempt
@authenticated_api_required
@json_api_errors
def edi837_upload_process(request):
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Only POST is allowed."}, status=405)
    client = _client_for_request(request, request.POST.get("client_id"))
    if client is None:
        return JsonResponse({"success": False, "error": "Select an authorized client."}, status=400)
    uploads = request.FILES.getlist("files") or request.FILES.getlist("file")
    if not uploads:
        return JsonResponse({"success": False, "error": "Select one or more 837 files."}, status=400)
    max_bytes = getattr(settings, "EDI837_MAX_UPLOAD_BYTES", 100 * 1024 * 1024)
    from .edi837_naming_views import get_saved_837_filename_format, resolve_837_filename_format
    resolved_base = resolve_837_filename_format(get_saved_837_filename_format(client))
    stem, extension = os.path.splitext(resolved_base)
    multiple = len(uploads) > 1
    results, errors = [], []
    for index, upload in enumerate(uploads, start=1):
        try:
            if not has_valid_file_extension(upload.name, "837"):
                raise ValueError("Unsupported file extension.")
            if upload.size > max_bytes:
                raise ValueError("File exceeds the 100 MB limit.")
            storage_name = f"{stem}_{index:03d}{extension}" if multiple else resolved_base
            edi_file, duplicate = ingest_837(
                client, request.user, upload.name, upload.read(),
                import_mode="MANUAL", storage_filename=storage_name,
            )
            results.append({"id": str(edi_file.id), "name": edi_file.original_filename,
                            "stored_name": edi_file.stored_filename,
                            "status": edi_file.status, "already_exists": duplicate,
                            "claim_count": edi_file.claim_count, "service_count": edi_file.service_count,
                            "total_charge_amount": str(edi_file.total_charge_amount)})
        except Exception as exc:
            errors.append({"name": os.path.basename(upload.name), "error": str(exc)})
    return JsonResponse({
        "success": bool(results), "files": results, "errors": errors,
        "processed_count": sum(1 for item in results if not item["already_exists"]),
        "duplicate_count": sum(1 for item in results if item["already_exists"]),
        "failed_count": len(errors),
        "error": "No 837 files could be processed." if not results else "",
    }, status=200 if results else 400)


@csrf_exempt
@authenticated_api_required
@json_api_errors
def edi837_sftp_batch_rename(request):
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Only POST is allowed."}, status=405)
    if not request.user.is_staff:
        return JsonResponse({"success": False, "error": "Administrator access is required."}, status=403)

    try:
        body = json.loads(request.body.decode("utf-8")) if request.body else {}
    except (TypeError, ValueError, UnicodeDecodeError):
        return JsonResponse({"success": False, "error": "Invalid JSON request."}, status=400)

    client = _client_for_request(request, body.get("client_id"))
    if client is None:
        return JsonResponse({"success": False, "error": "Select an authorized client."}, status=400)
    if str(client.stage or "").lower() == "offboarded":
        return JsonResponse({"success": False, "error": "This client is offboarded; SFTP changes are locked."}, status=409)

    filename = _safe_837_filename(body.get("filename"), fallback="")
    if not filename:
        return JsonResponse({"success": False, "error": "A valid 837 filename is required."}, status=400)
    stem, extension = os.path.splitext(filename)
    if not stem:
        return JsonResponse({"success": False, "error": "Enter a valid filename before renaming 837 files."}, status=400)

    config = resolve_sftp_config(client=client, outbound=False, purpose="837_IN")
    if not config:
        return JsonResponse({"success": False, "error": "No 837 inbound SFTP configuration is available for this client."}, status=400)
    if config.status != "CONNECTED":
        return JsonResponse({"success": False, "error": "The selected client's inbound SFTP connection is not connected."}, status=400)
    remote_folder = str(config.inbound_837_folder or "").strip()
    if not remote_folder:
        return JsonResponse({"success": False, "error": "The inbound 837 SFTP folder is not configured."}, status=400)

    try:
        credentials = get_sftp_runtime_credentials(config, outbound=False)
    except SFTPCredentialError as exc:
        return JsonResponse({"success": False, "error": str(exc)}, status=400)

    import paramiko

    ssh = paramiko.SSHClient()
    if credentials.get("trust_unknown_key"):
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    else:
        ssh.load_system_host_keys()

    auth_method = credentials.get("auth_method")
    password = credentials.get("password")
    pkey = None
    if auth_method in ["SSH Key", "SSH Key + Password"]:
        pkey = _load_private_key(paramiko, credentials.get("ssh_key"), password=password)
    pass_val = password if auth_method in ["Password", "SSH Key + Password"] else None

    sftp = None
    try:
        ssh.connect(
            hostname=credentials.get("host"),
            port=int(credentials.get("port") or 22),
            username=credentials.get("username"),
            password=pass_val,
            pkey=pkey,
            timeout=10,
            banner_timeout=10,
            auth_timeout=10,
            look_for_keys=False,
            allow_agent=False,
        )
        sftp = ssh.open_sftp()
        try:
            resolved_folder = sftp.normalize(remote_folder)
        except Exception:
            resolved_folder = remote_folder

        entries = sftp.listdir_attr(resolved_folder)
        existing_names = {entry.filename for entry in entries if not stat.S_ISDIR(entry.st_mode)}
        candidates = sorted(
            entry.filename for entry in entries
            if not stat.S_ISDIR(entry.st_mode)
            and not entry.filename.startswith(".")
            and has_valid_file_extension(entry.filename, "837")
        )
        if not candidates:
            return JsonResponse({"success": True, "renamed_count": 0, "renamed": [], "filename": filename,
                                 "message": "No valid 837 files were found to rename."})

        rename_plan = []
        target_names = set()
        multiple = len(candidates) > 1
        for index, old_name in enumerate(candidates, start=1):
            new_name = f"{stem}_{index:03d}{extension}" if multiple else filename
            if new_name in target_names:
                return JsonResponse({"success": False, "error": f"The rename plan would create duplicate filename {new_name}."}, status=409)
            if new_name in existing_names and new_name not in candidates:
                return JsonResponse({"success": False, "error": f"Cannot rename because {new_name} already exists in the SFTP folder."}, status=409)
            target_names.add(new_name)
            rename_plan.append((old_name, new_name))

        temporary_plan = []
        for index, (old_name, new_name) in enumerate(rename_plan, start=1):
            temp_name = f".__837rename_{timezone.now().strftime('%Y%m%d%H%M%S%f')}_{index}"
            old_path = posixpath.join(resolved_folder, old_name)
            temp_path = posixpath.join(resolved_folder, temp_name)
            sftp.rename(old_path, temp_path)
            temporary_plan.append((temp_name, new_name, old_name))

        renamed = []
        try:
            for temp_name, new_name, old_name in temporary_plan:
                sftp.rename(posixpath.join(resolved_folder, temp_name), posixpath.join(resolved_folder, new_name))
                renamed.append({"from": old_name, "to": new_name})
        except Exception:
            completed_targets = {item["to"] for item in renamed}
            for temp_name, new_name, old_name in temporary_plan:
                if new_name in completed_targets:
                    continue
                try:
                    sftp.rename(posixpath.join(resolved_folder, temp_name), posixpath.join(resolved_folder, old_name))
                except Exception:
                    pass
            raise

        message = f"Renamed {len(renamed)} 837 file(s) in the selected client's SFTP folder using {filename}."
        if multiple:
            message += " Numbered suffixes were added because more than one 837 file was present."
        return JsonResponse({
            "success": True,
            "renamed_count": len(renamed),
            "renamed": renamed,
            "filename": filename,
            "message": message,
        })
    except Exception as exc:
        return JsonResponse({"success": False, "error": f"837 SFTP rename failed: {exc}"}, status=400)
    finally:
        if sftp:
            try:
                sftp.close()
            except Exception:
                pass
        try:
            ssh.close()
        except Exception:
            pass


@authenticated_api_required
@json_api_errors
def edi837_search(request):
    if request.method != "GET":
        return JsonResponse({"success": False, "error": "Only GET is allowed."}, status=405)
    client = _client_for_request(request, request.GET.get("client_id"))
    if client is None:
        return JsonResponse({"success": False, "error": "Select an authorized client."}, status=400)
    query = str(request.GET.get("q") or "").strip()
    claims = EDI837Claim.objects.filter(client=client).select_related("edi_file")
    if query:
        claims = claims.filter(
            Q(claim_control_number__icontains=query) | Q(highmark_claim_number__icontains=query)
            | Q(internal_claim_number__icontains=query) | Q(patient_control_number__icontains=query)
            | Q(reference_9c__icontains=query)
        )
    else:
        claims = claims.none()
    try:
        limit = min(200, max(1, int(request.GET.get("limit", "50"))))
    except ValueError:
        limit = 50
    rows = [_claim_row(claim) for claim in claims.order_by("claim_control_number", "-edi_file__processed_at")[:limit]]
    return JsonResponse({"success": True, "query": query, "count": len(rows), "results": rows})


@authenticated_api_required
@json_api_errors
def edi837_files(request):
    if request.method != "GET":
        return JsonResponse({"success": False, "error": "Only GET is allowed."}, status=405)
    client = _client_for_request(request, request.GET.get("client_id"))
    if client is None:
        return JsonResponse({"success": False, "error": "Select an authorized client."}, status=400)
    query = str(request.GET.get("q") or "").strip()
    files = EDI837File.objects.filter(client=client)
    if query:
        status_query = query.upper().replace(" ", "_")
        source_query = query.upper()
        filters = Q(original_filename__icontains=query) | Q(stored_filename__icontains=query)
        if status_query in {choice[0] for choice in EDI837File.STATUS_CHOICES}:
            filters |= Q(status=status_query)
        if source_query in {choice[0] for choice in EDI837File.IMPORT_MODE_CHOICES}:
            filters |= Q(import_mode=source_query)
        if "NOT PUSHED" in source_query or "NOT_PUSHED" in status_query:
            filters |= Q(outbound_path="")
        elif "OUTBOUND" in source_query or "PUSHED" in source_query:
            filters |= ~Q(outbound_path="")
        files = files.filter(filters)
    try:
        page_number = max(1, int(request.GET.get("page", "1")))
        page_size = min(100, max(10, int(request.GET.get("page_size", "20"))))
    except ValueError:
        page_number, page_size = 1, 20
    paginator = Paginator(files.order_by("-uploaded_at"), page_size)
    page = paginator.get_page(page_number)
    rows = [{
        "id": str(item.id), "file_name": item.original_filename,
        "status": item.status, "inbound_source": item.get_import_mode_display(),
        "inbound_status": "Received", "outbound_status": "Pushed" if item.outbound_path else "Not pushed",
        "outbound_ready": bool(item.outbound_path), "claim_count": item.claim_count,
        "service_count": item.service_count, "total_charge_amount": str(item.total_charge_amount),
        "uploaded_at": item.uploaded_at.isoformat(),
        "processed_at": item.processed_at.isoformat() if item.processed_at else None,
    } for item in page.object_list]
    return JsonResponse({
        "success": True, "results": rows, "count": paginator.count,
        "page": page.number, "page_size": page_size, "pages": paginator.num_pages,
        "has_previous": page.has_previous(), "has_next": page.has_next(),
    })


@authenticated_api_required
@json_api_errors
def edi837_claim_detail(request, claim_id):
    if request.method != "GET":
        return JsonResponse({"success": False, "error": "Only GET is allowed."}, status=405)
    claim = _visible_claim(request, claim_id)
    if claim is None:
        return JsonResponse({"success": False, "error": "837 claim was not found."}, status=404)
    services = [{
        "sequence": line.service_sequence, "procedure_code": line.procedure_code,
        "qualifier": line.procedure_qualifier, "modifiers": line.modifiers,
        "revenue_code": line.revenue_code, "service_from_date": line.service_from_date,
        "service_to_date": line.service_to_date, "units": str(line.units),
        "charge_amount": str(line.charge_amount), "diagnosis_pointers": line.diagnosis_pointers,
    } for line in claim.service_lines.all()]
    return JsonResponse({"success": True, "claim": {
        **_claim_row(claim), "client_name": claim.client.name,
        "subscriber_name": " ".join(part for part in (claim.subscriber_first_name, claim.subscriber_last_name) if part),
        "billing_provider": claim.billing_provider_name, "rendering_provider": claim.rendering_provider_name,
        "referring_provider": claim.referring_provider_name,
        "payer": claim.payer_name, "claim_type": claim.claim_type,
        "place_of_service": claim.place_of_service, "claim_frequency_code": claim.claim_frequency_code,
        "original_claim_number": claim.original_claim_number, "diagnosis_codes": claim.diagnosis_codes,
        "services": services, "lifecycle": _claim_lifecycle(claim),
    }})


@authenticated_api_required
@json_api_errors
def edi837_claim_export(request, claim_id):
    if request.method != "GET":
        return JsonResponse({"success": False, "error": "Only GET is allowed."}, status=405)
    claim = _visible_claim(request, claim_id)
    if claim is None:
        return JsonResponse({"success": False, "error": "837 claim was not found."}, status=404)
    content = export_single_claim(claim)
    requested_filename = str(request.GET.get("filename") or "").strip()
    if requested_filename:
        filename = _safe_837_filename(requested_filename)
    else:
        safe_claim = "".join(char for char in claim.claim_control_number if char.isalnum() or char in "-_") or str(claim.id)
        filename = f"837_{safe_claim}.837"
    response = HttpResponse(content, content_type="application/edi-x12; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["X-OneSmarter-Filename"] = filename
    return response


@csrf_exempt
@authenticated_api_required
@json_api_errors
def edi837_claim_push_sftp(request, claim_id):
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Only POST is allowed."}, status=405)
    claim = _visible_claim(request, claim_id)
    if claim is None:
        return JsonResponse({"success": False, "error": "837 claim was not found."}, status=404)
    if str(claim.client.stage or "").lower() == "offboarded":
        return JsonResponse({"success": False, "error": "This client is offboarded; SFTP transfers are locked."}, status=409)
    try:
        body = json.loads(request.body.decode("utf-8")) if request.body else {}
    except (TypeError, ValueError, UnicodeDecodeError):
        return JsonResponse({"success": False, "error": "Invalid JSON request."}, status=400)
    filename = _safe_837_filename(body.get("filename"), fallback=f"837_{claim.claim_control_number or claim.id}.837")
    config = resolve_sftp_config(client=claim.client, outbound=True, purpose="837_OUT")
    if not config:
        return JsonResponse({"success": False, "error": "No 837 outbound SFTP configuration is available for this client."}, status=400)
    if config.status != "CONNECTED":
        return JsonResponse({"success": False, "error": "The selected client's 837 outbound SFTP connection is not connected."}, status=400)
    try:
        credentials = get_sftp_runtime_credentials(config, outbound=True)
    except SFTPCredentialError as exc:
        return JsonResponse({"success": False, "error": str(exc)}, status=400)
    remote_folder = str(credentials.get("remote_folder") or "").strip()
    if not remote_folder:
        return JsonResponse({"success": False, "error": "The 837 outbound SFTP folder is not configured."}, status=400)
    import paramiko
    ssh = paramiko.SSHClient()
    if credentials.get("trust_unknown_key"):
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    else:
        ssh.load_system_host_keys()
    auth_method, password = credentials.get("auth_method"), credentials.get("password")
    pkey = _load_private_key(paramiko, credentials.get("ssh_key"), password=password) if auth_method in ["SSH Key", "SSH Key + Password"] else None
    pass_val = password if auth_method in ["Password", "SSH Key + Password"] else None
    sftp, temporary_path = None, ""
    try:
        ssh.connect(hostname=credentials.get("host"), port=int(credentials.get("port") or 22),
                    username=credentials.get("username"), password=pass_val, pkey=pkey,
                    timeout=10, banner_timeout=10, auth_timeout=10, look_for_keys=False, allow_agent=False)
        sftp = ssh.open_sftp()
        try:
            resolved_folder = sftp.normalize(remote_folder)
        except Exception:
            resolved_folder = remote_folder
        target_path = posixpath.join(resolved_folder, filename)
        try:
            sftp.stat(target_path)
        except FileNotFoundError:
            pass
        else:
            return JsonResponse({"success": False, "error": f"{filename} already exists in the 837 outbound folder."}, status=409)
        temporary_path = posixpath.join(resolved_folder, f".{filename}.{uuid.uuid4().hex}.uploading")
        payload = export_single_claim(claim).encode("utf-8")
        sftp.putfo(io.BytesIO(payload), temporary_path, file_size=len(payload), confirm=True)
        sftp.rename(temporary_path, target_path)
        temporary_path = ""
        return JsonResponse({"success": True, "filename": filename, "remote_path": target_path,
                             "pushed_at": timezone.now().isoformat(),
                             "message": f"{filename} was pushed to the client's 837 outbound SFTP folder."})
    finally:
        if sftp:
            if temporary_path:
                try:
                    sftp.remove(temporary_path)
                except Exception:
                    pass
            sftp.close()
        ssh.close()
