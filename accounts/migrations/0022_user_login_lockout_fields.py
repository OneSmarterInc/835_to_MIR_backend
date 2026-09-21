from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0021_preserve_837_filename_database_default"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="failed_login_attempts",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="user",
            name="login_blocked_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="user",
            name="login_blocked_reason",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="user",
            name="login_blocked_metadata",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
