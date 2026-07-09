from django.conf import settings
from django.db import models

from apps.core.models import TimeStampedModel


class ExportBatch(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        GENERATED = "generated", "Generated"
        ERROR = "error", "Error"

    status = models.CharField("status", max_length=30, choices=Status.choices, default=Status.PENDING)
    template_path = models.CharField("template used", max_length=255, blank=True)
    file = models.FileField("generated spreadsheet", upload_to="generated/payments/%Y/%m/", blank=True)
    accounting_template_path = models.CharField("export template used", max_length=255, blank=True)
    import_template_path = models.CharField("import template used", max_length=255, blank=True)
    accounting_file = models.FileField(
        "accounting export spreadsheet",
        upload_to="generated/payments/%Y/%m/",
        blank=True,
    )
    import_file = models.FileField(
        "system import spreadsheet",
        upload_to="generated/payments/%Y/%m/",
        blank=True,
    )
    payments = models.ManyToManyField(
        "payments.Payment",
        blank=True,
        related_name="export_batches",
        verbose_name="payments",
    )
    records_count = models.PositiveIntegerField("record count", default=0)
    generated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="generated_export_batches",
        verbose_name="generated por",
    )
    generated_at = models.DateTimeField("generated at", blank=True, null=True)
    period_start = models.DateField("início do período", blank=True, null=True)
    period_end = models.DateField("fim do período", blank=True, null=True)
    error_message = models.TextField("error", blank=True)
    notes = models.TextField("notes", blank=True)

    class Meta:
        verbose_name = "lote de export"
        verbose_name_plural = "lotes de export"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["generated_at"]),
            models.Index(fields=["period_start", "period_end"]),
        ]

    def __str__(self) -> str:
        return f"Batch {self.pk or 'new'} - {self.get_status_display()}"
