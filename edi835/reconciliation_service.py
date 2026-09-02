"""Claim-level MIR to RECON reconciliation.

MIR continuation rows are already normalized into one MIRClaim with all service
lines attached, so every result here is one logical MIR claim, irrespective of
the 50-service physical-row limit.
"""

from decimal import Decimal
from collections import Counter
import re

from django.conf import settings
from django.db.models import Max, Min, Q, Sum
from django.db.models.functions import Coalesce

from .models import MIRClaim, RECONClaim, RECONFile


ZERO = Decimal("0.00")


def normalize_claim_id(value):
    return re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()


def latest_recon_file(client, recon_file_id=None):
    files = RECONFile.objects.filter(client=client, status__in=("PROCESSED", "PARTIAL"))
    if recon_file_id:
        return files.filter(id=recon_file_id).first()
    return files.order_by("-processed_at", "-uploaded_at").first()


def _money(value):
    return value if value is not None else ZERO


def reconciliation_status(amount_to_pay, recon_paid, matched):
    amount_to_pay, recon_paid = _money(amount_to_pay), _money(recon_paid)
    remaining = amount_to_pay - recon_paid
    if not matched:
        return "NOT_IN_RECON", remaining
    if amount_to_pay and recon_paid and ((amount_to_pay < 0) != (recon_paid < 0)):
        return "SIGNATURE_MISMATCH", remaining
    if remaining == ZERO:
        return "CLEAR", remaining
    if recon_paid == ZERO:
        return "UNPAID", remaining
    if abs(recon_paid) < abs(amount_to_pay):
        return "PARTIALLY_PAID", remaining
    if abs(recon_paid) > abs(amount_to_pay):
        return "OVERPAID", remaining
    return "AMOUNT_MISMATCH", remaining


def reconciliation_policy():
    """Expose the interim MPL interpretation until V7/V5 contradictions are resolved."""
    mir907_source = str(getattr(settings, "MPL_RECON_MIR907_SOURCE", "computed")).lower()
    mir908_source = str(getattr(settings, "MPL_RECON_MIR908_SOURCE", "computed")).lower()
    steps = tuple(getattr(
        settings, "MPL_RECON_WATERFALL_STEPS",
        ("MIR901", "MIR907", "MIR908", "MPL920"),
    ))
    return {
        "steps": list(steps),
        "mir907_source": mir907_source if mir907_source in {"computed", "supplied"} else "computed",
        "mir908_source": mir908_source if mir908_source in {"computed", "supplied"} else "computed",
        "include_mpl920": bool(getattr(settings, "MPL_RECON_INCLUDE_MPL920", True)),
        "interim": True,
    }


def reconciliation_waterfall(amount_to_pay, recon_paid, matched, fees=None, policy=None):
    """Run named, ordered MPL matching candidates and report the winning step."""
    base, recon_paid = _money(amount_to_pay), _money(recon_paid)
    fees = fees or {}
    values = {name: _money(fees.get(name)) for name in (
        "mir904", "mir905", "mir907", "mir908", "mpl920"
    )}
    policy = policy or reconciliation_policy()
    computed_907 = base + values["mir904"]
    amount_907 = values["mir907"] if policy["mir907_source"] == "supplied" else computed_907
    computed_908 = amount_907 + values["mir905"]
    amount_908 = values["mir908"] if policy["mir908_source"] == "supplied" else computed_908
    candidates = {
        "MIR901": base,
        "MIR907": amount_907,
        "MIR908": amount_908,
        "MPL920": amount_908 + values["mpl920"],
    }
    ordered = [
        (name, candidates[name]) for name in policy["steps"]
        if name in candidates and (name != "MPL920" or policy["include_mpl920"])
    ] or [("MIR901", base)]
    affected = any(value != ZERO for value in values.values())
    result = {
        "match_step": None,
        "matched_amount": ordered[-1][1],
        "candidates": {name: str(value) for name, value in ordered},
        "affected_by_interim_policy": affected,
        "policy_flags": ([
            f"MIR907_{policy['mir907_source'].upper()}",
            f"MIR908_{policy['mir908_source'].upper()}",
            "MPL920_INCLUDED" if policy["include_mpl920"] else "MPL920_EXCLUDED",
        ] if affected else []),
    }
    if not matched:
        result.update(status="NOT_IN_RECON", remaining=base)
        return result
    if base and recon_paid and ((base < 0) != (recon_paid < 0)):
        result.update(status="SIGNATURE_MISMATCH", remaining=base - recon_paid)
        return result
    for name, candidate in ordered:
        if candidate == recon_paid:
            result.update(
                status="CLEAR", remaining=ZERO, match_step=name, matched_amount=candidate
            )
            return result
    expected = ordered[-1][1]
    status, remaining = reconciliation_status(expected, recon_paid, True)
    result.update(status=status, remaining=remaining)
    return result


