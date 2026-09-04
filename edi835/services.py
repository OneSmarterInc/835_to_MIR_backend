import os
import json
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
from .file_types import file_extension_error, has_valid_file_extension
from .mir_persistence import set_mir_push_status, store_mir_file
from .parser import parse_835_to_mir, EDI835Validator
from .mir_exporter import export_mir_file
from .storage import (
    archive_inbound,
    client_storage_dirs,
    relative_media_path,
    remove_delivered_outbound,
    stage_inbound,
    write_mir_copies,
)

logger = logging.getLogger("edi835")


def validate_835_content(edi_text):
    """Run the authoritative validation gate used by every 835 ingestion path."""
    report = EDI835Validator().validate(edi_text)
    is_valid = report.get("valid", report.get("is_valid", False))
    return is_valid, report


def normalize_mir_generation_result(generated, claims):
    """Normalize supported MIR generator contracts without unpacking text."""
    claims = list(claims or [])
    fallback_summary = {
        "claims": len(claims),
        "services": sum(len(getattr(claim, "services", None) or []) for claim in claims),
    }

    if isinstance(generated, tuple) and len(generated) == 2:
        mir_text, summary = generated
    elif isinstance(generated, dict):
        mir_text = generated.get("text", "")
        summary = {
            "claims": generated.get("claims", generated.get("claims_count", fallback_summary["claims"])),
            "services": generated.get("services", generated.get("services_count", fallback_summary["services"])),
            "mir_records": generated.get("mir_records", generated.get("records_count")),
        }
        for key in ("delivered_claims", "delivered_services", "held_claims", "findings", "split_claims"):
            if key in generated:
                summary[key] = generated[key]
    elif isinstance(generated, str):
        mir_text = generated
        summary = fallback_summary
    else:
        raise TypeError("MIR generator returned an unsupported result format.")

    if not isinstance(mir_text, str):
        raise TypeError("MIR generator output text must be a string.")
    summary = summary if isinstance(summary, dict) else fallback_summary
    summary.setdefault("claims", fallback_summary["claims"])
    summary.setdefault("services", fallback_summary["services"])
    summary.setdefault("mir_records", len([line for line in mir_text.splitlines() if line.strip()]))
    return mir_text, summary


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


def unique_mir_filename(delivery_filename, file_uuid):
    """Return a collision-free local and outbound name for one conversion."""
    safe_name = os.path.basename(delivery_filename)
    stem, suffix = os.path.splitext(safe_name)
    return f"{stem}_{str(file_uuid).replace('-', '')[:12]}{suffix or '.MIR'}"


def resolve_sftp_config(client=None, outbound=False):
    """Resolve the correct tenant or global SFTP row for a transfer direction."""
    from .models import SFTPConfig

    def pick(queryset):
        compatible_types = ["OUTBOUND", "UNIFIED"] if outbound else ["INBOUND", "UNIFIED"]
        compatible = queryset.filter(connection_type__in=compatible_types).order_by("-updated_at")
        # The latest compatible client record is authoritative regardless of
        # whether an admin or client user saved it. Never silently fall back to
        # an older connection merely because it is still marked CONNECTED.
        return compatible.first()

    config = pick(SFTPConfig.objects.filter(client=client)) if client else None
    if config and config.use_default:
        config = None
    return config or pick(SFTPConfig.objects.filter(client__isnull=True))



def get_edi835_storage_dirs(client=None):
    """
    Returns compatibility aliases plus the complete client-scoped media tree.
    """
    scoped = client_storage_dirs(client)
    return {
        "base": scoped["root"] / "files",
        "input": scoped["835_in"],
        "processing": scoped["835_in"],
        "output": scoped["mir_out"],
        "archive": scoped["835_archive"],
        "error": scoped["835_archive"],
        "mir_archive": scoped["mir_archive"],
        **scoped,
    }


