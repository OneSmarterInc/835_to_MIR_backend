from django.db import migrations, models
from django.contrib.auth.hashers import identify_hasher, make_password


DEFAULT_ADMIN_SCREENS = [
    "clients", "onboard", "conversions", "files", "promote", "trust", "ops",
]


def seed_admin_screens(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    User.objects.filter(is_staff=True, is_superuser=False).update(
        admin_screens=DEFAULT_ADMIN_SCREENS
    )
    for user in User.objects.exclude(recovery_codes=[]).iterator():
        hashed = []
        for code in user.recovery_codes or []:
            try:
                identify_hasher(code)
                hashed.append(code)
            except ValueError:
                hashed.append(make_password(code))
        user.recovery_codes = hashed
        user.save(update_fields=["recovery_codes"])


class Migration(migrations.Migration):
    dependencies = [("accounts", "0017_client_zip_code")]

    operations = [
        migrations.AddField(
            model_name="user",
            name="admin_screens",
            field=models.JSONField(blank=True, default=None, help_text="Administrative navigation screens explicitly assigned to this administrator.", null=True),
        ),
        migrations.AddField(
            model_name="user",
            name="last_totp_counter",
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.RunPython(seed_admin_screens, migrations.RunPython.noop),
    ]
