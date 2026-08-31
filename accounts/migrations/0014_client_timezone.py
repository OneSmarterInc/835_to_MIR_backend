from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0013_alter_user_totp_enabled"),
    ]

    operations = [
        migrations.AddField(
            model_name="client",
            name="timezone",
            field=models.CharField(
                default="America/New_York",
                help_text="IANA timezone used for client-entered schedules.",
                max_length=64,
            ),
        ),
    ]
