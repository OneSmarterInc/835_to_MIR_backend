import os
import shutil
import uuid
import logging
from pathlib import Path
from django.conf import settings
from django.utils import timezone
from django.db import models
from project835.field_crypto import (
    get_sftp_runtime_credentials,
    SFTPCredentialError,
)

from .models import EDI835File
from .mir_persistence import set_mir_push_status, store_mir_file
from .parser import parse_835_to_mir, EDI835Validator
from .mir_exporter import export_mir_file

logger = logging.getLogger("edi835")


def resolve_mir_filename(client=None, fallback_base="MIR", now=None):
    """Resolve the client-facing MIR filename and supported date/time tokens."""
    now = now or timezone.localtime()
    default_format = "MIROUT_YYYY_MMDD_.MIR"
    format_value = (
        getattr(client, "mir_filename_format", None)
        if client
        else None
    ) or default_format

    resolved = os.path.basename(str(format_value).strip())
    replacements = (
        ("YYYY", now.strftime("%Y")),
        ("MM", now.strftime("%m")),
        ("DD", now.strftime("%d")),
        ("hh", now.strftime("%H")),
        ("mm", now.strftime("%M")),
        ("ss", now.strftime("%S")),
    )
    for token, value in replacements:
        resolved = resolved.replace(token, value)

    if not resolved:
        resolved = fallback_base
    if not resolved.lower().endswith(".mir"):
        resolved += ".mir"
    return resolved


def local_mir_filename(client, delivery_filename):
    """Namespace a locally stored MIR by tenant while preserving SFTP naming."""
    client_prefix = str(client.id) if client else "system"
    return f"{client_prefix}_{os.path.basename(delivery_filename)}"


def resolve_sftp_config(client=None, outbound=False):
    """Resolve the correct tenant or global SFTP row for a transfer direction."""
    from .models import SFTPConfig

    def pick(queryset):
        compatible_types = ["OUTBOUND", "UNIFIED"] if outbound else ["INBOUND", "UNIFIED"]
        compatible = queryset.filter(connection_type__in=compatible_types).order_by("-updated_at")
        return compatible.first()

    config = pick(SFTPConfig.objects.filter(client=client)) if client else None
    if config and config.use_default:
        config = None
    return config or pick(SFTPConfig.objects.filter(client__isnull=True))


def get_edi835_storage_dirs():
    base_media = Path(getattr(settings, "MEDIA_ROOT", Path(settings.BASE_DIR) / "media"))
    edi_base = base_media / "edi835"
    dirs = {
        "base": edi_base,
        "input": edi_base / "input",
        "processing": edi_base / "processing",
        "output": edi_base / "output",
        "archive": edi_base / "archive",
        "error": edi_base / "error",
    }
    for d in dirs.values():
        os.makedirs(d, exist_ok=True)
    return dirs


