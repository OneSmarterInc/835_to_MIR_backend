from django.db import migrations


ISSUES = [
    (
        "MIR_CLAIM_MISSING",
        "Matching MIR claim not found",
        "No MIR claim with the same verified Highmark or internal claim identity was found for this client.",
        "Verify that the originating 837 and applicable 835 belong to the same client and conversion batch. If the claim should have been converted, correct the source association or conversion failure, regenerate the MIR, and validate it before transmission.",
        "LOCAL_CATEGORY",
        "Python evidence comparison · 837/MIR",
    ),
    (
        "CHARGE_MISMATCH",
        "837 and reconciliation charge mismatch",
        "The submitted charge stored on the matched 837 differs from the charge stored in reconciliation for this claim.",
        "Compare the 837 claim charge and service-line totals with the reconciliation source. Correct the incorrect source association, parsed value, or approved mapping, then rerun reconciliation and validate the totals.",
        "LOCAL_CATEGORY",
        "Python evidence comparison · 837/RECON",
    ),
]


def seed_issues(apps, schema_editor):
    Issue = apps.get_model("edi835", "MPLIssueDefinition")
    for code, title, description, resolution, status, source in ISSUES:
        Issue.objects.update_or_create(code=code, defaults={
            "title": title,
            "description": description,
            "resolution": resolution,
            "mapping_status": status,
            "source": source,
            "active": True,
        })


def remove_seeded_issues(apps, schema_editor):
    apps.get_model("edi835", "MPLIssueDefinition").objects.filter(
        code__in=[row[0] for row in ISSUES]
    ).delete()


class Migration(migrations.Migration):
    dependencies = [("edi835", "0050_validation_rule_catalog")]
    operations = [migrations.RunPython(seed_issues, remove_seeded_issues)]
