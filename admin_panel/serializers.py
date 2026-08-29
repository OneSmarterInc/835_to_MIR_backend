"""Safe DRF representations for administrative API resources."""

from rest_framework import serializers

from .models import AuditLog, ClientDocument, ClientOffboardingStatus, ClientSmtpConfig, MirMappingField


class MirMappingFieldSerializer(serializers.ModelSerializer):
    class Meta:
        model = MirMappingField
        fields = "__all__"
        read_only_fields = ("id",)


class ClientDocumentSerializer(serializers.ModelSerializer):
    class Meta:
        model = ClientDocument
        fields = (
            "id", "client", "document_name", "original_filename", "document_type",
            "file_size", "uploaded_by", "created_at",
        )
        read_only_fields = fields


class AuditLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = AuditLog
        fields = ("id", "module", "action", "details", "performed_by", "timestamp", "client")
        read_only_fields = fields


class ClientOffboardingStatusSerializer(serializers.ModelSerializer):
    class Meta:
        model = ClientOffboardingStatus
        fields = ("id", "client", "step", "status", "document_path", "updated_at")
        read_only_fields = fields


class ClientSmtpConfigSerializer(serializers.ModelSerializer):
    """Never serialize the stored SMTP password."""

    has_password = serializers.SerializerMethodField()

    def get_has_password(self, obj):
        return bool(obj.smtp_password)

    class Meta:
        model = ClientSmtpConfig
        fields = (
            "id", "client", "sender_name", "sender_email", "smtp_host", "smtp_port",
            "smtp_username", "security", "reply_to", "use_default", "has_password",
            "created_at", "updated_at",
        )
        read_only_fields = ("id", "has_password", "created_at", "updated_at")
