from django.contrib import admin

from .models import UploadedFile


@admin.register(UploadedFile)
class UploadedFileAdmin(admin.ModelAdmin):
    list_display = (
        "original_filename",
        "kind",
        "source",
        "status",
        "telegram_user_id",
        "created_at",
    )
    list_filter = ("kind", "source", "status", "created_at")
    search_fields = (
        "original_filename",
        "sha256",
        "telegram_file_id",
        "telegram_message_id",
        "extracted_text",
    )
    autocomplete_fields = ("uploaded_by",)
    readonly_fields = ("created_at", "updated_at")
