from __future__ import annotations

import csv
import json
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management.base import CommandError, BaseCommand
from django.db import transaction
from django.db.models import Q, Sum

from apps.counterparties.models import Category, CostCenter, Counterparty, Origin, Work
from apps.counterparties.importers import clean_name, normalize_header, normalize_text
from apps.payments.importers import find_counterparty, parse_date, parse_decimal, person_type_from_document
from apps.payments.models import Payment


COLUMN_ALIASES = {
    "vencimento": "due_date",
    "due date": "due_date",
    "date_vencimento": "due_date",
    "status": "status",
    "amount": "amount",
    "favorecido": "counterparty_name",
    "payee": "counterparty_name",
    "vendor": "counterparty_name",
    "worker": "counterparty_name",
    "pago a": "counterparty_name",
    "descricao": "description",
    "description": "description",
    "descricao e category": "description_category",
    "description e category": "description_category",
    "category": "category",
    "n doc": "document_number",
    "nº doc": "document_number",
    "numero do documento": "document_number",
    "document number": "document_number",
    "condicao": "payment_condition",
    "condição": "payment_condition",
    "conta": "bank_account",
    "condicao e conta": "condition_account",
    "condição e conta": "condition_account",
    "cost center": "cost_center",
    "centro de custo": "cost_center",
    "project": "work",
    "pago": "payment_date",
    "payment date": "payment_date",
    "date_payment": "payment_date",
}


PAID_STATUS_VALUES = {"pago", "paga", "lancado", "posted", "quitado", "quitada"}
UNPAID_STATUS_VALUES = {"vencido", "vencida", "pending", "em aberto", "aberto", "nao pago", "no pago"}


class Command(BaseCommand):
    help = "Replaces payments in a period with a canonical CSV import, preserving audit data."

    def add_arguments(self, parser):
        parser.add_argument("--path", required=True, help="Path to the CSV with the corrected payments.")
        parser.add_argument("--start-date", required=True, help="Start date do período, em YYYY-MM-DD ou dd/mm/YYYY.")
        parser.add_argument("--end-date", required=True, help="End date do período, em YYYY-MM-DD ou dd/mm/YYYY.")
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Saves the replacement. Without this option, runs a dry-run and rolls everything back.",
        )

    def handle(self, *args, **options):
        csv_path = Path(options["path"])
        if not csv_path.exists():
            raise CommandError(f"File not found: {csv_path}")
        start_date = parse_required_date(options["start_date"], "start-date")
        end_date = parse_required_date(options["end_date"], "end-date")
        if start_date > end_date:
            raise CommandError("start-date cannot be later than end-date.")

        rows = read_rows(csv_path)
        if not rows:
            raise CommandError("No payment found in the CSV.")

        apply_changes = options["apply"]
        batch_id = f"replace_period:{start_date.isoformat()}:{end_date.isoformat()}:{csv_path.name}"
        with transaction.atomic():
            existing = existing_period_payments(start_date, end_date)
            existing_count = existing.count()
            existing_total = existing.filter(
                status__in=[Payment.Status.APPROVED, Payment.Status.RECONCILED, Payment.Status.POSTED]
            ).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")

            ignored_count = mark_existing_ignored(existing, batch_id)
            created_payments = [create_payment(row, csv_path, index, batch_id) for index, row in enumerate(rows, start=1)]
            created_realized_total = sum(
                (payment.amount for payment in created_payments if payment.status == Payment.Status.POSTED),
                Decimal("0.00"),
            )
            created_realized_total_by_payment_date = sum(
                (
                    payment.amount
                    for payment in created_payments
                    if payment.status == Payment.Status.POSTED
                    and payment.payment_date
                    and start_date <= payment.payment_date <= end_date
                ),
                Decimal("0.00"),
            )
            created_realized_total_by_due_date = sum(
                (
                    payment.amount
                    for payment in created_payments
                    if payment.status == Payment.Status.POSTED
                    and payment.due_date
                    and start_date <= payment.due_date <= end_date
                ),
                Decimal("0.00"),
            )
            created_pending_total = sum(
                (payment.amount for payment in created_payments if payment.status != Payment.Status.POSTED),
                Decimal("0.00"),
            )
            report = {
                "csv": str(csv_path),
                "period": f"{start_date.isoformat()} to {end_date.isoformat()}",
                "apply": apply_changes,
                "existing_active_found": existing_count,
                "existing_realized_total": str(existing_total),
                "existing_marked_ignored": ignored_count,
                "payments_created": len(created_payments),
                "created_realized_total": str(created_realized_total),
                "created_realized_total_by_payment_date": str(created_realized_total_by_payment_date),
                "created_realized_total_by_due_date": str(created_realized_total_by_due_date),
                "created_pending_total": str(created_pending_total),
                "created_ids": [payment.pk for payment in created_payments],
            }
            if not apply_changes:
                transaction.set_rollback(True)

        self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2))
        if apply_changes:
            self.stdout.write(self.style.SUCCESS("Replacement applied."))
        else:
            self.stdout.write(self.style.WARNING("Dry-run: no changes were saved. Use --apply to apply."))


