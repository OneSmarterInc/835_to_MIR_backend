"""Bounded, claim-only activity summaries independent of the recent feed."""
from collections import defaultdict

from django.db.models import F, Window
from django.db.models.functions import RowNumber

from .models import ClientPortalAction


def attach_claim_activity(client, claims: list[dict]) -> None:
    claim_ids = [claim["id"] for claim in claims if claim.get("id") is not None]
    if not claim_ids:
        for claim in claims:
            claim["review_activity"] = []
        return
    # Keep a pending request visible even when a newer note or reviewed
    # request exists. One query, at most one row per kind/status per claim.
    rows = ClientPortalAction.objects.filter(
        client=client, file__client=client, claim__edi_file__client=client,
        claim_id__in=claim_ids, file_id=F("claim__edi_file_id"),
    ).select_related("file", "created_by").only(
        "id", "kind", "status", "message", "file_id", "claim_id",
        "created_by_id", "created_by_label", "created_at", "reviewed_at",
        "file__original_filename", "created_by__name",
    ).annotate(
        activity_rank=Window(
            expression=RowNumber(),
            partition_by=[F("claim_id"), F("kind"), F("status")],
            order_by=[F("created_at").desc(), F("pk").desc()],
        )
    ).filter(activity_rank=1).order_by("-created_at", "-pk")
    # Import at call time: the existing action views depend on portal views.
    from .client_portal_actions import serialize

    by_claim = defaultdict(list)
    for action in rows:
        by_claim[action.claim_id].append(serialize(action))
    for claim in claims:
        claim["review_activity"] = by_claim[claim.get("id")]
