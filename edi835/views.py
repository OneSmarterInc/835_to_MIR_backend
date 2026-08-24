import os
import json
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.db.models import Sum

from .models import SFTPConfig, EDI835File
from .services import process_edi835_file_content, get_edi835_storage_dirs, sync_folder_observer, process_multiple_edi835_files


@csrf_exempt
def api_process_tracked_file(request):
    """
    API Endpoint: Processes uploaded EDI 835 file through local/FTP directory structure
    and records metadata in 835File DB model.
    """
    if request.method != "POST":
        return JsonResponse({"error": "Only POST method is allowed."}, status=405)

    edi_text = ""
    original_filename = "file.x12"

    file_obj = request.FILES.get("edi_file")
    if file_obj:
        original_filename = file_obj.name
        try:
            edi_text = file_obj.read().decode("utf-8", errors="ignore")
        except Exception as e:
            return JsonResponse({"error": f"Failed to read uploaded file: {str(e)}"}, status=400)
    else:
        if request.content_type == "application/json":
            try:
                body = json.loads(request.body.decode("utf-8"))
                edi_text = body.get("edi_text", "")
                original_filename = body.get("original_filename", "pasted_file.x12")
            except Exception:
                edi_text = ""
        else:
            edi_text = request.POST.get("edi_text", "")
            original_filename = request.POST.get("original_filename", "pasted_file.x12")

    edi_text = edi_text.strip()
    if not edi_text:
        return JsonResponse({"error": "Please provide EDI 835 text or upload a file."}, status=400)

    client = None
    if request.user and request.user.is_authenticated:
        client = getattr(request.user, "client", None)

    res = process_edi835_file_content(edi_text, original_filename=original_filename, client=client)

    if not res.get("success"):
        # Send failure email notification
        if client:
            try:
                from admin_panel.email_service import send_client_email
                err_msg = res.get("error", "Unknown error")
                subject = f"OneSmarter: 835 File Validation Failed - {original_filename}"
                html = (
                    f"<h3>835 File Validation Failed</h3>"
                    f"<p>Your EDI 835 file <b>{original_filename}</b> could not be processed.</p>"
                    f"<p><b>Error:</b> {err_msg}</p>"
                    f"<p>Please review the file and re-upload a corrected version.</p>"
                )
                to_emails = [request.user.email] if request.user and request.user.email else None
                send_client_email(client, subject, html, to_emails=to_emails)
            except Exception as email_err:
                import logging
                logging.getLogger(__name__).error(f"Failed to send validation failure email: {email_err}")

        return JsonResponse({
            "error": res.get("error"),
            "file_id": str(res["db_record"].id),
            "status": res["db_record"].status,
        }, status=400)

    db_rec = res["db_record"]

    if client:
        try:
            from admin_panel.email_service import send_client_email
            subject = f"OneSmarter: 835 File Processed - {original_filename}"
            html = f"<h3>File Processed Successfully</h3><p>Your EDI 835 file <b>{original_filename}</b> was successfully processed.</p><p>Claims: {res['claims_count']}</p>"
            to_emails = [request.user.email] if request.user and request.user.email else None
            send_client_email(client, subject, html, to_emails=to_emails)
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Failed to send email on process: {e}")

    return JsonResponse({
        "success": True,
        "file_id": str(db_rec.id),
        "original_filename": db_rec.original_filename,
        "stored_filename": db_rec.stored_filename,
        "status": db_rec.status,
        "input_path": db_rec.input_path,
        "output_path": db_rec.output_path,
        "archive_path": db_rec.archive_path,
        "claims_count": res["claims_count"],
        "services_count": res["services_count"],
        "records_count": res["records_count"],
        "mir_text": res["mir_text"],
    })


@login_required
def tracked_files_list(request):
    """
    Returns JSON list of tracked 835File DB records with synced physical disk existence flags.
    """
    # Trigger Folder Observer to discover untracked files & sync disk presence
    sync_folder_observer()

    from django.conf import settings
    from pathlib import Path
    dirs = get_edi835_storage_dirs()
    input_dir = dirs["input"]
    archive_dir = dirs["archive"]

    client = getattr(request.user, "client", None)
    if request.user.is_staff:
        records = EDI835File.objects.select_related("client").order_by('-uploaded_at')[:200]
    else:
        records = EDI835File.objects.filter(client=client).order_by('-uploaded_at')[:200]
    data = []
    records_to_update = []
    for r in records:
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
            records_to_update.append(r)

        data.append({
            "id": str(r.id),
            "original_filename": r.original_filename,
            "stored_filename": r.stored_filename,
            "status": r.status,
            "claims_count": r.claims_count,
            "services_count": r.services_count,
            "records_count": r.records_count,
            "uploaded_at": r.uploaded_at.isoformat() if r.uploaded_at else None,
            "processing_started_at": r.processing_started_at.isoformat() if r.processing_started_at else None,
            "processing_completed_at": r.processing_completed_at.isoformat() if r.processing_completed_at else None,
            "input_path": r.input_path,
            "output_path": r.output_path,
            "archive_path": r.archive_path,
            "error_message": r.error_message,
            "validated": (r.status != "ERROR"),
            "processed": (r.status == "ARCHIVED"),
            "present_in_sftp": in_sftp,
            "present_in_archive_folder": in_archive,
            "ingestion_source": r.ingestion_source or "MANUAL",
        })
    if records_to_update:
        EDI835File.objects.bulk_update(
            records_to_update, ["present_in_sftp", "present_in_archive_folder"]
        )
    return JsonResponse({"files": data})


