from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.db.models import Count, Q, Sum

from apps.banking.models import OfxFile, OfxTransaction, Reconciliation
from apps.counterparties.models import Origin
from apps.payments.models import Payment
from apps.telegrambot.models import TelegramDraft

from .services import payment_missing_required_fields


PENDING_PAYMENT_STATUSES = [
    Payment.Status.RECEIVED,
    Payment.Status.PROCESSING,
    Payment.Status.PENDING_REGISTRATION,
    Payment.Status.PENDING_CONFIRMATION,
    Payment.Status.CORRECTING,
    Payment.Status.POSSIBLE_DUPLICATE,
    Payment.Status.ERROR,
]
EXPORTABLE_PAYMENT_STATUSES = [
    Payment.Status.APPROVED,
    Payment.Status.RECONCILED,
]


@dataclass(frozen=True)
class PaymentRequiredFieldsIssue:
    payment_id: int
    missing_fields: list[str]


@dataclass(frozen=True)
class PaymentWorkWithoutBudgetIssue:
    payment_id: int
    work_id: int
    work_name: str


@dataclass(frozen=True)
class MonthlyClosingSummary:
    month: int
    year: int
    period_start: date
    period_end: date
    total_payments: int
    active_drafts_count: int
    received_count: int
    pending_total: int
    pending_registration_count: int
    pending_confirmation_count: int
    correcting_count: int
    approved_count: int
    reconciled_count: int
    canceled_count: int
    approved_amount: Decimal
    reconciled_amount: Decimal
    exportable_payments_count: int
    payments_missing_required_fields: list[PaymentRequiredFieldsIssue]
    payments_with_work_without_budget: list[PaymentWorkWithoutBudgetIssue]
    approved_unreconciled_count: int
    ofx_expense_without_payment_count: int
    ofx_suggested_pending_registration_count: int
    ofx_suggested_pending_confirmation_count: int
    ofx_ignored_credit_count: int
    ofx_pending_count: int
    ofx_divergent_count: int
    ofx_possible_duplicate_count: int
    has_ofx_imported: bool


def get_month_period(month: int, year: int) -> tuple[date, date]:
    if month < 1 or month > 12:
        raise ValueError("Month deve estar entre 1 e 12.")
    start = date(year, month, 1)
    end = date(year, month, monthrange(year, month)[1])
    return start, end


def payments_for_period(period_start: date, period_end: date):
    return Payment.objects.filter(payment_date__gte=period_start, payment_date__lte=period_end)


def active_drafts_for_period(period_start: date, period_end: date):
    return TelegramDraft.objects.filter(status=TelegramDraft.Status.ACTIVE).filter(
        Q(payment_date__gte=period_start, payment_date__lte=period_end)
        | Q(payment_date__isnull=True, updated_at__date__gte=period_start, updated_at__date__lte=period_end)
    )


