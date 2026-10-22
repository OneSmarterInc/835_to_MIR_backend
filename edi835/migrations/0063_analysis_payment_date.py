"""Freeze transaction-date extraction for existing Analysis projections."""
from datetime import date

from django.db import migrations, models, transaction


def dates_by_sequence(content):
    try:
        text = bytes(content).decode("utf-8-sig").strip()
    except (UnicodeError, TypeError):
        return {}
    position = sequence = 0
    element = terminator = ""
    active = False
    paid_on = None
    dates = {}
    while position < len(text):
        while position < len(text) and text[position].isspace():
            position += 1
        if position == len(text):
            break
        if text[position:position + 3] == "ISA":
            header = text[position:position + 106]
            if len(header) != 106:
                return {}
            element, terminator = header[3], header[105]
        if not element or not terminator:
            return {}
        end = text.find(terminator, position)
        if end < 0:
            return {}
        fields = text[position:end].strip().split(element)
        position = end + 1
        if fields[0] == "ST":
            active = len(fields) > 1 and fields[1] == "835"
            paid_on = None
        elif fields[0] == "BPR" and active:
            value = fields[16].strip() if len(fields) > 16 else ""
            paid_on = None
            if len(value) == 8 and value.isascii() and value.isdecimal():
                try:
                    paid_on = date(int(value[:4]), int(value[4:6]), int(value[6:8]))
                except ValueError:
                    pass
        elif fields[0] == "CLP" and active:
            sequence += 1
            dates[sequence] = paid_on
        elif fields[0] in {"SE", "GE", "IEA"}:
            active = False
    return dates


def backfill(apps, schema_editor):
    Upload = apps.get_model("edi835", "AnalysisUpload")
    Claim = apps.get_model("edi835", "AnalysisClaim")
    alias = schema_editor.connection.alias
    uploads = Upload.objects.using(alias).filter(kind="835").only("pk", "raw_content")
    # One uploaded file at a time; do not load or reprocess the full history.
    for upload in uploads.iterator(chunk_size=10):
        dates = dates_by_sequence(upload.raw_content)
        with transaction.atomic(using=alias):
            rows = list(Claim.objects.using(alias).filter(upload_id=upload.pk))
            for row in rows:
                row.payment_date = dates.get(row.sequence)
                row.internal_number = row.internal_number.strip().upper().removeprefix("ZZ:").strip()
            Claim.objects.using(alias).bulk_update(rows, ["payment_date", "internal_number"], batch_size=500)


class Migration(migrations.Migration):
    atomic = False
    dependencies = [("edi835", "0062_sftp_source_claims")]
    operations = [
        migrations.AddField(model_name="analysisclaim", name="payment_date",
                            field=models.DateField(blank=True, null=True)),
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
