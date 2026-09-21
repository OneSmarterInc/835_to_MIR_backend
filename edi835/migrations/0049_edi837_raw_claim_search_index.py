from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.operations import AddIndexConcurrently, TrigramExtension
from django.db import migrations


class Migration(migrations.Migration):
    atomic = False

    dependencies = [("edi835", "0048_mpl_issue_definitions")]

    operations = [
        TrigramExtension(),
        AddIndexConcurrently(
            model_name="edi837claim",
            index=GinIndex(
                fields=["raw_claim"],
                name="edi837_raw_claim_trgm_idx",
                opclasses=["gin_trgm_ops"],
            ),
        ),
    ]
