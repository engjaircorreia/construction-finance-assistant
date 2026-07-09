from __future__ import annotations

from apps.counterparties.importers import normalize_text
from apps.counterparties.models import CostCenter


def find_default_company_cost_center() -> CostCenter | None:
    return CostCenter.objects.filter(
        normalized_name__in=[normalize_text("Company"), normalize_text("Empresa")],
        is_active=True,
    ).first()


def find_default_work_cost_center() -> CostCenter | None:
    return CostCenter.objects.filter(
        normalized_name__in=[normalize_text("Project"), normalize_text("Obra")],
        is_active=True,
    ).first()


def apply_cost_center_default(record) -> None:
    if has_work(record):
        cost_center = find_default_work_cost_center()
        if cost_center:
            record.cost_center = cost_center
        return

    cost_center = find_default_company_cost_center()
    if cost_center:
        record.cost_center = cost_center
    if hasattr(record, "work_item_index"):
        record.work_item_index = ""


def has_work(record) -> bool:
    return bool(getattr(record, "work_id", None) or getattr(record, "work", None))