def read_rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        if not reader.fieldnames:
            return []
        normalized_fields = {field: COLUMN_ALIASES.get(normalize_header(field), normalize_header(field)) for field in reader.fieldnames}
        rows = []
        for raw_row in reader:
            row = {}
            for original_key, value in raw_row.items():
                key = normalized_fields.get(original_key, original_key)
                row[key] = clean_name(value)
            if any(row.values()):
                row = normalize_row(row)
                rows.append(row)
        return rows


def normalize_row(row: dict) -> dict:
    description_category = row.get("description_category") or ""
    if description_category and not row.get("category"):
        category_marker = "Category:"
        if category_marker.lower() in description_category.lower():
            before, _, after = description_category.partition(category_marker)
            row["description"] = row.get("description") or clean_name(before)
            row["category"] = clean_name(after)
    condition_account = row.get("condition_account") or ""
    if condition_account:
        parts = [clean_name(part) for part in condition_account.splitlines() if clean_name(part)]
        if parts:
            row["payment_condition"] = row.get("payment_condition") or parts[0]
        if len(parts) > 1:
            row["bank_account"] = row.get("bank_account") or parts[1]
    return row


def existing_period_payments(start_date: date, end_date: date):
    return Payment.objects.filter(
        Q(due_date__gte=start_date, due_date__lte=end_date)
        | Q(due_date__isnull=True, competence_date__gte=start_date, competence_date__lte=end_date)
        | Q(
            due_date__isnull=True,
            competence_date__isnull=True,
            payment_date__gte=start_date,
            payment_date__lte=end_date,
        )
    ).exclude(status=Payment.Status.IGNORED)


def mark_existing_ignored(queryset, batch_id: str) -> int:
    count = 0
    for payment in queryset.select_for_update():
        payload = payment.raw_payload or {}
        payload.setdefault("replacement_audit", [])
        payload["replacement_audit"].append(
            {
                "batch_id": batch_id,
                "previous_status": payment.status,
                "previous_source": payment.source,
            }
        )
        payment.raw_payload = payload
        payment.status = Payment.Status.IGNORED
        payment.needs_review = False
        payment.review_reason = "Replaced by canonical period import."
        payment.save(update_fields=["raw_payload", "status", "needs_review", "review_reason", "updated_at"])
        count += 1
    return count


