from django.db import migrations
from django.db.models import Q


def detach_administrators_from_clients(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    # Repair only legacy staff records that were incorrectly tenant-bound.
    # Such records may have been disabled when that tenant was offboarded.
    User.objects.filter(
        Q(is_staff=True) | Q(is_superuser=True),
        client__isnull=False,
    ).update(client=None, is_active=True)


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0014_client_timezone"),
    ]

    operations = [
        migrations.RunPython(
            detach_administrators_from_clients,
            migrations.RunPython.noop,
        ),
    ]