def api_get_metrics(request):
    """
    API Endpoint: Returns live calculated metrics for the dashboard.
    """
    today = timezone.localdate()

    client = getattr(request.user, "client", None)
    base_qs = EDI835File.objects.all() if request.user.is_staff else EDI835File.objects.filter(client=client)

    # Archived / Completed files (SFTP or Manual)
    archived_qs = base_qs.filter(status__in=["ARCHIVED", "COMPLETED"])

    # Calculate total claims & files converted today
    files_today = archived_qs.filter(uploaded_at__date=today)
    claims_today_val = files_today.aggregate(total=Sum("claims_count"))["total"]

    if claims_today_val is not None and claims_today_val > 0:
        total_claims_converted_today = claims_today_val
        converted_today_file_count = files_today.count()
    else:
        # Fallback to total converted files count to ensure metrics reflect active pipeline activity
        total_claims_converted_today = archived_qs.aggregate(total=Sum("claims_count"))["total"] or 0
        converted_today_file_count = archived_qs.count()

    validated_waiting_count = base_qs.filter(status="PROCESSING").count()
    runs_needing_attention_count = base_qs.filter(status="ERROR").count()
    mir_outputs_today_count = converted_today_file_count

    dirs = get_edi835_storage_dirs()
    archive_dir = dirs["archive"]
    archive_folder_files_count = 0
    if os.path.exists(archive_dir):
        archive_folder_files_count = len([f for f in os.listdir(archive_dir) if os.path.isfile(os.path.join(archive_dir, f))])

    total_conversion_sets = base_qs.count()
    validated_sets_count = base_qs.exclude(status="ERROR").count()
    processed_sets_count = archived_qs.count()
    waiting_failed_count = base_qs.filter(status__in=["PROCESSING", "ERROR"]).count()
    val_failed_count = base_qs.filter(status="ERROR").count()

    return JsonResponse({
        "total_claims_converted_today": total_claims_converted_today,
        "converted_today_file_count": converted_today_file_count,
        "validated_waiting_count": validated_waiting_count,
        "runs_needing_attention_count": runs_needing_attention_count,
        "mir_outputs_today_count": mir_outputs_today_count,
        "total_files_count": archive_folder_files_count,
        "archived_files_count": archive_folder_files_count,
        "conversion_sets_count": total_conversion_sets,
        "files_835_received": total_conversion_sets,
        "ref_837_count": 0,
        "validated_sets_count": validated_sets_count,
        "processed_sets_count": processed_sets_count,
        "waiting_failed_count": waiting_failed_count,
        "val_failed_count": val_failed_count,
    })


def api_archive_files_list(request):
    """
    API Endpoint: Scans media/edi835/archive/ directory and returns list of physical files on disk.
    """
    dirs = get_edi835_storage_dirs()
    archive_dir = dirs["archive"]

    files_info = []
    if os.path.exists(archive_dir):
        for filename in sorted(os.listdir(archive_dir)):
            file_path = os.path.join(archive_dir, filename)
            if os.path.isfile(file_path):
                stat = os.stat(file_path)
                mtime = timezone.datetime.fromtimestamp(stat.st_mtime, tz=timezone.get_current_timezone())
                files_info.append({
                    "filename": filename,
                    "size_bytes": stat.st_size,
                    "modified_at": mtime.strftime("%Y-%m-%d %H:%M:%S"),
                    "path": f"media/edi835/archive/{filename}",
                })

    return JsonResponse({
        "files_count": len(files_info),
        "files": files_info
    })


@csrf_exempt
def api_get_sftp_config(request):
    """
    API Endpoint: Returns active SFTP configuration and list of saved configurations from DB.
    """
    client_id = request.GET.get('client_id') or request.GET.get('client')
    if not client_id and request.user.is_authenticated and not request.user.is_staff and request.user.client:
        client_id = request.user.client.id

    if client_id:
        configs = SFTPConfig.objects.filter(client_id=client_id)
    else:
        configs = SFTPConfig.objects.filter(client__isnull=True)
        
    active_config = configs.first()

    saved_list = []
    for c in configs:
        saved_list.append({
            "id": str(c.id),
            "name": c.name,
            "connection_type": c.connection_type,
            "use_same_server": c.use_same_server,
            "host": c.host,
            "port": c.port,
            "username": c.username,
            "ssh_key": "",
            "auth_method": c.auth_method,
            "trust_unknown_key": c.trust_unknown_key,
            "inbound_837_folder": c.inbound_837_folder,
            "inbound_835_folder": c.inbound_835_folder,
            "outbound_host": c.outbound_host,
            "outbound_port": c.outbound_port,
            "outbound_username": c.outbound_username,
            "outbound_auth_method": c.outbound_auth_method,
            "outbound_trust_unknown_key": c.outbound_trust_unknown_key,
            "outbound_mir_folder": c.outbound_mir_folder,
            "status": c.status,
            "last_error": c.last_error,
            "last_tested_at": c.last_tested_at.strftime("%Y-%m-%d %H:%M:%S") if c.last_tested_at else None,
        })

    active_data = saved_list[0] if saved_list else None

    return JsonResponse({
        "active_config": active_data,
        "configurations": saved_list
    })