def export_candidate_payments_for_period(period_start: date, period_end: date):
    return (
        payments_for_period(period_start, period_end)
        .filter(status__in=EXPORTABLE_PAYMENT_STATUSES)
        .select_related(
            "counterparty",
            "counterparty__default_category",
            "counterparty__default_chart_account",
            "category",
            "chart_account",
            "cost_center",
            "work",
        )
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


def payments_with_work_without_budget_for_period(period_start: date, period_end: date):
    return (
        payments_for_period(period_start, period_end)
        .exclude(status=Payment.Status.CANCELED)
        .filter(work__isnull=False)
        .annotate(active_budget_items_count=Count("work__budget_items", filter=Q(work__budget_items__is_active=True)))
        .filter(active_budget_items_count=0)
        .select_related("work")
        .order_by("payment_date", "id")
    )


def build_monthly_closing(month: int, year: int) -> MonthlyClosingSummary:
    period_start, period_end = get_month_period(month, year)
    payments = payments_for_period(period_start, period_end)
    status_counts = dict(payments.values("status").annotate(total=Count("id")).values_list("status", "total"))
    ofx_transactions = ofx_transactions_for_period(period_start, period_end)
    ofx_transactions_list = list(ofx_transactions)
    export_candidates = export_candidate_payments_for_period(period_start, period_end)
    ofx_suggested_pending_registration_count = payments.filter(
        source=Origin.OFX,
        status=Payment.Status.PENDING_REGISTRATION,
    ).count()
    ofx_suggested_pending_confirmation_count = payments.filter(
        source=Origin.OFX,
        status=Payment.Status.PENDING_CONFIRMATION,
    ).count()

    missing_required_fields = []
    exportable_count = 0
    for payment in export_candidates:
        missing_fields = payment_missing_required_fields(payment)
        if missing_fields:
            missing_required_fields.append(
                PaymentRequiredFieldsIssue(payment_id=payment.pk, missing_fields=missing_fields)
            )
        else:
            exportable_count += 1
    work_without_budget_issues = [
        PaymentWorkWithoutBudgetIssue(
            payment_id=payment.pk,
            work_id=payment.work_id,
            work_name=payment.work.name,
        )
        for payment in payments_with_work_without_budget_for_period(period_start, period_end)
    ]

    return MonthlyClosingSummary(
        month=month,
        year=year,
        period_start=period_start,
        period_end=period_end,
        total_payments=payments.count(),
        active_drafts_count=active_drafts_for_period(period_start, period_end).count(),
        received_count=status_counts.get(Payment.Status.RECEIVED, 0),
        pending_total=payments.filter(status__in=PENDING_PAYMENT_STATUSES).count(),
        pending_registration_count=status_counts.get(Payment.Status.PENDING_REGISTRATION, 0),
        pending_confirmation_count=status_counts.get(Payment.Status.PENDING_CONFIRMATION, 0),
        correcting_count=status_counts.get(Payment.Status.CORRECTING, 0),
        approved_count=status_counts.get(Payment.Status.APPROVED, 0),
        reconciled_count=status_counts.get(Payment.Status.RECONCILED, 0),
        canceled_count=status_counts.get(Payment.Status.CANCELED, 0),
        approved_amount=sum_payments(payments, Payment.Status.APPROVED),
        reconciled_amount=sum_payments(payments, Payment.Status.RECONCILED),
        exportable_payments_count=exportable_count,
        payments_missing_required_fields=missing_required_fields,
        payments_with_work_without_budget=work_without_budget_issues,
        approved_unreconciled_count=approved_unreconciled_payments_for_period(period_start, period_end).count(),
        ofx_expense_without_payment_count=count_ofx_expenses_without_payment(ofx_transactions_list),
        ofx_suggested_pending_registration_count=ofx_suggested_pending_registration_count,
        ofx_suggested_pending_confirmation_count=ofx_suggested_pending_confirmation_count,
        ofx_ignored_credit_count=ofx_transactions.filter(
            status=OfxTransaction.Status.IGNORED,
            amount__gt=0,
        ).count(),
        ofx_pending_count=ofx_transactions.filter(status=OfxTransaction.Status.PENDING).count(),
        ofx_divergent_count=ofx_transactions.filter(status=OfxTransaction.Status.DIVERGENT).count(),
        ofx_possible_duplicate_count=ofx_transactions.filter(status=OfxTransaction.Status.POSSIBLE_DUPLICATE).count(),
        has_ofx_imported=ofx_files_for_period(period_start, period_end).exists(),
    )


def sum_payments(payments, status: str) -> Decimal:
    value = payments.filter(status=status).aggregate(total=Sum("amount"))["total"]
    return value or Decimal("0.00")


def approved_unreconciled_payments_for_period(period_start: date, period_end: date):
    return (
        payments_for_period(period_start, period_end)
        .filter(status__in=[Payment.Status.APPROVED, Payment.Status.POSTED])
        .exclude(reconciliations__status=Reconciliation.Status.CONFIRMED)
        .distinct()
    )


def count_ofx_expenses_without_payment(transactions: list[OfxTransaction]) -> int:
    debit_transactions = [
        transaction_record
        for transaction_record in transactions
        if transaction_record.amount < Decimal("0.00")
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