def upload_mir_to_sftp(local_file_path, mir_filename, client=None):
    import logging
    logger = logging.getLogger(__name__)
    cfg = None
    ssh = None
    sftp = None
    try:
        import paramiko
        cfg = resolve_sftp_config(client=client, outbound=True)
        if not cfg:
            logger.warning("upload_mir_to_sftp: No SFTPConfig found in database.")
            return False
        if cfg.status != "CONNECTED":
            logger.warning("upload_mir_to_sftp: SFTPConfig %s is not connected (status=%s).", cfg.id, cfg.status)
            return False
        try:
            credentials = get_sftp_runtime_credentials(cfg, outbound=True)
        except SFTPCredentialError as exc:
            logger.error("upload_mir_to_sftp: %s", exc)
            return False
        out_host = credentials["host"]
        out_port = credentials["port"]
        out_user = credentials["username"]
        out_pass = credentials["password"]
        out_key = credentials["ssh_key"]
        out_auth = credentials["auth_method"]
        out_folder = credentials["remote_folder"]
        trust_unknown_key = credentials["trust_unknown_key"]
        if not out_host or not out_user or not out_folder:
            logger.warning("upload_mir_to_sftp: Missing host, username, or outbound_mir_folder.")
            return False
        local_path = Path(local_file_path)
        if not local_path.is_file():
            raise RuntimeError("The generated MIR file is unavailable in temporary storage. Please retry the conversion.")
        ssh = paramiko.SSHClient()
        if trust_unknown_key:
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        else:
            ssh.load_system_host_keys()
        pkey = None
        if out_auth in ["SSH Key", "SSH Key + Password"]:
            try:
                from .views import parse_ssh_private_key
                pkey, _ = parse_ssh_private_key(out_key, password=out_pass)
            except Exception as pk_err:
                logger.warning(f"upload_mir_to_sftp: Error parsing SSH key: {pk_err}")
        pass_val = out_pass if out_auth in ["Password", "SSH Key + Password"] else None
        ssh.connect(hostname=out_host, port=out_port, username=out_user, password=pass_val, pkey=pkey, timeout=10, banner_timeout=10, auth_timeout=10, look_for_keys=False, allow_agent=False)
        sftp = ssh.open_sftp()
        try:
            sftp.chdir(out_folder)
        except Exception as folder_error:
            raise RuntimeError(f"The Step 7 outbound SFTP folder '{out_folder}' is not accessible: {folder_error}") from folder_error
        try:
            sftp.put(str(local_path), mir_filename)
        except Exception as upload_error:
            raise RuntimeError(f"The SFTP server rejected the upload to '{out_folder}': {upload_error}") from upload_error
        remote_path = f"{out_folder.rstrip('/')}/{mir_filename}"
        if cfg.last_error:
            cfg.last_error = None
            cfg.save(update_fields=["last_error", "updated_at"])
        logger.info(f"upload_mir_to_sftp: Successfully pushed {mir_filename} to remote SFTP outbound folder {remote_path}")
        return True
    except Exception as e:
        public_reason = str(e)
        error_message = f"MIR upload failed for '{mir_filename}': {public_reason}"
        logger.error(error_message, exc_info=True)
        try:
            if cfg:
                cfg.last_error = error_message[:2000]
                cfg.save(update_fields=["last_error", "updated_at"])
        except Exception:
            logger.exception("Could not persist the SFTP upload error")
        return False
    finally:
        if sftp:
            try: sftp.close()
            except Exception: pass
        if ssh:
            try: ssh.close()
            except Exception: pass


def push_file_record_to_sftp(file_id):
    from .models import EDI835File
    try:
        rec = EDI835File.objects.select_related("client").get(id=file_id)
    except (EDI835File.DoesNotExist, ValueError):
        return False, "File record not found in database."
    client = rec.client
    cfg = resolve_sftp_config(client=client, outbound=True)
    if not cfg:
        return False, "No SFTP connection configuration found. Please setup SFTP in Connections section first."
    if cfg.status != "CONNECTED":
        return False, f"SFTP connection is not active (Status: {cfg.status}). Please test and verify your SFTP credentials first."
    dirs = get_edi835_storage_dirs()
    success_mir = False
    stored_name = rec.stored_filename or rec.original_filename
    if rec.output_path:
        base_name = os.path.splitext(stored_name)[0]
        mir_filename = resolve_mir_filename(client=client, fallback_base=base_name)
        mir_path = Path(settings.BASE_DIR) / rec.output_path
        if not os.path.exists(mir_path):
            mir_path = dirs["output"] / f"{base_name}.mir"
        if os.path.exists(mir_path):
            success_mir = upload_mir_to_sftp(mir_path, mir_filename, client=client)
    if success_mir:
        rec.present_in_sftp = True
        rec.save(update_fields=["present_in_sftp"])
        return True, "Successfully pushed MIR file to remote SFTP outbound server!"
    cfg.refresh_from_db(fields=["last_error"])
    return False, (cfg.last_error or "Failed to upload MIR file to SFTP outbound server. Check SFTP credentials and outbound folder path.")


def upload_835_to_sftp(local_file_path, filename):
    return False