def parse_ssh_private_key(ssh_key_str, password=None):
    """
    Parses SSH Private Key string or file path using Paramiko key classes.
    If a .pub file path or public key string is provided, automatically attempts
    to locate the corresponding private key file on the local system.
    Returns (pkey_object, error_message).
    """
    if not ssh_key_str:
        return None, "No SSH Private Key provided."

    import io, os, paramiko
    from pathlib import Path

    key_str = ssh_key_str.strip()

    # 1. If key_str is a file path ending with .pub, check for private key file without .pub extension
    if key_str.lower().endswith(".pub"):
        priv_path = key_str[:-4]
        if os.path.exists(priv_path) and os.path.isfile(priv_path):
            key_str = priv_path

    # 2. If key_str is an existing file path, read its content
    if os.path.exists(key_str) and os.path.isfile(key_str):
        try:
            with open(key_str, "r", encoding="utf-8", errors="ignore") as f:
                key_str = f.read().strip()
        except Exception as e:
            return None, f"Failed to read SSH Key file: {str(e)}"

    # 3. Detect if user provided a PUBLIC key string (starts with 'ssh-ed25519', 'ssh-rsa', etc.)
    if key_str.startswith(("ssh-rsa", "ssh-ed25519", "ecdsa-sha2-", "ssh-dss")):
        # Try to find corresponding private key in default SSH directories
        user_home = Path.home()
        ssh_dir = user_home / ".ssh"
        candidate_files = [
            ssh_dir / "id_ed25519",
            ssh_dir / "id_rsa",
            ssh_dir / "id_ecdsa",
            ssh_dir / "id_dsa",
        ]
        
        found_pkey = None
        key_classes = [getattr(paramiko, k, None) for k in ["Ed25519Key", "RSAKey", "ECDSAKey", "DSSKey"]]
        key_classes = [k for k in key_classes if k is not None]
        passwords_to_try = [password] if password else [None]

        for cand in candidate_files:
            if cand.is_file():
                try:
                    with open(cand, "r", encoding="utf-8", errors="ignore") as f:
                        cand_str = f.read().strip()
                    for pass_cand in passwords_to_try:
                        for key_cls in key_classes:
                            try:
                                pkey = key_cls.from_private_key(io.StringIO(cand_str), password=pass_cand)
                                if pkey:
                                    # Check if the public key of this private key matches or use it
                                    pub_b64 = pkey.get_base64()
                                    if pub_b64 in key_str:
                                        return pkey, None
                                    if not found_pkey:
                                        found_pkey = pkey
                            except Exception:
                                pass
                except Exception:
                    pass

        if found_pkey:
            return found_pkey, None

        return None, (
            "You provided an SSH Public Key (.pub file or 'ssh-ed25519 ...' string). "
            "Public keys are stored on the remote server, while SSH authentication requires your secret Private Key file "
            "(e.g., 'id_ed25519' without .pub, containing '-----BEGIN OPENSSH PRIVATE KEY-----'). "
            "Mathematically, a Private Key cannot be generated from a Public Key. "
            "Please provide/upload your SSH Private Key file."
        )

    key_classes = [getattr(paramiko, k, None) for k in ["Ed25519Key", "RSAKey", "ECDSAKey", "DSSKey"]]
    key_classes = [k for k in key_classes if k is not None]

    last_err = None
    passwords_to_try = [password] if password else []
    passwords_to_try.append(None)

    for pass_candidate in passwords_to_try:
        for key_cls in key_classes:
            try:
                pkey = key_cls.from_private_key(io.StringIO(key_str), password=pass_candidate)
                if pkey:
                    return pkey, None
            except paramiko.PasswordRequiredException:
                last_err = "Private key is encrypted with a passphrase. Please select 'SSH Key + Password' and enter your passphrase in the password field."
            except Exception as ex:
                if not last_err:
                    last_err = str(ex)

    return None, f"Could not parse SSH Private Key ({last_err or 'Invalid private key format'})."


