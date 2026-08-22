"""Persistence and validation for editable MIR mapping configuration."""
from __future__ import annotations

import json
import logging
import os
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

from filelock import FileLock

from . import config
from .mapping_defaults import defaults
from .mapping_engine import validate_formula

logger = logging.getLogger(__name__)
CONFIG_PATH = config.MAPPING_CONFIG_PATH

ALLOWED_MAP_TYPES = {"Direct from 835", "Formula", "Hardcoded Text", "System / Runtime", "Blank"}
ALLOWED_SOURCES = {
    "CLP01","CLP02","CLP03","CLP04","CLP05","CLP07","CLP08","CLP09",
    "NM1[QC].NM103","NM1[QC].NM104","NM1[QC].NM105","NM1[IL,MI].NM109",
    "REF[1L].REF02","DTM[036].DTM02","DTM[050].DTM02",
    "SVC01","SVC02","SVC03","SVC05","DTM[472].DTM02",
    "CAS.group","CAS.reason","CAS.amount",
}
ALLOWED_SYSTEM = {"PROCESS_DATE","RECORD_SEQUENCE","MAX_RECORD_SEQUENCE","SERVICE_COUNT"}
SERVICE_ONLY_SOURCES = {"SVC01", "SVC02", "SVC03", "SVC05", "DTM[472].DTM02"}


def _lock() -> FileLock:
    return FileLock(f"{CONFIG_PATH}.lock", timeout=10)


def _merge_saved(saved: list[dict[str, Any]]) -> list[dict[str, Any]]:
    base = defaults()
    by_id = {f["id"]: f for f in base}
    allowed_editable = {
        "mapType","map","length","start","upper","trim","truncate","align","pad",
        "fallbackType","fallbackValue","technicalRule",
    }
    for item in saved:
        target = by_id.get(item.get("id"))
        if not target:
            continue
        for key in allowed_editable:
            if key in item:
                target[key] = item[key]
        target["end"] = int(target["start"]) + int(target["length"]) - 1
    return base


def get_mappings(client=None) -> list[dict[str, Any]]:
    from admin_panel.models import MirMappingField
    base = defaults()
    by_id = {f["id"]: f for f in base}
    
    if client is None:
        return base
        
    saved_records = MirMappingField.objects.filter(client=client)
    if not saved_records.exists():
        return base
        
    for record in saved_records:
        target = by_id.get(record.field_id)
        if not target:
            continue
        target["mapType"] = record.map_type
        target["map"] = record.map_value or ""
        target["length"] = record.length
        target["start"] = record.start
        target["upper"] = record.upper
        target["trim"] = record.trim
        target["truncate"] = record.truncate
        target["align"] = record.align
        target["pad"] = record.pad
        target["fallbackType"] = record.fallback_type or ""
        target["fallbackValue"] = record.fallback_value or ""
        target["technicalRule"] = record.technical_rule or ""
        target["end"] = int(target["start"]) + int(target["length"]) - 1
        
    try:
        issues = validate_mappings(base)
        if issues:
            raise ValueError("; ".join(issues))
    except Exception as e:
        logger.warning("Saved mapping configuration is invalid for client %s; using defaults: %s", client, e)
        return defaults()
        
    return base


def reset_mappings(client=None) -> list[dict[str, Any]]:
    from admin_panel.models import MirMappingField
    if client is not None:
        MirMappingField.objects.filter(client=client).delete()
    return defaults()


