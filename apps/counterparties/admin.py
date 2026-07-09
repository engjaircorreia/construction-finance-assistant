from django.contrib import admin

from .models import (
    BudgetImportBatch,
    BudgetItem,
    Category,
    ChartOfAccount,
    CostCenter,
    Counterparty,
    CounterpartyAlias,
    CounterpartyDocument,
    Work,
)


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "normalized_name", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "normalized_name")


@admin.register(ChartOfAccount)
class ChartOfAccountAdmin(admin.ModelAdmin):
    list_display = ("name", "normalized_name", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "normalized_name")


@admin.register(CostCenter)
class CostCenterAdmin(admin.ModelAdmin):
    list_display = ("name", "normalized_name", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "normalized_name")


@admin.register(Work)
class WorkAdmin(admin.ModelAdmin):
    list_display = ("name", "city", "state", "status", "is_active")
    list_filter = ("status", "is_active", "state")
    search_fields = ("name", "normalized_name", "city", "aliases")


@admin.register(BudgetItem)
class BudgetItemAdmin(admin.ModelAdmin):
    list_display = ("work", "index", "item_type", "description", "unit", "quantity", "total_cost", "is_active")
    list_filter = ("work", "item_type", "is_active")
    search_fields = ("index", "description", "normalized_description", "work__name")
    autocomplete_fields = ("work",)
    readonly_fields = ("created_at", "updated_at")


@admin.register(BudgetImportBatch)
class BudgetImportBatchAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "work",
        "status",
        "items_created",
        "items_updated",
        "items_unchanged",
        "rows_skipped",
        "uploaded_by",
        "created_at",
    )
    list_filter = ("status", "work")
    search_fields = ("work__name", "uploaded_file__original_filename", "error_message")
    autocomplete_fields = ("work", "uploaded_file", "uploaded_by")
    readonly_fields = ("created_at", "updated_at")


class CounterpartyAliasInline(admin.TabularInline):
    model = CounterpartyAlias
    extra = 0


class CounterpartyDocumentInline(admin.TabularInline):
    model = CounterpartyDocument
    extra = 0


@admin.register(Counterparty)
class CounterpartyAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "kind",
        "person_type",
        "primary_document",
        "default_category",
        "default_cost_center",
        "source",
        "is_active",
    )
    list_filter = ("kind", "person_type", "source", "is_active", "default_category", "default_cost_center")
    search_fields = ("name", "normalized_name", "primary_document", "aliases__name", "documents__number")
    autocomplete_fields = ("default_category", "default_chart_account", "default_cost_center", "default_work")
    inlines = (CounterpartyAliasInline, CounterpartyDocumentInline)


@admin.register(CounterpartyAlias)
class CounterpartyAliasAdmin(admin.ModelAdmin):
    list_display = ("name", "counterparty", "source")
    list_filter = ("source",)
    search_fields = ("name", "normalized_name", "counterparty__name")
    autocomplete_fields = ("counterparty",)


@admin.register(CounterpartyDocument)
class CounterpartyDocumentAdmin(admin.ModelAdmin):
    list_display = ("number", "document_type", "counterparty", "source", "is_primary", "confidence")
    list_filter = ("document_type", "source", "is_primary")
    search_fields = ("number", "counterparty__name")
    autocomplete_fields = ("counterparty",)