def upload_mir_to_sftp(local_file_path, mir_filename, client=None, sftp_config=None):
    """
    Uploads converted .mir file directly to the configured remote SFTP outbound folder.
    """
    import logging
    logger = logging.getLogger(__name__)

    cfg = None
    ssh = None
    sftp = None
    try:
        import paramiko

        # The batch endpoint may already have resolved and validated the exact
        # outbound configuration for the selected client. Reuse it so the
        # upload cannot accidentally resolve a different/global config.
        cfg = sftp_config or resolve_sftp_config(client=client, outbound=True)

        if not cfg:
            logger.warning("upload_mir_to_sftp: No SFTPConfig found in database.")
            return False

        if cfg.status != "CONNECTED":
            logger.warning(
                "upload_mir_to_sftp: SFTPConfig %s is not connected (status=%s).",
                cfg.id,
                cfg.status,
            )
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
            raise RuntimeError(
                "The generated MIR file is unavailable in temporary storage. "
                "Please retry the conversion."
            )

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

        ssh.connect(
            hostname=out_host,
            port=out_port,
            username=out_user,
            password=pass_val,
            pkey=pkey,
            timeout=10,
            banner_timeout=10,
            auth_timeout=10,
            look_for_keys=False,
            allow_agent=False,
        )
        sftp = ssh.open_sftp()

        # Enter the exact outbound directory selected in Step 7 and upload the
        # MIR by basename. This avoids joining server-specific absolute/chroot
        # paths on the Django host.
        try:
            sftp.chdir(out_folder)
        except Exception as folder_error:
            raise RuntimeError(
                f"The Step 7 outbound SFTP folder '{out_folder}' is not accessible: "
                f"{folder_error}"
            ) from folder_error

        try:
            sftp.put(str(local_path), mir_filename)
        except Exception as upload_error:
            raise RuntimeError(
                f"The SFTP server rejected the upload to '{out_folder}': "
                f"{upload_error}"
            ) from upload_error

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
            try:
                sftp.close()
            except Exception:
                pass
        if ssh:
            try:
                ssh.close()
            except Exception:
                pass


def push_file_record_to_sftp(file_id):
    """
    Pushes converted MIR file for a specific record ID to SFTP outbound folder.
    Returns (success_boolean, message_string).
    """
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

    dirs = get_edi835_storage_dirs(client)
    success_mir = False

    # Push MIR file to SFTP MIR outbound folder ONLY
    stored_name = rec.stored_filename or rec.original_filename
    if rec.output_path:
        base_name = os.path.splitext(stored_name)[0]
        # Resolve mir_filename dynamically
        mir_filename = resolve_mir_filename(client=client, fallback_base=base_name)
        archived_path = Path(settings.BASE_DIR) / rec.output_path
        if not archived_path.exists() and getattr(rec, "mir_file", None):
            archived_path, _ = write_mir_copies(
                client, local_mir_filename(client, rec.mir_file.mir_filename),
                rec.mir_file.file_content,
            )
        if archived_path.exists():
            out_path = dirs["mir_out"] / archived_path.name
            shutil.copy2(archived_path, out_path)
            success_mir = upload_mir_to_sftp(out_path, mir_filename, client=client)
            if success_mir:
                remove_delivered_outbound(client, "mir", out_path)

    if success_mir:
        rec.present_in_sftp = True
        rec.save(update_fields=["present_in_sftp"])
        return True, "Successfully pushed MIR file to remote SFTP outbound server!"

    cfg.refresh_from_db(fields=["last_error"])
    return False, (cfg.last_error or "Failed to upload MIR file to SFTP outbound server. Check SFTP credentials and outbound folder path.")


def upload_835_to_sftp(local_file_path, filename):
    """
    Inbound SFTP folder is strictly for receiving 835 files.
    We do NOT push 835 files to inbound SFTP folder.
    """
    return False


