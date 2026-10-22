from django.db import migrations
from django.db.models import Q


def backfill(apps, schema_editor):
    Receipt = apps.get_model('edi835', 'InboundTransfer')
    Quarantine = apps.get_model('edi835', 'IntakeQuarantine')
    for receipt in Receipt.objects.filter(Q(state='REJECTED') | Q(validation_outcome='REJECTED')).iterator(chunk_size=500):
        Quarantine.objects.get_or_create(receipt_id=receipt.pk, defaults={
            'safe_code': receipt.safe_error_code or 'VALIDATION_REJECTED', 'created_at': receipt.received_at})


class Migration(migrations.Migration):
    dependencies = [('edi835', '0067_intake_control')]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
