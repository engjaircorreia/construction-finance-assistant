from __future__ import annotations

from calendar import monthrange
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.db.models import Count, Prefetch, Q, Sum
from django.utils import timezone

from apps.banking.models import OfxFile, OfxTransaction, Reconciliation
from apps.counterparties.models import BudgetItem, Origin, Work
from apps.payments.models import Payment
from apps.telegrambot.models import TelegramDraft


REALIZED_PAYMENT_STATUSES = [
    Payment.Status.APPROVED,
    Payment.Status.RECONCILED,
    Payment.Status.POSTED,
]
PENDING_PAYMENT_STATUSES = [
    Payment.Status.RECEIVED,
    Payment.Status.PROCESSING,
    Payment.Status.PENDING_REGISTRATION,
    Payment.Status.PENDING_CONFIRMATION,
    Payment.Status.CORRECTING,
    Payment.Status.POSSIBLE_DUPLICATE,
    Payment.Status.ERROR,
]
APPROVED_WITHOUT_OFX_STATUSES = [
    Payment.Status.APPROVED,
    Payment.Status.POSTED,
]
IGNORED_FOR_OPERATIONAL_PENDENCIES = [
    Payment.Status.CANCELED,
    Payment.Status.IGNORED,
]
OFX_ISSUE_STATUSES = [
    OfxTransaction.Status.PENDING,
    OfxTransaction.Status.DIVERGENT,
    OfxTransaction.Status.POSSIBLE_DUPLICATE,
    OfxTransaction.Status.MISSING_PAYMENT,
]
ZERO = Decimal("0.00")


@dataclass(frozen=True)
class DashboardGroup:
    label: str
    amount: Decimal
    count: int
    percentage: Decimal
    kind: str = ""
    object_id: int | None = None


@dataclass(frozen=True)
class MonthlyEvolutionItem:
    month: int
    year: int
    period_start: date
    period_end: date
    realized_amount: Decimal
    reconciled_amount: Decimal
    pending_amount: Decimal
    payments_count: int


@dataclass(frozen=True)
class WorkBudgetSummary:
    work_id: int
    work_name: str
    monthly_spent: Decimal
    accumulated_spent: Decimal
    pending_amount: Decimal
    pending_count: int
    budget_total: Decimal | None
    consumed_percentage: Decimal | None
    estimated_balance: Decimal | None
    payments_count: int
    has_budget: bool
    status: str


@dataclass(frozen=True)
class DashboardSummary:
    month: int
    year: int
    period_start: date
    period_end: date
    realized_amount: Decimal
    accumulated_realized_amount: Decimal
    payments_count: int
    realized_payments_count: int
    pending_registration_count: int
    pending_registration_amount: Decimal
    pending_confirmation_count: int
    pending_confirmation_amount: Decimal
    correcting_count: int
    correcting_amount: Decimal
    pending_total_amount: Decimal
    pending_total_count: int
    undated_payments_count: int
    undated_payments_amount: Decimal
    operational_pendency_count: int
    active_drafts_count: int
    reconciled_amount: Decimal
    reconciled_count: int
    approved_unreconciled_amount: Decimal
    approved_unreconciled_count: int
    has_ofx_imported: bool
    ofx_imported_count: int
    ofx_expense_without_payment_count: int
    ofx_suggested_pending_registration_count: int
    ofx_suggested_pending_registration_amount: Decimal
    ofx_suggested_pending_confirmation_count: int
    ofx_suggested_pending_confirmation_amount: Decimal
    ofx_issue_count: int
    ofx_pending_count: int
    ofx_divergent_count: int
    ofx_possible_duplicate_count: int
    ofx_ignored_credit_count: int
    financial_center_groups: list[DashboardGroup]
    category_groups: list[DashboardGroup]
    counterparty_groups: list[DashboardGroup]
    monthly_evolution: list[MonthlyEvolutionItem]
    work_budget_summaries: list[WorkBudgetSummary]


