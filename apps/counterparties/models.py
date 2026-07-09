from django.conf import settings
from django.db import models

from apps.core.models import TimeStampedModel


class Origin(models.TextChoices):
    HISTORICAL = "historico", "Historical"
    OFX = "ofx", "OFX"
    TELEGRAM = "telegram", "Telegram"
    MANUAL = "manual", "Manual"
    AI = "ia", "AI"
    IMPORT = "importacao", "Import"


class Category(TimeStampedModel):
    name = models.CharField("name", max_length=150, unique=True)
    normalized_name = models.CharField("normalized name", max_length=150, unique=True)
    is_active = models.BooleanField("active", default=True)

    class Meta:
        verbose_name = "category"
        verbose_name_plural = "categories"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class ChartOfAccount(TimeStampedModel):
    name = models.CharField("name", max_length=150, unique=True)
    normalized_name = models.CharField("normalized name", max_length=150, unique=True)
    is_active = models.BooleanField("active", default=True)

    class Meta:
        verbose_name = "chart of accounts"
        verbose_name_plural = "chart of accounts"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class CostCenter(TimeStampedModel):
    name = models.CharField("name", max_length=150, unique=True)
    normalized_name = models.CharField("normalized name", max_length=150, unique=True)
    is_active = models.BooleanField("active", default=True)

    class Meta:
        verbose_name = "cost center"
        verbose_name_plural = "cost centers"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Work(TimeStampedModel):
    class Status(models.TextChoices):
        ACTIVE = "ativa", "Active"
        PAUSED = "pausada", "Paused"
        FINISHED = "concluida", "Finished"

    name = models.CharField("name", max_length=200)
    normalized_name = models.CharField("normalized name", max_length=200, unique=True)
    city = models.CharField("city", max_length=150, blank=True)
    state = models.CharField("state", max_length=2, blank=True)
    status = models.CharField("status", max_length=20, choices=Status.choices, default=Status.ACTIVE)
    aliases = models.TextField("aliases", blank=True)
    is_active = models.BooleanField("active", default=True)

    class Meta:
        verbose_name = "project"
        verbose_name_plural = "projects"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class BudgetItem(TimeStampedModel):
    class ItemType(models.TextChoices):
        STAGE = "etapa", "Stage"
        SUBSTAGE = "subetapa", "Substage"
        ITEM = "item", "Item"
        OTHER = "outro", "Other"

    work = models.ForeignKey(
        Work,
        on_delete=models.CASCADE,
        related_name="budget_items",
        verbose_name="project",
    )
    index = models.CharField("index", max_length=80)
    parent_index = models.CharField("parent index", max_length=80, blank=True)
    item_type = models.CharField("type", max_length=20, choices=ItemType.choices, default=ItemType.OTHER)
    code = models.CharField("code", max_length=80, blank=True)
    base = models.CharField("base", max_length=120, blank=True)
    service_type = models.CharField("service type", max_length=120, blank=True)
    description = models.TextField("description")
    normalized_description = models.TextField("normalized description")
    unit = models.CharField("unit", max_length=40, blank=True)
    quantity = models.DecimalField("quantity", max_digits=14, decimal_places=4, blank=True, null=True)
    unit_cost = models.DecimalField("unit cost", max_digits=14, decimal_places=4, blank=True, null=True)
    total_cost = models.DecimalField("total cost", max_digits=14, decimal_places=4, blank=True, null=True)
    source_file = models.CharField("source file", max_length=255, blank=True)
    source_row = models.PositiveIntegerField("source row", blank=True, null=True)
    is_active = models.BooleanField("active", default=True)

    class Meta:
        verbose_name = "budget item"
        verbose_name_plural = "budget items"
        ordering = ["work__name", "index"]
        constraints = [
            models.UniqueConstraint(fields=["work", "index"], name="unique_budget_item_per_work_index"),
        ]
        indexes = [
            models.Index(fields=["work", "index"]),
            models.Index(fields=["work", "item_type"]),
            models.Index(fields=["parent_index"]),
        ]

    def __str__(self) -> str:
        return f"{self.work} - {self.index} - {self.description}"


class BudgetImportBatch(TimeStampedModel):
    class Status(models.TextChoices):
        PROCESSED = "processado", "Processed"
        ERROR = "erro", "Error"

    work = models.ForeignKey(
        Work,
        on_delete=models.CASCADE,
        related_name="budget_import_batches",
        verbose_name="project",
    )
    uploaded_file = models.ForeignKey(
        "documents.UploadedFile",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="budget_import_batches",
        verbose_name="imported file",
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="budget_import_batches",
        verbose_name="imported by",
    )
    status = models.CharField("status", max_length=30, choices=Status.choices, default=Status.PROCESSED)
    rows_read = models.PositiveIntegerField("rows read", default=0)
    rows_skipped = models.PositiveIntegerField("rows skipped", default=0)
    items_created = models.PositiveIntegerField("items created", default=0)
    items_updated = models.PositiveIntegerField("items updated", default=0)
    items_unchanged = models.PositiveIntegerField("items unchanged", default=0)
    conflicts = models.JSONField("conflicts", default=list, blank=True)
    error_message = models.TextField("error", blank=True)

    class Meta:
        verbose_name = "budget import"
        verbose_name_plural = "budget imports"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["work", "status"]),
            models.Index(fields=["created_at"]),
        ]

    def __str__(self) -> str:
        return f"Budget import #{self.pk} - {self.work}"