def process_edi835_file_content(edi_text, original_filename="uploaded_file.x12", file_id=None, ingestion_source="MANUAL", client=None):
    if not has_valid_file_extension(original_filename, "835"):
        return {"success": False, "error": file_extension_error("835"), "db_record": None}
    edi_text = (edi_text or "").lstrip("\ufeff").strip()
    """
    Processes EDI 835 content through the complete pipeline when 'Submit & Convert to MIR' is triggered:
    1. Save to the client's 835/in folder.
    2. Process it and move it to 835/archive (leaving in empty).
    3. Perform MIR conversion
    4. Save converted MIR to both mir/archive and mir/out.
    5. Delete mir/out only after confirmed SFTP delivery.
    6. Archive the source 835 on success or failure.
    """
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

    dirs = get_edi835_storage_dirs(client)

    if client and str(getattr(client, "stage", "") or "").lower() == "offboarded":
        return {
            "success": False,
            "error": "This client has been permanently offboarded. New file processing is locked.",
            "code": "CLIENT_OFFBOARDED",
            "db_record": db_record,
        }

    # Sanitize filename to prevent path traversal
    original_filename = os.path.basename(original_filename)
    base_name = os.path.splitext(original_filename)[0]

    # Resolve mir_filename dynamically using client's format
    delivery_mir_filename = resolve_mir_filename(
        client=client,
        fallback_base=base_name,
    )
    delivery_mir_filename = unique_mir_filename(delivery_mir_filename, file_uuid)
    stored_mir_filename = local_mir_filename(client, delivery_mir_filename)

    # Prefix with UUID to prevent file overwrite collisions
    stored_filename = f"{file_uuid}_{original_filename}"

    # Step 1: Save uploaded file to input/ folder
    input_file_path = stage_inbound(client, "835", stored_filename, edi_text)
    # Move it immediately to permanent archive before database parsing or
    # external delivery. This keeps inbound transient and prevents an early
    # database/SFTP error from stranding a source file there.
    processing_file_path = archive_inbound(client, "835", input_file_path)
    relative_input_path = relative_media_path(processing_file_path)

    if not db_record:
        db_record = EDI835File.objects.create(
            id=file_uuid,
            client=client,
            original_filename=original_filename,
            stored_filename=stored_filename,
            input_file_content=edi_text,
            status="UPLOADED",
            input_path=relative_input_path,
            ingestion_source=ingestion_source
        )
    else:
        if client:
            db_record.client = client
        db_record.original_filename = original_filename
        db_record.stored_filename = stored_filename
        db_record.input_file_content = edi_text
        db_record.input_path = relative_input_path
        if ingestion_source and ingestion_source != "MANUAL":
            db_record.ingestion_source = ingestion_source

    db_record.status = "PROCESSING"
    db_record.processing_started_at = timezone.now()
    db_record.save()

    try:
        # Step 3: Perform 835 parsing and MIR conversion during processing
        client = db_record.client if db_record else None
        res = parse_835_to_mir(edi_text, filename=stored_filename, client=client)
        mir_text = res["text"]

        # Step 4: Write converted MIR file to output/ folder
        archived_mir_path, output_mir_path = write_mir_copies(client, stored_mir_filename, mir_text)
        rel_output_path = relative_media_path(archived_mir_path)

        # The database is the system of record. Persist the exact file and its
        # claim/chunk/service structure before attempting any external push.
        stored_mir = store_mir_file(
            source_835=db_record,
            mir_filename=delivery_mir_filename,
            mir_text=mir_text,
        )

        # Step 4b: Upload converted .mir file directly to configured SFTP outbound folder if active config exists
        sftp_uploaded = upload_mir_to_sftp(
            output_mir_path,
            delivery_mir_filename,
            client=client,
        )
        set_mir_push_status(stored_mir, sftp_uploaded)
        if sftp_uploaded:
            remove_delivered_outbound(client, "mir", output_mir_path)

        rel_archive_path = relative_media_path(processing_file_path)

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

        return {
            "success": True,
            "db_record": db_record,
            "mir_text": mir_text,
            "claims_count": res["claims_count"],
            "services_count": res["services_count"],
            "records_count": res["records_count"],
        }

    except Exception as err:
        err_str = str(err)
        logger.exception(f"EDI 835 processing failed for file '{stored_filename}': {err_str}")

        db_record.status = "ERROR"
        db_record.archive_path = relative_media_path(processing_file_path)
        db_record.present_in_archive_folder = True
        db_record.error_message = err_str
        db_record.processing_completed_at = timezone.now()
        db_record.save()

        return {
            "success": False,
            "db_record": db_record,
            "error": err_str,
        }


