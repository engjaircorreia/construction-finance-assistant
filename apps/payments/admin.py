from django.contrib import admin

from .models import Payment, PaymentConfirmation


class PaymentConfirmationInline(admin.TabularInline):
    model = PaymentConfirmation
    extra = 0
    readonly_fields = ("created_at", "updated_at")


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = (
        "payment_date",
        "counterparty",
        "amount",
        "category",
        "work",
        "work_item_index",
        "status",
        "source",
        "needs_review",
    )
    list_filter = ("status", "source", "needs_review", "payment_date", "work", "category")
    search_fields = (
        "description",
        "document_number",
        "counterparty__name",
        "counterparty__primary_document",
        "work__name",
        "work_item_index",
    )
    autocomplete_fields = (
        "counterparty",
        "category",
        "chart_account",
        "cost_center",
        "work",
        "uploaded_file",
        "created_by",
        "confirmed_by",
    )
    readonly_fields = ("created_at", "updated_at")
    inlines = [PaymentConfirmationInline]


@admin.register(PaymentConfirmation)
class PaymentConfirmationAdmin(admin.ModelAdmin):
    list_display = ("payment", "action", "user", "telegram_user_id", "created_at")
    list_filter = ("action", "created_at")
    search_fields = ("payment__description", "message", "telegram_user_id")
    autocomplete_fields = ("payment", "user")
    readonly_fields = ("created_at", "updated_at")
