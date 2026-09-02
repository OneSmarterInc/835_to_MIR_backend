"""
EDI 835 to MIR Parser Service
Pure Python implementation of EDI 835 / 853 parsing logic and MIR record generation.
"""

import math
import re
import datetime

def extract_date_from_filename(filename):
    if not filename:
        return None
    # Search for an 8-digit YYYYMMDD date string in filename
    match = re.search(r'(20\d{6})', str(filename)) or re.search(r'(\d{8})', str(filename))
    if match:
        d_str = match.group(1)
        try:
            year = int(d_str[:4])
            month = int(d_str[4:6])
            day = int(d_str[6:8])
            if 2000 <= year <= 2099 and 1 <= month <= 12 and 1 <= day <= 31:
                return d_str
        except Exception:
            pass
    return None

def parse_decimal(val, default_val=0.0):
    if not val:
        return float(default_val)
    val_str = str(val).strip()
    try:
        return float(val_str)
    except ValueError:
        return float(default_val)

def format_signed_amount(num, digits=10):
    if num is None or not isinstance(num, (int, float)) or math.isnan(num):
        num = 0.0
    sign = "+" if num >= 0 else "-"
    cents = int(round(abs(num) * 100))
    cents_str = str(cents).zfill(digits)[-digits:]
    return cents_str + sign

def format_signed_count(num, digits=5):
    if num is None or not isinstance(num, (int, float)) or math.isnan(num):
        num = 0.0
    rounded = int(round(num))
    sign = "+" if rounded >= 0 else "-"
    abs_str = str(abs(rounded)).zfill(digits)[-digits:]
    return abs_str + sign

def normalize_text(val, length):
    s = str(val or "").strip().upper()
    return s[:length]

def normalize_member_id(val):
    s = str(val or "").strip().upper()
    return s[:12]

def put_field(buffer, start_1_based, length, value, align="left"):
    val = "" if value is None else str(value)
    if len(val) > length:
        val = val[:length]
    
    if align == "right":
        val = val.rjust(length, " ")
    else:
        val = val.ljust(length, " ")
        
    start_0 = start_1_based - 1
    for i in range(length):
        buffer[start_0 + i] = val[i]

def parse_segments(text):
    text = (text or "").lstrip("\ufeff").strip()
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if "~" in normalized:
        raw = normalized.replace("\n", "").split("~")
    else:
        raw = normalized.split("\n")
        
    segments = []
    for seg in raw:
        seg_str = seg.strip()
        if seg_str:
            segments.append(seg_str)
    return segments

def parse_835(text, filename=None):
    claims = []
    current_claim = None
    current_service = None

    delimiter = "*"
    segments = parse_segments(text)
    for segment in segments:
        if segment.startswith("ISA") and len(segment) >= 4:
            delimiter = segment[3]

        parts = segment.split(delimiter)
        tag = parts[0].strip().upper()

        if tag == "CLP":
            current_claim = {
                "source_filename": filename,
                "claim_number": parts[1].strip() if len(parts) > 1 else "",
                "status": parts[2].strip() if len(parts) > 2 else "",
                "total_charge": parse_decimal(parts[3] if len(parts) > 3 else "0"),
                "total_paid": parse_decimal(parts[4] if len(parts) > 4 else "0"),
                "patient_responsibility": parse_decimal(parts[5] if len(parts) > 5 else "0"),
                "claim_reference": parts[7].strip() if len(parts) > 7 else "",
                "patient_last_name": "",
                "patient_first_name": "",
                "patient_middle": "",
                "subscriber_id": "",
                "group_number": "",
                "dob": "",
                "claim_received_date": "",
                "services": []
            }
            claims.append(current_claim)
            current_service = None
            continue

        if not current_claim:
            continue

        if tag == "NM1":
            entity = parts[1].strip().upper() if len(parts) > 1 else ""
            if entity == "QC":
                current_claim["patient_last_name"] = parts[3].strip() if len(parts) > 3 else ""
                current_claim["patient_first_name"] = parts[4].strip() if len(parts) > 4 else ""
                current_claim["patient_middle"] = parts[5].strip() if len(parts) > 5 else ""
            elif entity == "IL":
                qualifier = parts[8].strip().upper() if len(parts) > 8 else ""
                if qualifier == "MI":
                    current_claim["subscriber_id"] = parts[9].strip() if len(parts) > 9 else ""
                elif len(parts) > 1:
                    current_claim["subscriber_id"] = parts[-1].strip()
                if not current_claim["patient_last_name"] and len(parts) > 3:
                    current_claim["patient_last_name"] = parts[3].strip()
                if not current_claim["patient_first_name"] and len(parts) > 4:
                    current_claim["patient_first_name"] = parts[4].strip()
                if not current_claim["patient_middle"] and len(parts) > 5:
                    current_claim["patient_middle"] = parts[5].strip()
        elif tag == "REF":
            ref_qual = parts[1].strip().upper() if len(parts) > 1 else ""
            if ref_qual == "1L":
                current_claim["group_number"] = parts[2].strip() if len(parts) > 2 else ""
        elif tag == "DTM":
            qualifier = parts[1].strip() if len(parts) > 1 else ""
            val = parts[2].strip() if len(parts) > 2 else ""
            if qualifier == "036":
                current_claim["dob"] = val
            elif qualifier == "050":
                current_claim["claim_received_date"] = val
            elif qualifier == "472" and current_service:
                current_service["service_date"] = val
        elif tag == "SVC":
            composite = parts[1].strip() if len(parts) > 1 else ""
            svc_code = composite.split(":")[0].strip() if composite else ""
            current_service = {
                "service_code": svc_code,
                "charge": parse_decimal(parts[2] if len(parts) > 2 else "0"),
                "paid": parse_decimal(parts[3] if len(parts) > 3 else "0"),
                "units": parse_decimal(parts[5] if len(parts) > 5 else "1", 1.0),
                "service_date": "",
                "adjustments": []
            }
            current_claim["services"].append(current_service)
        elif tag == "CAS" and current_service:
            group = parts[1].strip().upper() if len(parts) > 1 else ""
            idx = 2
            while idx < len(parts):
                reason = parts[idx].strip() if idx < len(parts) else ""
                amount_text = parts[idx + 1].strip() if idx + 1 < len(parts) else "0"
                quantity = parts[idx + 2].strip() if idx + 2 < len(parts) else ""
                if reason:
                    current_service["adjustments"].append({
                        "group": group,
                        "reason": reason,
                        "amount": parse_decimal(amount_text),
                        "quantity": quantity
                    })
                idx += 3

    return claims

