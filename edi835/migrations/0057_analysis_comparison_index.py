from django.db import migrations, models
from django.db.models.functions import Trim, Upper


class Migration(migrations.Migration):
    dependencies = [("edi835", "0056_analysis_exact_claim_identity")]

    operations = [migrations.AddIndex(
        model_name="analysisclaim",
        index=models.Index(models.F("upload"), Upper(Trim("claim_number")), name="analysis_upload_number_idx"),
    )]