class Counterparty(TimeStampedModel):
    class Kind(models.TextChoices):
        SUPPLIER = "fornecedor", "Vendor"
        WORKER = "trabalhador", "Worker"
        CLIENT = "cliente", "Client"
        BANK = "banco", "Bank"
        TAX = "imposto", "Tax"
        PUBLIC_AGENCY = "orgao_publico", "Public agency"
        PARTNER = "socio", "Partner/Administrator"
        OTHER = "outro", "Other"

    class PersonType(models.TextChoices):
        INDIVIDUAL = "fisica", "Individual"
        COMPANY = "juridica", "Legal entity"
        UNKNOWN = "desconhecida", "Unknown"

    name = models.CharField("primary name", max_length=255)
    normalized_name = models.CharField("normalized name", max_length=255, db_index=True)
    kind = models.CharField("type", max_length=30, choices=Kind.choices, default=Kind.SUPPLIER)
    person_type = models.CharField(
        "person type",
        max_length=20,
        choices=PersonType.choices,
        default=PersonType.UNKNOWN,
    )
    primary_document = models.CharField("Primary taxpayer ID", max_length=14, blank=True, db_index=True)
    default_category = models.ForeignKey(
        Category,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="default_counterparties",
        verbose_name="default category",
    )
    default_chart_account = models.ForeignKey(
        ChartOfAccount,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="default_counterparties",
        verbose_name="default chart of accounts",
    )
    default_cost_center = models.ForeignKey(
        CostCenter,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="default_counterparties",
        verbose_name="default cost center",
    )
    default_work = models.ForeignKey(
        Work,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="default_counterparties",
        verbose_name="default project",
    )
    source = models.CharField("source", max_length=30, choices=Origin.choices, default=Origin.MANUAL)
    confidence = models.DecimalField("confidence", max_digits=5, decimal_places=2, default=0)
    is_active = models.BooleanField("active", default=True)
    notes = models.TextField("notes", blank=True)

    class Meta:
        verbose_name = "counterparty"
        verbose_name_plural = "counterparties"
        ordering = ["name"]
        indexes = [
            models.Index(fields=["normalized_name"]),
            models.Index(fields=["primary_document"]),
            models.Index(fields=["kind", "person_type"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["primary_document"],
                condition=~models.Q(primary_document=""),
                name="unique_counterparty_primary_document",
            ),
            models.CheckConstraint(
                condition=models.Q(confidence__gte=0) & models.Q(confidence__lte=1),
                name="counterparty_confidence_between_0_and_1",
            ),
        ]

    def __str__(self) -> str:
        return self.name


class CounterpartyAlias(TimeStampedModel):
    counterparty = models.ForeignKey(
        Counterparty,
        on_delete=models.CASCADE,
        related_name="aliases",
        verbose_name="counterparty",
    )
    name = models.CharField("name", max_length=255)
    normalized_name = models.CharField("normalized name", max_length=255, db_index=True)
    source = models.CharField("source", max_length=30, choices=Origin.choices, default=Origin.MANUAL)

    class Meta:
        verbose_name = "counterparty alias"
        verbose_name_plural = "counterparty aliases"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["counterparty", "normalized_name"],
                name="unique_alias_per_counterparty",
            )
        ]

    def __str__(self) -> str:
        return self.name


class CounterpartyDocument(TimeStampedModel):
    class DocumentType(models.TextChoices):
        CPF = "cpf", "CPF"
        CNPJ = "cnpj", "CNPJ"
        MASKED = "mascarado", "Masked"
        OTHER = "outro", "Other"

    counterparty = models.ForeignKey(
        Counterparty,
        on_delete=models.CASCADE,
        related_name="documents",
        verbose_name="counterparty",
    )
    document_type = models.CharField("type", max_length=20, choices=DocumentType.choices)
    number = models.CharField("number", max_length=32, unique=True)
    source = models.CharField("source", max_length=30, choices=Origin.choices, default=Origin.MANUAL)
    confidence = models.DecimalField("confidence", max_digits=5, decimal_places=2, default=1)
    is_primary = models.BooleanField("primary", default=False)

    class Meta:
        verbose_name = "counterparty document"
        verbose_name_plural = "counterparty documents"
        ordering = ["number"]
        constraints = [
            models.UniqueConstraint(
                fields=["counterparty"],
                condition=models.Q(is_primary=True),
                name="unique_primary_document_per_counterparty",
            ),
            models.CheckConstraint(
                condition=models.Q(confidence__gte=0) & models.Q(confidence__lte=1),
                name="counterparty_document_confidence_between_0_and_1",
            ),
        ]

    def __str__(self) -> str:
        return self.number