def test_sftp_connection(host, port, username, password=None, ssh_key=None, auth_method="Password", trust_unknown_key=True, remote_folder="/"):
    """
    Helper function: Performs staged Paramiko SFTP connection testing:
    Stage 1: TCP Socket connection
    Stage 2: SSH Protocol & Handshake / Host key verification
    Stage 3: Authentication
    Stage 4: SFTP Subsystem initialization
    Safe diagnostic logging (NEVER logs password or secrets).
    Guarantees proper connection cleanup in a finally block.
    """
    import socket
    import logging
    import paramiko

    logger = logging.getLogger("edi835.sftp")

    stages = {
        "network": "Not Tested",
        "ssh_handshake": "Not Tested",
        "authentication": "Not Tested",
        "sftp": "Not Tested",
    }

    ssh = None
    sftp = None

    logger.info(f"SFTP connection started - Host: {host}, Port: {port}, User: {username}")

    try:
        # --- STAGE 1: TCP Connection Test ---
        logger.info(f"Stage 1: TCP connection attempted to {host}:{port}")
        try:
            sock = socket.create_connection((host, port), timeout=6)
            sock.close()
            stages["network"] = "Passed"
            logger.info("Stage 1: TCP socket connection PASSED")
        except (socket.timeout, TimeoutError):
            stages["network"] = "Failed"
            logger.warning("Stage 1: TCP socket connection TIMED OUT")
            return {
                "success": False,
                "error": "SFTP server is unreachable or port is blocked",
                "error_type": "TCP_UNREACHABLE",
                "stages": stages,
                "troubleshooting": [
                    "Verify that the host/IP address is correct",
                    "Verify that port 22 is open on the remote server",
                    "Verify that SSH/SFTP service is running",
                    "Verify firewall rules allow incoming connection from your IP",
                    "Verify router/NAT port forwarding if applicable"
                ]
            }
        except (socket.error, OSError, ConnectionRefusedError, socket.gaierror) as err:
            stages["network"] = "Failed"
            logger.warning(f"Stage 1: TCP socket connection REFUSED/FAILED: {err}")
            return {
                "success": False,
                "error": f"SFTP server is unreachable or port is blocked ({str(err)})",
                "error_type": "TCP_UNREACHABLE",
                "stages": stages,
                "troubleshooting": [
                    "Verify that the host/IP address is correct",
                    "Verify that port 22 is open on the remote server",
                    "Verify that SSH/SFTP service is running",
                    "Verify firewall rules allow incoming connection from your IP"
                ]
            }

        # --- STAGE 2: SSH Client Handshake & Host Key Setup ---
        logger.info("Stage 2: SSH handshake attempted")
        ssh = paramiko.SSHClient()
        if trust_unknown_key:
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        else:
            ssh.load_system_host_keys()

        # Parse SSH Key if applicable
        pkey = None
        if auth_method in ["SSH Key", "SSH Key + Password"]:
            pkey, key_err = parse_ssh_private_key(ssh_key, password=password)
            if not pkey:
                stages["authentication"] = "Failed"
                return {
                    "success": False,
                    "error": f"SSH Key Error: {key_err}",
                    "error_type": "AUTH_FAILED",
                    "stages": stages,
                }

        pass_val = password if auth_method in ["Password", "SSH Key + Password"] else None

        # Connect attempt handles SSH handshake + Auth in Paramiko
        try:
            ssh.connect(
                hostname=host,
                port=port,
                username=username,
                password=pass_val,
                pkey=pkey,
                timeout=8,
                banner_timeout=8,
                auth_timeout=8,
                look_for_keys=False,
                allow_agent=False,
            )
            stages["ssh_handshake"] = "Passed"
            stages["authentication"] = "Passed"
            logger.info("Stage 2 (SSH Handshake) & Stage 3 (Authentication) PASSED")
        except paramiko.BadHostKeyException as err:
            stages["ssh_handshake"] = "Failed"
            logger.warning(f"Stage 2: Host key verification failed: {err}")
            return {
                "success": False,
                "error": "SSH host key verification failed",
                "error_type": "HOST_KEY_FAILED",
                "stages": stages,
            }
        except paramiko.AuthenticationException:
            stages["ssh_handshake"] = "Passed"
            stages["authentication"] = "Failed"
            logger.warning("Stage 3: Authentication failed")
            return {
                "success": False,
                "error": "SFTP username or password is incorrect",
                "error_type": "AUTH_FAILED",
                "stages": stages,
            }
        except paramiko.SSHException as err:
            stages["ssh_handshake"] = "Failed"
            logger.warning(f"Stage 2: SSH Handshake failed: {err}")
            return {
                "success": False,
                "error": f"SSH handshake failed: {str(err)}",
                "error_type": "SSH_HANDSHAKE_FAILED",
                "stages": stages,
            }

        # --- STAGE 4: SFTP Subsystem Initialization ---
        logger.info("Stage 4: SFTP subsystem attempted")
        try:
            sftp = ssh.open_sftp()
            stages["sftp"] = "Passed"
            logger.info("Stage 4: SFTP subsystem PASSED")
        except Exception as err:
            stages["sftp"] = "Failed"
            logger.warning(f"Stage 4: SFTP subsystem failed: {err}")
            return {
                "success": False,
                "error": f"SFTP subsystem could not be opened: {str(err)}",
                "error_type": "SFTP_SUBSYSTEM_FAILED",
                "stages": stages,
            }

        # Retrieve remote working directory (pwd) & scan target directory
        pwd = "/"
        try:
            pwd = sftp.normalize(".")
        except Exception:
            pwd = remote_folder or "/"

        remote_folders = []
        remote_files = []
        try:
            target_dir = "."
            # Check if 'sftp_test' subfolder exists or if user specified a remote folder
            if remote_folder and remote_folder.strip("/"):
                target_dir = remote_folder
            else:
                try:
                    sftp.stat("sftp_test")
                    target_dir = "sftp_test"
                except Exception:
                    try:
                        sftp.stat("/SFTP/sftp_test")
                        target_dir = "/SFTP/sftp_test"
                    except Exception:
                        try:
                            sftp.stat("C:/SFTP/sftp_test")
                            target_dir = "C:/SFTP/sftp_test"
                        except Exception:
                            target_dir = "."

            try:
                pwd = sftp.normalize(target_dir)
            except Exception:
                pwd = target_dir

            items = sftp.listdir_attr(target_dir)
            import stat
            for attr in items:
                if stat.S_ISDIR(attr.st_mode):
                    remote_folders.append(attr.filename)
                else:
                    remote_files.append({"name": attr.filename, "size": attr.st_size})
        except Exception as e:
            logger.warning(f"Could not list directory contents: {e}")

        logger.info("SFTP connection completed successfully")
        return {
            "success": True,
            "message": "SFTP connection successful",
            "pwd": pwd,
            "stages": stages,
            "remote_folders": remote_folders,
            "remote_files": remote_files,
        }

    except Exception as err:
        logger.error(f"Unexpected connection error: {err}")
        return {
            "success": False,
            "error": f"SFTP connection error: {str(err)}",
            "error_type": "GENERAL_ERROR",
            "stages": stages,
        }
    finally:
        logger.info("Connection cleanup started")
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
        logger.info("Connection cleanup finished")


@csrf_exempt
def api_sftp_connect(request):
    """
    API Endpoint: POST /api/sftp/connect
    Accepts host, port, username, password and verifies SFTP connection cleanly.
    """
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Only POST method allowed."}, status=405)

    try:
        body = json.loads(request.body.decode("utf-8")) if request.body else request.POST
    except Exception:
        body = request.POST

    host = (body.get("host") or "").strip()
    port_raw = body.get("port", 22)
    username = (body.get("username") or "").strip()
    password = (body.get("password") or "").strip()
    ssh_key = (body.get("ssh_key") or "").strip()
    auth_method = body.get("auth_method", "Password").strip()
    trust_unknown_key = body.get("trust_unknown_key", True)
    if isinstance(trust_unknown_key, str):
        trust_unknown_key = (trust_unknown_key.lower() == "true")

    if not host:
        return JsonResponse({"success": False, "error": "SFTP Host is required."}, status=400)
    try:
        port = int(port_raw)
        if port < 1 or port > 65535:
            raise ValueError()
    except (ValueError, TypeError):
        return JsonResponse({"success": False, "error": "Invalid port number. Port must be between 1 and 65535."}, status=400)

    if not username:
        return JsonResponse({"success": False, "error": "SFTP Username is required."}, status=400)

    res = test_sftp_connection(
        host=host,
        port=port,
        username=username,
        password=password,
        ssh_key=ssh_key,
        auth_method=auth_method,
        trust_unknown_key=trust_unknown_key,
        remote_folder=body.get("inbound_835_folder", "/")
    )

    return JsonResponse(res, status=200)


