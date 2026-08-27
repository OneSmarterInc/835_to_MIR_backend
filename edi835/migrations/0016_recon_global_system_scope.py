from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("edi835", "0015_reconfile_reconclaim_reconprocessingrun_and_more")]

    operations = [
        migrations.AlterField(
            model_name="reconfile",
            name="client",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="recon_files", to="accounts.client"),
        ),
        migrations.AlterField(
            model_name="reconclaim",
            name="client",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="recon_claims", to="accounts.client"),
        ),
        migrations.AlterField(
            model_name="reconprocessingrun",
            name="client",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="recon_processing_runs", to="accounts.client"),
        ),
    ]
