from django.contrib import admin

from .models import ExportBatch


@admin.register(ExportBatch)
class ExportBatchAdmin(admin.ModelAdmin):
    list_display = ("id", "status", "records_count", "generated_by", "generated_at", "created_at")
    list_filter = ("status", "generated_at", "created_at")
    search_fields = ("template_path", "accounting_template_path", "import_template_path", "notes", "error_message")
    autocomplete_fields = ("payments", "generated_by")
    readonly_fields = ("created_at", "updated_at")
