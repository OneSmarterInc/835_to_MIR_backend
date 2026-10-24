"""Read-only application readiness; does not certify AWS networking or TLS."""
import ipaddress
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from edi835.models import IntakePartner
from edi835.partner_routing import route_enforcement_enabled


class Command(BaseCommand):
    help = "Check application mTLS configuration, permissions, routes and credential expiry."

    def add_arguments(self, parser):
        parser.add_argument("--require-mtls", action="store_true")

    def handle(self, *args, **options):
        failures = []
        enabled = getattr(settings, "INTAKE_MTLS_ENABLED", False)
        if options["require_mtls"] and not enabled:
            failures.append("INTAKE_MTLS_ENABLED is false.")
        if enabled:
            if not settings.INTAKE_PARTNER_ENABLED:
                failures.append("INTAKE_PARTNER_ENABLED is false.")
            if len(settings.INTAKE_MTLS_GATEWAY_SECRET) < 32:
                failures.append("Configure a strong gateway secret of at least 32 characters.")
            try:
                if not settings.INTAKE_MTLS_TRUSTED_PEERS:
                    raise ValueError
                for value in settings.INTAKE_MTLS_TRUSTED_PEERS:
                    peer = ipaddress.ip_address(value)
                    if peer.is_unspecified or peer.is_multicast:
                        raise ValueError
            except ValueError:
                failures.append("Configure explicit gateway peer IP addresses, never a wildcard or CIDR.")
        now = timezone.now()
        if not route_enforcement_enabled():
            self.stdout.write("Envelope route matching is disabled (under development); saved routes are informational.")
        for partner in IntakePartner.objects.filter(active=True, auth_mode="MTLS").prefetch_related("credentials", "permissions", "routes"):
            credentials = [c for c in partner.credentials.all() if c.kind == "CERTIFICATE" and c.active
                           and not c.revoked_at and c.not_before <= now < c.expires_at]
            if not credentials:
                failures.append(f"Partner {partner.pk}: no currently valid certificate.")
            elif max(c.expires_at for c in credentials) < now + timedelta(days=14):
                self.stderr.write(f"WARNING partner {partner.pk}: certificates expire within 14 days.")
            for permission in partner.permissions.all():
                if route_enforcement_enabled() and permission.action == "SUBMIT" and permission.workflow in partner.workflows and not any(r.active and r.workflow == permission.workflow
                        and r.client_id == partner.client_id for r in partner.routes.all()):
                    failures.append(f"Partner {partner.pk}: SUBMIT {permission.workflow} has no approved route.")
        if failures:
            raise CommandError("\n".join(failures))
        self.stdout.write("Application configuration checks passed. AWS gateway, certificate-chain/CRL and direct-access denial still require deployment tests.")
