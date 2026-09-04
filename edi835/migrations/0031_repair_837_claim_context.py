from django.db import migrations, models


def repair_claim_context(apps, schema_editor):
    from edi835.claim_numbers import split_claim_number
    from edi835.edi837_service import parse_837

    EDI837File = apps.get_model("edi835", "EDI837File")
    EDI837Claim = apps.get_model("edi835", "EDI837Claim")
    fields = [
        "member_id", "patient_first_name", "patient_last_name",
        "subscriber_first_name", "subscriber_last_name",
        "billing_provider_name", "rendering_provider_name",
        "referring_provider_name", "payer_name", "claim_frequency_code",
        "original_claim_number", "highmark_claim_number",
        "internal_claim_number",
    ]
    for edi_file in EDI837File.objects.only("pk", "file_content").iterator(chunk_size=50):
        try:
            parsed = parse_837(edi_file.file_content)
        except (TypeError, ValueError):
            continue
        stored = list(EDI837Claim.objects.filter(edi_file_id=edi_file.pk).order_by("claim_sequence"))
        if len(stored) != len(parsed["claims"]):
            continue
        for claim, data in zip(stored, parsed["claims"]):
            split = split_claim_number(data["claim_control_number"])
            if not split["internal_claim_number"] and data["reference_9c"]:
                split["internal_claim_number"] = data["reference_9c"]
            claim.member_id = data["member_id"]
            claim.patient_first_name = data["patient_first"]
            claim.patient_last_name = data["patient_last"]
            claim.subscriber_first_name = data["subscriber_first"]
            claim.subscriber_last_name = data["subscriber_last"]
            claim.billing_provider_name = data["billing_provider"]
            claim.rendering_provider_name = data["rendering_provider"]
            claim.referring_provider_name = data["referring_provider"]
            claim.payer_name = data["payer"]
            claim.claim_frequency_code = data["claim_frequency_code"]
            claim.original_claim_number = data["original_claim_number"]
            claim.highmark_claim_number = split["highmark_claim_number"]
            claim.internal_claim_number = split["internal_claim_number"]
        EDI837Claim.objects.bulk_update(stored, fields, batch_size=1000)


class Migration(migrations.Migration):
    dependencies = [("edi835", "0030_add_837_claim_tables")]

    operations = [
        migrations.AddField(
            model_name="edi837claim",
            name="claim_frequency_code",
            field=models.CharField(blank=True, default="", max_length=5),
        ),
        migrations.AddField(
            model_name="edi837claim",
            name="original_claim_number",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AddField(
            model_name="edi837claim",
            name="referring_provider_name",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.RunPython(repair_claim_context, migrations.RunPython.noop),
    ]
