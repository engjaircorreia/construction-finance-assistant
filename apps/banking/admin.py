from django.contrib import admin

from .models import OfxFile, OfxTransaction, Reconciliation


class OfxTransactionInline(admin.TabularInline):
    model = OfxTransaction
    extra = 0
    fields = ("posted_at", "amount", "fitid", "name_extracted", "document_extracted", "status")
    readonly_fields = fields
    can_delete = False


@admin.register(OfxFile)
class OfxFileAdmin(admin.ModelAdmin):
    list_display = ("original_filename", "bank_id", "account_id", "start_date", "end_date", "status")
    list_filter = ("status", "bank_id", "created_at")
    search_fields = ("original_filename", "bank_id", "account_id", "notes")
    autocomplete_fields = ("uploaded_file", "imported_by")
    readonly_fields = ("created_at", "updated_at")
    inlines = [OfxTransactionInline]


@admin.register(OfxTransaction)
class OfxTransactionAdmin(admin.ModelAdmin):
    list_display = ("posted_at", "amount", "counterparty", "status", "reconciliation_count", "fitid")
    list_filter = ("status", "posted_at", "transaction_type")
    search_fields = ("fitid", "memo", "normalized_memo", "document_extracted", "name_extracted")
    autocomplete_fields = ("ofx_file", "counterparty")
    readonly_fields = ("created_at", "updated_at")

    @admin.display(description="reconciliations")
    def reconciliation_count(self, obj):
        return obj.reconciliations.count()


@admin.register(Reconciliation)
class ReconciliationAdmin(admin.ModelAdmin):
    list_display = ("payment", "transaction", "status", "confidence", "payment_status", "transaction_status", "created_at")
    list_filter = ("status", "transaction__status", "payment__status", "created_at")
    search_fields = ("payment__description", "transaction__fitid", "transaction__memo")
    autocomplete_fields = ("payment", "transaction", "created_by")
    readonly_fields = ("created_at", "updated_at")

    @admin.display(description="status payment")
    def payment_status(self, obj):
        return obj.payment.status

    @admin.display(description="status OFX")
    def transaction_status(self, obj):
        return obj.transaction.status
