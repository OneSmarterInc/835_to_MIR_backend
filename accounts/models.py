import uuid

from django.contrib.auth.models import (
    AbstractBaseUser,
    PermissionsMixin,
    BaseUserManager,
)
from django.db import models


class Client(models.Model):
    STATUS_CHOICES = [
        ("ACTIVE", "Active"),
        ("INACTIVE", "Inactive"),
    ]

    STAGE_CHOICES = [
        ("onboarding", "Onboarding"),
        ("onboarding_completed", "Onboarding Completed"),
        ("golive_pending", "Go Live Pending"),
        ("production_pending", "Production Pending"),
        ("production", "Production"),
        ("offboarded", "Offboarded"),
    ]

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    name = models.CharField(
        max_length=255,
        help_text="Client / Organization Name",
    )

    client_code = models.CharField(
        max_length=50,
        unique=True,
        help_text="Unique Client Identifier",
    )

    email = models.EmailField(
        help_text="Primary Contact Email",
    )

    phone = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        help_text="Contact Phone",
    )

    address = models.TextField(
        blank=True,
        null=True,
        help_text="Physical / Billing Address",
    )

    state = models.CharField(
        max_length=2,
        blank=True,
        null=True,
        help_text="US state abbreviation",
    )

    zip_code = models.CharField(
        max_length=10,
        blank=True,
        null=True,
        help_text="US ZIP or ZIP+4 code",
    )

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="ACTIVE",
    )

    notes = models.TextField(
        blank=True,
        null=True,
        help_text="Administrative notes",
    )

    claims_system = models.CharField(
        max_length=100,
        default="Vendor Hosted",
        help_text="Claims System (e.g., Epic, Facets)",
    )

    owner = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text="Assigned admin user",
    )

    stage = models.CharField(
        max_length=50,
        choices=STAGE_CHOICES,
        default="onboarding",
    )

    progress_pct = models.IntegerField(
        default=0,
        help_text="Onboarding progress percentage (0-100)",
    )

    live_since = models.DateTimeField(
        blank=True,
        null=True,
    )

    mir_filename_format = models.CharField(
        max_length=255,
        default="MIROUT_YYYY_MMDD_.MIR",
        help_text="Preferred MIR filename format",
    )

    edi837_filename_format = models.CharField(
        max_length=255,
        default="YYYYMMDDhhmmss.837",
        help_text="Preferred 837 outbound filename format",
    )

    timezone = models.CharField(
        max_length=64,
        default="America/New_York",
        help_text="IANA timezone used for client-entered schedules.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "client"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} ({self.client_code})"

class UserManager(BaseUserManager):

    def create_user(
        self,
        email,
        name,
        mobile,
        password=None,
        **extra_fields,
    ):
        if not email:
            raise ValueError("Email is required")

        email = self.normalize_email(email)

        user = self.model(
            email=email,
            name=name,
            mobile=mobile,
            **extra_fields,
        )

        user.set_password(password)
        user.save(using=self._db)

        return user

    def create_superuser(
        self,
        email,
        name,
        mobile,
        password=None,
        **extra_fields,
    ):
        extra_fields["is_staff"] = True
        extra_fields["is_superuser"] = True
        extra_fields.setdefault("is_active", True)

        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")

        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")

        user = self.create_user(
            email=email,
            name=name,
            mobile=mobile,
            password=password,
            **extra_fields,
        )

        return user


class User(AbstractBaseUser, PermissionsMixin):
    email = models.EmailField(unique=True)
    name = models.CharField(max_length=150)
    mobile = models.CharField(max_length=20, unique=True)
    client = models.ForeignKey(Client, on_delete=models.SET_NULL, null=True, blank=True, related_name="users")
    totp_secret = models.CharField(max_length=64, blank=True, null=True)
    totp_enabled = models.BooleanField(default=False)
    recovery_codes = models.JSONField(default=list, blank=True)
    last_totp_counter = models.BigIntegerField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    admin_screens = models.JSONField(null=True, blank=True, default=None, help_text="Administrative navigation screens explicitly assigned to this administrator.")
    first_login = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    objects = UserManager()
    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["name", "mobile"]

    def __str__(self):
        return self.email


class ClientContact(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="contacts")
    role_name = models.CharField(max_length=100)
    name = models.CharField(max_length=150)
    email = models.EmailField(blank=True, null=True)
    phone = models.CharField(max_length=50, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "client_contact"
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.name} - {self.role_name} ({self.client.name})"


class EmployeeRole(models.Model):
    role_name = models.CharField(max_length=255, unique=True)
    description = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "employee_role"
        ordering = ["role_name"]

    def __str__(self):
        return self.role_name


class ClientStepComment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="step_comments")
    step_number = models.IntegerField()
    comment = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    author = models.CharField(max_length=100, default="System")

    class Meta:
        db_table = "client_step_comment"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["client", "step_number", "-created_at"])]
