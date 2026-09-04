import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def migrate_legacy_837_rows(apps, schema_editor):
    LegacyFile = apps.get_model("edi835", "RECONFile")
    LegacyClaim = apps.get_model("edi835", "RECONClaim")
    LegacyService = apps.get_model("edi835", "RECONServiceLine")
    EDI837File = apps.get_model("edi835", "EDI837File")
    EDI837Claim = apps.get_model("edi835", "EDI837Claim")
    EDI837ServiceLine = apps.get_model("edi835", "EDI837ServiceLine")

    for legacy in LegacyFile.objects.filter(file_kind="837").iterator():
        edi_file, _ = EDI837File.objects.get_or_create(
            client_id=legacy.client_id, file_hash=legacy.file_hash,
            defaults={
                "uploaded_by_id": legacy.uploaded_by_id,
                "original_filename": legacy.original_filename,
                "stored_filename": legacy.stored_filename,
                "file_content": legacy.file_content,
                "file_size": legacy.file_size,
                "import_mode": legacy.import_mode,
                "archive_path": legacy.archive_path,
                "claim_count": legacy.claim_count,
                "service_count": legacy.service_count,
                "total_charge_amount": legacy.total_charge_amount,
                "status": "PROCESSED" if legacy.status in {"PROCESSED", "PARTIAL"} else "FAILED",
                "processing_error": legacy.processing_error,
                "processed_at": legacy.processed_at,
            },
        )
        if edi_file.claims.exists():
            continue
        claim_map = {}
        for old_claim in LegacyClaim.objects.filter(recon_file_id=legacy.id).order_by("claim_sequence").iterator():
            value = str(old_claim.claim_control_number or "").strip()
            split_at = 0
            while split_at < len(value) and value[split_at].isdigit():
                split_at += 1
            prefix = value[:split_at]
            suffix = value[split_at:]
            new_claim = EDI837Claim.objects.create(
                edi_file=edi_file, client_id=legacy.client_id,
                claim_sequence=old_claim.claim_sequence, claim_control_number=value,
                highmark_claim_number=prefix, internal_claim_number=suffix,
                patient_control_number=old_claim.patient_control_number,
                member_id=old_claim.member_id, service_from_date=old_claim.service_from_date,
                service_to_date=old_claim.service_to_date,
                service_count=old_claim.service_count,
                total_charge_amount=old_claim.charge_amount,
                segment_data=old_claim.segment_data or {}, raw_claim=old_claim.raw_record or "",
            )
            claim_map[old_claim.id] = new_claim
        services = []
        for old_line in LegacyService.objects.filter(recon_file_id=legacy.id).iterator():
            new_claim = claim_map.get(old_line.recon_claim_id)
            if not new_claim:
                continue
            services.append(EDI837ServiceLine(
                claim=new_claim, edi_file=edi_file,
                service_sequence=old_line.service_sequence,
                procedure_code=old_line.procedure_code,
                revenue_code=old_line.revenue_code,
                service_from_date=old_line.service_from_date,
                service_to_date=old_line.service_to_date,
                units=old_line.units, charge_amount=old_line.charge_amount,
                raw_segments=old_line.raw_service or "", segment_data=old_line.segment_data or {},
            ))
        EDI837ServiceLine.objects.bulk_create(services, batch_size=1000)


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0019_add_search_admin_screen"),
        ("edi835", "0029_remove_835_outbound_route"),
    ]

    operations = [
        migrations.CreateModel(
            name="EDI837File",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("original_filename", models.CharField(max_length=255)),
                ("stored_filename", models.CharField(max_length=255)),
                ("file_content", models.TextField()),
                ("file_hash", models.CharField(db_index=True, max_length=64)),
                ("file_size", models.BigIntegerField(default=0)),
                ("import_mode", models.CharField(choices=[("MANUAL", "Manual"), ("SFTP", "SFTP")], default="MANUAL", max_length=20)),
                ("remote_path", models.CharField(blank=True, default="", max_length=500)),
                ("archive_path", models.CharField(blank=True, default="", max_length=500)),
                ("outbound_path", models.CharField(blank=True, default="", max_length=500)),
                ("claim_count", models.PositiveIntegerField(default=0)),
                ("service_count", models.PositiveIntegerField(default=0)),
                ("total_charge_amount", models.DecimalField(decimal_places=2, default=0, max_digits=18)),
                ("status", models.CharField(choices=[("PROCESSING", "Processing"), ("PROCESSED", "Processed"), ("FAILED", "Failed")], db_index=True, default="PROCESSING", max_length=20)),
                ("processing_error", models.TextField(blank=True, default="")),
                ("uploaded_at", models.DateTimeField(auto_now_add=True)),
                ("processed_at", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("client", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="edi837_files", to="accounts.client")),
                ("uploaded_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="uploaded_837_files", to=settings.AUTH_USER_MODEL)),
            ],
            options={"db_table": "837_file", "ordering": ["-uploaded_at"]},
        ),
        migrations.CreateModel(
            name="EDI837Claim",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("claim_sequence", models.PositiveIntegerField()),
                ("claim_control_number", models.CharField(db_index=True, max_length=100)),
                ("highmark_claim_number", models.CharField(blank=True, db_index=True, default="", max_length=100)),
                ("internal_claim_number", models.CharField(blank=True, db_index=True, default="", max_length=100)),
                ("reference_9c", models.CharField(blank=True, db_index=True, default="", max_length=100)),
                ("patient_control_number", models.CharField(blank=True, default="", max_length=100)),
                ("member_id", models.CharField(blank=True, db_index=True, default="", max_length=100)),
                ("patient_first_name", models.CharField(blank=True, default="", max_length=100)),
                ("patient_last_name", models.CharField(blank=True, default="", max_length=100)),
                ("subscriber_first_name", models.CharField(blank=True, default="", max_length=100)),
                ("subscriber_last_name", models.CharField(blank=True, default="", max_length=100)),
                ("billing_provider_name", models.CharField(blank=True, default="", max_length=255)),
                ("rendering_provider_name", models.CharField(blank=True, default="", max_length=255)),
                ("payer_name", models.CharField(blank=True, default="", max_length=255)),
                ("claim_type", models.CharField(blank=True, default="", max_length=30)),
                ("place_of_service", models.CharField(blank=True, default="", max_length=10)),
                ("service_from_date", models.CharField(blank=True, default="", max_length=10)),
                ("service_to_date", models.CharField(blank=True, default="", max_length=10)),
                ("diagnosis_codes", models.JSONField(blank=True, default=list)),
                ("service_count", models.PositiveIntegerField(default=0)),
                ("total_charge_amount", models.DecimalField(decimal_places=2, default=0, max_digits=18)),
                ("raw_claim", models.TextField(blank=True, default="")),
                ("segment_data", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("client", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="edi837_claims", to="accounts.client")),
                ("edi_file", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="claims", to="edi835.edi837file")),
            ],
            options={"db_table": "837_claim", "ordering": ["claim_sequence"]},
        ),
        migrations.CreateModel(
            name="EDI837ServiceLine",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("service_sequence", models.PositiveIntegerField()),
                ("procedure_code", models.CharField(blank=True, default="", max_length=50)),
                ("procedure_qualifier", models.CharField(blank=True, default="", max_length=10)),
                ("modifiers", models.JSONField(blank=True, default=list)),
                ("revenue_code", models.CharField(blank=True, default="", max_length=30)),
                ("service_from_date", models.CharField(blank=True, default="", max_length=10)),
                ("service_to_date", models.CharField(blank=True, default="", max_length=10)),
                ("units", models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                ("charge_amount", models.DecimalField(decimal_places=2, default=0, max_digits=18)),
                ("diagnosis_pointers", models.JSONField(blank=True, default=list)),
                ("raw_segments", models.TextField(blank=True, default="")),
                ("segment_data", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("claim", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="service_lines", to="edi835.edi837claim")),
                ("edi_file", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="service_lines", to="edi835.edi837file")),
            ],
            options={"db_table": "837_service_line", "ordering": ["service_sequence"]},
        ),
        migrations.AddConstraint(model_name="edi837file", constraint=models.UniqueConstraint(fields=("client", "file_hash"), name="uniq_client_837_hash")),
        migrations.AddIndex(model_name="edi837file", index=models.Index(fields=["client", "-uploaded_at"], name="edi837_file_client_date_idx")),
        migrations.AddConstraint(model_name="edi837claim", constraint=models.UniqueConstraint(fields=("edi_file", "claim_sequence"), name="uniq_837_claim_sequence")),
        migrations.AddIndex(model_name="edi837claim", index=models.Index(fields=["client", "claim_control_number"], name="edi837_claim_control_idx")),
        migrations.AddIndex(model_name="edi837claim", index=models.Index(fields=["client", "highmark_claim_number"], name="edi837_highmark_idx")),
        migrations.AddIndex(model_name="edi837claim", index=models.Index(fields=["client", "internal_claim_number"], name="edi837_internal_idx")),
        migrations.AddConstraint(model_name="edi837serviceline", constraint=models.UniqueConstraint(fields=("claim", "service_sequence"), name="uniq_837_service_sequence")),
        migrations.RunPython(migrate_legacy_837_rows, migrations.RunPython.noop),
    ]