def create_payment(row: dict, csv_path: Path, row_number: int, batch_id: str) -> Payment:
    due_date = parse_required_date(row.get("due_date"), f"row {row_number}: due date")
    original_status = normalize_text(row.get("status") or "pago")
    is_paid = original_status in PAID_STATUS_VALUES or (original_status and original_status not in UNPAID_STATUS_VALUES)
    payment_date = parse_date(row.get("payment_date")) if row.get("payment_date") else None
    if is_paid and payment_date is None:
        payment_date = due_date
    amount = parse_money(row.get("amount"))
    if amount <= 0:
        raise CommandError(f"Row {row_number}: invalid or zero amount.")

    counterparty = get_or_create_counterparty(row, row_number)
    category = get_or_create_lookup(Category, row.get("category"))
    cost_center, work = resolve_cost_center_and_work(row)
    description = row.get("description") or (f"Status original: {row.get('status')}" if row.get("status") else "")
    status = Payment.Status.POSTED if is_paid else Payment.Status.PENDING_CONFIRMATION
    needs_review = status != Payment.Status.POSTED
    return Payment.objects.create(
        competence_date=payment_date or due_date,
        due_date=due_date,
        payment_date=payment_date,
        amount=amount,
        counterparty=counterparty,
        description=description[:200],
        document_number=row.get("document_number") or "",
        category=category,
        payment_method=row.get("payment_condition") or "À Vista",
        payer=settings.DEFAULT_PAYER,
        bank_account=row.get("bank_account") or settings.DEFAULT_BANK_ACCOUNT,
        cost_center=cost_center,
        work=work,
        source=Origin.IMPORT,
        status=status,
        confidence=1,
        needs_review=needs_review,
        review_reason="Imported payment is pending because it is not marked as paid." if needs_review else "",
        raw_payload={
            "replacement_batch_id": batch_id,
            "source_file": csv_path.name,
            "source_row": row_number,
            "original_status": row.get("status") or "",
            "raw": row,
        },
    )


def get_or_create_counterparty(row: dict, row_number: int) -> Counterparty:
    name = row.get("counterparty_name") or ""
    if not name:
        raise CommandError(f"Row {row_number}: payee is required.")
    normalized_name = normalize_text(name)
    kind = infer_counterparty_kind(name=name, category=row.get("category"))
    counterparty = find_counterparty("", normalized_name, kind)
    if counterparty:
        changed = False
        if counterparty.kind != kind and counterparty.source == Origin.IMPORT:
            counterparty.kind = kind
            changed = True
        if changed:
            counterparty.save(update_fields=["kind", "updated_at"])
        return counterparty
    return Counterparty.objects.create(
        name=name,
        normalized_name=normalized_name,
        kind=kind,
        person_type=person_type_from_document(""),
        source=Origin.IMPORT,
        confidence=0,
    )


def infer_counterparty_kind(*, name: str, category: str | None) -> str:
    normalized_category = normalize_text(category)
    normalized_name = normalize_text(name)
    if normalized_category in {"mao de obra terceirizada", "mao de project terceirizada", "labor terceirizada"}:
        return Counterparty.Kind.WORKER
    if "dividendo" in normalized_category or "socio" in normalized_category:
        return Counterparty.Kind.PARTNER
    if "emprestimo" in normalized_category or "sicredi" in normalized_name:
        return Counterparty.Kind.BANK
    return Counterparty.Kind.SUPPLIER


def get_or_create_lookup(model, name: str | None):
    name = clean_name(name)
    if not name:
        return None
    normalized_name = normalize_text(name)
    existing = model.objects.filter(name__iexact=name).first()
    if existing:
        return existing
    return model.objects.get_or_create(normalized_name=normalized_name, defaults={"name": name})[0]


def resolve_cost_center_and_work(row: dict) -> tuple[CostCenter | None, Work | None]:
    cost_center_name = row.get("cost_center") or ""
    explicit_work_name = row.get("work") or ""
    normalized_cost_center = normalize_text(cost_center_name)
    if explicit_work_name:
        return get_or_create_lookup(CostCenter, "Project"), get_or_create_work(explicit_work_name)
    if normalized_cost_center and normalized_cost_center not in {"empresa", "company"}:
        return get_or_create_lookup(CostCenter, "Project"), get_or_create_work(cost_center_name)
    return get_or_create_lookup(CostCenter, "Company"), None


def get_or_create_work(name: str | None):
    name = clean_name(name)
    if not name:
        return None
    normalized_name = normalize_text(name)
    return Work.objects.get_or_create(normalized_name=normalized_name, defaults={"name": name})[0]


def parse_required_date(value: object, label: str) -> date:
    parsed = parse_date(value)
    if parsed is None:
        raise CommandError(f"Invalid date in {label}: {value}")
    return parsed


def parse_money(value: object) -> Decimal:
    if value is None:
        return Decimal("0.00")
    text = str(value).strip()
    if not text:
        return Decimal("0.00")
    text = re.sub(r"[^\d,.-]", "", text)
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    return parse_decimal(text)
