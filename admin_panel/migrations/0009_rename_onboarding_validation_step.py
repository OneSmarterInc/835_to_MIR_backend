from django.db import migrations


def rename_validation_step(apps, schema_editor):
    OnboardingStepDefinition = apps.get_model(
        "admin_panel", "OnboardingStepDefinition"
    )
    OnboardingStepDefinition.objects.filter(step_number=8).update(
        title="Validate 835 and Push MIR to SFTP",
        description=(
            "Validate the 835, convert it to MIR, and upload the MIR to the "
            "configured outbound SFTP folder."
        ),
    )


class Migration(migrations.Migration):
    dependencies = [
        ("admin_panel", "0008_seed_onboarding_steps"),
    ]

    operations = [
        migrations.RunPython(rename_validation_step, migrations.RunPython.noop),
    ]
