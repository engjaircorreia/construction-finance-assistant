from django.contrib import admin

from .models import TelegramDraft


@admin.register(TelegramDraft)
class TelegramDraftAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "telegram_user_id",
        "sender_name",
        "status",
        "amount",
        "counterparty",
        "work",
        "work_item_index",
        "finalized_payment",
        "updated_at",
    )
    list_filter = ("status", "needs_ai", "work", "category")
    search_fields = ("text_content", "description", "sender_name", "sender_username", "counterparty__name")
    autocomplete_fields = ("counterparty", "category", "cost_center", "work", "finalized_payment")
    readonly_fields = ("created_at", "updated_at")
