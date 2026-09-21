from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("edi835", "0044_normalize_835_claims"),
    ]

    operations = [
        migrations.AddField(
            model_name="mplnoticeclaim",
            name="workflow_status",
            field=models.CharField(
                choices=[
                    ("YET_TO_START", "Yet to start"),
                    ("HOLD", "Hold"),
                    ("IN_PROGRESS", "In progress"),
                    ("RESOLVED", "Resolved"),
                ],
                db_index=True,
                default="YET_TO_START",
                max_length=20,
            ),
        ),
    ]
