"""Backfill normalized 835 claims from database records and filesystem files."""

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.models import Client
from edi835.edi835_claim_service import normalize_835_file, normalized_835_claims
from edi835.models import EDI835File


SUPPORTED_SUFFIXES = {".835", ".x12", ".edi"}


def _read_edi(path):
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace")


def _looks_like_835(content):
    upper = str(content or "").upper()
    return "CLP*" in upper and ("ISA*" in upper or "ST*835*" in upper)


class Command(BaseCommand):
    help = (
        "Hydrate stored 835 content from filesystem paths and normalize every "
        "CLP loop. Use --root with --client-id to import filesystem-only files."
    )

    def add_arguments(self, parser):
        parser.add_argument("--root", action="append", default=[], help="835 directory to scan recursively; repeatable.")
        parser.add_argument("--client-id", help="Client assigned to files discovered under --root.")
        parser.add_argument("--replace", action="store_true", help="Rebuild existing normalized claim rows.")
        parser.add_argument("--dry-run", action="store_true", help="Report changes without writing them.")

    def handle(self, *args, **options):
        roots = [Path(value).expanduser().resolve() for value in options["root"]]
        client = None
        if roots:
            if not options["client_id"]:
                raise CommandError("--client-id is required when --root is used.")
            try:
                client = Client.objects.get(pk=options["client_id"])
            except Client.DoesNotExist as exc:
                raise CommandError("The selected client does not exist.") from exc
            missing = [str(root) for root in roots if not root.is_dir()]
            if missing:
                raise CommandError("835 root does not exist: " + ", ".join(missing))

        stats = {
            "records": 0, "hydrated": 0, "imported": 0, "normalized": 0,
            "claims": 0, "skipped": 0, "errors": 0,
        }

        # First normalize every 835 record already known to the application.
        for edi_file in EDI835File.objects.select_related("client").iterator():
            stats["records"] += 1
            try:
                content = edi_file.input_file_content
                source_path = None
                if not content:
                    for value in (edi_file.input_path, edi_file.archive_path):
                        if value and Path(value).is_file():
                            source_path = Path(value)
                            content = _read_edi(source_path)
                            break
                if not content or not _looks_like_835(content):
                    stats["skipped"] += 1
                    continue
                rows = normalized_835_claims(content)
                if options["dry_run"]:
                    stats["hydrated"] += int(not edi_file.input_file_content)
                    stats["normalized"] += 1
                    stats["claims"] += len(rows)
                    continue
                with transaction.atomic():
                    if not edi_file.input_file_content:
                        EDI835File.objects.filter(pk=edi_file.pk).update(
                            input_file_content=content,
                            input_path=edi_file.input_path or (str(source_path) if source_path else None),
                            claims_count=len(rows),
                            services_count=sum(row["service_count"] for row in rows),
                        )
                        edi_file.input_file_content = content
                        stats["hydrated"] += 1
                    count = normalize_835_file(edi_file, replace=options["replace"])
                stats["normalized"] += 1
                stats["claims"] += count
            except Exception as exc:
                stats["errors"] += 1
                self.stderr.write(f"{edi_file.original_filename}: {exc}")

        # Explicit roots may contain historical files that have no database row.
        seen_paths = set(
            value for pair in EDI835File.objects.values_list("input_path", "archive_path")
            for value in pair if value
        )
        for root in roots:
            for path in sorted(item for item in root.rglob("*") if item.is_file() and item.suffix.lower() in SUPPORTED_SUFFIXES):
                resolved = str(path.resolve())
                if resolved in seen_paths:
                    continue
                try:
                    content = _read_edi(path)
                    if not _looks_like_835(content):
                        stats["skipped"] += 1
                        continue
                    rows = normalized_835_claims(content)
                    if options["dry_run"]:
                        stats["imported"] += 1
                        stats["normalized"] += 1
                        stats["claims"] += len(rows)
                        continue
                    with transaction.atomic():
                        edi_file = EDI835File.objects.create(
                            client=client,
                            original_filename=path.name,
                            stored_filename=path.name,
                            input_file_content=content,
                            status="ARCHIVED",
                            claims_count=len(rows),
                            services_count=sum(row["service_count"] for row in rows),
                            input_path=resolved,
                            archive_path=resolved,
                            present_in_archive_folder=True,
                            ingestion_source="FILESYSTEM",
                        )
                        count = normalize_835_file(edi_file)
                    seen_paths.add(resolved)
                    stats["imported"] += 1
                    stats["normalized"] += 1
                    stats["claims"] += count
                except Exception as exc:
                    stats["errors"] += 1
                    self.stderr.write(f"{path}: {exc}")

        mode = "DRY RUN" if options["dry_run"] else "COMPLETE"
        self.stdout.write(self.style.SUCCESS(
            f"{mode}: records={stats['records']} hydrated={stats['hydrated']} "
            f"imported={stats['imported']} normalized={stats['normalized']} "
            f"claims={stats['claims']} skipped={stats['skipped']} errors={stats['errors']}"
        ))
        if stats["errors"]:
            raise CommandError(f"{stats['errors']} file(s) could not be normalized.")
