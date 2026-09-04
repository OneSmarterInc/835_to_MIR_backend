"""Tenant-scoped local file storage and lifecycle helpers."""

import hashlib
import os
import shutil
import uuid
from pathlib import Path

from django.conf import settings


FILE_KINDS = {"835", "837", "mir", "recon"}


def client_storage_key(client):
    return str(getattr(client, "id", None) or "system")


def client_storage_dirs(client=None):
    root = Path(settings.MEDIA_ROOT) / client_storage_key(client)
    files = root / "files"
    dirs = {
        "root": root,
        "documents": root / "documents",
        "837_in": files / "837" / "in",
        "837_archive": files / "837" / "archive",
        "837_out": files / "837" / "out",
        "835_in": files / "835" / "in",
        "835_archive": files / "835" / "archive",
        "mir_archive": files / "mir" / "archive",
        "mir_out": files / "mir" / "out",
        "recon_in": files / "recon" / "in",
        "recon_archive": files / "recon" / "archive",
    }
    for directory in dirs.values():
        directory.mkdir(parents=True, exist_ok=True)
    return dirs


def safe_filename(filename):
    name = os.path.basename(str(filename or "")).strip()
    if not name or name in {".", ".."}:
        raise ValueError("A valid filename is required.")
    return name


def relative_media_path(path):
    relative = Path(path).resolve().relative_to(Path(settings.MEDIA_ROOT).resolve())
    return (Path("media") / relative).as_posix()


def _atomic_write(path, content, binary=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    mode = "wb" if binary else "w"
    kwargs = {} if binary else {"encoding": "utf-8"}
    try:
        with open(temporary, mode, **kwargs) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return path


def stage_inbound(client, kind, stored_filename, content, binary=False):
    kind = str(kind).lower()
    if kind not in {"835", "837", "recon"}:
        raise ValueError(f"Unsupported inbound file type: {kind}")
    return _atomic_write(
        client_storage_dirs(client)[f"{kind}_in"] / safe_filename(stored_filename),
        content,
        binary=binary,
    )


def archive_inbound(client, kind, inbound_path, archive_name=None):
    kind = str(kind).lower()
    source = Path(inbound_path)
    destination = client_storage_dirs(client)[f"{kind}_archive"] / safe_filename(
        archive_name or source.name
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.resolve() != source.resolve():
        # Never overwrite an earlier archive. Identical names can legitimately
        # recur across runs, so retain both with a unique suffix.
        destination = destination.with_name(
            f"{destination.stem}_{uuid.uuid4().hex[:12]}{destination.suffix}"
        )
    os.replace(source, destination)
    return destination


def write_mir_copies(client, stored_filename, content):
    dirs = client_storage_dirs(client)
    name = safe_filename(stored_filename)
    archive_path = _atomic_write(dirs["mir_archive"] / name, content)
    out_path = dirs["mir_out"] / name
    temporary = out_path.with_name(f".{out_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copy2(archive_path, temporary)
        os.replace(temporary, out_path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return archive_path, out_path


def remove_delivered_outbound(client, kind, path):
    kind = str(kind).lower()
    if kind not in {"837", "mir"}:
        raise ValueError("Only 837 and MIR outbound files may be deleted after delivery.")
    allowed_root = client_storage_dirs(client)[f"{kind}_out"].resolve()
    target = Path(path).resolve()
    if target.parent != allowed_root:
        raise ValueError("Refusing to delete a file outside the client outbound folder.")
    if target.exists():
        target.unlink()


def verified_copy(source, destination):
    """Copy without deleting the source and verify exact bytes for migration."""
    source, destination = Path(source), Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if _sha256(source) != _sha256(destination):
            raise FileExistsError(f"Different file already exists at {destination}")
        return destination
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copy2(source, temporary)
        if _sha256(source) != _sha256(temporary):
            raise IOError(f"Checksum verification failed for {source}")
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return destination


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def client_document_upload_to(instance, filename):
    return f"{client_storage_key(instance.client)}/documents/{safe_filename(filename)}"
