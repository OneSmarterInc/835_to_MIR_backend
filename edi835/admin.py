from django.contrib import admin
from .models import (
    EDI835File, MIRClaim, MIRClaimChunk, MIRFile, MIRServiceLine,
    RECONClaim, RECONFile, RECONProcessingError, RECONProcessingRun, RECONServiceLine,
)


@admin.register(EDI835File)
class EDI835FileAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "original_filename",
        "stored_filename",
        "status",
        "uploaded_at",
        "processing_completed_at",
    )
    list_filter = ("status", "uploaded_at")
    search_fields = ("id", "original_filename", "stored_filename")
    readonly_fields = (
        "id",
        "original_filename",
        "stored_filename",
        "uploaded_at",
        "processing_started_at",
        "processing_completed_at",
        "input_path",
        "output_path",
        "archive_path",
    )


@admin.register(MIRFile)
class MIRFileAdmin(admin.ModelAdmin):
    list_display = ("mir_filename", "client", "claim_count", "service_count", "status", "converted_at")
    list_filter = ("status", "converted_at")
    search_fields = ("mir_filename", "file_hash", "original_835_filename")
    readonly_fields = ("file_hash", "file_size", "created_at", "updated_at")


@admin.register(MIRClaim)
class MIRClaimAdmin(admin.ModelAdmin):
    list_display = ("claim_control_number", "mir_file", "member_id", "service_count", "chunk_count")
    search_fields = ("claim_control_number", "member_id", "patient_first_name", "patient_last_name")


@admin.register(MIRClaimChunk)
class MIRClaimChunkAdmin(admin.ModelAdmin):
    list_display = ("mir_claim", "chunk_number", "services_in_chunk", "physical_row_number")


@admin.register(MIRServiceLine)
class MIRServiceLineAdmin(admin.ModelAdmin):
    list_display = (
        "mir_claim", "service_sequence", "charge_amount", "allowed_amount",
        "paid_amount", "patient_liability", "reason_code",
    )
    search_fields = ("mir_claim__claim_control_number", "reason_code")


@admin.register(RECONFile)
class RECONFileAdmin(admin.ModelAdmin):
    list_display = ("original_filename", "client", "status", "claim_count", "service_count", "uploaded_at")
    list_filter = ("status", "client", "uploaded_at")
    search_fields = ("original_filename", "file_hash", "client__name", "client__client_code")
    readonly_fields = ("file_hash", "file_size", "uploaded_at", "processed_at")


@admin.register(RECONClaim)
class RECONClaimAdmin(admin.ModelAdmin):
    list_display = ("claim_control_number", "client", "service_count", "charge_amount", "paid_amount")
    search_fields = ("claim_control_number", "member_id", "patient_control_number")


@admin.register(RECONServiceLine)
class RECONServiceLineAdmin(admin.ModelAdmin):
    list_display = ("recon_claim", "service_sequence", "procedure_code", "charge_amount", "paid_amount")


@admin.register(RECONProcessingRun)
class RECONProcessingRunAdmin(admin.ModelAdmin):
    list_display = ("recon_file", "status", "claims_created", "services_created", "started_at")


@admin.register(RECONProcessingError)
class RECONProcessingErrorAdmin(admin.ModelAdmin):
    list_display = ("recon_file", "row_number", "error_code", "created_at")
