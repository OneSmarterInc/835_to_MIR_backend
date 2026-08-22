from django.contrib import admin
from .models import EDI835File


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
