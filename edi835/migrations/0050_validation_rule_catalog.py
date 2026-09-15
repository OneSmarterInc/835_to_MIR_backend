from django.db import migrations


# Approved validation catalog supplied by operations. Descriptions are kept
# verbatim; resolutions state the direct gate correction without inventing
# payer-specific processing instructions.
RULES = [
    ("ENV-001", "Interchange control mismatch", "ISA and IEA control numbers match", "Correct the ISA/IEA control numbers so they are identical, regenerate the 837, and rerun validation.", "AUTHORITATIVE", "837 · X12 005010 · Refuse"),
    ("ENV-002", "Functional group control mismatch", "GS and GE group control numbers match, transaction count agrees", "Correct the GS/GE control numbers and GE transaction count, regenerate the 837, and rerun validation.", "AUTHORITATIVE", "837 · X12 005010 · Refuse"),
    ("ENV-004", "Transaction segment count mismatch", "ST and SE segment count matches the trailer", "Correct SE01 to the actual ST-through-SE segment count and rerun validation.", "AUTHORITATIVE", "837 · X12 005010 · Refuse"),
    ("SEG-011", "Required segment order", "Required segments present in the required order", "Restore the required segments in the X12-defined order, regenerate the 837, and rerun validation.", "AUTHORITATIVE", "837 · X12 005010 · Refuse"),
    ("SEG-023", "Loop repeat limit", "Loop repeats within their stated limits", "Reduce or split the repeated loop data to the permitted implementation-guide limit and rerun validation.", "AUTHORITATIVE", "837 · X12 005010 · Hold"),
    ("ELM-034", "Invalid element value", "Element data types, lengths, and code list membership", "Correct the identified element's type, length, or code-list value and rerun validation.", "AUTHORITATIVE", "837 · X12 005010 · Hold"),
    ("ELM-052", "Implausible or invalid date", "Dates valid and within a plausible range", "Verify the source date, correct its X12 format or value, and rerun validation.", "AUTHORITATIVE", "837 · X12 005010 · Warn"),
    ("REF-002", "Duplicate 837 claim identifier", "Claim identifiers unique within the interchange", "Assign the correct unique claim identifier or remove the unintended duplicate claim before reprocessing.", "LOCAL_CATEGORY", "837 · Our own · Hold"),
    ("ENV-001b", "835 interchange control mismatch", "ISA and IEA control numbers match", "Correct the ISA/IEA control numbers so they are identical, regenerate the 835, and rerun validation.", "AUTHORITATIVE", "835 · X12 005010 · Refuse"),
    ("BAL-001", "835 payment balance", "BPR total equals the sum of all CLP payment amounts", "Reconcile BPR02 with the sum of CLP payment amounts, correct the source values, and rerun validation.", "AUTHORITATIVE", "835 · X12 005010 · Hold"),
    ("BAL-002", "Claim service-line balance", "Each CLP charge equals the sum of its SVC line charges", "Reconcile the CLP charge with its SVC line charges, correct the inconsistent source amount, and rerun validation.", "AUTHORITATIVE", "835 · X12 005010 · Hold"),
    ("BAL-004", "Allowance balance", "Approved to pay plus patient liability equals the BCBS allowance", "Correct the approved-to-pay, patient-liability, or allowance value so the documented balance holds, then rerun validation.", "EMAIL_SUPPORTED", "835 · MPL exception sheet · Hold"),
    ("BAL-007", "CAS adjustment balance", "CAS adjustment amounts reconcile against charge minus paid", "Reconcile CAS adjustments to charge minus paid, correct the inconsistent CAS or payment values, and rerun validation.", "AUTHORITATIVE", "835 · X12 005010 · Hold"),
    ("XRF-001", "Missing originating 837 claim", "Every claim on the 835 exists on the matching 837", "Locate and associate the originating 837 claim; if it is unavailable, keep the claim on hold for source-file review.", "LOCAL_CATEGORY", "835 · Our own · Hold"),
    ("XRF-002", "837 line-count mismatch", "Line counts per claim match the 837 exactly", "Compare the 835 and originating 837 claim lines, correct the inconsistent source or mapping, and rerun validation.", "EMAIL_SUPPORTED", "835 · Learned from a notice · Hold"),
    ("XRF-003", "837 submitted-charge mismatch", "Submitted charges unchanged from the 837", "Compare submitted charges with the originating 837, correct the changed source or mapping value, and rerun validation.", "EMAIL_SUPPORTED", "835 · Learned from a notice · Hold"),
    ("MIR-012", "Invalid MIR record length", "Fixed 150 byte record length with filler intact", "Restore the 150-byte record layout and required filler, regenerate the MIR, and rerun validation.", "AUTHORITATIVE", "MIR · MIR record layout · Refuse"),
    ("MIR-014", "Invalid signed numeric field", "Signed numeric fields carry a trailing sign", "Encode the affected numeric field with its required trailing sign and regenerate the MIR.", "AUTHORITATIVE", "MIR · MIR record layout · Refuse"),
    ("MIR-015", "Invalid decimal representation", "Implied versus explicit decimal per field, not per record", "Apply the documented decimal representation for the affected MIR field and regenerate the MIR.", "AUTHORITATIVE", "MIR · MIR record layout · Refuse"),
    ("MIR-021", "Malformed MIR ICN", "ICN base plus suffix well formed, 00 for originals", "Correct the ICN base/suffix construction, using suffix 00 only for an original claim, and regenerate the MIR.", "AUTHORITATIVE", "MIR · MIR record layout · Hold"),
    ("MIR-023", "Payment disposition sign mismatch", "Payment disposition code consistent with the sign of the amounts", "Correct the disposition code or signed amounts so they agree, then regenerate and validate the MIR.", "AUTHORITATIVE", "MIR · MIR record layout · Hold"),
    ("MPL-003", "MIR cross-foot mismatch", "Cross-foot: MIR1018 plus MIR1019 equals MIR1017", "Correct MIR1017, MIR1018, or MIR1019 so the cross-foot balances, then regenerate and validate.", "EMAIL_SUPPORTED", "MIR · MPL exception sheet · Hold"),
    ("MPL-011", "Timely-filing liability error", "Timely filing claims have all lines denied and zero liability", "Set every affected line to the approved denial handling with zero liability, then regenerate and validate.", "EMAIL_SUPPORTED", "MIR · MPL exception sheet · Hold"),
    ("MPL-013", "Missing Highmark subgroup", "Sub-group number present for the listed Highmark plan codes", "Populate the approved subgroup for the applicable Highmark plan code and regenerate the MIR.", "EMAIL_SUPPORTED", "MIR · MPL exception sheet · Hold"),
    ("MPL-011d", "Duplicate delivered ICN", "Duplicate ICN not already delivered for this client", "Confirm the prior delivered claim and keep the duplicate on hold unless an approved correction or adjustment is required.", "EMAIL_SUPPORTED", "MIR · MPL exception sheet · Hold"),
    ("MPL-036", "Private-room differential", "Private room differential carries CO41 where required", "Verify the private-room differential and apply CO41 only where the approved rule requires it, then revalidate.", "EMAIL_SUPPORTED", "MIR · Learned from a notice · Warn"),
    ("RR-001", "Returned claim mismatch", "Lines and charges returned match the original 837", "Compare returned lines and charges with the original 837, correct the mismatch, and rerun validation.", "EMAIL_SUPPORTED", "MIR · Learned from a notice · Hold"),
    ("OUT-002", "MIR trailer record-count mismatch", "Record count in the trailer matches records written", "Correct the trailer count to the number of MIR records written and regenerate the file.", "LOCAL_CATEGORY", "MIR · Our own · Refuse"),
    ("OUT-005", "PHI detected in logs", "No PHI in any log line produced by this run", "Remove or redact the PHI from logs, correct the logging path, and rerun the conversion securely.", "LOCAL_CATEGORY", "MIR · Our own · Refuse"),
]

