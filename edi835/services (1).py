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
from .parser import parse_835_to_mir, EDI835Validator
from .mir_exporter import export_mir_file

logger = logging.getLogger("edi835")



def get_edi835_storage_dirs():
    """
    Returns dictionary of local/FTP storage directories under media/edi835/.
    """
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
    """
    Uploads converted .mir file directly to the configured remote SFTP outbound folder.
    """
    import logging
    logger = logging.getLogger(__name__)

    try:
        from .models import SFTPConfig
        import paramiko

        if client:
            cfg = SFTPConfig.objects.filter(client=client).first()
            if not cfg or cfg.use_default:
                cfg = SFTPConfig.objects.filter(client__isnull=True).first()
        else:
            cfg = SFTPConfig.objects.filter(client__isnull=True).first()

        if not cfg:
            logger.warning("upload_mir_to_sftp: No SFTPConfig found in database.")
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
        out_trust_unknown_key = credentials["trust_unknown_key"]
        out_folder = credentials["remote_folder"]

        if not out_host or not out_user or not cfg.outbound_mir_folder:
            logger.warning("upload_mir_to_sftp: Missing host, username, or outbound_mir_folder.")
            return False

        ssh = paramiko.SSHClient()
        if out_trust_unknown_key:
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

        # Ensure directory exists on remote SFTP server
        p = out_folder.strip("/")
        parts = p.split("/") if p else []
        curr = ""
        for part in parts:
            curr += "/" + part
            try:
                sftp.stat(curr)
            except FileNotFoundError:
                try:
                    sftp.mkdir(curr)
                except Exception:
                    pass
            except Exception:
                pass

        remote_path = f"{out_folder.rstrip('/')}/{mir_filename}"
        sftp.put(str(local_file_path), remote_path)

        sftp.close()
        ssh.close()
        logger.info(f"upload_mir_to_sftp: Successfully pushed {mir_filename} to remote SFTP outbound folder {remote_path}")
        return True
    except Exception as e:
        logger.error(f"upload_mir_to_sftp failed to upload {mir_filename}: {e}", exc_info=True)
        return False


def push_file_record_to_sftp(file_id):
    """
    Pushes converted MIR file for a specific record ID to SFTP outbound folder.
    Returns (success_boolean, message_string).
    """
    from .models import SFTPConfig, EDI835File
    try:
        rec = EDI835File.objects.select_related("client").get(id=file_id)
    except (EDI835File.DoesNotExist, ValueError):
        return False, "File record not found in database."

    client = rec.client
    if client:
        cfg = SFTPConfig.objects.filter(client=client).first()
        if not cfg or cfg.use_default:
            cfg = SFTPConfig.objects.filter(client__isnull=True).first()
    else:
        cfg = SFTPConfig.objects.filter(client__isnull=True).first()

    if not cfg:
        return False, "No SFTP connection configuration found. Please setup SFTP in Connections section first."

    if cfg.status != "CONNECTED":
        return False, f"SFTP connection is not active (Status: {cfg.status}). Please test and verify your SFTP credentials first."

    dirs = get_edi835_storage_dirs()
    success_mir = False

    # Push MIR file to SFTP MIR outbound folder ONLY
    stored_name = rec.stored_filename or rec.original_filename
    if rec.output_path:
        base_name = os.path.splitext(stored_name)[0]
        # Resolve mir_filename dynamically
        now = timezone.now()
        fmt = "MIROUT_YYYY_MMDD_.MIR"
        if client and getattr(client, "mir_filename_format", None):
            fmt = client.mir_filename_format
            
        resolved_name = fmt
        resolved_name = resolved_name.replace("YYYY", now.strftime("%Y"))
        resolved_name = resolved_name.replace("MM", now.strftime("%m"))
        resolved_name = resolved_name.replace("DD", now.strftime("%d"))
        
        if not resolved_name.upper().endswith(".MIR"):
            resolved_name += ".mir"
            
        mir_filename = resolved_name
        mir_path = Path(settings.BASE_DIR) / rec.output_path
        if not os.path.exists(mir_path):
            mir_path = dirs["output"] / f"{base_name}.mir"

        if os.path.exists(mir_path):
            success_mir = upload_mir_to_sftp(mir_path, mir_filename, client=client)

    if success_mir:
        rec.present_in_sftp = True
        rec.save(update_fields=["present_in_sftp"])
        return True, "Successfully pushed MIR file to remote SFTP outbound server!"

    return False, "Failed to upload MIR file to SFTP outbound server. Check SFTP credentials and outbound folder path."


