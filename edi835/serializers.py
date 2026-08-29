from rest_framework import serializers

from .models import EDI835File, MIRClaim, MIRFile, RECONFile, SFTPConfig


class MIRFileMetadataSerializer(serializers.ModelSerializer):
    class Meta:
        model = MIRFile
        fields = (
            "id", "source_835", "client", "mir_filename", "original_835_filename",
            "file_hash", "file_size", "claim_count", "physical_row_count",
            "service_count", "status", "converted_at", "created_at", "updated_at",
        )
        read_only_fields = fields


class EDI835FileSerializer(serializers.ModelSerializer):
    mir_filename = serializers.CharField(source="mir_file.mir_filename", read_only=True, default="")

    class Meta:
        model = EDI835File
        fields = (
            "id", "client", "original_filename", "stored_filename", "mir_filename",
            "status", "claims_count", "services_count", "records_count", "uploaded_at",
            "processing_started_at", "processing_completed_at", "error_message",
            "present_in_sftp", "present_in_archive_folder", "ingestion_source",
        )
        read_only_fields = fields


class MIRClaimSerializer(serializers.ModelSerializer):
    class Meta:
        model = MIRClaim
        exclude = ("header_raw",)
        read_only_fields = tuple(field.name for field in MIRClaim._meta.fields if field.name != "header_raw")


class RECONFileSerializer(serializers.ModelSerializer):
    class Meta:
        model = RECONFile
        exclude = ("file_content",)
        read_only_fields = tuple(field.name for field in RECONFile._meta.fields if field.name != "file_content")


class SFTPConfigSerializer(serializers.ModelSerializer):
    class Meta:
        model = SFTPConfig
        exclude = ("password", "ssh_key", "outbound_password", "outbound_ssh_key")
        read_only_fields = tuple(
            field.name for field in SFTPConfig._meta.fields
            if field.name not in {"password", "ssh_key", "outbound_password", "outbound_ssh_key"}
        )