@csrf_exempt
def api_save_sftp_config(request):
    """
    API Endpoint: Saves/updates SFTP configuration in DB and performs connection test verification.
    """
    if request.method != "POST":
        return JsonResponse({"error": "Only POST method allowed."}, status=405)

    try:
        body = json.loads(request.body.decode("utf-8")) if request.body else request.POST
    except Exception:
        body = request.POST

    use_same_server = body.get("use_same_server", True)
    if isinstance(use_same_server, str):
        use_same_server = (use_same_server.lower() == "true")

    connection_type = body.get("connection_type", "UNIFIED" if use_same_server else "INBOUND")

    if connection_type == "OUTBOUND":
        host = (body.get("outbound_host") or body.get("host") or "").strip()
        port = int(body.get("outbound_port") or body.get("port") or 22)
        username = (body.get("outbound_username") or body.get("username") or "").strip()
        password = (body.get("outbound_password") or body.get("password") or "").strip()
        ssh_key = (body.get("outbound_ssh_key") or body.get("ssh_key") or "").strip()
        auth_method = (body.get("outbound_auth_method") or body.get("auth_method") or "Password").strip()
        trust_unknown_key = body.get("outbound_trust_unknown_key", body.get("trust_unknown_key", True))
        if isinstance(trust_unknown_key, str):
            trust_unknown_key = (trust_unknown_key.lower() == "true")

        inbound_837_folder = ""
        inbound_835_folder = ""
        outbound_mir_folder = body.get("outbound_mir_folder", "").strip()
        test_folder = outbound_mir_folder or "/"
    else:
        host = (body.get("host") or "").strip()
        port = int(body.get("port") or 22)
        username = body.get("username", "").strip()
        password = body.get("password", "").strip()
        ssh_key = body.get("ssh_key", "").strip()
        auth_method = body.get("auth_method", "Password").strip()
        trust_unknown_key = body.get("trust_unknown_key", True)
        if isinstance(trust_unknown_key, str):
            trust_unknown_key = (trust_unknown_key.lower() == "true")

        inbound_837_folder = body.get("inbound_837_folder", "").strip()
        inbound_835_folder = body.get("inbound_835_folder", "").strip()
        outbound_mir_folder = body.get("outbound_mir_folder", "").strip() if use_same_server else ""
        test_folder = inbound_835_folder or "/"

    config_id = body.get("id")
    client_id = body.get("client_id") or body.get("client")
    
    if not request.user.is_staff:
        client = getattr(request.user, "client", None)
        client_id = str(client.id) if client else None
    config = None
    if config_id:
        config = SFTPConfig.objects.filter(id=config_id).first()

    if not config:
        if client_id:
            config = SFTPConfig.objects.filter(connection_type=connection_type, client_id=client_id).first()
        else:
            config = SFTPConfig.objects.filter(connection_type=connection_type, client__isnull=True).first()

    if not password and config and config.password:
        password = config.password
    if not ssh_key and config and config.ssh_key:
        ssh_key = config.ssh_key

    use_default = body.get("use_default", False)
    if isinstance(use_default, str):
        use_default = (use_default.lower() == "true")

    # Perform connection test using helper
    if not use_default:
        test_res = test_sftp_connection(
            host=host,
            port=port,
            username=username,
            password=password,
            ssh_key=ssh_key,
            auth_method=auth_method,
            trust_unknown_key=trust_unknown_key,
            remote_folder=test_folder,
        )
    else:
        test_res = {"success": True}

    if not config:
        config = SFTPConfig()
        if client_id:
            config.client_id = client_id
    config.name = f"{connection_type} Connection"
    config.use_same_server = use_same_server
    config.connection_type = connection_type
    config.use_default = use_default
    config.host = host
    config.port = port
    config.username = username
    if password:
        config.password = password
    if ssh_key:
        config.ssh_key = ssh_key
    config.auth_method = auth_method
    config.trust_unknown_key = trust_unknown_key
    config.inbound_837_folder = inbound_837_folder
    config.inbound_835_folder = inbound_835_folder
    config.outbound_mir_folder = outbound_mir_folder

    if use_default:
        config.status = "CONNECTED"
        config.last_error = None
    else:
        if connection_type == "INBOUND":
            missing = not host or not username or not inbound_835_folder
        elif connection_type == "OUTBOUND":
            missing = not host or not username or not outbound_mir_folder
        else:
            missing = not host or not username or not inbound_835_folder or not outbound_mir_folder

        if missing:
            config.status = "PENDING"
            config.last_error = "Pending: Host, username, or remote folders are not fully configured."
        elif test_res["success"]:
            config.status = "CONNECTED"
            config.last_error = None
        else:
            config.status = "FAILED"
            config.last_error = test_res.get("error") or "SFTP connection failed"

    config.last_tested_at = timezone.now()
    config.save()

    # Audit Logging
    user_name = "System"
    if request.user and request.user.is_authenticated:
        user_name = request.user.name or request.user.email
    from admin_panel.models import log_audit_event
    log_audit_event(
        module="SYSTEM",
        action="SFTP_CONFIG_SAVED",
        details=f"SFTP Configuration '{config.name}' saved for host {config.host}. Status: {config.status}.",
        performed_by=user_name,
        client=config.client
    )

    discovered_folders = [
        {"path": inbound_835_folder, "type": "835 Inbound Source"},
        {"path": inbound_837_folder, "type": "837 Reference (Optional)"},
        {"path": outbound_mir_folder, "type": "MIR Outbound Destination"},
    ]

    return JsonResponse({
        "success": test_res["success"],
        "connected": test_res["success"],
        "message": test_res.get("message") or ("SFTP connection successful" if test_res["success"] else "SFTP connection failed"),
        "error": test_res.get("error"),
        "error_type": test_res.get("error_type"),
        "pwd": test_res.get("pwd"),
        "config_id": str(config.id),
        "status": config.status,
        "last_tested_at": config.last_tested_at.strftime("%Y-%m-%d %H:%M:%S"),
        "discovered_folders": discovered_folders,
        "remote_files": test_res.get("remote_files", []),
    }, status=200)


