from django.db import models
from accounts.models import Client

class OnboardingStepDefinition(models.Model):
    step_number = models.IntegerField(unique=True)
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['step_number']

    def __str__(self):
        return f"Step {self.step_number}: {self.title}"


class ClientStepStatus(models.Model):
    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('IN_PROGRESS', 'In Progress'),
        ('COMPLETED', 'Completed'),
        ('ERROR', 'Error'),
    ]
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name='onboarding_steps')
    step = models.ForeignKey(OnboardingStepDefinition, on_delete=models.CASCADE)
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='PENDING')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('client', 'step')
        ordering = ['step__step_number']


class GoLiveStepDefinition(models.Model):
    step_number = models.IntegerField(unique=True)
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['step_number']

    def __str__(self):
        return f"Go-Live Step {self.step_number}: {self.title}"


class ClientGoLiveStatus(models.Model):
    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('IN_PROGRESS', 'In Progress'),
        ('COMPLETED', 'Completed'),
        ('ERROR', 'Error'),
    ]
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name='golive_steps')
    step = models.ForeignKey(GoLiveStepDefinition, on_delete=models.CASCADE)
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='PENDING')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('client', 'step')
        ordering = ['step__step_number']


class OffboardingStepDefinition(models.Model):
    step_number = models.IntegerField(unique=True)
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['step_number']

    def __str__(self):
        return f"Offboarding Step {self.step_number}: {self.title}"


class ClientOffboardingStatus(models.Model):
    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('COMPLETED', 'Completed'),
        ('ERROR', 'Error'),
    ]
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name='offboarding_steps')
    step = models.ForeignKey(OffboardingStepDefinition, on_delete=models.CASCADE)
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='PENDING')
    document_path = models.CharField(max_length=500, blank=True, null=True) # for tracking uploaded file names if any
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('client', 'step')
        ordering = ['step__step_number']


class ClientTestEnvironment(models.Model):
    client = models.OneToOneField(Client, on_delete=models.CASCADE, related_name='test_environment')
    sftp_host = models.CharField(max_length=255, default='sftp-test.internal')
    sftp_username = models.CharField(max_length=255)
    watched_folder = models.CharField(max_length=255)
    test_status = models.CharField(max_length=50, default='In Progress')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Test Env for {self.client.name}"


class ImmutableAuditQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise PermissionError("Audit log entries are immutable.")

    def delete(self):
        raise PermissionError("Audit log entries cannot be deleted.")


class AuditLog(models.Model):
    module = models.CharField(max_length=100)
    action = models.CharField(max_length=100)
    details = models.TextField()
    performed_by = models.CharField(max_length=255)
    timestamp = models.DateTimeField(auto_now_add=True)
    
    # Optional relation if tied specifically to a client
    client = models.ForeignKey(Client, on_delete=models.SET_NULL, null=True, blank=True, related_name='audit_logs')
    previous_hash = models.CharField(max_length=64, blank=True, default="")
    entry_hash = models.CharField(max_length=64, unique=True, editable=False)

    objects = ImmutableAuditQuerySet.as_manager()

    class Meta:
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['client', '-timestamp']),
        ]

    def __str__(self):
        return f"[{self.module}] {self.action} by {self.performed_by} at {self.timestamp}"

    def save(self, *args, **kwargs):
        if self.pk:
            raise PermissionError("Audit log entries are immutable.")
        import hashlib
        from django.db import transaction

        with transaction.atomic():
            previous = type(self).objects.select_for_update().order_by("-id").first()
            self.previous_hash = previous.entry_hash if previous else ""
            payload = "|".join((self.previous_hash, self.module, self.action, self.details, self.performed_by))
            self.entry_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionError("Audit log entries cannot be deleted.")


class AdminClientAccessGrant(models.Model):
    administrator = models.ForeignKey("accounts.User", on_delete=models.CASCADE, related_name="client_access_grants")
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="administrator_access_grants")
    reason = models.TextField()
    approved_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="approved_client_access_grants")
    granted_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["administrator", "client", "expires_at"],
                name="admin_panel_adminis_725c13_idx",
            )
        ]

    @property
    def active(self):
        from django.utils import timezone
        return self.revoked_at is None and self.expires_at > timezone.now()


import uuid

class ClientDocument(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name='documents')
    document_name = models.CharField(max_length=255)
    original_filename = models.CharField(max_length=255)
    document_type = models.CharField(max_length=100, default='General Document')
    file = models.FileField(upload_to='documents/')
    file_size = models.IntegerField(default=0)
    uploaded_by = models.CharField(max_length=255, default='Admin User')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['client', 'document_type', '-created_at']),
        ]

    def __str__(self):
        return f"{self.document_name} ({self.client.name})"


class MirMappingField(models.Model):
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name='mir_mappings')
    field_id = models.CharField(max_length=50)
    map_type = models.CharField(max_length=50)
    map_value = models.TextField(blank=True, null=True)
    length = models.IntegerField()
    start = models.IntegerField()
    upper = models.BooleanField(default=False)
    trim = models.BooleanField(default=False)
    truncate = models.BooleanField(default=False)
    align = models.CharField(max_length=10)
    pad = models.CharField(max_length=10)
    fallback_type = models.CharField(max_length=50, blank=True, null=True)
    fallback_value = models.TextField(blank=True, null=True)
    technical_rule = models.TextField(blank=True, null=True)

    class Meta:
        unique_together = ('client', 'field_id')

    def __str__(self):
        return f"{self.client.name} - {self.field_id}"


class ClientSmtpConfig(models.Model):
    SECURITY_CHOICES = [
        ('STARTTLS', 'STARTTLS'),
        ('SSL_TLS',  'SSL / TLS'),
        ('NONE',     'None'),
    ]

    client        = models.OneToOneField(Client, on_delete=models.CASCADE, null=True, blank=True, related_name='smtp_config')
    sender_name   = models.CharField(max_length=255, default='OneSmarter Support')
    sender_email  = models.EmailField(default='support@onesmarter.com')
    smtp_host     = models.CharField(max_length=255, default='smtp.gmail.com')
    smtp_port     = models.IntegerField(default=587)
    smtp_username = models.CharField(max_length=255, default='support@onesmarter.com')
    smtp_password = models.TextField(blank=True)
    security      = models.CharField(max_length=20, choices=SECURITY_CHOICES, default='STARTTLS')
    reply_to      = models.EmailField(blank=True, null=True)
    use_default   = models.BooleanField(default=False, help_text="Whether to use default SMTP settings")
    created_at    = models.DateTimeField(auto_now_add=True)
    updated_at    = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'client_smtp_config'

    def __str__(self):
        return f"SMTP for {self.client.name if self.client else 'Default/Global'} ({self.smtp_host})"


def log_audit_event(module, action, details, performed_by="System", client=None):
    try:
        AuditLog.objects.create(
            module=module.upper(),
            action=action.upper(),
            details=details,
            performed_by=performed_by,
            client=client
        )
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Failed to log audit event: {e}")
