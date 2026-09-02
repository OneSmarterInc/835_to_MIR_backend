from decimal import Decimal, InvalidOperation

from django.db import migrations, models


def _money(raw):
    value = (raw or "").strip()
    if not value:
        return Decimal("0")
    sign = Decimal("-1") if value[-1:] == "-" else Decimal("1")
    digits = value[:-1] if value[-1:] in "+-" else value
    try:
        return sign * Decimal(digits or "0").scaleb(-2)
    except InvalidOperation:
        return Decimal("0")


def backfill_allowed_amount(apps, schema_editor):
    ServiceLine = apps.get_model("edi835", "MIRServiceLine")
    batch = []
    for service in ServiceLine.objects.only("id", "service_raw").iterator(chunk_size=2000):
        service.allowed_amount = _money(service.service_raw[83:94])
        batch.append(service)
        if len(batch) == 2000:
            ServiceLine.objects.bulk_update(batch, ["allowed_amount"], batch_size=2000)
            batch = []
    if batch:
        ServiceLine.objects.bulk_update(batch, ["allowed_amount"], batch_size=2000)


class Migration(migrations.Migration):
    dependencies = [("edi835", "0022_recon_partial_and_waterfall_fields")]

    operations = [
        migrations.AddField(
            model_name="mirserviceline",
            name="allowed_amount",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=18),
        ),
        migrations.RunPython(backfill_allowed_amount, migrations.RunPython.noop),
    ]
