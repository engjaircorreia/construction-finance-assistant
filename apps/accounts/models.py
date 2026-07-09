from django.conf import settings
from django.db import models

from apps.core.models import TimeStampedModel


class AuthorizedTelegramUser(TimeStampedModel):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="authorized_telegram",
        verbose_name="user Django",
    )
    telegram_user_id = models.BigIntegerField("Telegram ID", unique=True)
    name = models.CharField("name", max_length=255)
    username = models.CharField("username", max_length=255, blank=True)
    is_active = models.BooleanField("active", default=True)
    notes = models.TextField("notes", blank=True)
    last_seen_at = models.DateTimeField("last seen at", blank=True, null=True)

    class Meta:
        verbose_name = "authorized Telegram user"
        verbose_name_plural = "authorized Telegram users"
        ordering = ["name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.telegram_user_id})"