def get_co_adjustment_total(service):
    total = 0.0
    for adj in service.get("adjustments", []):
        if adj.get("group") == "CO":
            total += adj.get("amount", 0.0)
    return total

def get_covered_charge(service):
    val = service.get("charge", 0.0) - get_co_adjustment_total(service)
    return val

def get_patient_liability(service):
    val = get_covered_charge(service) - service.get("paid", 0.0)
    return val

def get_first_adjustment_code(service, filter_group=None):
    for adj in service.get("adjustments", []):
        if filter_group and adj.get("group") != filter_group:
            continue
        candidate = (adj.get("group") or "") + (adj.get("reason") or "")
        if candidate:
            return normalize_text(candidate, 5)
    return ""

def get_claim_primary_reason(claim):
    if claim.get("status") != "1":
        for service in claim.get("services", []):
            code = get_first_adjustment_code(service)
            if code:
                return code
        return ""
    for service in claim.get("services", []):
        for adj in service.get("adjustments", []):
            if adj.get("group") == "CO" and adj.get("reason") != "45" and adj.get("amount", 0.0) >= service.get("charge", 0.0) and service.get("charge", 0.0) > 0:
                return normalize_text("CO" + adj.get("reason"), 5)
    return ""

def get_service_status_and_reason(service, claim_status, inherited_reason=""):
    if claim_status != "1":
        return {
            "status": normalize_text(claim_status, 1),
            "reason": get_first_adjustment_code(service) or inherited_reason
        }
    if inherited_reason:
        return {
            "status": normalize_text(claim_status, 1),
            "reason": inherited_reason
        }
    if service.get("paid", 0.0) == 0.0 and get_patient_liability(service) > 0.0:
        for adj in service.get("adjustments", []):
            if adj.get("group") == "PR":
                if adj.get("reason") not in ["1", "2", "3"]:
                    return {
                        "status": "4",
                        "reason": normalize_text("PR" + adj.get("reason"), 5)
                    }
    return {
        "status": normalize_text(claim_status, 1),
        "reason": ""
    }