def build_dashboard_summary(
    *,
    month: int | None = None,
    year: int | None = None,
    period_start: date | None = None,
    period_end: date | None = None,
    today: date | None = None,
    ranking_limit: int = 10,
) -> DashboardSummary:
    period_start, period_end = resolve_dashboard_period(
        month=month,
        year=year,
        period_start=period_start,
        period_end=period_end,
        today=today,
    )
    month = period_start.month
    year = period_start.year

    period_payments = list(
        payment_queryset()
        .filter(payment_period_q(period_start, period_end))
        .exclude(status__in=IGNORED_FOR_OPERATIONAL_PENDENCIES)
    )
    realized_period_payments = [payment for payment in period_payments if is_realized_payment(payment)]
    pending_period_payments = [payment for payment in period_payments if payment.status in PENDING_PAYMENT_STATUSES]
    pending_registration_payments = [
        payment for payment in period_payments if payment.status == Payment.Status.PENDING_REGISTRATION
    ]
    pending_confirmation_payments = [
        payment for payment in period_payments if payment.status == Payment.Status.PENDING_CONFIRMATION
    ]
    correcting_payments = [payment for payment in period_payments if payment.status == Payment.Status.CORRECTING]
    reconciled_period_payments = [payment for payment in period_payments if is_reconciled_payment(payment)]
    approved_unreconciled_payments = [
        payment
        for payment in period_payments
        if has_positive_amount(payment)
        and payment.status in APPROVED_WITHOUT_OFX_STATUSES
        and not is_reconciled_payment(payment)
    ]
    realized_amount = sum_amounts(realized_period_payments)
    pending_total_amount = sum_amounts(pending_period_payments)
    undated_queryset = undated_payments()
    undated_payments_count = undated_queryset.count()
    undated_payments_amount = undated_queryset.aggregate(total=Sum("amount"))["total"] or ZERO
    active_drafts_count = active_drafts_for_period(period_start, period_end).count()
    ofx_period_transactions = ofx_transactions_for_period(period_start, period_end)
    ofx_period_transactions_list = list(ofx_period_transactions)
    ofx_expense_without_payment_count = count_ofx_expenses_without_payment(ofx_period_transactions_list)
    ofx_suggested_pending_registration_payments = [
        payment
        for payment in pending_registration_payments
        if payment.source == Origin.OFX
    ]
    ofx_suggested_pending_confirmation_payments = [
        payment
        for payment in pending_confirmation_payments
        if payment.source == Origin.OFX
    ]
    ofx_issue_count = ofx_period_transactions.filter(status__in=OFX_ISSUE_STATUSES).count()
    ofx_pending_count = ofx_period_transactions.filter(status=OfxTransaction.Status.PENDING).count()
    ofx_divergent_count = ofx_period_transactions.filter(status=OfxTransaction.Status.DIVERGENT).count()
    ofx_possible_duplicate_count = ofx_period_transactions.filter(status=OfxTransaction.Status.POSSIBLE_DUPLICATE).count()
    ofx_ignored_credit_count = ofx_period_transactions.filter(
        status=OfxTransaction.Status.IGNORED,
        amount__gt=0,
    ).count()
    ofx_imported_count = ofx_files_for_period(period_start, period_end).count()
    accumulated_realized_amount = accumulated_realized_payments().aggregate(total=Sum("amount"))["total"] or ZERO

    work_budget_summaries = build_work_budget_summaries(period_start, period_end)
    work_without_budget_count = sum(1 for item in work_budget_summaries if item.status == "sem_orcamento")

    operational_pendency_count = (
        len(pending_period_payments)
        + undated_payments_count
        + active_drafts_count
        + ofx_expense_without_payment_count
        + len(approved_unreconciled_payments)
        + ofx_issue_count
        + work_without_budget_count
    )

    return DashboardSummary(
        month=month,
        year=year,
        period_start=period_start,
        period_end=period_end,
        realized_amount=realized_amount,
        accumulated_realized_amount=accumulated_realized_amount,
        payments_count=len(period_payments),
        realized_payments_count=len(realized_period_payments),
        pending_registration_count=len(pending_registration_payments),
        pending_registration_amount=sum_amounts(pending_registration_payments),
        pending_confirmation_count=len(pending_confirmation_payments),
        pending_confirmation_amount=sum_amounts(pending_confirmation_payments),
        correcting_count=len(correcting_payments),
        correcting_amount=sum_amounts(correcting_payments),
        pending_total_amount=pending_total_amount,
        pending_total_count=len(pending_period_payments),
        undated_payments_count=undated_payments_count,
        undated_payments_amount=undated_payments_amount,
        operational_pendency_count=operational_pendency_count,
        active_drafts_count=active_drafts_count,
        reconciled_amount=sum_amounts(reconciled_period_payments),
        reconciled_count=len(reconciled_period_payments),
        approved_unreconciled_amount=sum_amounts(approved_unreconciled_payments),
        approved_unreconciled_count=len(approved_unreconciled_payments),
        has_ofx_imported=ofx_imported_count > 0,
        ofx_imported_count=ofx_imported_count,
        ofx_expense_without_payment_count=ofx_expense_without_payment_count,
        ofx_suggested_pending_registration_count=len(ofx_suggested_pending_registration_payments),
        ofx_suggested_pending_registration_amount=sum_amounts(ofx_suggested_pending_registration_payments),
        ofx_suggested_pending_confirmation_count=len(ofx_suggested_pending_confirmation_payments),
        ofx_suggested_pending_confirmation_amount=sum_amounts(ofx_suggested_pending_confirmation_payments),
        ofx_issue_count=ofx_issue_count,
        ofx_pending_count=ofx_pending_count,
        ofx_divergent_count=ofx_divergent_count,
        ofx_possible_duplicate_count=ofx_possible_duplicate_count,
        ofx_ignored_credit_count=ofx_ignored_credit_count,
        financial_center_groups=build_groups(realized_period_payments, financial_center_key, realized_amount),
        category_groups=build_groups(realized_period_payments, category_key, realized_amount, limit=ranking_limit),
        counterparty_groups=build_groups(realized_period_payments, counterparty_key, realized_amount, limit=ranking_limit),
        monthly_evolution=build_monthly_evolution(year),
        work_budget_summaries=work_budget_summaries,
    )


