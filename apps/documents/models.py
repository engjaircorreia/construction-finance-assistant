from django.conf import settings
from django.db import models

from apps.core.models import TimeStampedModel


class UploadedFile(TimeStampedModel):
    class Source(models.TextChoices):
        TELEGRAM = "telegram", "Telegram"
        MANUAL = "manual", "Manual"
        OFX = "ofx", "OFX"
        IMPORT = "importacao", "Import"
        HISTORICAL = "historico", "Historical"

    class Kind(models.TextChoices):
        RECEIPT = "comprovante", "Receipt"
        OFX = "ofx", "OFX"
        SPREADSHEET = "planilha", "Spreadsheet"
        PDF = "pdf", "PDF"
        IMAGE = "imagem", "Image"
        TEXT = "texto", "Text"
        OTHER = "outro", "Other"

    class Status(models.TextChoices):
        RECEIVED = "recebido", "Received"
        PROCESSED = "processado", "Processed"
        ERROR = "erro", "Error"
        IGNORED = "ignorado", "Ignored"

    file = models.FileField("file", upload_to="uploads/%Y/%m/%d/", blank=True)
    original_filename = models.CharField("original filename", max_length=255)
    content_type = models.CharField("content type", max_length=120, blank=True)
    size_bytes = models.PositiveBigIntegerField("size in bytes", blank=True, null=True)
    sha256 = models.CharField("SHA-256", max_length=64, blank=True, db_index=True)
    source = models.CharField("source", max_length=30, choices=Source.choices, default=Source.MANUAL)
    kind = models.CharField("type", max_length=30, choices=Kind.choices, default=Kind.OTHER)
    status = models.CharField("status", max_length=30, choices=Status.choices, default=Status.RECEIVED)
    telegram_file_id = models.CharField("Telegram file ID", max_length=255, blank=True)
    telegram_message_id = models.CharField("Telegram message ID", max_length=100, blank=True)
    telegram_user_id = models.BigIntegerField("Telegram user ID", blank=True, null=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="uploaded_files",
        verbose_name="uploaded by",
    )
    extracted_text = models.TextField("extracted text", blank=True)
    error_message = models.TextField("error", blank=True)
    notes = models.TextField("notes", blank=True)

    class Meta:
        verbose_name = "received file"
        verbose_name_plural = "received files"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["source", "kind"]),
            models.Index(fields=["status"]),
            models.Index(fields=["telegram_user_id"]),
        ]

    def __str__(self) -> str:
        return self.original_filename
