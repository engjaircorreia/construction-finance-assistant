from django.db import models

from apps.core.models import TimeStampedModel


class TelegramDraft(TimeStampedModel):
    class Status(models.TextChoices):
        ACTIVE = "ativo", "Active"
        FINALIZED = "finalizado", "Finalized"
        CANCELED = "cancelado", "Canceled"

    telegram_user_id = models.BigIntegerField("Telegram user ID", db_index=True)
    sender_name = models.CharField("sender name", max_length=255, blank=True)
    sender_username = models.CharField("sender username", max_length=255, blank=True)
    status = models.CharField("status", max_length=20, choices=Status.choices, default=Status.ACTIVE)
    text_content = models.TextField("accumulated text", blank=True)
    uploaded_files = models.ManyToManyField(
        "documents.UploadedFile",
        blank=True,
        related_name="telegram_drafts",
        verbose_name="received files",
    )
    amount = models.DecimalField("amount", max_digits=14, decimal_places=2, blank=True, null=True)
    payment_date = models.DateField("payment date", blank=True, null=True)
    counterparty = models.ForeignKey(
        "counterparties.Counterparty",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="telegram_drafts",
        verbose_name="vendor/worker",
    )
    category = models.ForeignKey(
        "counterparties.Category",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="telegram_drafts",
        verbose_name="category",
    )
    cost_center = models.ForeignKey(
        "counterparties.CostCenter",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="telegram_drafts",
        verbose_name="cost center",
    )
    work = models.ForeignKey(
        "counterparties.Work",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="telegram_drafts",
        verbose_name="project",
    )
    work_item_index = models.CharField("budget item index", max_length=80, blank=True)
    payment_method = models.CharField("payment method", max_length=80, blank=True)
    description = models.CharField("description", max_length=255, blank=True)
    confidence = models.DecimalField("confidence", max_digits=5, decimal_places=2, default=0)
    needs_ai = models.BooleanField("needs AI", default=False)
    raw_payload = models.JSONField("raw payload", default=dict, blank=True)
    finalized_payment = models.ForeignKey(
        "payments.Payment",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="source_telegram_drafts",
        verbose_name="finalized payment",
    )

    class Meta:
        verbose_name = "Telegram draft"
        verbose_name_plural = "Telegram drafts"
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["telegram_user_id", "status"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self) -> str:
        return f"Draft #{self.pk} - {self.telegram_user_id}"
