from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("edi835", "0033_flexible_automation_triggers")]

    operations = [
        migrations.AlterModelOptions(
            name="sftpautomationschedule",
            options={"ordering": ["client__name", "automation_type", "direction"]},
        ),
    ]