def resolve_dashboard_period(
    *,
    month: int | None = None,
    year: int | None = None,
    period_start: date | None = None,
    period_end: date | None = None,
    today: date | None = None,
) -> tuple[date, date]:
    if period_start or period_end:
        if not period_start or not period_end:
            raise ValueError("Informe periodo inicial e final juntos.")
        if period_start > period_end:
            raise ValueError("Periodo inicial nao pode ser maior que periodo final.")
        return period_start, period_end

    today = today or timezone.localdate()
    month = month or today.month
    year = year or today.year
    if month < 1 or month > 12:
        raise ValueError("Mes deve estar entre 1 e 12.")
    return month_period(year, month)


def month_period(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, monthrange(year, month)[1])


def payment_queryset():
    confirmed_reconciliations = Reconciliation.objects.filter(status=Reconciliation.Status.CONFIRMED)
    return Payment.objects.select_related("counterparty", "category", "cost_center", "work").prefetch_related(
        Prefetch("reconciliations", queryset=confirmed_reconciliations, to_attr="confirmed_reconciliations")
    )


def accumulated_realized_payments():
    return Payment.objects.filter(amount__gt=0, status__in=REALIZED_PAYMENT_STATUSES)


def undated_payments():
    return Payment.objects.filter(
        due_date__isnull=True,
        competence_date__isnull=True,
        payment_date__isnull=True,
    ).exclude(status__in=IGNORED_FOR_OPERATIONAL_PENDENCIES)


