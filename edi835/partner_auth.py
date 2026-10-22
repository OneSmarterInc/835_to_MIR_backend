"""Machine authentication. Nginx verifies chain/CRL; Django verifies identity.

The trusted peer AND secret are required: forwarded headers alone are not proof.
Browser sessions never substitute for an unsuccessful machine authentication.
"""
import hashlib
import hmac
import ipaddress
import uuid
from urllib.parse import unquote_to_bytes

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.x509.oid import ExtendedKeyUsageOID
from django.conf import settings
from django.utils import timezone

from .models import PartnerCredential

HEADERS = ("X-FileX-Gateway", "X-FileX-Client-Verify", "X-FileX-Client-Cert")


def machine_request(request) -> bool:
    return "Authorization" in request.headers or any(name in request.headers for name in HEADERS)


def certificate_identity(pem: bytes):
    if len(pem) > 16384 or pem.count(b"-----BEGIN CERTIFICATE-----") != 1:
        raise ValueError("Supply one leaf certificate.")
    cert = x509.load_pem_x509_certificate(pem)
    try:
        if cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
            raise ValueError("A CA certificate cannot identify a partner.")
        if ExtendedKeyUsageOID.CLIENT_AUTH not in cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value:
            raise ValueError("Certificate must permit client authentication.")
    except (x509.ExtensionNotFound, x509.DuplicateExtension) as exc:
        raise ValueError("Certificate requires basic constraints and client authentication usage.") from exc
    return cert.fingerprint(hashes.SHA256()).hex(), cert.not_valid_before_utc, cert.not_valid_after_utc


def authenticate_partner(request):
    if not settings.INTAKE_PARTNER_ENABLED or not request.is_secure():
        return None
    now = timezone.now()
    credentials = PartnerCredential.objects.select_related("partner__client").filter(
        active=True, revoked_at__isnull=True, not_before__lte=now, expires_at__gt=now,
        partner__active=True,
    )
    if any(name in request.headers for name in HEADERS):
        if not getattr(settings, "INTAKE_MTLS_ENABLED", False) or "Authorization" in request.headers:
            return None
        expected = getattr(settings, "INTAKE_MTLS_GATEWAY_SECRET", "")
        try:
            peer = ipaddress.ip_address(request.META.get("REMOTE_ADDR", ""))
            trusted = {ipaddress.ip_address(item) for item in settings.INTAKE_MTLS_TRUSTED_PEERS}
        except ValueError:
            return None
        if (peer not in trusted or len(expected) < 32
                or not hmac.compare_digest(request.headers.get(HEADERS[0], "").encode(), expected.encode())
                or request.headers.get(HEADERS[1]) != "SUCCESS"):
            return None
        value = request.headers.get(HEADERS[2], "")
        if len(value) > 24576:
            return None
        try:
            fingerprint, start, end = certificate_identity(unquote_to_bytes(value))
        except (ValueError, TypeError):
            return None
        if not start <= now < end:
            return None
        credential = credentials.filter(kind="CERTIFICATE", digest=fingerprint, partner__auth_mode="MTLS").first()
    else:
        authorization = request.headers.get("Authorization", "")
        if not authorization.startswith("Bearer ") or len(authorization) > 200:
            return None
        try:
            identifier, secret = authorization[7:].split(".", 1)
            identifier = uuid.UUID(identifier)
        except ValueError:
            return None
        credential = credentials.filter(pk=identifier, kind="TOKEN", partner__auth_mode="TOKEN").first()
        if not credential or not hmac.compare_digest(credential.digest, hashlib.sha256(secret.encode()).hexdigest()):
            return None
    if not credential or not client_ids(credential.partner).exists():
        return None
    request.partner_credential = credential
    return credential.partner


def allowed_workflows(partner, action: str) -> list[str]:
    return list(partner.permissions.filter(action=action, channel="PARTNER", workflow__in=partner.workflows)
                .values_list("workflow", flat=True))


def permitted(partner, action: str, workflow: str) -> bool:
    return partner.active and workflow in allowed_workflows(partner, action)


def accepted_authorization_valid(receipt) -> bool:
    """Natural expiry/retirement does not invalidate a previously accepted job.

Security revocation or suspension does stop queued work. Already running effects
cannot be undone here; operators must reconcile those explicitly.
    """
    partner = receipt.partner
    if (not partner or not client_permitted(partner, receipt.client_id)
            or not permitted(partner, "SUBMIT", receipt.workflow)):
        return False
    if receipt.partner_credential_id:
        return receipt.partner_credential.revoked_at is None
    if receipt.partner_authorization.get("credential_id"):
        return False
    # Legacy receipts without an authentication snapshot retain the old boundary.
    return partner.expires_at > timezone.now()


def client_ids(partner):
    from accounts.models import Client
    from django.db.models import Q
    primary = Q(pk=partner.client_id) if partner.primary_client_enabled else Q(pk__in=[])
    return Client.objects.filter(primary | Q(intake_partners=partner),
                                 status="ACTIVE").exclude(stage="offboarded").values_list("pk", flat=True)


def client_permitted(partner, client_id):
    return client_id in client_ids(partner)


def resolve_client(partner, value=None):
    from accounts.models import Client
    ids = list(client_ids(partner))
    if not value:
        if len(ids) != 1:
            raise ValueError("An explicit permitted client_id is required.")
        value = ids[0]
    if str(value) not in {str(pk) for pk in ids}:
        raise PartnerClientDenied("Client not found.")
    return Client.objects.get(pk=value, pk__in=ids)


class PartnerClientDenied(ValueError):
    pass


def consume_rate(partner):
    from django.db import transaction
    from .models import IntakePartner
    with transaction.atomic():
        current = IntakePartner.objects.select_for_update().get(pk=partner.pk)
        now = timezone.now()
        if (now - current.rate_window_started_at).total_seconds() >= 60:
            current.rate_window_started_at, current.rate_window_count = now, 0
        if current.rate_window_count >= current.requests_per_minute:
            return False
        current.rate_window_count += 1
        current.save(update_fields=["rate_window_started_at", "rate_window_count"])
        return True
