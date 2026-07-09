from django.conf import settings
from django.db import models

from apps.core.models import TimeStampedModel
from apps.counterparties.models import Origin


class Payment(TimeStampedModel):
    class Status(models.TextChoices):
        RECEIVED = "recebido", "Received"
        PROCESSING = "processando", "Processing"
        PENDING_REGISTRATION = "pendente_cadastro", "Pending registration"
        PENDING_CONFIRMATION = "pendente_confirmacao", "Pending approval"
        CORRECTING = "corrigindo", "Correcting"
        APPROVED = "aprovado", "Approved"
        CANCELED = "cancelado", "Canceled"
        POSTED = "lancado", "Posted"
        RECONCILED = "conciliado", "Reconciled"
        POSSIBLE_DUPLICATE = "possivel_duplicado", "Possible duplicate"
        ERROR = "erro", "Error"
        IGNORED = "ignorado", "Ignored"

    class ConfirmationAction(models.TextChoices):
        APPROVE = "aprovar", "Approve"
        CORRECT = "corrigir", "Correct"
        CANCEL = "cancelar", "Cancel"

    competence_date = models.DateField("accrual date", blank=True, null=True)
    due_date = models.DateField("due date", blank=True, null=True)
    payment_date = models.DateField("payment date", blank=True, null=True)
    amount = models.DecimalField("amount", max_digits=14, decimal_places=2, default=0)
    counterparty = models.ForeignKey(
        "counterparties.Counterparty",
        on_delete=models.PROTECT,
        blank=True,
        null=True,
        related_name="payments",
        verbose_name="vendor/worker",
    )
    description = models.TextField("description", blank=True)
    document_number = models.CharField("document number", max_length=100, blank=True)
    category = models.ForeignKey(
        "counterparties.Category",
        on_delete=models.PROTECT,
        blank=True,
        null=True,
        related_name="payments",
        verbose_name="category",
    )
    chart_account = models.ForeignKey(
        "counterparties.ChartOfAccount",
        on_delete=models.PROTECT,
        blank=True,
        null=True,
        related_name="payments",
        verbose_name="chart of accounts",
    )
    payment_method = models.CharField("payment method", max_length=80, blank=True)
    payer = models.CharField("payer", max_length=120, blank=True)
    bank_account = models.CharField("bank account", max_length=160, blank=True)
    cost_center = models.ForeignKey(
        "counterparties.CostCenter",
        on_delete=models.PROTECT,
        blank=True,
        null=True,
        related_name="payments",
        verbose_name="cost center",
    )
    work = models.ForeignKey(
        "counterparties.Work",
        on_delete=models.PROTECT,
        blank=True,
        null=True,
        related_name="payments",
        verbose_name="project",
    )
    work_item_index = models.CharField("budget item index", max_length=80, blank=True)
    source = models.CharField("source", max_length=30, choices=Origin.choices, default=Origin.TELEGRAM)
    status = models.CharField("status", max_length=40, choices=Status.choices, default=Status.RECEIVED)
    uploaded_file = models.ForeignKey(
        "documents.UploadedFile",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="payments",
        verbose_name="source file",
    )
    confidence = models.DecimalField("confidence", max_digits=5, decimal_places=2, default=0)
    needs_review = models.BooleanField("needs review", default=True)
    review_reason = models.TextField("review reason", blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="created_payments",
        verbose_name="created by",
    )
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="confirmed_payments",
        verbose_name="confirmed by",
    )
    confirmed_at = models.DateTimeField("confirmed at", blank=True, null=True)
    user_action = models.CharField(
        "user action",
        max_length=20,
        choices=ConfirmationAction.choices,
        blank=True,
    )
    raw_payload = models.JSONField("raw payload", default=dict, blank=True)

    class Meta:
        verbose_name = "payment"
        verbose_name_plural = "payments"
        ordering = ["-payment_date", "-created_at"]
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["source"]),
            models.Index(fields=["payment_date"]),
            models.Index(fields=["document_number"]),
            models.Index(fields=["work", "work_item_index"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gte=0),
                name="payment_amount_cannot_be_negative",
            ),
            models.CheckConstraint(
                condition=models.Q(confidence__gte=0) & models.Q(confidence__lte=1),
                name="payment_confidence_between_0_and_1",
            ),
        ]

    def __str__(self) -> str:
        name = self.counterparty.name if self.counterparty else "no counterparty"
        return f"{self.payment_date or 'no date'} - {name} - {self.amount}"


class PaymentConfirmation(TimeStampedModel):
    payment = models.ForeignKey(
        Payment,
        on_delete=models.CASCADE,
        related_name="confirmations",
        verbose_name="payment",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="payment_confirmations",
        verbose_name="user",
    )
    telegram_user_id = models.BigIntegerField("Telegram user ID", blank=True, null=True)
    action = models.CharField("action", max_length=20, choices=Payment.ConfirmationAction.choices)
    message = models.TextField("message", blank=True)
    payload = models.JSONField("payload", default=dict, blank=True)

    class Meta:
        verbose_name = "payment confirmation"
        verbose_name_plural = "payment confirmations"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["action"]),
            models.Index(fields=["telegram_user_id"]),
        ]

    def __str__(self) -> str:
        return f"{self.get_action_display()} - {self.payment_id}"