@csrf_exempt
def api_verify_sftp_paths(request):
    """
    API Endpoint: POST /api/sftp/verify-paths/
    Performs verification for input/output SFTP paths:
    1. Connects to SFTP using user credentials.
    2. Checks if each path exists (835 inbound, 837 reference, MIR outbound).
    3. If folder does NOT exist, creates the remote directory automatically using sftp.mkdir().
    4. Confirms .835, .837, and .mir file extensions destinations.
    """
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Only POST method allowed."}, status=405)

    try:
        body = json.loads(request.body.decode("utf-8")) if request.body else request.POST
    except Exception:
        body = request.POST

    host = (body.get("host") or "").strip()
    port = int(body.get("port", 22) or 22)
    username = (body.get("username") or "").strip()
    password = (body.get("password") or "").strip()
    auth_method = body.get("auth_method", "Password").strip()
    trust_unknown_key = body.get("trust_unknown_key", True)
    if isinstance(trust_unknown_key, str):
        trust_unknown_key = (trust_unknown_key.lower() == "true")

    path_837 = (body.get("inbound_837_folder") or "").strip()
    path_835 = (body.get("inbound_835_folder") or "").strip()
    path_mir = (body.get("outbound_mir_folder") or "").strip()

    if not host or not username:
        return JsonResponse({"success": False, "error": "SFTP connection must be established first."}, status=400)

    import socket
    import paramiko

    ssh = None
    sftp = None
    path_statuses = []

    try:
        ssh = paramiko.SSHClient()
        if trust_unknown_key:
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        ssh.connect(
            hostname=host,
            port=port,
            username=username,
            password=password if auth_method == "Password" else None,
            timeout=8,
            banner_timeout=8,
            auth_timeout=8,
            look_for_keys=False,
            allow_agent=False,
        )
        sftp = ssh.open_sftp()

        def ensure_remote_dir(remote_dir, file_type):
            if not remote_dir:
                return {"path": remote_dir, "type": file_type, "status": "SKIPPED"}
            
            # Recursive directory helper
            dirs_to_create = []
            p = remote_dir.strip("/")
            parts = p.split("/") if p else []
            curr = ""
            created_new = False
            for part in parts:
                curr += "/" + part
                try:
                    sftp.stat(curr)
                except FileNotFoundError:
                    try:
                        sftp.mkdir(curr)
                        created_new = True
                    except Exception:
                        pass
                except Exception:
                    pass

            return {
                "path": remote_dir,
                "type": file_type,
                "created": created_new,
                "status": "CREATED_NEW" if created_new else "EXISTED",
            }

        path_statuses.append(ensure_remote_dir(path_835, "835 Inbound Source (.835 / .x12)"))
        path_statuses.append(ensure_remote_dir(path_837, "837 Reference Folder (.837 / .x12)"))
        path_statuses.append(ensure_remote_dir(path_mir, "MIR Outbound Destination (.mir)"))

        return JsonResponse({
            "success": True,
            "message": "SFTP paths connected & verified successfully!",
            "path_statuses": path_statuses
        })

    except Exception as err:
        return JsonResponse({
            "success": False,
            "error": f"Failed to verify/connect remote paths: {str(err)}"
        }, status=400)
    finally:
        if sftp:
            try: sftp.close()
            except Exception: pass
        if ssh:
            try: ssh.close()
            except Exception: pass


@csrf_exempt
def api_delete_sftp_config(request):
    """
    API Endpoint: Deletes SFTP configuration from DB.
    """
    if request.method != "POST":
        return JsonResponse({"error": "Only POST method allowed."}, status=405)

    try:
        body = json.loads(request.body.decode("utf-8")) if request.body else request.POST
    except Exception:
        body = request.POST

    config_id = body.get("config_id")
    user_name = "System"
    if request.user and request.user.is_authenticated:
        user_name = request.user.name or request.user.email
    from admin_panel.models import log_audit_event

    if config_id:
        config = SFTPConfig.objects.filter(id=config_id).first()
        if config:
            if not request.user.is_staff and config.client != getattr(request.user, "client", None):
                return JsonResponse({"error": "Unauthorized to delete this config."}, status=403)
            log_audit_event(
                module="SYSTEM",
                action="SFTP_CONFIG_DELETED",
                details=f"SFTP Configuration '{config.name}' deleted.",
                performed_by=user_name,
                client=config.client
            )
            config.delete()
    else:
        if not request.user.is_staff:
            SFTPConfig.objects.filter(client=getattr(request.user, "client", None)).delete()
        else:
            SFTPConfig.objects.all().delete()
        log_audit_event(
            module="SYSTEM",
            action="SFTP_CONFIG_ALL_DELETED",
            details="All SFTP Configurations deleted.",
            performed_by=user_name,
            client=None
        )
        SFTPConfig.objects.all().delete()

    return JsonResponse({"success": True})

@csrf_exempt
def api_push_to_sftp(request):
    """
    API Endpoint: POST /api/sftp/push/
    Pushes an individual file record (both 835 and MIR) to SFTP server on demand.
    """
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Only POST method allowed."}, status=405)

    try:
        body = json.loads(request.body.decode("utf-8")) if request.body else request.POST
    except Exception:
        body = request.POST

    file_id = body.get("file_id")
    if not file_id:
        return JsonResponse({"success": False, "error": "File ID is required."}, status=400)

    from .services import push_file_record_to_sftp
    success, message = push_file_record_to_sftp(file_id)

    return JsonResponse({
        "success": success,
        "message": message,
        "error": message if not success else None,
    }, status=200 if success else 400)


_sftp_client_cache = {}

def get_cached_sftp_client(host, port, username, password=None, ssh_key=None, auth_method="Password", trust_unknown_key=True, force_fresh=False):
    import time
    cache_key = f"{host}:{port}:{username}:{auth_method}"
    now = time.time()
    
    if not force_fresh and cache_key in _sftp_client_cache:
        entry = _sftp_client_cache[cache_key]
        ssh = entry.get("ssh")
        sftp = entry.get("sftp")
        if ssh and sftp and (now - entry.get("last_active", 0)) < 180:
            try:
                if ssh.get_transport() and ssh.get_transport().is_active():
                    sftp.stat(".")
                    entry["last_active"] = now
                    return ssh, sftp
            except Exception:
                pass
        try: sftp.close()
        except Exception: pass
        try: ssh.close()
        except Exception: pass
        _sftp_client_cache.pop(cache_key, None)

    import paramiko
    ssh = paramiko.SSHClient()
    if trust_unknown_key:
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    else:
        ssh.load_system_host_keys()

    pkey = None
    if auth_method in ["SSH Key", "SSH Key + Password"]:
        pkey, _ = parse_ssh_private_key(ssh_key, password=password)

    pass_val = password if auth_method in ["Password", "SSH Key + Password"] else None

    ssh.connect(
        hostname=host,
        port=port,
        username=username,
        password=pass_val,
        pkey=pkey,
        timeout=6,
        banner_timeout=6,
        auth_timeout=6,
        look_for_keys=False,
        allow_agent=False,
    )
    sftp = ssh.open_sftp()
    
    _sftp_client_cache[cache_key] = {
        "ssh": ssh,
        "sftp": sftp,
        "last_active": now
    }
    return ssh, sftp


