import uuid

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0014_client_timezone"),
        ("edi835", "0018_reconfile_import_mode"),
    ]

    operations = [
        migrations.CreateModel(
            name="SFTPAutomationSchedule",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("run_time", models.TimeField()),
                ("timezone", models.CharField(default="America/New_York", max_length=64)),
                ("enabled", models.BooleanField(default=True)),
                ("next_run_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("last_run_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("client", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="sftp_automation_schedule", to="accounts.client")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="created_sftp_automation_schedules", to="accounts.user")),
                ("updated_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="updated_sftp_automation_schedules", to="accounts.user")),
            ],
            options={"db_table": "sftp_automation_schedule", "ordering": ["client__name"]},
        ),
        migrations.CreateModel(
            name="SFTPAutomationRun",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("scheduled_for", models.DateTimeField(db_index=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("status", models.CharField(choices=[("QUEUED", "Queued"), ("RUNNING", "Running"), ("SUCCESS", "Success"), ("FAILED", "Failed"), ("SKIPPED", "Skipped")], db_index=True, default="QUEUED", max_length=20)),
                ("job_id", models.UUIDField(blank=True, null=True, unique=True)),
                ("input_835_files", models.JSONField(blank=True, default=list)),
                ("input_recon_files", models.JSONField(blank=True, default=list)),
                ("mir_output_files", models.JSONField(blank=True, default=list)),
                ("processed_835_count", models.PositiveIntegerField(default=0)),
                ("recon_file_count", models.PositiveIntegerField(default=0)),
                ("error_message", models.TextField(blank=True, default="")),
                ("result", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("client", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="sftp_automation_runs", to="accounts.client")),
                ("schedule", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="runs", to="edi835.sftpautomationschedule")),
            ],
            options={"db_table": "sftp_automation_run", "ordering": ["-scheduled_for"]},
        ),
        migrations.AddIndex(model_name="sftpautomationrun", index=models.Index(fields=["client", "-scheduled_for"], name="sftp_auto_client_sched_idx")),
        migrations.AddIndex(model_name="sftpautomationrun", index=models.Index(fields=["status", "-scheduled_for"], name="sftp_auto_status_sched_idx")),
    ]