def process_multiple_edi835_files(
    files_list,
    ingestion_source="SFTP",
    client=None,
    outbound_config=None,
):
    """
    Takes a list of file items: [ {"filename": "f1.835", "content": "..."}, {"filename": "f2.835", "content": "..."} ]
    Parses claims from all 835 files, combines them into a SINGLE MIR output file,
    creates a SINGLE DB record in the table with multiple input names and single output name,
    saves the single MIR file to output/, archives individual 835 files, and uploads to SFTP outbound.
    """
    from admin_panel.mir_mapper_logic.edi835_parser import parse_835
    from admin_panel.mir_mapper_logic.mir_generator import generate_mir_text
    if client and str(getattr(client, "stage", "") or "").lower() == "offboarded":
        return {
            "success": False,
            "error": "This client has been permanently offboarded. New file processing is locked.",
            "code": "CLIENT_OFFBOARDED",
        }
    dirs = get_edi835_storage_dirs(client)

    all_claims = []
    input_contents = []
    file_names = []
    errors = []

    if not files_list:
        return {"success": False, "error": "No files provided for batch conversion."}

    first_archive_rel_path = None

    file_uuid = uuid.uuid4()
    for idx, item in enumerate(files_list):
        fname = item.get("filename") or item.get("original_filename") or f"file_{idx+1}.835"
        fname = os.path.basename(fname)
        if not has_valid_file_extension(fname, "835"):
            return {"success": False, "error": file_extension_error("835")}
        file_names.append(fname)

        content = (item.get("content") or item.get("edi_text") or "").lstrip("\ufeff").strip()
        if not content:
            continue
        input_contents.append(content)

        # Prefix with UUID to avoid overwrites in batch mode
        stored_fname = f"{file_uuid}_{idx}_{fname}"
        
        # A file already dropped in this client's inbound directory is the
        # staged copy.  Reuse it so the original is moved (not duplicated)
        # into archive and the inbound directory is empty after the attempt.
        supplied_path = item.get("local_path")
        inbound_path = Path(supplied_path) if supplied_path else None
        expected_in = dirs["835_in"].resolve()
        if not (
            inbound_path
            and inbound_path.is_file()
            and inbound_path.resolve().parent == expected_in
        ):
            inbound_path = stage_inbound(client, "835", stored_fname, content)
        archive_path_file = archive_inbound(client, "835", inbound_path)
        rel_archive_path = relative_media_path(archive_path_file)
        if not first_archive_rel_path:
            first_archive_rel_path = rel_archive_path

        try:
            claims = parse_835(content)
            all_claims.extend(claims)
        except Exception as e:
            errors.append(f"{fname}: {str(e)}")

    if not all_claims:
        return {
            "success": False,
            "error": "No CLP claim segments could be parsed from any of the provided 835 files.",
            "errors": errors
        }

    # Generate ONE single combined MIR file from all claims across all input 835 files
    mir_text, mir_res = generate_mir_text(all_claims, client=client)

    first_base_name = os.path.splitext(file_names[0])[0] if file_names else "batch"
    combined_base_name = f"MIR_COMBINED_{first_base_name}" if len(file_names) > 1 else f"MIR_{first_base_name}"
    delivery_mir_filename = resolve_mir_filename(
        client=client,
        fallback_base=combined_base_name,
    )
    delivery_mir_filename = unique_mir_filename(delivery_mir_filename, file_uuid)
    stored_mir_filename = local_mir_filename(client, delivery_mir_filename)
    archived_mir_path, output_mir_path = write_mir_copies(client, stored_mir_filename, mir_text)
    rel_output_path = relative_media_path(archived_mir_path)

    # Combine all input file names into a single string for table 835 IN column
    combined_inputs_str = ", ".join(file_names)

    claims_count = mir_res.get("claims", 0) if isinstance(mir_res, dict) else getattr(mir_res, "get", lambda k, d: 0)("claims", 0)
    services_count = mir_res.get("services", 0) if isinstance(mir_res, dict) else getattr(mir_res, "get", lambda k, d: 0)("services", 0)
    records_count = mir_res.get("mir_records", 0) if isinstance(mir_res, dict) else getattr(mir_res, "get", lambda k, d: 0)("mir_records", 0)

    # Create one source record, then store the complete normalized MIR before
    # attempting the outbound SFTP push.
    db_rec = EDI835File.objects.create(
        id=file_uuid,
        client=client,
        original_filename=combined_inputs_str,
        stored_filename=file_names[0] if file_names else "batch.835",
        input_file_content="\n\n".join(input_contents),
        status="ARCHIVED",
        claims_count=claims_count,
        services_count=services_count,
        records_count=records_count,
        output_path=rel_output_path,
        archive_path=first_archive_rel_path,
        present_in_sftp=False,
        present_in_archive_folder=True,
        ingestion_source=ingestion_source,
        processing_completed_at=timezone.now()
    )

    stored_mir = store_mir_file(
        source_835=db_rec,
        mir_filename=delivery_mir_filename,
        mir_text=mir_text,
    )
    sftp_uploaded = upload_mir_to_sftp(
        output_mir_path,
        delivery_mir_filename,
        client=client,
        sftp_config=outbound_config,
    )
    set_mir_push_status(stored_mir, sftp_uploaded)
    if sftp_uploaded:
        remove_delivered_outbound(client, "mir", output_mir_path)

    # Surface the real outbound error to the API instead of returning only a
    # boolean. This is especially important for direct execution where the
    # admin page must distinguish conversion success from delivery failure.
    sftp_error = ""
    if not sftp_uploaded:
        try:
            cfg_for_error = outbound_config or resolve_sftp_config(
                client=client,
                outbound=True,
            )
            if cfg_for_error:
                cfg_for_error.refresh_from_db(fields=["last_error"])
                sftp_error = cfg_for_error.last_error or ""
        except Exception:
            logger.exception("Unable to read outbound SFTP upload error")

    if sftp_uploaded:
        db_rec.present_in_sftp = True
        db_rec.save(update_fields=["present_in_sftp"])

    return {
        "success": True,
        "mir_text": mir_text,
        "combined_filename": delivery_mir_filename,
        "stored_filename": stored_mir_filename,
        "files_count": len(file_names),
        "claims_count": claims_count,
        "services_count": services_count,
        "records_count": records_count,
        "sftp_uploaded": sftp_uploaded,
        "sftp_error": sftp_error,
        "db_record": db_rec,
        "errors": errors,
    }