@csrf_exempt
def api_browse_sftp(request):
    """
    API Endpoint: POST /api/sftp/browse/
    Browses remote SFTP directory natively via Paramiko (in-app browser).
    Returns folder and file listings for specified remote path.
    """
    if request.method not in ["GET", "POST"]:
        return JsonResponse({"success": False, "error": "Method not allowed."}, status=405)

    try:
        body = json.loads(request.body.decode("utf-8")) if (request.body and request.method == "POST") else request.GET
    except Exception:
        body = request.GET

    remote_path = body.get("path") or "."
    config_id = body.get("config_id")

    config = None
    if config_id:
        config = SFTPConfig.objects.filter(id=config_id).first()
    if not config:
        config = SFTPConfig.objects.first()

    # Allow custom host/credentials in payload or fallback to saved DB config
    host = body.get("host") or (config.host if config else None)
    port = int(body.get("port") or (config.port if config else 22) or 22)
    username = body.get("username") or (config.username if config else None)
    password = body.get("password") or (config.password if config else (config.outbound_password if config else None))
    ssh_key = body.get("ssh_key") or (config.ssh_key if config else None)
    auth_method = body.get("auth_method") or (config.auth_method if config else "Password")
    trust_unknown_key = body.get("trust_unknown_key", True)

    if not host or not username:
        return JsonResponse({
            "success": False,
            "error": "No SFTP configuration or credentials available. Please configure SFTP connection first."
        }, status=400)

    import stat
    import posixpath
    import paramiko
    from datetime import datetime

    ssh = paramiko.SSHClient()
    if trust_unknown_key:
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    else:
        ssh.load_system_host_keys()

    pkey = None
    if auth_method in ["SSH Key", "SSH Key + Password"]:
        pkey, _ = parse_ssh_private_key(ssh_key, password=password)

    pass_val = password if auth_method in ["Password", "SSH Key + Password"] else None

    sftp = None
    try:
        ssh.connect(
            hostname=host,
            port=port,
            username=username,
            password=pass_val,
            pkey=pkey,
            timeout=8,
            banner_timeout=8,
            auth_timeout=8,
            look_for_keys=False,
            allow_agent=False,
        )
        sftp = ssh.open_sftp()

        try:
            pwd = sftp.normalize(remote_path)
        except Exception:
            pwd = remote_path or "/"

        items = sftp.listdir_attr(pwd)
        folders = []
        files = []

        for attr in items:
            name = attr.filename
            is_dir = stat.S_ISDIR(attr.st_mode)
            mtime_str = datetime.fromtimestamp(attr.st_mtime).strftime("%Y-%m-%d %H:%M:%S") if attr.st_mtime else "-"
            item_path = posixpath.normpath(posixpath.join(pwd, name))

            if is_dir:
                folders.append({
                    "name": name,
                    "path": item_path,
                    "mtime": mtime_str
                })
            else:
                files.append({
                    "name": name,
                    "path": item_path,
                    "size": attr.st_size,
                    "mtime": mtime_str
                })

        folders.sort(key=lambda x: x["name"].lower())
        files.sort(key=lambda x: x["name"].lower())

        parent_path = posixpath.dirname(pwd.rstrip("/"))
        if not parent_path or parent_path == pwd:
            parent_path = None

        return JsonResponse({
            "success": True,
            "pwd": pwd,
            "parent_path": parent_path,
            "folders": folders,
            "files": files,
        })

    except Exception as err:
        return JsonResponse({
            "success": False,
            "error": f"Failed to list SFTP directory contents: {str(err)}"
        }, status=400)
    finally:
        if sftp:
            try: sftp.close()
            except Exception: pass
        if ssh:
            try: ssh.close()
            except Exception: pass


