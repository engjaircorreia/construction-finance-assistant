from django.contrib import admin

from .models import AuthorizedTelegramUser


@admin.register(AuthorizedTelegramUser)
class AuthorizedTelegramUserAdmin(admin.ModelAdmin):
    list_display = ("name", "username", "telegram_user_id", "user", "is_active", "last_seen_at")
    list_filter = ("is_active",)
    search_fields = ("name", "username", "telegram_user_id", "user__username")
    readonly_fields = ("created_at", "updated_at", "last_seen_at")

