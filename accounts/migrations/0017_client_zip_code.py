from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0016_client_state"),
    ]

    operations = [
        migrations.AddField(
            model_name="client",
            name="zip_code",
            field=models.CharField(
                blank=True,
                help_text="US ZIP or ZIP+4 code",
                max_length=10,
                null=True,
            ),
        ),
    ]
