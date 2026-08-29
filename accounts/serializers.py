from rest_framework import serializers

from .models import Client, ClientContact, User


class ClientSerializer(serializers.ModelSerializer):
    class Meta:
        model = Client
        fields = (
            "id", "name", "client_code", "email", "phone", "address",
            "status", "claims_system", "owner", "stage", "progress_pct",
            "live_since", "mir_filename_format", "created_at", "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at")


class UserSerializer(serializers.ModelSerializer):
    client_id = serializers.SerializerMethodField()
    client_name = serializers.SerializerMethodField()

    def get_client_id(self, obj):
        return str(obj.client_id) if obj.client_id else None

    def get_client_name(self, obj):
        return obj.client.name if obj.client_id else None

    class Meta:
        model = User
        fields = (
            "id", "email", "name", "mobile", "client_id", "client_name",
            "is_active", "is_staff", "first_login", "totp_enabled",
            "last_login", "created_at", "updated_at",
        )
        read_only_fields = fields


class ClientContactSerializer(serializers.ModelSerializer):
    class Meta:
        model = ClientContact
        fields = ("id", "client", "role_name", "name", "email", "phone", "created_at")
        read_only_fields = ("id", "created_at")
