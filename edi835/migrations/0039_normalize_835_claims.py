import re
from decimal import Decimal, InvalidOperation

from django.db import migrations, models
import django.db.models.deletion


def _decimal(value):
    try:
        return Decimal(str(value or "0"))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def backfill_835_claims(apps, schema_editor):
    EDI835File = apps.get_model("edi835", "EDI835File")
    EDI835Claim = apps.get_model("edi835", "EDI835Claim")
    pending = []
    for source in EDI835File.objects.exclude(input_file_content="").iterator(chunk_size=100):
        segments = [
            item.strip()
            for item in re.split(r"[~\r\n]+", source.input_file_content or "")
            if item.strip()
        ]
        current = None
        claims = []
        for segment in segments:
            fields = segment.split("*")
            tag = fields[0].upper()
            if tag == "CLP":
                if current:
                    claims.append(current)
                highmark = fields[1].strip() if len(fields) > 1 else ""
                candidates = [value.strip() for value in fields[6:] if value.strip()]
                internal = next((
                    value for value in candidates
                    if value.upper() != highmark.upper()
                    and re.search(r"[A-Za-z]", value)
                    and re.search(r"\d", value)
                ), "")
                current = {
                    "highmark": highmark,
                    "internal": internal,
                    "status": fields[2].strip() if len(fields) > 2 else "",
                    "charge": _decimal(fields[3] if len(fields) > 3 else 0),
                    "paid": _decimal(fields[4] if len(fields) > 4 else 0),
                    "responsibility": _decimal(fields[5] if len(fields) > 5 else 0),
                    "services": 0,
                    "segments": [segment],
                }
            elif current:
                current["segments"].append(segment)
                if tag == "SVC":
                    current["services"] += 1
        if current:
            claims.append(current)
        for sequence, claim in enumerate((item for item in claims if item["highmark"]), start=1):
            pending.append(EDI835Claim(
                edi_file_id=source.pk,
                claim_sequence=sequence,
                highmark_claim_number=claim["highmark"],
                internal_claim_number=claim["internal"],
                claim_status=claim["status"],
                total_charge_amount=claim["charge"],
                paid_amount=claim["paid"],
                patient_responsibility=claim["responsibility"],
                service_count=claim["services"],
                raw_claim="~".join(claim["segments"]) + "~",
                segment_data={"segments": claim["segments"]},
            ))
        if len(pending) >= 2000:
            EDI835Claim.objects.bulk_create(pending, batch_size=500)
            pending = []
    if pending:
        EDI835Claim.objects.bulk_create(pending, batch_size=500)


class Migration(migrations.Migration):
    dependencies = [("edi835", "0038_mpl_notice_analysis")]

    operations = [
        migrations.CreateModel(
            name="EDI835Claim",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("claim_sequence", models.PositiveIntegerField()),
                ("highmark_claim_number", models.CharField(db_index=True, max_length=100)),
                ("internal_claim_number", models.CharField(blank=True, db_index=True, default="", max_length=100)),
                ("claim_status", models.CharField(blank=True, default="", max_length=20)),
                ("total_charge_amount", models.DecimalField(decimal_places=2, default=0, max_digits=18)),
                ("paid_amount", models.DecimalField(decimal_places=2, default=0, max_digits=18)),
                ("patient_responsibility", models.DecimalField(decimal_places=2, default=0, max_digits=18)),
                ("service_count", models.PositiveIntegerField(default=0)),
                ("raw_claim", models.TextField(blank=True, default="")),
                ("segment_data", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("edi_file", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="claims", to="edi835.edi835file")),
            ],
            options={"db_table": "835_claim", "ordering": ["claim_sequence"]},
        ),
        migrations.AddConstraint(
            model_name="edi835claim",
            constraint=models.UniqueConstraint(fields=("edi_file", "claim_sequence"), name="uniq_835_claim_sequence"),
        ),
        migrations.AddIndex(
            model_name="edi835claim",
            index=models.Index(fields=["edi_file", "highmark_claim_number"], name="edi835_file_highmark_idx"),
        ),
        migrations.AddIndex(
            model_name="edi835claim",
            index=models.Index(fields=["internal_claim_number"], name="edi835_internal_idx"),
        ),
        migrations.RunPython(backfill_835_claims, migrations.RunPython.noop),
    ]
