import uuid
from django.db import models
from django.utils import timezone


class EDI835File(models.Model):
    STATUS_CHOICES = [
        ("UPLOADED", "Uploaded"),
        ("PROCESSING", "Processing"),
        ("COMPLETED", "Completed"),
        ("ARCHIVED", "Archived"),
        ("ERROR", "Error"),
    ]

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
        help_text="Unique ID for the file."
    )
    client = models.ForeignKey(
        'accounts.Client',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='edi835_files',
        help_text="Client associated with this file."
    )
    original_filename = models.CharField(
        max_length=255,
        help_text="Original uploaded filename."
    )
    stored_filename = models.CharField(
        max_length=255,
        help_text="Unique physical filename."
    )
    status = models.CharField(
        max_length=50,
        choices=STATUS_CHOICES,
        default="UPLOADED",
        help_text="Current processing state."
    )
    claims_count = models.IntegerField(
        default=0,
        help_text="Number of claims in file."
    )
    services_count = models.IntegerField(
        default=0,
        help_text="Number of service lines in file."
    )
    records_count = models.IntegerField(
        default=0,
        help_text="Number of MIR records in file."
    )
    uploaded_at = models.DateTimeField(
        auto_now_add=True,
        help_text="Upload timestamp."
    )
    processing_started_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Processing start timestamp."
    )
    processing_completed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Processing completion timestamp."
    )
    input_path = models.CharField(
        max_length=500,
        null=True,
        blank=True,
        help_text="Input file location."
    )
    output_path = models.CharField(
        max_length=500,
        null=True,
        blank=True,
        help_text="Generated MIR location."
    )
    archive_path = models.CharField(
        max_length=500,
        null=True,
        blank=True,
        help_text="Archived file location."
    )
    error_message = models.TextField(
        null=True,
        blank=True,
        help_text="Error information if processing fails."
    )
    present_in_sftp = models.BooleanField(
        default=False,
        help_text="Boolean indicator if file is currently present in SFTP/input folder."
    )
    present_in_archive_folder = models.BooleanField(
        default=False,
        help_text="Boolean indicator if file is currently present in archive folder on disk."
    )
    ingestion_source = models.CharField(
        max_length=50,
        default="MANUAL",
        help_text="File ingestion origin: SFTP or MANUAL."
    )

    class Meta:
        db_table = "835file"
        verbose_name = "835File"
        verbose_name_plural = "835Files"
        ordering = ["-uploaded_at"]
        indexes = [
            models.Index(fields=["client", "-uploaded_at"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self):
        return f"{self.original_filename} ({self.id})"


class SFTPConfig(models.Model):
    CONNECTION_TYPES = [
        ("UNIFIED", "Unified SFTP"),
        ("INBOUND", "Inbound SFTP"),
        ("OUTBOUND", "Outbound SFTP"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey(
        'accounts.Client',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='sftp_configs',
        help_text="Client associated with this SFTP config."
    )
    name = models.CharField(max_length=255, default="SFTP Connection")
    connection_type = models.CharField(max_length=50, choices=CONNECTION_TYPES, default="UNIFIED")
    use_same_server = models.BooleanField(default=True)

    # Inbound / Unified Host Details
    host = models.CharField(max_length=255, blank=True, null=True, default="sftp.example.com")
    port = models.IntegerField(default=22)
    username = models.CharField(max_length=255, blank=True, null=True)
    password = models.CharField(max_length=255, blank=True, null=True)
    ssh_key = models.TextField(blank=True, null=True)
    auth_method = models.CharField(max_length=50, default="Password")
    trust_unknown_key = models.BooleanField(default=True)
    inbound_837_folder = models.CharField(max_length=500, blank=True, null=True, default="/relay/abc-health/in/837/")
    inbound_835_folder = models.CharField(max_length=500, blank=True, null=True, default="/relay/abc-health/in/835/")

    # Outbound Host Details (Used when use_same_server is False)
    outbound_host = models.CharField(max_length=255, blank=True, null=True)
    outbound_port = models.IntegerField(default=22)
    outbound_username = models.CharField(max_length=255, blank=True, null=True)
    outbound_password = models.TextField(blank=True, null=True)
    outbound_ssh_key = models.TextField(blank=True, null=True)
    outbound_auth_method = models.CharField(max_length=50, default="Password")
    outbound_trust_unknown_key = models.BooleanField(default=True)
    outbound_mir_folder = models.CharField(max_length=500, blank=True, null=True, default="/relay/abc-health/out/mir/")

    status = models.CharField(max_length=50, default="NOT_CONFIGURED")
    use_default = models.BooleanField(default=False, help_text="Whether to use default SFTP settings")
    last_error = models.TextField(null=True, blank=True)
    last_tested_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "sftp_config"
        verbose_name = "SFTP Configuration"
        verbose_name_plural = "SFTP Configurations"
        ordering = ["-updated_at"]

    def __str__(self):
        return f"{self.connection_type} - {self.host or 'Unconfigured'}"


class MIRFile(models.Model):
    """An exact database copy of one generated MIR output file."""

    STATUS_CHOICES = [
        ("GENERATED", "Generated"),
        ("PUSHED", "Pushed"),
        ("PUSH_FAILED", "Push failed"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_835 = models.OneToOneField(
        EDI835File,
        on_delete=models.CASCADE,
        related_name="mir_file",
    )
    client = models.ForeignKey(
        "accounts.Client",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="mir_files",
    )
    mir_filename = models.CharField(max_length=255)
    original_835_filename = models.TextField(blank=True, default="")
    file_content = models.TextField()
    file_hash = models.CharField(max_length=64, db_index=True)
    file_size = models.BigIntegerField(default=0)
    claim_count = models.PositiveIntegerField(default=0)
    physical_row_count = models.PositiveIntegerField(default=0)
    service_count = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="GENERATED")
    converted_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "mir_file"
        ordering = ["-converted_at"]
        indexes = [
            models.Index(fields=["client", "-converted_at"]),
            models.Index(fields=["mir_filename"]),
        ]


class MIRClaim(models.Model):
    mir_file = models.ForeignKey(MIRFile, on_delete=models.CASCADE, related_name="claims")
    claim_sequence = models.PositiveIntegerField()
    claim_control_number = models.CharField(max_length=64, blank=True, default="", db_index=True)
    record_type = models.CharField(max_length=2, blank=True, default="")
    member_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    patient_first_name = models.CharField(max_length=100, blank=True, default="")
    patient_last_name = models.CharField(max_length=100, blank=True, default="")
    date_of_birth = models.CharField(max_length=8, blank=True, default="")
    claim_status = models.CharField(max_length=10, blank=True, default="")
    primary_reason = models.CharField(max_length=10, blank=True, default="")
    service_count = models.PositiveIntegerField(default=0)
    chunk_count = models.PositiveIntegerField(default=1)
    header_raw = models.CharField(max_length=334)
    segment_data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "mir_claim"
        ordering = ["claim_sequence"]
        constraints = [
            models.UniqueConstraint(fields=["mir_file", "claim_sequence"], name="uniq_mir_claim_sequence"),
        ]


class MIRClaimChunk(models.Model):
    mir_claim = models.ForeignKey(MIRClaim, on_delete=models.CASCADE, related_name="chunks")
    chunk_number = models.PositiveSmallIntegerField()
    service_start_number = models.PositiveIntegerField(default=0)
    service_end_number = models.PositiveIntegerField(default=0)
    services_in_chunk = models.PositiveSmallIntegerField(default=0)
    physical_row_number = models.PositiveIntegerField()
    raw_row = models.TextField()
    row_length = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "mir_claim_chunk"
        ordering = ["physical_row_number"]
        constraints = [
            models.UniqueConstraint(fields=["mir_claim", "chunk_number"], name="uniq_mir_claim_chunk"),
            models.CheckConstraint(
                condition=models.Q(services_in_chunk__gte=0, services_in_chunk__lte=50),
                name="mir_chunk_max_50_services",
            ),
        ]


class MIRServiceLine(models.Model):
    mir_claim = models.ForeignKey(MIRClaim, on_delete=models.CASCADE, related_name="service_lines")
    mir_chunk = models.ForeignKey(MIRClaimChunk, on_delete=models.CASCADE, related_name="service_lines")
    service_sequence = models.PositiveIntegerField()
    chunk_service_sequence = models.PositiveSmallIntegerField()
    procedure_code = models.CharField(max_length=64, blank=True, default="")
    service_date = models.CharField(max_length=8, blank=True, default="")
    units = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    charge_amount = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    paid_amount = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    patient_liability = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    reason_code = models.CharField(max_length=10, blank=True, default="")
    service_raw = models.CharField(max_length=303)
    segment_data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "mir_service_line"
        ordering = ["service_sequence"]
        constraints = [
            models.UniqueConstraint(fields=["mir_claim", "service_sequence"], name="uniq_mir_service_sequence"),
            models.UniqueConstraint(fields=["mir_chunk", "chunk_service_sequence"], name="uniq_mir_chunk_service_sequence"),
            models.CheckConstraint(
                condition=models.Q(chunk_service_sequence__gte=1, chunk_service_sequence__lte=50),
                name="mir_service_position_1_50",
            ),
        ]
