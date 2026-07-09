from django.conf import settings
from django.db import models

from apps.core.models import TimeStampedModel


class OfxFile(TimeStampedModel):
    class Status(models.TextChoices):
        IMPORTED = "importado", "Imported"
        PROCESSED = "processado", "Processed"
        ERROR = "erro", "Error"
        IGNORED = "ignorado", "Ignored"

    uploaded_file = models.OneToOneField(
        "documents.UploadedFile",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="ofx_file",
        verbose_name="received file",
    )
    original_filename = models.CharField("original filename", max_length=255)
    bank_id = models.CharField("bank", max_length=50, blank=True)
    account_id = models.CharField("account", max_length=80, blank=True)
    start_date = models.DateField("start date", blank=True, null=True)
    end_date = models.DateField("end date", blank=True, null=True)
    imported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="imported_ofx_files",
        verbose_name="imported by",
    )
    status = models.CharField("status", max_length=30, choices=Status.choices, default=Status.IMPORTED)
    notes = models.TextField("notes", blank=True)

    class Meta:
        verbose_name = "OFX file"
        verbose_name_plural = "OFX files"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["bank_id", "account_id"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self) -> str:
        return self.original_filename


class OfxTransaction(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = "pendente", "Pending"
        RECONCILED = "conciliada", "Reconciled"
        POSSIBLE_DUPLICATE = "possivel_duplicada", "Possible duplicate"
        MISSING_PAYMENT = "sem_lancamento", "Missing payment"
        DIVERGENT = "divergente", "Divergent"
        IGNORED = "ignorada", "Ignored"

    ofx_file = models.ForeignKey(
        OfxFile,
        on_delete=models.CASCADE,
        related_name="transactions",
        verbose_name="OFX file",
    )
    fitid = models.CharField("FITID", max_length=120)
    transaction_type = models.CharField("type", max_length=30, blank=True)
    posted_at = models.DateField("data")
    amount = models.DecimalField("amount", max_digits=14, decimal_places=2)
    memo = models.TextField("memo/history", blank=True)
    normalized_memo = models.TextField("normalized memo", blank=True)
    document_extracted = models.CharField("extracted CPF/CNPJ", max_length=32, blank=True)
    name_extracted = models.CharField("extracted name", max_length=255, blank=True)
    counterparty = models.ForeignKey(
        "counterparties.Counterparty",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="ofx_transactions",
        verbose_name="counterparty",
    )
    status = models.CharField("status", max_length=40, choices=Status.choices, default=Status.PENDING)
    raw_payload = models.JSONField("raw payload", default=dict, blank=True)

    class Meta:
        verbose_name = "OFX transaction"
        verbose_name_plural = "OFX transactions"
        ordering = ["-posted_at", "-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["ofx_file", "fitid"], name="unique_fitid_per_ofx_file"),
            models.CheckConstraint(condition=~models.Q(fitid=""), name="ofx_transaction_fitid_not_blank"),
        ]
        indexes = [
            models.Index(fields=["posted_at"]),
            models.Index(fields=["amount"]),
            models.Index(fields=["status"]),
            models.Index(fields=["document_extracted"]),
        ]

    def __str__(self) -> str:
        return f"{self.posted_at} - {self.amount} - {self.fitid}"


class Reconciliation(TimeStampedModel):
    class Status(models.TextChoices):
        SUGGESTED = "sugerida", "Suggested"
        CONFIRMED = "confirmada", "Confirmed"
        REJECTED = "rejeitada", "Rejected"

    payment = models.ForeignKey(
        "payments.Payment",
        on_delete=models.CASCADE,
        related_name="reconciliations",
        verbose_name="payment",
    )
    transaction = models.ForeignKey(
        OfxTransaction,
        on_delete=models.CASCADE,
        related_name="reconciliations",
        verbose_name="OFX transaction",
    )
    status = models.CharField("status", max_length=30, choices=Status.choices, default=Status.SUGGESTED)
    confidence = models.DecimalField("confidence", max_digits=5, decimal_places=2, default=0)
    notes = models.TextField("notes", blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="created_reconciliations",
        verbose_name="created by",
    )

    class Meta:
        verbose_name = "reconciliation"
        verbose_name_plural = "reconciliations"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["payment", "transaction"], name="unique_payment_transaction"),
            models.UniqueConstraint(
                fields=["payment"],
                condition=models.Q(status="confirmada"),
                name="unique_confirmed_reconciliation_per_payment",
            ),
            models.UniqueConstraint(
                fields=["transaction"],
                condition=models.Q(status="confirmada"),
                name="unique_confirmed_reconciliation_per_transaction",
            ),
            models.CheckConstraint(
                condition=models.Q(confidence__gte=0) & models.Q(confidence__lte=1),
                name="reconciliation_confidence_between_0_and_1",
            ),
        ]
        indexes = [
            models.Index(fields=["status"]),
        ]

    def __str__(self) -> str:
        return f"{self.payment_id} x {self.transaction_id}"
