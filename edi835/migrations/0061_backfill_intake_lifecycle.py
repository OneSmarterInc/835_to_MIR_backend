from django.db import migrations


def backfill(apps, schema_editor):
    transfers = apps.get_model("edi835", "InboundTransfer").objects.using(schema_editor.connection.alias)
    for old, new in (("QUEUED", "QUEUED"), ("RUNNING", "PROCESSING"),
                     ("COMPLETED", "COMPLETED"), ("FAILED", "FAILED")):
        transfers.filter(dispatch__state=old).update(state=new, validation_outcome="NOT_RECORDED")


class Migration(migrations.Migration):
    dependencies = [("edi835", "0060_intake_lifecycle_attempts_outputs")]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