def process_edi835_file_content(edi_text, original_filename="uploaded_file.x12", file_id=None, ingestion_source="MANUAL", client=None):
    edi_text = (edi_text or "").lstrip("\ufeff").strip()
    dirs = get_edi835_storage_dirs()
    db_record = None
    file_uuid = uuid.uuid4()
    if file_id:
        try:
            db_record = EDI835File.objects.select_related("client").get(id=file_id)
            file_uuid = db_record.id
            if db_record.client:
                client = db_record.client
        except (EDI835File.DoesNotExist, ValueError):
            db_record = None

    # Always retain the actual uploaded X12 name for traceability, but expose the
    # configured Step 10 name as the canonical conversion filename everywhere the
    # client expects the generated file name.
    original_filename = os.path.basename(original_filename)
    base_name = os.path.splitext(original_filename)[0]
    delivery_mir_filename = resolve_mir_filename(client=client, fallback_base=base_name)
    stored_mir_filename = local_mir_filename(client, delivery_mir_filename)
    stored_filename = f"{file_uuid}_{original_filename}"

    input_file_path = dirs["input"] / stored_filename
    with open(input_file_path, "w", encoding="utf-8") as f:
        f.write(edi_text)
    relative_input_path = (Path("media") / "edi835" / "input" / stored_filename).as_posix()

    if not db_record:
        db_record = EDI835File.objects.create(id=file_uuid, client=client, original_filename=original_filename, stored_filename=stored_filename, status="UPLOADED", input_path=relative_input_path, ingestion_source=ingestion_source)
    else:
        if client: db_record.client = client
        db_record.original_filename = original_filename
        db_record.stored_filename = stored_filename
        db_record.input_path = relative_input_path
        if ingestion_source and ingestion_source != "MANUAL": db_record.ingestion_source = ingestion_source

    processing_file_path = dirs["processing"] / stored_filename
    if os.path.exists(input_file_path): shutil.move(input_file_path, processing_file_path)
    db_record.status = "PROCESSING"
    db_record.processing_started_at = timezone.now()
    db_record.save()

    try:
        client = db_record.client if db_record else None
        res = parse_835_to_mir(edi_text, filename=stored_filename, client=client)
        mir_text = res["text"]
        output_mir_path = Path(export_mir_file(mir_text, dirs["output"], stored_mir_filename))
        rel_output_path = (Path("media") / "edi835" / "output" / output_mir_path.name).as_posix()
        stored_mir = store_mir_file(source_835=db_record, mir_filename=delivery_mir_filename, mir_text=mir_text)
        sftp_uploaded = upload_mir_to_sftp(output_mir_path, delivery_mir_filename, client=client)
        set_mir_push_status(stored_mir, sftp_uploaded)
        archived_835_path = dirs["archive"] / stored_filename
        if os.path.exists(processing_file_path): shutil.move(processing_file_path, archived_835_path)
        rel_archive_path = (Path("media") / "edi835" / "archive" / stored_filename).as_posix()
        db_record.status = "ARCHIVED"
        db_record.output_path = rel_output_path
        db_record.archive_path = rel_archive_path
        db_record.claims_count = res["claims_count"]
        db_record.services_count = res["services_count"]
        db_record.records_count = res["records_count"]
        db_record.error_message = None
        db_record.present_in_sftp = sftp_uploaded
        db_record.present_in_archive_folder = True
        db_record.processing_completed_at = timezone.now()
        db_record.save()
        return {"success": True, "db_record": db_record, "mir_text": mir_text, "claims_count": res["claims_count"], "services_count": res["services_count"], "records_count": res["records_count"], "mir_filename": delivery_mir_filename}
    except Exception as err:
        err_str = str(err)
        logger.exception(f"EDI 835 processing failed for file '{stored_filename}': {err_str}")
        error_file_path = dirs["error"] / stored_filename
        if os.path.exists(processing_file_path): shutil.move(processing_file_path, error_file_path)
        db_record.status = "ERROR"
        db_record.error_message = err_str
        db_record.processing_completed_at = timezone.now()
        db_record.save()
        return {"success": False, "db_record": db_record, "error": err_str}