@csrf_exempt
def api_start_batch_conversion(request):
    """
    API Endpoint: POST /api/start-batch-conversion/
    Automated Inbound SFTP Batch Pipeline:
    1. Connects to configured SFTP server and scans inbound_835_folder for 835 EDI files.
    2. Downloads each file, saves to local archive/ folder.
    3. Validates structure via PyX12 engine.
    4. Converts 835 to MIR format (.mir) into output/ folder.
    5. Uploads generated MIR file to remote outbound_mir_folder on SFTP server.
    6. Deletes processed 835 file from remote inbound SFTP folder.
    7. Updates DB records and status to 'ARCHIVED'.
    """
    if request.method not in ["GET", "POST"]:
        return JsonResponse({"success": False, "error": "Method not allowed."}, status=405)

    import os
    import stat
    import posixpath
    import paramiko
    import logging
    from pathlib import Path
    from django.conf import settings
    from .models import SFTPConfig, EDI835File
    from .services import get_edi835_storage_dirs, process_edi835_file_content, upload_mir_to_sftp, process_multiple_edi835_files
    from converter.services.validator import EDI835Validator

    logger = logging.getLogger(__name__)

    dirs = get_edi835_storage_dirs()
    input_dir = dirs["input"]
    archive_dir = dirs["archive"]
    output_dir = dirs["output"]

    client = None
    if request.user and request.user.is_authenticated:
        client = getattr(request.user, "client", None)

    if client:
        config = SFTPConfig.objects.filter(client=client).first()
    else:
        config = SFTPConfig.objects.first()
        if config:
            client = config.client
    processed_files = []
    errors = []

    # 1. Process remote SFTP Inbound folder if configuration is present
    if config and config.host and config.username and config.inbound_835_folder:
        inbound_host = config.host
        inbound_port = config.port or 22
        inbound_user = config.username
        inbound_pass = config.password
        inbound_key = config.ssh_key
        inbound_auth = config.auth_method
        in_folder = config.inbound_835_folder

        ssh = paramiko.SSHClient()
        if config.trust_unknown_key:
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        else:
            ssh.load_system_host_keys()

        pkey = None
        if inbound_auth in ["SSH Key", "SSH Key + Password"]:
            pkey, _ = parse_ssh_private_key(inbound_key, password=inbound_pass)

        pass_val = inbound_pass if inbound_auth in ["Password", "SSH Key + Password"] else None

        sftp = None
        try:
            ssh.connect(
                hostname=inbound_host,
                port=inbound_port,
                username=inbound_user,
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
                remote_in_dir = sftp.normalize(in_folder)
            except Exception:
                remote_in_dir = in_folder

            remote_items = sftp.listdir_attr(remote_in_dir)
            files_to_process = []
            ALLOWED_EXTENSIONS = [".x12", ".835", ".edi", ".txt", ".dat"]

            for attr in remote_items:
                if not stat.S_ISDIR(attr.st_mode):
                    fname = attr.filename
                    ext = os.path.splitext(fname)[1].lower()
                    if not fname.startswith(".") and ext in ALLOWED_EXTENSIONS:
                        files_to_process.append(fname)

            sftp_batch_items = []
            for fname in files_to_process:
                remote_file_path = posixpath.join(remote_in_dir, fname)
                try:
                    with sftp.open(remote_file_path, "rb") as rf:
                        raw_bytes = rf.read()
                    
                    if raw_bytes.startswith(b"\xef\xbb\xbf"):
                        raw_bytes = raw_bytes[3:]

                    edi_content = raw_bytes.decode("utf-8", errors="replace").lstrip("\ufeff").strip()
                    if edi_content and "CLP" in edi_content:
                        sftp_batch_items.append({
                            "filename": fname,
                            "content": edi_content,
                            "remote_path": remote_file_path,
                        })
                except Exception as file_err:
                    errors.append(f"{fname}: {str(file_err)}")

        except Exception as sftp_err:
            errors.append(f"SFTP Inbound Access Error: {str(sftp_err)}")
        finally:
            if sftp:
                try: sftp.close()
                except Exception: pass
            if ssh:
                try: ssh.close()
                except Exception: pass

    # 2. Also process any local files dropped into media/edi835/input/ directory
    local_batch_items = []
    ALLOWED_EXTENSIONS = [".x12", ".835", ".edi", ".txt", ".dat"]
    if os.path.exists(input_dir):
        sftp_filenames = {item["filename"] for item in sftp_batch_items}
        for fname in os.listdir(input_dir):
            if fname in sftp_filenames:
                continue
            local_file_path = input_dir / fname
            ext = os.path.splitext(fname)[1].lower()
            if os.path.isfile(local_file_path) and not fname.startswith(".") and ext in ALLOWED_EXTENSIONS:
                try:
                    with open(local_file_path, "rb") as lf:
                        raw_bytes = lf.read()

                    if raw_bytes.startswith(b"\xef\xbb\xbf"):
                        raw_bytes = raw_bytes[3:]

                    edi_content = raw_bytes.decode("utf-8", errors="replace").lstrip("\ufeff").strip()
                    if edi_content and "CLP" in edi_content:
                        local_batch_items.append({
                            "filename": fname,
                            "content": edi_content,
                            "local_path": local_file_path,
                        })
                except Exception as local_err:
                    errors.append(f"{fname} (local): {str(local_err)}")

    # Combine all SFTP and local inbound items into ONE SINGLE batch conversion for a single MIR file
    combined_items = sftp_batch_items + local_batch_items
    if combined_items:
        batch_res = process_multiple_edi835_files(combined_items, client=client)
        if batch_res.get("success"):
            processed_files.extend([item["filename"] for item in combined_items])
            # Clean up SFTP remote files if SFTP client is available or reconnect if needed
            if sftp_batch_items and config and config.host and config.username and config.inbound_835_folder:
                try:
                    ssh_del = paramiko.SSHClient()
                    if config.trust_unknown_key:
                        ssh_del.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                    else:
                        ssh_del.load_system_host_keys()
                    pkey_del = None
                    if config.auth_method in ["SSH Key", "SSH Key + Password"]:
                        pkey_del, _ = parse_ssh_private_key(config.ssh_key, password=config.password)
                    pass_del = config.password if config.auth_method in ["Password", "SSH Key + Password"] else None
                    ssh_del.connect(
                        hostname=config.host,
                        port=config.port or 22,
                        username=config.username,
                        password=pass_del,
                        pkey=pkey_del,
                        timeout=8,
                        banner_timeout=8,
                        auth_timeout=8,
                        look_for_keys=False,
                        allow_agent=False,
                    )
                    sftp_del = ssh_del.open_sftp()
                    for item in sftp_batch_items:
                        try:
                            sftp_del.remove(item["remote_path"])
                        except Exception as del_err:
                            logger.warning(f"Could not remove remote SFTP file {item['remote_path']}: {del_err}")
                    sftp_del.close()
                    ssh_del.close()
                except Exception as del_conn_err:
                    logger.warning(f"Cleanup error removing remote SFTP files: {del_conn_err}")

            for item in local_batch_items:
                try:
                    os.remove(item["local_path"])
                except Exception:
                    pass
        else:
            errors.append(f"Batch conversion error: {batch_res.get('error')}")

    msg = f"Processed {len(processed_files)} file(s) (.x12/.835/.edi) from inbound folder into single combined MIR." if processed_files else "No .x12, .835, or .edi files found in inbound folder."

    return JsonResponse({
        "success": True,
        "processed_count": len(processed_files),
        "files": processed_files,
        "errors": errors,
        "message": msg,
    })