def payment_period_q(period_start: date, period_end: date) -> Q:
    return (
        Q(due_date__gte=period_start, due_date__lte=period_end)
        | Q(due_date__isnull=True, competence_date__gte=period_start, competence_date__lte=period_end)
        | Q(
            due_date__isnull=True,
            competence_date__isnull=True,
            payment_date__gte=period_start,
            payment_date__lte=period_end,
        )
    )


def payment_period_date(payment: Payment) -> date | None:
    return payment.due_date or payment.competence_date or payment.payment_date


def active_drafts_for_period(period_start: date, period_end: date):
    return TelegramDraft.objects.filter(status=TelegramDraft.Status.ACTIVE).filter(
        Q(payment_date__gte=period_start, payment_date__lte=period_end)
        | Q(payment_date__isnull=True, updated_at__date__gte=period_start, updated_at__date__lte=period_end)
    )


def ofx_transactions_for_period(period_start: date, period_end: date):
    return OfxTransaction.objects.filter(posted_at__gte=period_start, posted_at__lte=period_end)


def ofx_files_for_period(period_start: date, period_end: date):
    period_overlap = Q(start_date__lte=period_end) & (Q(end_date__isnull=True) | Q(end_date__gte=period_start))
    transaction_in_period = Q(transactions__posted_at__gte=period_start, transactions__posted_at__lte=period_end)
    return (
        OfxFile.objects.exclude(status__in=[OfxFile.Status.ERROR, OfxFile.Status.IGNORED])
        .filter(period_overlap | transaction_in_period)
        .distinct()
    )


def count_ofx_expenses_without_payment(transactions: list[OfxTransaction]) -> int:
    debit_transactions = [
        transaction_record
        for transaction_record in transactions
        if transaction_record.amount < ZERO
        and transaction_record.status not in {OfxTransaction.Status.IGNORED, OfxTransaction.Status.RECONCILED}
    ]
    transaction_ids = [transaction_record.pk for transaction_record in debit_transactions]
    if not transaction_ids:
        return 0

    transaction_ids_with_payments = ofx_transaction_ids_with_payments(transaction_ids)
    transaction_ids_with_reconciliation = set(
        Reconciliation.objects.exclude(status=Reconciliation.Status.REJECTED)
        .filter(transaction_id__in=transaction_ids)
        .values_list("transaction_id", flat=True)
    )
    resolved_ids = transaction_ids_with_payments | transaction_ids_with_reconciliation
    return sum(1 for transaction_record in debit_transactions if transaction_record.pk not in resolved_ids)


def ofx_transaction_ids_with_payments(transaction_ids: list[int]) -> set[int]:
    values = Payment.objects.filter(
        source=Origin.OFX,
        raw_payload__ofx_transaction_id__in=transaction_ids,
    ).values_list("raw_payload__ofx_transaction_id", flat=True)
    parsed_ids = set()
    for value in values:
        try:
            parsed_ids.add(int(value))
        except (TypeError, ValueError):
            continue
    return parsed_ids


def is_realized_payment(payment: Payment) -> bool:
    return has_positive_amount(payment) and payment.status in REALIZED_PAYMENT_STATUSES


def is_reconciled_payment(payment: Payment) -> bool:
    return has_positive_amount(payment) and (
        payment.status == Payment.Status.RECONCILED or bool(getattr(payment, "confirmed_reconciliations", []))
    )


def has_positive_amount(payment: Payment) -> bool:
    return (payment.amount or ZERO) > ZERO


def sum_amounts(payments) -> Decimal:
    total = ZERO
    for payment in payments:
        amount = payment.amount or ZERO
        if amount > ZERO:
            total += amount
    return total


