from django.db import migrations, models


ISSUES = [
    ("MP003", "Claim cross-foot", "BCBS Allowance (MIR1017) must equal Approved to Pay (MIR1018) plus Patient Liability (MIR1019) on every service line.", "Correct the inconsistent amount, regenerate the MIR, and validate the cross-foot before transmission.", "AUTHORITATIVE", "MPL_Exception-Codes-List-5_2-v21_0.docx / preventive rule registry"),
    ("MP011", "Timely filing", "A timely-filing claim must have every line denied with fund and patient-liability amounts equal to zero.", "Verify the timely-filing denial, set the approved claim- and line-level rejection handling, ensure MIR1019 is zero, then regenerate and validate.", "AUTHORITATIVE", "MPL_Exception-Codes-List-5_2-v21_0.docx / preventive rule registry"),
    ("MP013", "Missing group or subgroup number", "The required MIR group or subgroup number is blank unless the documented PR31 exception applies.", "Compare the 837 and MIR group fields, populate the approved group/subgroup value, or document the valid PR31 exception before regenerating.", "AUTHORITATIVE", "MPL_Exception-Codes-List-5_2-v21_0.docx / preventive rule registry"),
    ("RR001", "Returned claim differs from 837", "The returned claim does not match the submitted 837, commonly because charges or the service-line count changed.", "Compare the returned record with the original 837 line by line, correct the approved source or mapping, and return the claim consistently with the 837.", "EMAIL_SUPPORTED", "MPL operational email instruction"),
    ("UE036", "Room-rate acknowledgement", "The claim requires acknowledgement of the applicable private or non-private room-rate allowance.", "Verify room type, allowance and current rejection data; apply the approved CO41 acknowledgement handling only when supported, then validate and resubmit.", "EMAIL_SUPPORTED", "MPL operational email instruction"),
    ("UE084", "Reject-code handling", "The MPL instruction identifies a rejection-handling issue and requests PR31 handling.", "Confirm PR31 applies to this claim and member-liability evidence, apply the approved reject-code handling, then validate before resubmission.", "EMAIL_SUPPORTED", "MPL operational email instruction"),
    ("UE011", "Previously processed or duplicate claim", "The claim is reported as already processed or submitted more than once.", "Confirm the earlier 835 and reconciliation event and duplicate submission date; do not resubmit unless operations determines a valid correction is required.", "EMAIL_SUPPORTED", "MPL operational email instruction"),
    ("MP001", "Fund or liability differs from direction", "The fund or member-liability amount differs from the directed calculation.", "Recalculate fund and member liability from the approved source, separately accounting for supplemental fees and COB, correct the discrepant value, and revalidate.", "EMAIL_SUPPORTED", "MPL operational email instruction"),
    ("MP002", "Fund or liability differs from direction", "The fund or member-liability amount differs from the directed calculation.", "Recalculate fund and member liability from the approved source, separately accounting for supplemental fees and COB, correct the discrepant value, and revalidate.", "EMAIL_SUPPORTED", "MPL operational email instruction"),
    ("MP014", "Surprise-bill review", "The claim is reported as requiring surprise-bill classification and operations review.", "Verify the surprise-bill indicator, claim type and processing history, then route through the approved operations workflow; do not infer a financial correction.", "EMAIL_SUPPORTED", "Locally approved MPL email interpretation; convention document confirmation pending"),
    ("UE017", "UE017 convention edit", "The available evidence identifies UE017 but does not provide an approved business definition.", "Obtain the UE017 definition from the controlling convention-code document and verify it against claim evidence before correction.", "REQUIRES_MAPPING", "Code observed in MPL portal; authoritative definition unavailable"),
    ("UE106", "UE106 convention edit", "The available evidence identifies UE106 but does not provide an approved business definition.", "Obtain the UE106 definition from the controlling convention-code document and verify it against claim evidence before correction.", "REQUIRES_MAPPING", "Code observed in MPL portal; authoritative definition unavailable"),
    ("UE109", "UE109 convention edit", "The available evidence identifies UE109 but does not provide an approved business definition.", "Obtain the UE109 definition from the controlling convention-code document and verify it against claim evidence before correction.", "REQUIRES_MAPPING", "Code observed in MPL portal; authoritative definition unavailable"),
    ("UE112", "UE112 convention edit", "The available evidence identifies UE112 but does not provide an approved business definition.", "Obtain the UE112 definition from the controlling convention-code document and verify it against claim evidence before correction.", "REQUIRES_MAPPING", "Code observed in MPL portal; authoritative definition unavailable"),
    ("UE115", "UE115 convention edit", "The available evidence identifies UE115 and requests operations handling, but does not provide an approved business definition.", "Obtain the UE115 definition from the controlling convention-code document, verify the claim evidence, and route through the approved operations workflow.", "REQUIRES_MAPPING", "Code observed in MPL email; authoritative definition unavailable"),
    ("UE999", "Unknown/test convention edit", "No approved business definition is available; this value has appeared in test or captured data.", "Treat as unmapped and obtain an approved definition before changing a claim.", "REQUIRES_MAPPING", "Test/captured portal value"),
    ("INCLUSIVE_PRICING", "Inclusive pricing", "The email reports that the claim did not process because a service or allowance was inclusively priced.", "Verify the applicable inclusive-pricing rule, procedure data and allowance before making a claim change.", "LOCAL_CATEGORY", "MPL email category"),
    ("NO_PREFIX_RETURN", "No-prefix return", "The claim was returned without the expected prefix and may not have appeared on the MIR Back to the TPA file.", "Verify prefix configuration and generated MIR output; correct the approved mapping, regenerate and validate before transmission.", "LOCAL_CATEGORY", "MPL email category"),
    ("ADJUSTMENT_PENDING", "Adjustment pending", "The original claim may not have processed or the reconciliation record may not have finalized.", "Confirm the original claim status and reconciliation finalization before closing or resubmitting the adjustment.", "LOCAL_CATEGORY", "MPL email category"),
    ("NOT_PROCESSED", "Claim not processed", "The MPL email reports that the claim did not process without establishing a specific convention code.", "Use the matched 835, MIR and reconciliation history to identify the failure, then follow the approved operations workflow.", "LOCAL_CATEGORY", "MPL email category"),
    ("COB_REVIEW", "Coordination-of-benefits review", "The claim requires COB review based on the MPL email section.", "Compare COB amounts and responsibility fields across the 837, 835, MIR and reconciliation records before correction.", "LOCAL_CATEGORY", "MPL email category"),
    ("F_AND_A", "F&A claim review", "The MPL email classifies the item as an F&A claim but does not provide a specific convention error definition.", "Verify the F&A classification and processing instruction against archived claim records before operations handling.", "LOCAL_CATEGORY", "MPL email category"),
]


