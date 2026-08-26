from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("edi835", "0011_sftpconfig_use_default"),
    ]

    operations = [
        migrations.AddField(
            model_name="sftpconfig",
            name="outbound_ssh_key",
            field=models.TextField(blank=True, null=True),
        ),
    ]