def build_groups(payments, key_func, total_amount: Decimal, limit: int | None = None) -> list[DashboardGroup]:
    grouped = {}
    for payment in payments:
        key = key_func(payment)
        current = grouped.setdefault(
            key.label,
            {"label": key.label, "kind": key.kind, "object_id": key.object_id, "amount": ZERO, "count": 0},
        )
        current["amount"] += payment.amount or ZERO
        current["count"] += 1

    rows = [row for row in grouped.values() if row["amount"] > ZERO]
    chart_total = sum((row["amount"] for row in rows), ZERO)
    rows = sorted(rows, key=lambda row: (-row["amount"], row["label"]))
    if limit is not None:
        rows = rows[:limit]
    return [
        DashboardGroup(
            label=row["label"],
            amount=row["amount"],
            count=row["count"],
            percentage=percentage(row["amount"], chart_total),
            kind=row["kind"],
            object_id=row["object_id"],
        )
        for row in rows
    ]


@dataclass(frozen=True)
class GroupKey:
    label: str
    kind: str
    object_id: int | None = None


def financial_center_key(payment: Payment) -> GroupKey:
    if payment.work_id:
        return GroupKey(f"Project: {payment.work.name}", "work", payment.work_id)
    if payment.cost_center_id:
        return GroupKey(payment.cost_center.name, "cost_center", payment.cost_center_id)
    return GroupKey("Company", "company")


def category_key(payment: Payment) -> GroupKey:
    if payment.category_id:
        return GroupKey(payment.category.name, "category", payment.category_id)
    return GroupKey("No category", "category")


def counterparty_key(payment: Payment) -> GroupKey:
    if payment.counterparty_id:
        return GroupKey(payment.counterparty.name, "counterparty", payment.counterparty_id)
    return GroupKey("Sem vendor/worker", "counterparty")


def percentage(amount: Decimal, total: Decimal) -> Decimal:
    if not total:
        return ZERO
    return amount / total * Decimal("100")


def build_monthly_evolution(year: int) -> list[MonthlyEvolutionItem]:
    month_starts = [date(year, month, 1) for month in range(1, 13)]
    first_start = month_starts[0]
    last_end = month_period(month_starts[-1].year, month_starts[-1].month)[1]
    payments = list(
        payment_queryset()
        .filter(payment_period_q(first_start, last_end))
        .exclude(status__in=IGNORED_FOR_OPERATIONAL_PENDENCIES)
    )
    by_month = defaultdict(list)
    for payment in payments:
        period_date = payment_period_date(payment)
        if period_date:
            by_month[(period_date.year, period_date.month)].append(payment)

    evolution = []
    for month_start in month_starts:
        month_payments = by_month[(month_start.year, month_start.month)]
        realized = [payment for payment in month_payments if is_realized_payment(payment)]
        reconciled = [payment for payment in month_payments if is_reconciled_payment(payment)]
        pending = [payment for payment in month_payments if payment.status in PENDING_PAYMENT_STATUSES]
        start, end = month_period(month_start.year, month_start.month)
        evolution.append(
            MonthlyEvolutionItem(
                month=month_start.month,
                year=month_start.year,
                period_start=start,
                period_end=end,
                realized_amount=sum_amounts(realized),
                reconciled_amount=sum_amounts(reconciled),
                pending_amount=sum_amounts(pending),
                payments_count=len(month_payments),
            )
        )
    return evolution