def seed_issues(apps, schema_editor):
    Issue = apps.get_model("edi835", "MPLIssueDefinition")
    for code, title, description, resolution, status, source in ISSUES:
        Issue.objects.update_or_create(code=code, defaults={"title": title, "description": description, "resolution": resolution, "mapping_status": status, "source": source, "active": True})


def remove_seeded_issues(apps, schema_editor):
    apps.get_model("edi835", "MPLIssueDefinition").objects.filter(code__in=[row[0] for row in ISSUES]).delete()


class Migration(migrations.Migration):
    dependencies = [("edi835", "0047_merge_claim_alert_mpl_workflows")]
    operations = [
        migrations.CreateModel(
            name="MPLIssueDefinition",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("code", models.CharField(max_length=50, unique=True)),
                ("title", models.CharField(max_length=160)),
                ("description", models.TextField()),
                ("resolution", models.TextField()),
                ("mapping_status", models.CharField(choices=[("AUTHORITATIVE", "Authoritative convention rule"), ("EMAIL_SUPPORTED", "Supported by MPL instructions"), ("LOCAL_CATEGORY", "Portal operational category"), ("REQUIRES_MAPPING", "Requires authoritative mapping")], max_length=30)),
                ("source", models.CharField(max_length=255)),
                ("active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"db_table": "mpl_issue_definition", "ordering": ["code"]},
        ),
        migrations.RunPython(seed_issues, remove_seeded_issues),
    ]
