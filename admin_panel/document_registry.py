from django.utils import timezone

from .models import ClientDocumentRegister


DOCUMENT_CATALOG = (
    {"type": "Onboarding Step 1", "name": "Mutual NDA", "category": "Legal / Confidentiality", "direction": "Both", "requires_signature": True},
    {"type": "Onboarding Step 2", "name": "Business associate agreement", "category": "Legal / HIPAA", "direction": "Both", "requires_signature": True},
    {"type": "Onboarding Step 3", "name": "Security review", "category": "Security / Compliance", "direction": "Both", "requires_signature": True},
    {"type": "Onboarding Step 8", "name": "835 validation file", "category": "Onboarding / Data", "direction": "From client", "requires_signature": False},
    {"type": "Go-Live Step 1", "name": "Go-Live authorization", "category": "Go Live / Authorization", "direction": "Both", "requires_signature": True},
    {"type": "Go-Live Step 2", "name": "Data transfer security attestation", "category": "Go Live / Security", "direction": "Both", "requires_signature": True},
    {"type": "Offboarding Step 1", "name": "Termination notice", "category": "Offboarding / Legal", "direction": "Both", "requires_signature": True},
)

CATALOG_BY_TYPE = {item["type"]: item for item in DOCUMENT_CATALOG}


def document_definition(document_type, document_name=""):
    definition = CATALOG_BY_TYPE.get(document_type)
    if definition:
        return definition
    return {
        "type": document_type or "General Document",
        "name": document_name or "General document",
        "category": document_type or "General Document",
        "direction": "From client",
        "requires_signature": False,
    }


def record_document_sent(client, document_type):
    ClientDocumentRegister.objects.update_or_create(
        client=client,
        document_type=document_type,
        defaults={"sent_at": timezone.now()},
    )