def upload_835_to_sftp(local_file_path, filename):
    """
    Inbound SFTP folder is strictly for receiving 835 files.
    We do NOT push 835 files to inbound SFTP folder.
    """
    return False


def process_edi835_file_content(edi_text, original_filename="uploaded_file.x12", file_id=None, ingestion_source="MANUAL", client=None):
    edi_text = (edi_text or "").lstrip("\ufeff").strip()
    """
    Processes EDI 835 content through the complete pipeline when 'Submit & Convert to MIR' is triggered:
    1. Save to input/
    2. Move input/ -> processing/ (leaving input/ empty)
    3. Perform MIR conversion
    4. Save converted MIR to output/<base_name>.mir
    5. Move 835 EDI file (.x12/.835) from processing/ -> archive/ (saving ONLY .x12/.835 in archive/, leaving processing/ empty)
    6. On error -> move processing/ -> error/
    """
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

    # Sanitize filename to prevent path traversal
    original_filename = os.path.basename(original_filename)
    base_name = os.path.splitext(original_filename)[0]

    # Resolve mir_filename dynamically using client's format
    if client:
        now = timezone.now()
        fmt = getattr(client, "mir_filename_format", "MIROUT_YYYY_MMDD_.MIR") or "MIROUT_YYYY_MMDD_.MIR"
        resolved_name = fmt
        resolved_name = resolved_name.replace("YYYY", now.strftime("%Y"))
        resolved_name = resolved_name.replace("MM", now.strftime("%m"))
        resolved_name = resolved_name.replace("DD", now.strftime("%d"))
        if not resolved_name.upper().endswith(".MIR"):
            resolved_name += ".mir"
        mir_filename = resolved_name
    else:
        mir_filename = f"{base_name}.mir"

    # Prefix with UUID to prevent file overwrite collisions
    stored_filename = f"{file_uuid}_{original_filename}"

    # Step 1: Save uploaded file to input/ folder
    input_file_path = dirs["input"] / stored_filename
    with open(input_file_path, "w", encoding="utf-8") as f:
        f.write(edi_text)

    relative_input_path = (Path("media") / "edi835" / "input" / stored_filename).as_posix()

    if not db_record:
        db_record = EDI835File.objects.create(
            id=file_uuid,
            client=client,
            original_filename=original_filename,
            stored_filename=stored_filename,
            status="UPLOADED",
            input_path=relative_input_path,
            ingestion_source=ingestion_source
        )
    else:
        if client:
            db_record.client = client
        db_record.original_filename = original_filename
        db_record.stored_filename = stored_filename
        db_record.input_path = relative_input_path
        if ingestion_source and ingestion_source != "MANUAL":
            db_record.ingestion_source = ingestion_source

    # Step 2: Move file from input/ to processing/ (input/ folder becomes empty)
    processing_file_path = dirs["processing"] / stored_filename
    if os.path.exists(input_file_path):
        shutil.move(input_file_path, processing_file_path)

    db_record.status = "PROCESSING"
    db_record.processing_started_at = timezone.now()
    db_record.save()

    try:
        # Step 3: Perform 835 parsing and MIR conversion during processing
        client = db_record.client if db_record else None
        res = parse_835_to_mir(edi_text, filename=stored_filename, client=client)
        mir_text = res["text"]

        # Step 4: Write converted MIR file to output/ folder
        output_mir_path = dirs["output"] / mir_filename
        export_mir_file(mir_text, dirs["output"], mir_filename)
        rel_output_path = (Path("media") / "edi835" / "output" / mir_filename).as_posix()

        # Step 4b: Upload converted .mir file directly to configured SFTP outbound folder if active config exists
        sftp_uploaded = upload_mir_to_sftp(output_mir_path, mir_filename, client=client)

        # Step 5: Move original 835/x12 EDI file from processing/ to archive/
        archived_835_path = dirs["archive"] / stored_filename
        if os.path.exists(processing_file_path):
            shutil.move(processing_file_path, archived_835_path)
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

        # Step 6: On error, move file from processing/ to error/ folder
        error_file_path = dirs["error"] / stored_filename
        if os.path.exists(processing_file_path):
            shutil.move(processing_file_path, error_file_path)

        db_record.status = "ERROR"
        db_record.error_message = err_str
        db_record.processing_completed_at = timezone.now()
        db_record.save()

        return {
            "success": False,
            "db_record": db_record,
            "error": err_str,
        }