ALIASES = {
    "MP003": "MPL-003",
    "MP011": "MPL-011",
    "MP013": "MPL-013",
    "UE036": "MPL-036",
    "RR001": "RR-001",
}


def apply_catalog(apps, schema_editor):
    Issue = apps.get_model("edi835", "MPLIssueDefinition")
    rows = {row[0]: row for row in RULES}
    for code, title, description, resolution, status, source in RULES:
        Issue.objects.update_or_create(code=code, defaults={
            "title": title,
            "description": description,
            "resolution": resolution,
            "mapping_status": status,
            "source": source,
            "active": True,
        })
    for alias, canonical in ALIASES.items():
        _, title, description, resolution, status, source = rows[canonical]
        Issue.objects.update_or_create(code=alias, defaults={
            "title": title,
            "description": description,
            "resolution": resolution,
            "mapping_status": status,
            "source": f"{source} · compatibility alias for {canonical}",
            "active": True,
        })


def reverse_catalog(apps, schema_editor):
    # Preserve pre-existing aliases on rollback; remove only canonical rows
    # introduced by this migration.
    apps.get_model("edi835", "MPLIssueDefinition").objects.filter(
        code__in=[row[0] for row in RULES]
    ).delete()


class Migration(migrations.Migration):
    dependencies = [("edi835", "0049_edi837_raw_claim_search_index")]
    operations = [migrations.RunPython(apply_catalog, reverse_catalog)]
