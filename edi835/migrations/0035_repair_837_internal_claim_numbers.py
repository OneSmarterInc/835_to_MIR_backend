from django.db import migrations


def repair_internal_claim_numbers(apps, schema_editor):
    from edi835.claim_numbers import split_claim_number

    EDI837Claim = apps.get_model("edi835", "EDI837Claim")
    pending = []
    for claim in EDI837Claim.objects.only(
        "pk", "claim_control_number", "reference_9c",
        "highmark_claim_number", "internal_claim_number",
    ).iterator(chunk_size=2000):
        split = split_claim_number(claim.claim_control_number)
        highmark = split["highmark_claim_number"] or (claim.claim_control_number or "").strip()
        internal = (claim.reference_9c or "").strip() or split["internal_claim_number"]
        if claim.highmark_claim_number != highmark or claim.internal_claim_number != internal:
            claim.highmark_claim_number = highmark
            claim.internal_claim_number = internal
            pending.append(claim)
        if len(pending) >= 2000:
            EDI837Claim.objects.bulk_update(
                pending, ["highmark_claim_number", "internal_claim_number"], batch_size=2000
            )
            pending.clear()
    if pending:
        EDI837Claim.objects.bulk_update(
            pending, ["highmark_claim_number", "internal_claim_number"], batch_size=2000
        )


class Migration(migrations.Migration):
    dependencies = [("edi835", "0034_sftp_automation_schedule_ordering")]

    operations = [
        migrations.RunPython(repair_internal_claim_numbers, migrations.RunPython.noop),
    ]
