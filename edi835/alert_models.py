"""Persistent audit records for operational claim alert emails."""

from __future__ import annotations

import uuid

from django.db import models
from django.utils import timezone


class ClaimAlertEmail(models.Model):
    """One consolidated operational claim-alert email for one client/day/category."""

    CATEGORY_CHOICES = [
        ("CONVERSION_HOLD", "Conversion hold"),
        ("MISSING_REFERENCE", "Missing 837 / RECON"),
    ]
    STATUS_CHOICES = [
        ("PENDING", "Pending"),
        ("SENT", "Sent"),
        ("FAILED", "Failed"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey(
        "accounts.Client",
        on_delete=models.CASCADE,
        related_name="claim_alert_emails",
    )
    category = models.CharField(max_length=32, choices=CATEGORY_CHOICES, db_index=True)
    alert_date = models.DateField(help_text="Eastern-calendar date for this daily consolidated alert.")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="PENDING", db_index=True)
    subject = models.CharField(max_length=500)
    recipients = models.JSONField(default=list, blank=True)
    claims = models.JSONField(default=list, blank=True)
    body_text = models.TextField(blank=True, default="")
    sent_at = models.DateTimeField(null=True, blank=True, db_index=True)
    error_message = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "edi835"
        db_table = "claim_alert_email"
        ordering = ["-sent_at", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "category", "alert_date"],
                name="uniq_claim_alert_client_category_day",
            )
        ]
        indexes = [
            models.Index(fields=["client", "-sent_at"], name="claim_alert_client_sent_idx"),
            models.Index(fields=["category", "-sent_at"], name="claim_alert_category_sent_idx"),
        ]