def process_multiple_edi835_files(files_list, ingestion_source="SFTP", client=None):
    from admin_panel.mir_mapper_logic.edi835_parser import parse_835
    from admin_panel.mir_mapper_logic.mir_generator import generate_mir_text
    dirs = get_edi835_storage_dirs()
    all_claims = []
    file_names = []
    errors = []
    if not files_list:
        return {"success": False, "error": "No files provided for batch conversion."}
    first_archive_rel_path = None
    file_uuid = uuid.uuid4()
    for idx, item in enumerate(files_list):
        fname = item.get("filename") or item.get("original_filename") or f"file_{idx+1}.835"
        fname = os.path.basename(fname)
        file_names.append(fname)
        content = (item.get("content") or item.get("edi_text") or "").lstrip("\ufeff").strip()
        if not content: continue
        stored_fname = f"{file_uuid}_{idx}_{fname}"
        archive_path_file = dirs["archive"] / stored_fname
        with open(archive_path_file, "w", encoding="utf-8") as af: af.write(content)
        rel_archive_path = (Path("media") / "edi835" / "archive" / stored_fname).as_posix()
        if not first_archive_rel_path: first_archive_rel_path = rel_archive_path
        try: all_claims.extend(parse_835(content))
        except Exception as e: errors.append(f"{fname}: {str(e)}")
    if not all_claims:
        return {"success": False, "error": "No CLP claim segments could be parsed from any of the provided 835 files.", "errors": errors}
    mir_text, mir_res = generate_mir_text(all_claims, client=client)
    first_base_name = os.path.splitext(file_names[0])[0] if file_names else "batch"
    combined_base_name = f"MIR_COMBINED_{first_base_name}" if len(file_names) > 1 else f"MIR_{first_base_name}"
    delivery_mir_filename = resolve_mir_filename(client=client, fallback_base=combined_base_name)
    stored_mir_filename = local_mir_filename(client, delivery_mir_filename)
    output_mir_path = Path(export_mir_file(mir_text, dirs["output"], stored_mir_filename))
    rel_output_path = (Path("media") / "edi835" / "output" / output_mir_path.name).as_posix()
    combined_inputs_str = ", ".join(file_names)
    claims_count = mir_res.get("claims", 0) if isinstance(mir_res, dict) else getattr(mir_res, "get", lambda k, d: 0)("claims", 0)
    services_count = mir_res.get("services", 0) if isinstance(mir_res, dict) else getattr(mir_res, "get", lambda k, d: 0)("services", 0)
    records_count = mir_res.get("mir_records", 0) if isinstance(mir_res, dict) else getattr(mir_res, "get", lambda k, d: 0)("mir_records", 0)
    db_rec = EDI835File.objects.create(id=file_uuid, client=client, original_filename=combined_inputs_str, stored_filename=file_names[0] if file_names else "batch.835", status="ARCHIVED", claims_count=claims_count, services_count=services_count, records_count=records_count, output_path=rel_output_path, archive_path=first_archive_rel_path, present_in_sftp=False, present_in_archive_folder=True, ingestion_source=ingestion_source, processing_completed_at=timezone.now())
    stored_mir = store_mir_file(source_835=db_rec, mir_filename=delivery_mir_filename, mir_text=mir_text)
    sftp_uploaded = upload_mir_to_sftp(output_mir_path, delivery_mir_filename, client=client)
    set_mir_push_status(stored_mir, sftp_uploaded)
    if sftp_uploaded:
        db_rec.present_in_sftp = True
        db_rec.save(update_fields=["present_in_sftp"])
    return {"success": True, "mir_text": mir_text, "combined_filename": delivery_mir_filename, "stored_filename": stored_mir_filename, "files_count": len(file_names), "claims_count": claims_count, "services_count": services_count, "records_count": records_count, "sftp_uploaded": sftp_uploaded, "db_record": db_rec, "errors": errors}


def sync_folder_observer():
    dirs = get_edi835_storage_dirs()
    input_dir = dirs["input"]
    archive_dir = dirs["archive"]
    if os.path.exists(input_dir):
        untracked_fnames = [fname for fname in os.listdir(input_dir) if os.path.isfile(input_dir / fname)]
        if untracked_fnames:
            existing_names = set()
            for orig, stored in EDI835File.objects.filter(models.Q(original_filename__in=untracked_fnames) | models.Q(stored_filename__in=untracked_fnames)).values_list("original_filename", "stored_filename"):
                if orig: existing_names.add(orig)
                if stored: existing_names.add(stored)
            for fname in untracked_fnames:
                if fname in existing_names: continue
                rel_input_path = (Path("media") / "edi835" / "input" / fname).as_posix()
                EDI835File.objects.create(original_filename=fname, stored_filename=fname, status="UPLOADED", input_path=rel_input_path, present_in_sftp=True, present_in_archive_folder=False, ingestion_source="SFTP")
    to_update = []
    for r in EDI835File.objects.all().iterator():
        in_sftp = r.present_in_sftp
        in_archive = False
        if r.stored_filename and os.path.exists(archive_dir / r.stored_filename): in_archive = True
        elif r.original_filename and os.path.exists(archive_dir / r.original_filename): in_archive = True
        elif r.archive_path and os.path.exists(Path(settings.BASE_DIR) / r.archive_path): in_archive = True
        if r.present_in_sftp != in_sftp or r.present_in_archive_folder != in_archive:
            r.present_in_sftp = in_sftp
            r.present_in_archive_folder = in_archive
            to_update.append(r)
    if to_update:
        EDI835File.objects.bulk_update(to_update, ["present_in_sftp", "present_in_archive_folder"])