def sync_folder_observer(client=None):
    """
    Folder Observer Service for one client scope:
    1. Scans <client>/files/835/in for new untracked files.
       Creates a DB record with present_in_sftp=True.
    2. Scans all EDI835File records and updates physical existence booleans:
       - present_in_sftp: True if file exists in input/ folder on disk.
       - present_in_archive_folder: True if file exists in archive/ folder on disk.
    """
    dirs = get_edi835_storage_dirs(client)
    input_dir = dirs["input"]
    archive_dir = dirs["archive"]

    # 1. Scan input folder for untracked files dropped into SFTP/input directory
    if os.path.exists(input_dir):
        untracked_fnames = [
            fname for fname in os.listdir(input_dir)
            if os.path.isfile(input_dir / fname)
        ]
        if untracked_fnames:
            existing_names = set()
            for orig, stored in EDI835File.objects.filter(
                models.Q(original_filename__in=untracked_fnames)
                | models.Q(stored_filename__in=untracked_fnames)
            ).values_list("original_filename", "stored_filename"):
                if orig:
                    existing_names.add(orig)
                if stored:
                    existing_names.add(stored)

            for fname in untracked_fnames:
                if fname in existing_names:
                    continue
                rel_input_path = relative_media_path(input_dir / fname)
                EDI835File.objects.create(
                    original_filename=fname,
                    stored_filename=fname,
                    status="UPLOADED",
                    input_path=rel_input_path,
                    present_in_sftp=True,
                    present_in_archive_folder=False,
                    ingestion_source="SFTP",
                    client=client,
                )

    # 2. Sync physical disk existence for all DB records
    to_update = []
    records = EDI835File.objects.filter(client=client)
    for r in records.iterator():
        record_dirs = get_edi835_storage_dirs(r.client)
        archive_dir = record_dirs["archive"]
        # Remote delivery is updated only by a successful SFTP upload. A local
        # MIR/output/archive file must not turn the SFTP status green.
        in_sftp = r.present_in_sftp

        in_archive = False
        if r.stored_filename and os.path.exists(archive_dir / r.stored_filename):
            in_archive = True
        elif r.original_filename and os.path.exists(archive_dir / r.original_filename):
            in_archive = True
        elif r.archive_path and os.path.exists(Path(settings.BASE_DIR) / r.archive_path):
            in_archive = True

        if r.present_in_sftp != in_sftp or r.present_in_archive_folder != in_archive:
            r.present_in_sftp = in_sftp
            r.present_in_archive_folder = in_archive
            to_update.append(r)

    if to_update:
        EDI835File.objects.bulk_update(to_update, ["present_in_sftp", "present_in_archive_folder"])
