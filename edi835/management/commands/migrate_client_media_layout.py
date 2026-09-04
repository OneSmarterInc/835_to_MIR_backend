"""Copy legacy media into tenant folders without deleting any source file."""

import hashlib
import os
import tempfile
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from accounts.models import Client
from admin_panel.models import ClientDocument
from edi835.models import EDI835File, RECONFile
from edi835.storage import client_storage_dirs, relative_media_path, verified_copy


def _first_file(*paths):
    return next((Path(path) for path in paths if path and Path(path).is_file()), None)


def _write_verified(path, content):
    path = Path(path)
    data = content if isinstance(content, bytes) else str(content or "").encode("utf-8")
    expected = hashlib.sha256(data).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise FileExistsError(f"Different file already exists at {path}")
        return path
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary = Path(temporary_name)
        if hashlib.sha256(temporary.read_bytes()).hexdigest() != expected:
            raise IOError(f"Checksum verification failed for {path}")
        os.replace(temporary, path)
    finally:
        temporary = Path(temporary_name)
        if temporary.exists():
            temporary.unlink()
    return path


def _content_hash(content):
    data = content if isinstance(content, bytes) else str(content or "").encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _file_hash(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _collision_safe_target(path, expected_hash, identity):
    """Keep an existing different file and select a stable record-specific name."""
    path = Path(path)
    if not path.exists() or _file_hash(path) == expected_hash:
        return path
    suffix = str(identity).replace("-", "")[:12]
    candidate = path.with_name(f"{path.stem}_{suffix}{path.suffix}")
    if not candidate.exists() or _file_hash(candidate) == expected_hash:
        return candidate
    return path.with_name(f"{path.stem}_{suffix}_{expected_hash[:12]}{path.suffix}")


class Command(BaseCommand):
    help = "Copy legacy media to media/<client-id>/...; never deletes legacy files."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Perform verified copies and update database paths.")

    def handle(self, *args, **options):
        apply_changes = options["apply"]
        base_dir = Path(settings.BASE_DIR)
        media_root = Path(settings.MEDIA_ROOT)
        counts = {"clients": 0, "835": 0, "mir": 0, "recon": 0, "documents": 0, "missing": 0}

        for client in Client.objects.all().iterator():
            counts["clients"] += 1
            if apply_changes:
                client_storage_dirs(client)

        for record in EDI835File.objects.select_related("client", "mir_file").iterator():
            dirs = client_storage_dirs(record.client) if apply_changes else None
            archive_source = _first_file(
                base_dir / record.archive_path if record.archive_path else None,
                media_root / "edi835" / "archive" / (record.stored_filename or ""),
                media_root / "edi835" / "archive" / (record.original_filename or ""),
            )
            if apply_changes:
                if archive_source:
                    archive_target = _collision_safe_target(
                        dirs["835_archive"] / Path(record.stored_filename or record.original_filename).name,
                        _file_hash(archive_source), record.id,
                    )
                    verified_copy(archive_source, archive_target)
                elif record.input_file_content:
                    archive_target = _collision_safe_target(
                        dirs["835_archive"] / Path(record.stored_filename or record.original_filename).name,
                        _content_hash(record.input_file_content), record.id,
                    )
                    _write_verified(archive_target, record.input_file_content)
                else:
                    counts["missing"] += 1
                    archive_target = None
                if archive_target:
                    record.archive_path = relative_media_path(archive_target)

                mir = getattr(record, "mir_file", None)
                if mir:
                    mir_name = Path(record.output_path or "").name or f"{record.id}_{mir.mir_filename}"
                    mir_source = _first_file(
                        base_dir / record.output_path if record.output_path else None,
                        media_root / "edi835" / "output" / mir_name,
                    )
                    if mir_source:
                        mir_target = _collision_safe_target(
                            dirs["mir_archive"] / mir_name,
                            _file_hash(mir_source), record.id,
                        )
                        verified_copy(mir_source, mir_target)
                    else:
                        mir_target = _collision_safe_target(
                            dirs["mir_archive"] / mir_name,
                            _content_hash(mir.file_content), record.id,
                        )
                        _write_verified(mir_target, mir.file_content)
                    record.output_path = relative_media_path(mir_target)
                    if not record.present_in_sftp:
                        verified_copy(mir_target, dirs["mir_out"] / mir_target.name)
                    counts["mir"] += 1
                record.save(update_fields=["archive_path", "output_path"])
            counts["835"] += 1

        for record in RECONFile.objects.select_related("client").iterator():
            is_837 = record.file_kind == "837" or record.claims.filter(record_type="837").exists()
            kind = "837" if is_837 else "recon"
            if apply_changes:
                dirs = client_storage_dirs(record.client)
                source = _first_file(
                    base_dir / record.archive_path if record.archive_path else None,
                    media_root / "edi835" / "archive" / (record.stored_filename or ""),
                )
                if source:
                    target = _collision_safe_target(
                        dirs[f"{kind}_archive"] / Path(record.stored_filename or record.original_filename).name,
                        _file_hash(source), record.id,
                    )
                    verified_copy(source, target)
                elif record.file_content:
                    target = _collision_safe_target(
                        dirs[f"{kind}_archive"] / Path(record.stored_filename or record.original_filename).name,
                        _content_hash(record.file_content), record.id,
                    )
                    _write_verified(target, record.file_content)
                else:
                    counts["missing"] += 1
                    continue
                record.file_kind = "837" if is_837 else "RECON"
                record.archive_path = relative_media_path(target)
                record.save(update_fields=["file_kind", "archive_path", "updated_at"])
                if is_837:
                    verified_copy(target, dirs["837_out"] / target.name)
            counts["recon"] += 1

        for document in ClientDocument.objects.select_related("client").iterator():
            if not document.file:
                continue
            if apply_changes:
                source = Path(document.file.path)
                target = _collision_safe_target(
                    client_storage_dirs(document.client)["documents"] / source.name,
                    _file_hash(source), document.id,
                )
                if source.resolve() != target.resolve():
                    verified_copy(source, target)
                document.file.name = target.relative_to(media_root).as_posix()
                document.save(update_fields=["file"])
            counts["documents"] += 1

        mode = "APPLIED" if apply_changes else "DRY RUN"
        self.stdout.write(self.style.SUCCESS(
            f"{mode}: clients={counts['clients']} 835={counts['835']} MIR={counts['mir']} "
            f"RECON/837={counts['recon']} documents={counts['documents']} missing={counts['missing']}"
        ))
        if not apply_changes:
            self.stdout.write("Run again with --apply after reviewing this inventory. No files were changed.")
