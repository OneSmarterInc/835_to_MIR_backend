"""Code-backed validation catalog for the Checks screen.

The frontend must not invent rule totals. Counts returned here are derived from
rules/checks that are actually implemented by the current backend.
"""
from django.http import JsonResponse

from admin_panel.mir_mapper_logic.mapping_defaults import DEFAULT_MAPPINGS
from admin_panel.mir_mapper_logic.rule_registry import RULE_REGISTRY


EDI837_RULES = {
    "envelope": [
        {"code": "837-ISA", "segment": "ISA", "name": "ISA envelope required", "description": "parse_837 rejects a file when ISA is missing."},
        {"code": "837-GS", "segment": "GS", "name": "GS functional group required", "description": "parse_837 rejects a file when GS is missing."},
        {"code": "837-ST", "segment": "ST01", "name": "837 transaction identifier", "description": "At least one ST transaction must identify transaction type 837."},
    ],
    "segment": [
        {"code": "837-CONTENT", "segment": "FILE", "name": "837 content required", "description": "The 837 cannot be empty."},
        {"code": "837-CLM", "segment": "CLM", "name": "At least one claim required", "description": "At least one CLM claim segment must be present."},
    ],
    "element": [
        {"code": "837-CLM01", "segment": "CLM01", "name": "Claim number required", "description": "Every parsed claim must contain a CLM01 claim control number."},
    ],
}


EDI835_ENVELOPE_RULES = [
    {"code": "835-ENV-001", "segment": "ISA", "name": "ISA header required", "description": "The interchange must contain an ISA header."},
    {"code": "835-ENV-002", "segment": "ISA", "name": "ISA first", "description": "ISA must be the first segment."},
    {"code": "835-ENV-003", "segment": "IEA", "name": "IEA last", "description": "IEA must be the final segment."},
    {"code": "835-ENV-004", "segment": "ISA/IEA", "name": "Interchange envelope balance", "description": "Exactly one ISA and one IEA are required."},
    {"code": "835-ENV-005", "segment": "GS/GE", "name": "Functional group balance", "description": "GS and GE counts must be present and balanced."},
    {"code": "835-ENV-006", "segment": "ST/SE", "name": "Transaction set balance", "description": "ST and SE counts must be present and balanced."},
    {"code": "835-ENV-007", "segment": "ISA13/IEA02", "name": "Interchange control number match", "description": "ISA13 must match IEA02."},
    {"code": "835-ENV-008", "segment": "ST01", "name": "835 transaction identifier", "description": "The transaction set must identify type 835."},
    {"code": "835-ENV-009", "segment": "ST/SE", "name": "Transaction envelope ordering", "description": "Each ST must close with SE before another ST begins."},
    {"code": "835-ENV-010", "segment": "SE01", "name": "SE segment count", "description": "SE01 must equal the actual ST-through-SE segment count."},
    {"code": "835-ENV-011", "segment": "ST02/SE02", "name": "Transaction control number match", "description": "ST02 and SE02 must be present and match."},
]

EDI835_REQUIRED_SEGMENTS = [
    {"code": "835-SEG-BPR", "segment": "BPR", "name": "Financial payment information", "description": "BPR is required."},
    {"code": "835-SEG-TRN", "segment": "TRN", "name": "Reconciliation trace", "description": "TRN is required."},
    {"code": "835-SEG-N1", "segment": "N1", "name": "Payer/payee entities", "description": "N1 payer/payee entity information is required."},
    {"code": "835-SEG-CLP", "segment": "CLP", "name": "Claim level payment", "description": "At least one CLP claim payment segment is required."},
]


def _group(key, title, unit, rules, source, description):
    return {
        "key": key,
        "title": title,
        "count": len(rules),
        "unit": unit,
        "source": source,
        "description": description,
        "rules": rules,
    }


def api_checks_catalog(request):
    mir_rules = [
        {
            "code": rule.code,
            "segment": rule.scope,
            "name": rule.name,
            "description": rule.description,
            "source": rule.source,
            "severity": rule.severity.value,
        }
        for rule in RULE_REGISTRY.definitions()
    ]

    mir_fields = [
        {
            "code": field.get("id", ""),
            "segment": field.get("section", ""),
            "name": field.get("name", ""),
            "description": field.get("desc", ""),
            "source": field.get("map", "") or field.get("mapType", ""),
        }
        for field in DEFAULT_MAPPINGS
    ]

    catalog = {
        "gate1": {
            "title": "837 as received",
            "subtitle": "From the payer, kept as the reference copy",
            "groups": [
                _group("837-envelope", "Envelope and control", "rules", EDI837_RULES["envelope"], "edi835.edi837_service.parse_837", "837 envelope and transaction identification checks actually enforced by parse_837."),
                _group("837-segment", "Segment structure", "rules", EDI837_RULES["segment"], "edi835.edi837_service.parse_837", "837 file/claim structural checks actually enforced by parse_837."),
                _group("837-element", "Element syntax", "rules", EDI837_RULES["element"], "edi835.edi837_service.parse_837", "837 required claim-element checks actually enforced by parse_837."),
            ],
        },
        "gate2": {
            "title": "835 from the claims system",
            "subtitle": "What the plan adjudicated",
            "groups": [
                _group("835-envelope", "Envelope and control", "rules", EDI835_ENVELOPE_RULES, "validate_x12_835_content", "Explicit 835 envelope/control checks implemented by the backend validator."),
                _group("835-segments", "Required business segments", "rules", EDI835_REQUIRED_SEGMENTS, "validate_x12_835_content", "Explicit required 835 business-segment checks implemented by the backend validator."),
                {
                    "key": "835-pyx12",
                    "title": "PyX12 standards validation",
                    "count": 1,
                    "unit": "engine",
                    "source": "converter.services.validator.PyX12Validator",
                    "description": "The full 835 is also validated by the active PyX12 X12 validation engine. PyX12 schema edits are not misrepresented as a made-up fixed rule count.",
                    "rules": [{"code": "PYX12", "segment": "X12 835", "name": "PyX12 standards validation", "description": "Runs pyx12.x12n_document validation against the parsed 835 transaction."}],
                },
            ],
        },
        "gate3": {
            "title": "MIR before it goes",
            "subtitle": "The last chance to catch anything",
            "groups": [
                _group("mir-fields", "Record layout and fields", "fields", mir_fields, "DEFAULT_MAPPINGS", "Fields currently defined by the active MIR mapping configuration."),
                _group("mir-rules", "Preventive MIR rules", "rules", mir_rules, "RULE_REGISTRY", "Preventive MIR rules actually registered and executed by the MIR rule engine."),
            ],
        },
    }
    return JsonResponse({"success": True, "catalog": catalog})