def validate_mappings(fields: list[dict[str, Any]]) -> list[str]:
    issues: list[str] = []
    baseline = {f["id"]: f for f in defaults()}
    expected_ids = set(baseline)
    seen = set()
    by_scope: dict[str, list[tuple[int,int,str]]] = {"header": [], "service": []}

    for index, f in enumerate(fields):
        if not isinstance(f, dict):
            issues.append(f"Mapping item {index + 1} must be an object")
            continue
        fid = str(f.get("id", ""))
        if fid not in expected_ids:
            issues.append(f"Unknown MIR field: {fid or '(missing id)'}")
            continue
        if fid in seen:
            issues.append(f"Duplicate MIR field: {fid}")
        seen.add(fid)
        try:
            start = int(f.get("start", 0))
            length = int(f.get("length", 0))
        except (TypeError, ValueError):
            issues.append(f"{fid}: start/size must be numbers")
            continue
        if start < 1 or length < 1:
            issues.append(f"{fid}: invalid start/size")
            continue
        scope = baseline[fid]["scope"]
        max_len = config.MIR_SERVICE_BLOCK_LENGTH if scope == "Service" else config.MIR_HEADER_LENGTH
        end = start + length - 1
        if end > max_len:
            issues.append(f"{fid}: position {start}-{end} exceeds {'service' if scope == 'Service' else 'header'} length {max_len}")
        bucket = "service" if scope == "Service" else "header"
        by_scope[bucket].append((start, end, fid))

        mt = f.get("mapType")
        mapping = str(f.get("map", ""))
        if mt not in ALLOWED_MAP_TYPES:
            issues.append(f"{fid}: unsupported mapping type {mt!r}")
        elif mt != "Blank" and mt != "Formula" and not mapping.strip():
            issues.append(f"{fid}: mapping/value is empty")
        elif mt == "Formula" and not str(f.get("technicalRule") or mapping).strip():
            issues.append(f"{fid}: formula is empty")
        elif mt == "Direct from 835" and mapping not in ALLOWED_SOURCES:
            issues.append(f"{fid}: unsupported 835 source {mapping!r}")
        elif mt == "Direct from 835" and scope != "Service" and mapping in SERVICE_ONLY_SOURCES:
            issues.append(f"{fid}: 835 source {mapping!r} is only available in Service scope")
        elif mt == "System / Runtime" and mapping not in ALLOWED_SYSTEM:
            issues.append(f"{fid}: unsupported system value {mapping!r}")
        elif mt == "Formula":
            try:
                validate_formula(str(f.get("technicalRule") or mapping), scope)
            except ValueError as exc:
                issues.append(f"{fid}: {exc}")

        if baseline[fid]["type"] == "N" and mt in {"Direct from 835", "Formula"} and length not in {5, 6, 11}:
            issues.append(f"{fid}: calculated numeric fields must have length 5, 6, or 11")

        if f.get("align") not in {"left", "right"}:
            issues.append(f"{fid}: alignment must be left or right")
        pad = str(f.get("pad", " "))
        if len(pad) != 1:
            issues.append(f"{fid}: pad character must be one character")
        elif not pad.isascii():
            issues.append(f"{fid}: pad character must be ASCII")
        if mt == "Hardcoded Text" and not mapping.isascii():
            issues.append(f"{fid}: hardcoded value must be ASCII")
        if f.get("fallbackType") == "Hardcoded" and not str(f.get("fallbackValue", "")).isascii():
            issues.append(f"{fid}: fallback value must be ASCII")

    # Existing MIR fields intentionally have gaps, but configured fields must not overlap.
    missing = sorted(expected_ids - seen)
    if missing:
        issues.append("Missing MIR field(s): " + ", ".join(missing))
    for bucket, ranges in by_scope.items():
        ranges.sort()
        for (s1,e1,id1),(s2,e2,id2) in zip(ranges, ranges[1:]):
            if s2 <= e1:
                issues.append(f"{bucket}: {id1} ({s1}-{e1}) overlaps {id2} ({s2}-{e2})")

    return issues


def save_mappings(fields: list[dict[str, Any]], client=None) -> list[dict[str, Any]]:
    from admin_panel.models import MirMappingField
    base_by_id = {f["id"]: f for f in defaults()}
    if any(not isinstance(field, dict) for field in fields):
        raise ValueError("Every mapping field must be an object")
    unknown_ids = [str(f.get("id", "") or "(missing id)") for f in fields if f.get("id") not in base_by_id]
    if unknown_ids:
        raise ValueError("Unknown MIR field(s): " + ", ".join(unknown_ids))
    normalized: list[dict[str, Any]] = []
    for incoming in fields:
        fid = incoming.get("id")
        if fid not in base_by_id:
            continue
        merged = deepcopy(base_by_id[fid])
        for key in ("mapType","map","length","start","upper","trim","truncate","align","pad","fallbackType","fallbackValue","technicalRule"):
            if key in incoming:
                merged[key] = incoming[key]
        if merged["mapType"] == "Formula" and not str(merged.get("technicalRule", "")).strip():
            merged["technicalRule"] = str(merged.get("map", "")).strip()
        merged["start"] = int(merged["start"])
        merged["length"] = int(merged["length"])
        merged["end"] = merged["start"] + merged["length"] - 1
        normalized.append(merged)

    issues = validate_mappings(normalized)
    if issues:
        raise ValueError("\n".join(issues))

    if client is None:
        raise ValueError("A client must be specified to save mapping configuration.")

    for item in normalized:
        MirMappingField.objects.update_or_create(
            client=client,
            field_id=item["id"],
            defaults={
                "map_type": item.get("mapType", ""),
                "map_value": item.get("map", ""),
                "length": item.get("length", 1),
                "start": item.get("start", 1),
                "upper": bool(item.get("upper", False)),
                "trim": bool(item.get("trim", False)),
                "truncate": bool(item.get("truncate", False)),
                "align": item.get("align", "left"),
                "pad": item.get("pad", " "),
                "fallback_type": item.get("fallbackType", ""),
                "fallback_value": item.get("fallbackValue", ""),
                "technical_rule": item.get("technicalRule", ""),
            }
        )

    return get_mappings(client)