def build_mir_header(claim, sequence, max_sequence, service_count, filename=None):
    b = [" "] * 334
    primary_reason = get_claim_primary_reason(claim)

    # Processing date (current date) in YYYYMMDDYYYYMMDD format (16 characters total across pos 37 and pos 45)
    processing_date_8 = datetime.date.today().strftime("%Y%m%d")

    put_field(b, 1, 2, "MO")
    put_field(b, 3, 17, claim.get("claim_number", ""))
    put_field(b, 20, 6, claim.get("claim_reference", ""))
    # Position 37: Date of processing in YYYYMMDDYYYYMMDD format (16 characters total across pos 37 and pos 45)
    put_field(b, 37, 8, processing_date_8)
    put_field(b, 45, 8, processing_date_8)
    put_field(b, 53, 1, claim.get("status", ""))
    put_field(b, 55, 5, primary_reason)
    put_field(b, 60, 12, normalize_member_id(claim.get("subscriber_id", "")))
    
    # Position 77: Group number (if empty or missing, replace with 99999999)
    grp_num = (claim.get("group_number") or "").strip()
    if not grp_num:
        grp_num = "99999999"
    put_field(b, 77, 8, grp_num)

    put_field(b, 86, 20, claim.get("patient_last_name", ""))
    put_field(b, 106, 10, claim.get("patient_first_name", ""))
    put_field(b, 116, 1, claim.get("patient_middle", ""))
    put_field(b, 118, 8, claim.get("dob", ""))

    zero_amount = "0000000000+"
    put_field(b, 131, 11, zero_amount)
    put_field(b, 142, 11, zero_amount)
    put_field(b, 153, 11, zero_amount)
    put_field(b, 164, 11, zero_amount)
    put_field(b, 175, 11, zero_amount)
    put_field(b, 186, 11, zero_amount)
    put_field(b, 197, 11, zero_amount)
    put_field(b, 233, 11, zero_amount)

    put_field(b, 249, 2, str(sequence).zfill(2))
    put_field(b, 251, 2, str(max_sequence).zfill(2))
    put_field(b, 333, 2, str(service_count).zfill(2))

    return "".join(b)

def build_mir_service_block(service, claim_status, inherited_reason=""):
    b = [" "] * 303
    sr = get_service_status_and_reason(service, claim_status, inherited_reason)

    put_field(b, 16, 1, sr["status"])
    put_field(b, 18, 5, sr["reason"])
    put_field(b, 23, 6, format_signed_count(service.get("units", 1), 5))
    put_field(b, 29, 6, "00000+")
    put_field(b, 35, 5, "0000+")
    put_field(b, 40, 11, "0000000000+")
    put_field(b, 51, 11, format_signed_amount(service.get("charge", 0.0)))
    put_field(b, 62, 11, "0000000000+")
    put_field(b, 73, 11, "0000000000+")
    put_field(b, 84, 11, format_signed_amount(get_covered_charge(service)))
    put_field(b, 95, 11, format_signed_amount(service.get("paid", 0.0)))
    put_field(b, 106, 11, format_signed_amount(get_patient_liability(service)))
    put_field(b, 117, 5, "0000+")
    put_field(b, 122, 11, "0000000000+")
    put_field(b, 133, 11, "0000000000+")

    # Payment reductions
    pr_map = {}
    for adj in service.get("adjustments", []):
        if adj.get("group") == "PR":
            try:
                r_num = int(adj.get("reason", ""))
                if 1 <= r_num <= 10:
                    pr_map[r_num] = pr_map.get(r_num, 0.0) + adj.get("amount", 0.0)
            except ValueError:
                pass

    for slot in range(1, 11):
        reason_pos = 144 + (slot - 1) * 16
        amount_pos = 149 + (slot - 1) * 16
        if slot in pr_map:
            put_field(b, reason_pos, 5, f"PR{slot}")
            put_field(b, amount_pos, 11, format_signed_amount(pr_map[slot]))
        else:
            put_field(b, reason_pos, 5, "")
            put_field(b, amount_pos, 11, "0000000000+")

    return "".join(b)

def generate_mir_text(claims, filename=None):
    records = []
    total_services = 0

    for claim in claims:
        services = claim.get("services", [])
        total_services += len(services)

        max_per_record = 50
        chunks = []
        if not services:
            chunks.append([])
        else:
            for i in range(0, len(services), max_per_record):
                chunks.append(services[i:i + max_per_record])

        max_sequence = len(chunks)
        inherited_reason = get_claim_primary_reason(claim)
        claim_fn = filename or claim.get("source_filename")

        for seq_idx, chunk in enumerate(chunks, start=1):
            rec = build_mir_header(claim, seq_idx, max_sequence, len(chunk), filename=claim_fn)
            for svc in chunk:
                rec += build_mir_service_block(svc, claim.get("status", ""), inherited_reason)
            records.append(rec)

    # Join records cleanly while preserving fixed-width line lengths
    text = "\n".join([r.rstrip("\r\n") for r in records if r and r.strip()]) + ("\n" if records else "")
    return {
        "text": text,
        "claims_count": len(claims),
        "services_count": total_services,
        "records_count": len(records)
    }

def parse_835_to_mir(text, filename=None, client=None):
    from admin_panel.mir_mapper_logic.converter import convert_835_to_mir
    mir_text, summary = convert_835_to_mir(text, client=client)
    return {
        "text": mir_text,
        "claims_count": summary["claims"],
        "services_count": summary["services"],
        "records_count": summary["mir_records"],
        "delivered_claims_count": summary.get("delivered_claims", summary["claims"]),
        "delivered_services_count": summary.get("delivered_services", summary["services"]),
        "held_claims_count": summary.get("held_claims", 0),
        "conversion_findings": summary.get("findings", []),
    }
