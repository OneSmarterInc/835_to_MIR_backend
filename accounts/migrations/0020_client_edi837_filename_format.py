from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("accounts", "0019_add_search_admin_screen")]

    operations = [
        migrations.AddField(
            model_name="client",
            name="edi837_filename_format",
            field=models.CharField(
                default="YYYYMMDDhhmmss.837",
                help_text="Preferred 837 outbound filename format",
                max_length=255,
            ),
        ),
    ]
