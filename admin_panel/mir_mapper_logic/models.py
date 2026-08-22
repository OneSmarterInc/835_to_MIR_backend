from dataclasses import dataclass, field
from decimal import Decimal
from typing import List

@dataclass
class Adjustment:
    group: str
    reason: str
    amount: Decimal
    quantity: str = ""

@dataclass
class ServiceLine:
    procedure: str = ""
    charge: Decimal = Decimal("0")
    paid: Decimal = Decimal("0")
    units: Decimal = Decimal("1")
    service_date: str = ""
    adjustments: List[Adjustment] = field(default_factory=list)

@dataclass
class Claim:
    claim_number: str = ""
    status: str = ""
    total_charge: Decimal = Decimal("0")
    total_paid: Decimal = Decimal("0")
    patient_responsibility: Decimal = Decimal("0")
    claim_reference: str = ""
    facility_type: str = ""
    claim_frequency: str = ""
    patient_last_name: str = ""
    patient_first_name: str = ""
    patient_middle: str = ""
    subscriber_id: str = ""
    group_number: str = ""
    dob: str = ""
    claim_received_date: str = ""
    adjustments: List[Adjustment] = field(default_factory=list)
    services: List[ServiceLine] = field(default_factory=list)
