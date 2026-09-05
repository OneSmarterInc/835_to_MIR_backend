from django.db import migrations, models


def anchor_existing_schedules(apps, schema_editor):
    Schedule = apps.get_model("edi835", "SFTPAutomationSchedule")
    for schedule in Schedule.objects.filter(start_date__isnull=True).iterator():
        schedule.start_date = schedule.created_at.date()
        schedule.save(update_fields=["start_date"])


class Migration(migrations.Migration):
    dependencies = [("edi835", "0032_directional_sftp_automation")]
    operations = [
        migrations.AddField(model_name="sftpautomationschedule", name="schedule_type", field=models.CharField(choices=[("ONCE", "One time"), ("DAILY", "Every N days"), ("WEEKLY", "Selected weekdays"), ("MONTHLY", "Monthly")], default="DAILY", max_length=12)),
        migrations.AddField(model_name="sftpautomationschedule", name="interval_value", field=models.PositiveSmallIntegerField(default=1)),
        migrations.AddField(model_name="sftpautomationschedule", name="weekdays", field=models.JSONField(blank=True, default=list)),
        migrations.AddField(model_name="sftpautomationschedule", name="month_days", field=models.JSONField(blank=True, default=list)),
        migrations.AddField(model_name="sftpautomationschedule", name="start_date", field=models.DateField(blank=True, null=True)),
        migrations.AddField(model_name="sftpautomationschedule", name="end_date", field=models.DateField(blank=True, null=True)),
        migrations.AddField(model_name="sftpautomationschedule", name="one_time_date", field=models.DateField(blank=True, null=True)),
        migrations.AddField(model_name="sftpautomationschedule", name="misfire_policy", field=models.CharField(choices=[("RUN_ASAP", "Run as soon as possible"), ("SKIP", "Skip missed run")], default="RUN_ASAP", max_length=12)),
        migrations.AddField(model_name="sftpautomationschedule", name="overlap_policy", field=models.CharField(choices=[("SKIP", "Skip new run"), ("QUEUE", "Queue one run")], default="SKIP", max_length=8)),
        migrations.AddField(model_name="sftpautomationschedule", name="retry_count", field=models.PositiveSmallIntegerField(default=0)),
        migrations.AddField(model_name="sftpautomationschedule", name="retry_delay_minutes", field=models.PositiveSmallIntegerField(default=5)),
        migrations.AddField(model_name="sftpautomationrun", name="attempt_count", field=models.PositiveSmallIntegerField(default=1)),
        migrations.RunPython(anchor_existing_schedules, migrations.RunPython.noop),
    ]