def process_multiple_edi835_files(files_list, ingestion_source="SFTP", client=None):
    """
    Takes a list of file items: [ {"filename": "f1.835", "content": "..."}, {"filename": "f2.835", "content": "..."} ]
    Parses claims from all 835 files, combines them into a SINGLE MIR output file,
    creates a SINGLE DB record in the table with multiple input names and single output name,
    saves the single MIR file to output/, archives individual 835 files, and uploads to SFTP outbound.
    """
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
        if not content:
            continue

        # Prefix with UUID to avoid overwrites in batch mode
        stored_fname = f"{file_uuid}_{idx}_{fname}"
        
        # Save each input file to archive/
        archive_path_file = dirs["archive"] / stored_fname
        with open(archive_path_file, "w", encoding="utf-8") as af:
            af.write(content)
        rel_archive_path = (Path("media") / "edi835" / "archive" / stored_fname).as_posix()
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
    mir_filename = f"{combined_base_name}.mir"
    output_mir_path = dirs["output"] / mir_filename

    export_mir_file(mir_text, dirs["output"], mir_filename)
    rel_output_path = (Path("media") / "edi835" / "output" / mir_filename).as_posix()

    # Upload single combined MIR file directly to configured SFTP outbound folder
    sftp_uploaded = upload_mir_to_sftp(output_mir_path, mir_filename, client=client)

    # Combine all input file names into a single string for table 835 IN column
    combined_inputs_str = ", ".join(file_names)

    claims_count = mir_res.get("claims", 0) if isinstance(mir_res, dict) else getattr(mir_res, "get", lambda k, d: 0)("claims", 0)
    services_count = mir_res.get("services", 0) if isinstance(mir_res, dict) else getattr(mir_res, "get", lambda k, d: 0)("services", 0)
    records_count = mir_res.get("mir_records", 0) if isinstance(mir_res, dict) else getattr(mir_res, "get", lambda k, d: 0)("mir_records", 0)

    # Create or update a SINGLE DB record for this batch run
    db_rec = EDI835File.objects.create(
        id=file_uuid,
        client=client,
        original_filename=combined_inputs_str,
        stored_filename=file_names[0] if file_names else "batch.835",
        status="ARCHIVED",
        claims_count=claims_count,
        services_count=services_count,
        records_count=records_count,
        output_path=rel_output_path,
        archive_path=first_archive_rel_path,
        present_in_sftp=sftp_uploaded,
        present_in_archive_folder=True,
        ingestion_source=ingestion_source,
        processing_completed_at=timezone.now()
    )

    return {
        "success": True,
        "mir_text": mir_text,
        "combined_filename": mir_filename,
        "files_count": len(file_names),
        "claims_count": claims_count,
        "services_count": services_count,
        "records_count": records_count,
        "sftp_uploaded": sftp_uploaded,
        "db_record": db_rec,
        "errors": errors,
    }


def sync_folder_observer():
    """
    Folder Observer Service:
    1. Scans media/edi835/input/ (SFTP Inbound folder) for any new untracked files dropped into the folder.
       Creates a DB record with present_in_sftp=True.
    2. Scans all EDI835File records and updates physical existence booleans:
       - present_in_sftp: True if file exists in input/ folder on disk.
       - present_in_archive_folder: True if file exists in archive/ folder on disk.
    """
    dirs = get_edi835_storage_dirs()
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
                rel_input_path = (Path("media") / "edi835" / "input" / fname).as_posix()
                EDI835File.objects.create(
                    original_filename=fname,
                    stored_filename=fname,
                    status="UPLOADED",
                    input_path=rel_input_path,
                    present_in_sftp=True,
                    present_in_archive_folder=False,
                    ingestion_source="SFTP",
                )

    # 2. Sync physical disk existence for all DB records
    to_update = []
    for r in EDI835File.objects.all().iterator():
        in_sftp = r.present_in_sftp
        if not in_sftp:
            if r.output_path and os.path.exists(Path(settings.BASE_DIR) / r.output_path):
                in_sftp = True
            elif r.status in ["ARCHIVED", "COMPLETED"]:
                in_sftp = True
            elif r.stored_filename and os.path.exists(input_dir / r.stored_filename):
                in_sftp = True
            elif r.original_filename and os.path.exists(input_dir / r.original_filename):
                in_sftp = True

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