def build_work_budget_summaries(period_start: date, period_end: date) -> list[WorkBudgetSummary]:
    budget_totals = work_budget_totals()
    period_spent, _period_counts = work_spending_maps(period_start=period_start, period_end=period_end)
    accumulated_spent, accumulated_counts = work_spending_maps()
    pending_amounts, pending_counts = work_payment_maps(
        statuses=PENDING_PAYMENT_STATUSES,
        period_start=period_start,
        period_end=period_end,
    )
    work_ids = set(budget_totals) | set(period_spent) | set(accumulated_spent) | set(pending_amounts)
    works = Work.objects.filter(id__in=work_ids).order_by("name")

    summaries = []
    for work in works:
        budget_total = budget_totals.get(work.id)
        accumulated = accumulated_spent.get(work.id, ZERO)
        has_budget = bool(budget_total and budget_total > ZERO)
        consumed = percentage(accumulated, budget_total) if has_budget else None
        balance = budget_total - accumulated if has_budget else None
        summaries.append(
            WorkBudgetSummary(
                work_id=work.id,
                work_name=work.name,
                monthly_spent=period_spent.get(work.id, ZERO),
                accumulated_spent=accumulated,
                pending_amount=pending_amounts.get(work.id, ZERO),
                pending_count=pending_counts.get(work.id, 0),
                budget_total=budget_total if has_budget else None,
                consumed_percentage=consumed,
                estimated_balance=balance,
                payments_count=accumulated_counts.get(work.id, 0),
                has_budget=has_budget,
                status=work_budget_status(
                    has_budget,
                    accumulated,
                    budget_total,
                    consumed,
                    pending_amounts.get(work.id, ZERO),
                ),
            )
        )
    return summaries


def work_spending_maps(period_start: date | None = None, period_end: date | None = None) -> tuple[dict[int, Decimal], dict[int, int]]:
    return work_payment_maps(
        statuses=REALIZED_PAYMENT_STATUSES,
        period_start=period_start,
        period_end=period_end,
    )


def work_payment_maps(
    *,
    statuses,
    period_start: date | None = None,
    period_end: date | None = None,
) -> tuple[dict[int, Decimal], dict[int, int]]:
    payments = Payment.objects.filter(amount__gt=0, work__isnull=False, status__in=statuses)
    if period_start and period_end:
        payments = payments.filter(payment_period_q(period_start, period_end))
    amount_by_work = defaultdict(lambda: ZERO)
    count_by_work = defaultdict(int)
    for row in payments.values("work_id").annotate(total=Sum("amount"), count=Count("id")):
        amount_by_work[row["work_id"]] = row["total"] or ZERO
        count_by_work[row["work_id"]] = int(row["count"] or 0)
    return dict(amount_by_work), dict(count_by_work)


def work_budget_totals() -> dict[int, Decimal]:
    """Soma somente itens folha para evitar duplicar etapa, subetapa e item."""
    items_by_work = defaultdict(list)
    items = BudgetItem.objects.filter(is_active=True).exclude(total_cost__isnull=True).only(
        "work_id",
        "index",
        "parent_index",
        "total_cost",
    )
    active_parent_rows = BudgetItem.objects.filter(is_active=True).exclude(parent_index="").values_list(
        "work_id",
        "parent_index",
    )
    parent_indices_by_work = defaultdict(set)
    for work_id, parent_index in active_parent_rows:
        parent_indices_by_work[work_id].add(parent_index)
    for item in items:
        items_by_work[item.work_id].append(item)

    totals = {}
    for work_id, work_items in items_by_work.items():
        parent_indices = parent_indices_by_work[work_id]
        leaf_items = [item for item in work_items if item.index not in parent_indices]
        selected_items = leaf_items or deepest_budget_items(work_items)
        if selected_items:
            totals[work_id] = sum((item.total_cost or ZERO for item in selected_items), ZERO)
    return totals


def deepest_budget_items(items: list[BudgetItem]) -> list[BudgetItem]:
    if not items:
        return []
    deepest = max(index_depth(item.index) for item in items)
    return [item for item in items if index_depth(item.index) == deepest]


def index_depth(index: str) -> int:
    return len([part for part in index.split(".") if part])


def work_budget_status(
    has_budget: bool,
    accumulated_spent: Decimal,
    budget_total: Decimal | None,
    consumed_percentage: Decimal | None,
    pending_amount: Decimal = ZERO,
) -> str:
    if not has_budget:
        return "sem_orcamento" if accumulated_spent > ZERO or pending_amount > ZERO else "sem_movimento"
    if budget_total is not None and accumulated_spent > budget_total:
        return "acima_orcamento"
    if consumed_percentage is not None and consumed_percentage >= Decimal("80"):
        return "atencao"
    return "ok"
