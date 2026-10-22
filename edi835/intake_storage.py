"""Private, bounded-memory storage. Originals are never served from MEDIA_ROOT."""
import hashlib
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


@dataclass(frozen=True)
class StoredSource:
    key: str
    checksum: str
    size: int


def storage_root() -> Path:
    root = Path(settings.INTAKE_STORAGE_ROOT).resolve()
    public = Path(settings.MEDIA_ROOT).resolve()
    if root == public or public in root.parents:
        raise ImproperlyConfigured("INTAKE_STORAGE_ROOT must be outside publicly served MEDIA_ROOT.")
    static = getattr(settings, "STATIC_ROOT", None)
    if static:
        public_static = Path(static).resolve()
        if root == public_static or public_static in root.parents:
            raise ImproperlyConfigured("INTAKE_STORAGE_ROOT must be outside STATIC_ROOT.")
    return root


def source_path(key: str) -> Path:
    root = storage_root()
    path = (root / key).resolve()
    if path == root or root not in path.parents:
        raise ValueError("Invalid private source key.")
    return path


def _sync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def store_stream(stream: BinaryIO, client_id, *, max_bytes: int) -> StoredSource:
    if max_bytes < 1:
        raise ValueError("A positive upload limit is required.")
    scope = str(uuid.UUID(str(client_id))) if client_id else "system"
    root = storage_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    _sync_directory(root.parent)
    directory = root / scope
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    _sync_directory(directory.parent)
    name = uuid.uuid4().hex
    temporary, destination = directory / f".{name}.part", directory / f"{name}.bin"
    digest, size = hashlib.sha256(), 0
    try:
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as output:
            while True:
                chunk = stream.read(min(1024 * 1024, max_bytes - size + 1))
                if not chunk:
                    break
                if not isinstance(chunk, bytes):
                    raise ValueError("Intake requires a binary stream.")
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError("File exceeds the configured intake size limit.")
                digest.update(chunk)
                output.write(chunk)
            if not size:
                raise ValueError("The submitted file is empty.")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
        _sync_directory(directory)
        source = StoredSource(f"{scope}/{destination.name}", digest.hexdigest(), size)
        from .sftp_intake import _SourceReader
        if isinstance(stream, _SourceReader):
            stream.intake_stored(source)
        return source
    finally:
        temporary.unlink(missing_ok=True)


def read_verified(source) -> bytes:
    with source_path(source.storage_key).open("rb") as handle:
        content = handle.read(source.byte_count + 1)
    if len(content) != source.byte_count or hashlib.sha256(content).hexdigest() != source.checksum:
        raise ValueError("Stored source integrity check failed.")
    return content


def open_verified(source):
    """Verify with bounded memory and serve the same open descriptor."""
    handle = source_path(source.storage_key).open("rb")
    try:
        digest, size = hashlib.sha256(), 0
        while chunk := handle.read(1024 * 1024):
            size += len(chunk)
            if size > source.byte_count:
                raise ValueError("Stored source integrity check failed.")
            digest.update(chunk)
        if size != source.byte_count or digest.hexdigest() != source.checksum:
            raise ValueError("Stored source integrity check failed.")
        handle.seek(0)
        return handle
    except BaseException:
        handle.close()
        raise
