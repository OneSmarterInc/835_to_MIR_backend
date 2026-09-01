from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0015_detach_administrators_from_clients"),
    ]

    operations = [
        migrations.AddField(
            model_name="client",
            name="state",
            field=models.CharField(
                blank=True,
                help_text="US state abbreviation",
                max_length=2,
                null=True,
            ),
        ),
    ]
