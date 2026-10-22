"""Audited provisioning. Use from the restricted operator shell, not a public API."""
import getpass
import json
from pathlib import Path

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError

from accounts.models import Client
from edi835.models import IntakePartner
from edi835.partner_management import change_partner, create_partner


class Command(BaseCommand):
    help = "Manage partner identity, approved routes, scoped permissions and rotating credentials."

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["create", "status", "suspend", "activate", "policy", "permissions",
            "token", "certificate", "retire", "revoke", "route", "retire-route"])
        parser.add_argument("--partner")
        parser.add_argument("--client-code")
        parser.add_argument("--name")
        parser.add_argument("--workflow", choices=["835", "837"], action="append")
        parser.add_argument("--mode", choices=["TOKEN", "MTLS"], default="MTLS")
        parser.add_argument("--reason")
        parser.add_argument("--config", type=Path, help="JSON operation fields; never a private key.")
        parser.add_argument("--certificate", type=Path, help="Public PEM leaf certificate, never a private key.")

    def handle(self, *args, **options):
        actor = f"operator-shell:{getpass.getuser()}"
        action = options["action"]
        try:
            if action == "status":
                partner = IntakePartner.objects.get(pk=options["partner"])
                result = {"id": str(partner.pk), "name": partner.name, "client_id": str(partner.client_id),
                    "active": partner.active, "auth_mode": partner.auth_mode, "routing_required": partner.routing_required,
                    "permissions": list(partner.permissions.values("action", "workflow", "channel")),
                    "routes": list(partner.routes.values()),
                    "credentials": list(partner.credentials.values("id", "kind", "reference", "active", "not_before", "expires_at", "revoked_at", "replaces_id"))}
            elif action == "create":
                client = Client.objects.get(client_code=options["client_code"])
                partner = create_partner(client=client, name=options["name"], workflows=options["workflow"],
                    mode=options["mode"], actor=actor, reason=options["reason"])
                result = {"partner_id": str(partner.pk)}
            else:
                data = {}
                if options["config"]:
                    if options["config"].stat().st_size > 32768:
                        raise ValueError("Configuration is too large.")
                    data = json.loads(options["config"].read_text(encoding="utf-8"))
                    if not isinstance(data, dict):
                        raise ValueError("Configuration must be a JSON object.")
                if options["certificate"]:
                    with options["certificate"].open("rb") as stream:
                        data["certificate"] = stream.read(16385)
                result = change_partner(options["partner"], action=action.upper().replace("-", "_"),
                    actor=actor, reason=options["reason"], data=data)
            if "token" in result:
                self.stderr.write("Token shown once. Store securely; do not copy into logs or tickets.")
            self.stdout.write(json.dumps(result, default=str, indent=2))
        except (ValueError, TypeError, ObjectDoesNotExist, ValidationError, IntegrityError, OSError) as exc:
            # Never echo supplied certificate/config content or database details.
            message = str(exc) if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError) else "Partner operation failed. Check identifiers, configuration and uniqueness."
            raise CommandError(message) from exc
