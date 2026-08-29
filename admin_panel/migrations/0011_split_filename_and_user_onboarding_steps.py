from django.db import migrations


def split_filename_and_user_step(apps, schema_editor):
    OnboardingStepDefinition = apps.get_model("admin_panel", "OnboardingStepDefinition")
    ClientStepStatus = apps.get_model("admin_panel", "ClientStepStatus")

    filename_step, _ = OnboardingStepDefinition.objects.update_or_create(
        step_number=10,
        defaults={
            "title": "MIR Output Filename Format",
            "description": "Define the naming convention used for generated MIR output files.",
        },
    )
    user_step, _ = OnboardingStepDefinition.objects.update_or_create(
        step_number=16,
        defaults={
            "title": "Create Client User",
            "description": "Create and associate the client's application user account.",
        },
    )

    # Preserve completed onboarding progress for existing clients. New clients
    # complete the two actions independently.
    completed_client_ids = ClientStepStatus.objects.filter(
        step=filename_step, status="COMPLETED"
    ).values_list("client_id", flat=True)
    for client_id in completed_client_ids.iterator():
        ClientStepStatus.objects.update_or_create(
            client_id=client_id,
            step=user_step,
            defaults={"status": "COMPLETED"},
        )


class Migration(migrations.Migration):
    dependencies = [
        ("admin_panel", "0010_alter_clientsmtpconfig_smtp_password"),
    ]

    operations = [
        migrations.RunPython(split_filename_and_user_step, migrations.RunPython.noop),
    ]
