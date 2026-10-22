"""Backward-compatible token provisioning; prefer partner_security for new identities."""
import getpass
from django.core.management.base import BaseCommand, CommandError
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import transaction
from accounts.models import Client
from edi835.models import IntakePartner, PartnerCredential
from edi835.partner_management import create_partner, change_partner


class Command(BaseCommand):
    help = "Issue a legacy-compatible client-scoped token or suspend its partner."

    def add_arguments(self, parser):
        parser.add_argument("--client-code")
        parser.add_argument("--name")
        parser.add_argument("--workflow", choices=["835", "837"], action="append")
        parser.add_argument("--expires-days", type=int, default=30)
        parser.add_argument("--revoke", help="Stable partner UUID (suspends all its credentials).")
        parser.add_argument("--reason", default="Legacy operator provisioning")

    @transaction.atomic
    def handle(self, *args, **options):
        actor = f"operator-shell:{getpass.getuser()}"
        try:
            if options["revoke"]:
                identifier = options["revoke"]
                if not IntakePartner.objects.filter(pk=identifier).exists():
                    identifier = PartnerCredential.objects.get(pk=identifier).partner_id
                change_partner(identifier, action="SUSPEND", actor=actor, reason=options["reason"])
                self.stdout.write("Partner suspended.")
                return
            client = Client.objects.get(client_code=options["client_code"])
            partner = create_partner(client=client, name=options["name"], workflows=options["workflow"],
                actor=actor, reason=options["reason"], mode="TOKEN", routing_required=False)
            change_partner(partner.pk, action="PERMISSIONS", actor=actor, reason=options["reason"], data={
                "permissions": [{"action": action, "workflow": workflow} for workflow in partner.workflows
                                for action in ("SUBMIT", "READ", "DOWNLOAD")]})
            result = change_partner(partner.pk, action="TOKEN", actor=actor, reason=options["reason"],
                data={"days": options["expires_days"]})
            self.stderr.write(f"Partner ID: {partner.pk}. Token shown once; store securely, never in logs.")
            self.stdout.write(result["token"])
        except (ValueError, ObjectDoesNotExist, ValidationError) as exc:
            raise CommandError("Invalid partner provisioning request. Check client, workflows, expiry and reason.") from exc