def waterfall_summary(rows):
    step_counts = Counter(row.get("match_step") or "NO_MATCH" for row in rows)
    flag_counts = Counter(flag for row in rows for flag in row.get("policy_flags", []))
    return {
        "match_step_counts": dict(step_counts),
        "interim_policy_affected_records": sum(
            1 for row in rows if row.get("affected_by_interim_policy")
        ),
        "policy_flag_counts": dict(flag_counts),
    }


SORT_FIELDS = {
    "claim_id": lambda row: row["claim_id"].casefold(),
    "patient_name": lambda row: row["patient_name"].casefold(),
    "mir_filename": lambda row: row["mir_filename"].casefold(),
    "recon_filename": lambda row: row["recon_filename"].casefold(),
    "amount_to_pay": lambda row: Decimal(row["amount_to_pay"]),
    "recon_paid_amount": lambda row: Decimal(row["recon_paid_amount"]),
    "difference_amount": lambda row: Decimal(row["difference_amount"]),
    "status": lambda row: row["status"].casefold(),
}


def reconciliation_rows(
    client, recon_files=None, page=None, page_size=200, claim_id=None, search="",
    sort_by="", sort_direction="asc", status_filter="",
):
    claims = (
        MIRClaim.objects.filter(mir_file__client=client)
        .values(
            "id", "claim_control_number", "member_id", "patient_first_name",
            "patient_last_name", "service_count", "claim_sequence",
            "mir_file__mir_filename", "mir_file__converted_at",
        )
        .annotate(
            mir_charge=Coalesce(Sum("service_lines__charge_amount"), ZERO),
            mir_allowed=Coalesce(Sum("service_lines__allowed_amount"), ZERO),
            mir_payable=Coalesce(Sum("service_lines__paid_amount"), ZERO),
            mir_patient_liability=Coalesce(Sum("service_lines__patient_liability"), ZERO),
        )
        .order_by("-mir_file__converted_at", "claim_sequence")
    )
    if claim_id is not None:
        claims = claims.filter(id=claim_id)
    claims = list(claims)

    # Keep only claim-level aggregates in memory. The previous implementation
    # retained every RECONClaim model (and all MIRClaim model fields) until the
    # very end of the request, which allowed one Results request to exceed 2GB.
    recon_base = RECONClaim.objects.filter(
        recon_file__in=(recon_files or []), recon_file__status__in=("PROCESSED", "PARTIAL")
    )
    recon_by_claim = {}
    recon_summaries = recon_base.values("claim_control_number").annotate(
        paid_amount=Coalesce(Sum("paid_amount"), ZERO),
        charge_amount=Coalesce(Sum("charge_amount"), ZERO),
        service_count=Sum("service_count"),
        member_id=Max("member_id"),
        patient_control_number=Max("patient_control_number"),
        latest_date=Max("recon_file__processed_at"),
        first_filename=Min("recon_file__original_filename"),
        mir904=Coalesce(Sum("mir904_bluecard_fee"), ZERO),
        mir905=Coalesce(Sum("mir905_aea"), ZERO),
        mir907=Coalesce(Sum("mir907_amount"), ZERO),
        mir908=Coalesce(Sum("mir908_amount"), ZERO),
        mpl920=Coalesce(Sum("mpl920_pca_fee"), ZERO),
    )
    for summary in recon_summaries.iterator(chunk_size=2000):
        normalized_id = normalize_claim_id(summary["claim_control_number"])
        if normalized_id:
            aggregate = recon_by_claim.setdefault(normalized_id, {
                "raw_ids": [], "paid_amount": ZERO, "charge_amount": ZERO,
                "service_count": 0, "member_id": "", "patient_control_number": "",
                "latest_date": None, "first_filename": "",
                "mir904": ZERO, "mir905": ZERO, "mir907": ZERO,
                "mir908": ZERO, "mpl920": ZERO,
            })
            aggregate["raw_ids"].append(summary["claim_control_number"])
            aggregate["paid_amount"] += _money(summary["paid_amount"])
            aggregate["charge_amount"] += _money(summary["charge_amount"])
            aggregate["service_count"] += summary["service_count"] or 0
            for fee_name in ("mir904", "mir905", "mir907", "mir908", "mpl920"):
                aggregate[fee_name] += _money(summary[fee_name])
            aggregate["member_id"] = aggregate["member_id"] or summary["member_id"]
            aggregate["patient_control_number"] = aggregate["patient_control_number"] or summary["patient_control_number"]
            if not aggregate["latest_date"] or (summary["latest_date"] and summary["latest_date"] > aggregate["latest_date"]):
                aggregate["latest_date"] = summary["latest_date"]
            filenames = [value for value in (aggregate["first_filename"], summary["first_filename"]) if value]
            aggregate["first_filename"] = min(filenames) if filenames else ""

    output = []
    mir_claim_ids = set()
    for claim in claims:
        claim_number = normalize_claim_id(claim["claim_control_number"])
        mir_claim_ids.add(claim_number)
        recon = recon_by_claim.get(claim_number)
        recon_paid = recon["paid_amount"] if recon else ZERO
        recon_charge = recon["charge_amount"] if recon else ZERO
        recon_services = recon["service_count"] if recon else 0
        match = reconciliation_waterfall(claim["mir_payable"], recon_paid, bool(recon), recon)
        status, remaining = match["status"], match["remaining"]
        output.append({
            "mir_claim_id": claim["id"],
            "claim_id": claim_number,
            "patient_name": " ".join(part for part in [claim["patient_first_name"], claim["patient_last_name"]] if part).strip(),
            "member_id": claim["member_id"],
            "mir_filename": claim["mir_file__mir_filename"],
            "mir_date": claim["mir_file__converted_at"].isoformat(),
            "mir_service_count": claim["service_count"],
            "mir_charge_amount": str(claim["mir_charge"]),
            "mir_allowed_amount": str(claim["mir_allowed"]),
            "mir_patient_liability": str(claim["mir_patient_liability"]),
            "mp003_cross_foot_valid": (
                claim["mir_allowed"]
                == claim["mir_payable"] + claim["mir_patient_liability"]
            ),
            "amount_to_pay": str(claim["mir_payable"]),
            "recon_claim_id": None,
            "recon_filename": recon["first_filename"] if recon else "",
            "recon_date": recon["latest_date"].isoformat() if recon and recon["latest_date"] else None,
            "recon_service_count": recon_services,
            "recon_charge_amount": str(recon_charge),
            "recon_paid_amount": str(recon_paid),
            "recon_fees": {
                name.upper(): str(recon[name]) if recon else str(ZERO)
                for name in ("mir904", "mir905", "mir907", "mir908", "mpl920")
            },
            "recon_matches": [],
            "_recon_raw_ids": recon["raw_ids"] if recon else [],
            "remaining_amount": str(remaining),
            "difference_amount": str(recon_paid - match["matched_amount"]),
            "status": status,
            "match_step": match["match_step"],
            "matched_amount": str(match["matched_amount"]),
            "waterfall_candidates": match["candidates"],
            "affected_by_interim_policy": match["affected_by_interim_policy"],
            "policy_flags": match["policy_flags"],
        })

    # RECON claims without a corresponding MIR record remain visible. Their
    # RECON occurrences are aggregated exactly like matched claims, while MIR
    # values remain empty and the status explains the missing side.
    if claim_id is None:
        for claim_number, recon in recon_by_claim.items():
            if claim_number in mir_claim_ids:
                continue
            recon_paid = recon["paid_amount"]
            recon_charge = recon["charge_amount"]
            output.append({
                "mir_claim_id": None,
                "claim_id": claim_number,
                "patient_name": "",
                "member_id": recon["member_id"] or recon["patient_control_number"],
                "mir_filename": "",
                "mir_date": None,
                "mir_service_count": 0,
                "mir_charge_amount": str(ZERO),
                "mir_allowed_amount": str(ZERO),
                "mir_patient_liability": str(ZERO),
                "mp003_cross_foot_valid": None,
                "amount_to_pay": str(ZERO),
                "recon_claim_id": None,
                "recon_filename": recon["first_filename"],
                "recon_date": recon["latest_date"].isoformat() if recon["latest_date"] else None,
                "recon_service_count": recon["service_count"],
                "recon_charge_amount": str(recon_charge),
                "recon_paid_amount": str(recon_paid),
                "recon_fees": {
                    name.upper(): str(recon[name])
                    for name in ("mir904", "mir905", "mir907", "mir908", "mpl920")
                },
                "recon_matches": [],
                "_recon_raw_ids": recon["raw_ids"],
                "remaining_amount": str(-recon_paid),
                "difference_amount": str(recon_paid),
                "status": "NOT_IN_MIR",
                "match_step": None,
                "matched_amount": str(ZERO),
                "waterfall_candidates": {},
                "affected_by_interim_policy": False,
                "policy_flags": [],
            })

    search_terms = [value.strip().casefold() for value in str(search or "").split(",") if value.strip()]
    if search_terms:
        recon_filter = Q()
        for term in search_terms:
            recon_filter |= (
                Q(claim_control_number__icontains=term)
                | Q(member_id__icontains=term)
                | Q(patient_control_number__icontains=term)
                | Q(recon_file__original_filename__icontains=term)
            )
        recon_search_ids = {
            normalize_claim_id(value)
            for value in recon_base.filter(recon_filter).values_list("claim_control_number", flat=True)
        }
        normalized_terms = {normalize_claim_id(term) for term in search_terms}
        searchable_fields = (
            "claim_id", "patient_name", "member_id", "mir_filename",
            "recon_filename", "status",
        )
        output = [row for row in output if (
            any(
                term in str(row.get(field) or "").casefold()
                for term in search_terms
                for field in searchable_fields
            )
            or any(term and term in row["claim_id"] for term in normalized_terms)
            or row["claim_id"] in recon_search_ids
        )]
    if status_filter:
        output = [row for row in output if row["status"] == status_filter]
    total = len(output)
    summary = waterfall_summary(output)
    if sort_by in SORT_FIELDS:
        key = SORT_FIELDS[sort_by]
        output.sort(key=key, reverse=sort_direction == "desc")
    if page is not None:
        start = (page - 1) * page_size
        output = output[start:start + page_size]

    # Fetch per-file histories only for claims that survived pagination.
    raw_ids = {raw_id for row in output for raw_id in row.pop("_recon_raw_ids", [])}
    matches_by_claim = {}
    if raw_ids:
        page_matches = recon_base.filter(claim_control_number__in=raw_ids).values(
            "id", "claim_control_number", "paid_amount", "charge_amount", "service_count",
            "mir904_bluecard_fee", "mir905_aea", "mir907_amount", "mir908_amount",
            "mpl920_pca_fee",
            "recon_file__original_filename", "recon_file__processed_at",
        ).order_by("recon_file__processed_at", "recon_file__uploaded_at", "claim_sequence")
        for match in page_matches.iterator(chunk_size=500):
            matches_by_claim.setdefault(normalize_claim_id(match["claim_control_number"]), []).append({
                "recon_claim_id": match["id"],
                "filename": match["recon_file__original_filename"],
                "date": match["recon_file__processed_at"].isoformat() if match["recon_file__processed_at"] else None,
                "paid_amount": str(_money(match["paid_amount"])),
                "charge_amount": str(_money(match["charge_amount"])),
                "service_count": match["service_count"],
                "fees": {
                    "MIR904": str(_money(match["mir904_bluecard_fee"])),
                    "MIR905": str(_money(match["mir905_aea"])),
                    "MIR907": str(_money(match["mir907_amount"])),
                    "MIR908": str(_money(match["mir908_amount"])),
                    "MPL920": str(_money(match["mpl920_pca_fee"])),
                },
            })
    for row in output:
        row["recon_matches"] = matches_by_claim.get(row["claim_id"], [])
        if row["recon_matches"]:
            row["recon_claim_id"] = row["recon_matches"][-1]["recon_claim_id"]
            row["recon_filename"] = ", ".join(dict.fromkeys(
                match["filename"] for match in row["recon_matches"]
            ))
    return (output, total, summary) if page is not None else output
