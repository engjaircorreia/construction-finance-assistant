import hashlib
import json
import os
import re
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.files.base import ContentFile
from django.db import IntegrityError, connection, transaction
from django.db.models import Prefetch, Q
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.text import get_valid_filename
from django.views.decorators.http import require_POST

from apps.banking.ofx_import import import_uploaded_ofx_file
from apps.accounts.models import AuthorizedTelegramUser
from apps.banking.models import OfxFile, OfxTransaction, Reconciliation
from apps.banking.payment_suggestions import suggest_payments_from_ofx
from apps.banking.reconciliation import mark_reconciled, reconcile_ofx_transactions
from apps.banking.reports import build_ofx_import_summary
from apps.counterparties.importers import import_budget_workbook_for_work, normalize_text
from apps.counterparties.models import BudgetImportBatch, BudgetItem, Category, CostCenter, Counterparty, Origin, Work
from apps.documents.models import UploadedFile
from apps.exports.monthly_closing import build_monthly_closing, get_month_period
from apps.exports.models import ExportBatch
from apps.exports.services import (
    ExportValidationError,
    export_approved_payments,
    export_approved_payments_for_period,
    payment_missing_required_fields,
)
from apps.core.dashboard import REALIZED_PAYMENT_STATUSES as DASHBOARD_REALIZED_PAYMENT_STATUSES
from apps.core.dashboard import build_dashboard_summary
from apps.core.dashboard import payment_period_q
from apps.core.forms import (
    BudgetImportForm,
    CounterpartyManualForm,
    CounterpartyQuickForm,
    OfxPaymentBulkEditForm,
    PaymentManualForm,
    TelegramDraftForm,
    WorkCostCenterQuickForm,
)
from apps.payments.confirmation import (
    PaymentConfirmationError,
    approve_payment,
    cancel_payment,
    format_payment_suggestion,
    request_payment_correction,
)
from apps.payments.models import Payment
from apps.telegrambot.models import TelegramDraft
from apps.telegrambot.services import (
    TelegramIntakeService,
    TelegramSender,
    clear_draft_counterparty_candidate,
    clear_draft_work_candidate,
    draft_blocking_pendency_lines,
)


PENDING_PAYMENT_STATUSES = [
    Payment.Status.RECEIVED,
    Payment.Status.PROCESSING,
    Payment.Status.PENDING_REGISTRATION,
    Payment.Status.PENDING_CONFIRMATION,
    Payment.Status.CORRECTING,
    Payment.Status.POSSIBLE_DUPLICATE,
    Payment.Status.ERROR,
]
PAYMENT_STATUS_FILTERS = [
    ("all", "All"),
    ("pendencias", "Pending items"),
    ("realizados", "Realized"),
    (Payment.Status.RECEIVED, "Received"),
    (Payment.Status.PROCESSING, "Processing"),
    (Payment.Status.PENDING_REGISTRATION, "Pending registration"),
    (Payment.Status.PENDING_CONFIRMATION, "Pending"),
    (Payment.Status.CORRECTING, "Under review"),
    (Payment.Status.APPROVED, "Approved"),
    (Payment.Status.POSTED, "Posted"),
    (Payment.Status.RECONCILED, "Reconciled"),
    (Payment.Status.POSSIBLE_DUPLICATE, "Possible duplicate"),
    (Payment.Status.ERROR, "Error"),
    (Payment.Status.IGNORED, "Ignored"),
]
PAYMENT_EXACT_STATUS_VALUES = {value for value, _label in PAYMENT_STATUS_FILTERS if value not in {"all", "pendencias", "realizados"}}
DOCUMENT_FILTERS = [
    ("all", "All"),
    ("com", "With CPF/CNPJ"),
    ("sem", "Without CPF/CNPJ"),
]
DATE_STATUS_FILTERS = [
    ("all", "All"),
    ("com_date", "With date"),
    ("sem_date", "Without date"),
]
RECONCILIATION_FILTERS = [
    ("all", "All"),
    ("com", "With reconciled OFX"),
    ("sem", "Without reconciled OFX"),
]
BULK_APPROVABLE_PAYMENT_STATUSES = {
    Payment.Status.PENDING_CONFIRMATION,
}

TELEGRAM_DRAFT_STATUS_FILTERS = [
    (TelegramDraft.Status.ACTIVE, "Active"),
    (TelegramDraft.Status.FINALIZED, "Finalized"),
    (TelegramDraft.Status.CANCELED, "Canceled"),
    ("all", "All"),
]
TELEGRAM_DRAFT_EXACT_STATUS_VALUES = {value for value, _label in TelegramDraft.Status.choices}

UNRECONCILED_OFX_STATUSES = [
    OfxTransaction.Status.PENDING,
    OfxTransaction.Status.POSSIBLE_DUPLICATE,
    OfxTransaction.Status.MISSING_PAYMENT,
    OfxTransaction.Status.DIVERGENT,
]
OFX_STATUS_FILTERS = [
    ("pendencias", "Pending items"),
    ("all", "All"),
    (OfxTransaction.Status.PENDING, "Pending"),
    (OfxTransaction.Status.RECONCILED, "Reconciled"),
    (OfxTransaction.Status.DIVERGENT, "Divergent"),
    (OfxTransaction.Status.POSSIBLE_DUPLICATE, "Possible duplicate"),
    (OfxTransaction.Status.MISSING_PAYMENT, "Missing payment"),
    (OfxTransaction.Status.IGNORED, "Ignored credit"),
]
OFX_EXACT_STATUS_VALUES = {value for value, _label in OFX_STATUS_FILTERS if value not in {"pendencias", "all"}}
OFX_PAYMENT_FILTERS = [
    ("all", "All"),
    ("com_sugestao", "With suggested payment"),
    (Payment.Status.PENDING_REGISTRATION, "Pending registration"),
    (Payment.Status.PENDING_CONFIRMATION, "Pending"),
]
OFX_PAYMENT_FILTER_VALUES = {value for value, _label in OFX_PAYMENT_FILTERS}
OFX_BULK_EDIT_BLOCKED_STATUSES = {
    Payment.Status.APPROVED,
    Payment.Status.POSTED,
    Payment.Status.RECONCILED,
    Payment.Status.CANCELED,
    Payment.Status.IGNORED,
}

MONTH_CHOICES = [
    (1, "January"),
    (2, "February"),
    (3, "March"),
    (4, "April"),
    (5, "May"),
    (6, "June"),
    (7, "July"),
    (8, "August"),
    (9, "September"),
    (10, "October"),
    (11, "November"),
    (12, "December"),
]
BUDGET_IMPORT_HINT = (
    "Use the project's Import budget button. As an operational fallback, run "
    "python manage.py import_budget_items --path /path/to/budget.xlsx."
)
DASHBOARD_CHART_COLORS = [
    "#36b595",
    "#4aa8ff",
    "#f2c96d",
    "#8f6df2",
    "#ff8d87",
    "#6ed0e0",
    "#f39c6b",
    "#b5d56a",
    "#d67adf",
    "#9ca9b8",
]
DASHBOARD_MAX_FINANCIAL_CENTER_SLICES = 8
MONTH_SHORT_NAMES = {
    1: "Jan",
    2: "Feb",
    3: "Mar",
    4: "Apr",
    5: "May",
    6: "Jun",
    7: "Jul",
    8: "Aug",
    9: "Sep",
    10: "Oct",
    11: "Nov",
    12: "Dec",
}

SENSITIVE_SETTING_NAMES = [
    "SECRET_KEY",
    "TELEGRAM_BOT_TOKEN",
    "OPENAI_API_KEY",
    "DATABASE_URL",
    "POSTGRES_PASSWORD",
    "DB_PASSWORD",
    "CELERY_BROKER_URL",
    "CELERY_RESULT_BACKEND",
]


def service_status(label, checker):
    try:
        status = checker()
    except Exception:
        status = {
            "status": "not checked",
            "detail": "Check unavailable right now.",
            "level": "unknown",
        }
    status.setdefault("detail", "")
    status.setdefault("level", "unknown")
    return {"label": label, **status}


def check_database_status():
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        cursor.fetchone()
    return {"status": "ok", "detail": "Connection active.", "level": "ok"}


def check_redis_status():
    redis_url = getattr(settings, "REDIS_URL", "") or getattr(settings, "CELERY_BROKER_URL", "")
    if not redis_url:
        return {"status": "not configured", "detail": "URL do Redis ausente.", "level": "warning"}
    import redis

    client = redis.Redis.from_url(redis_url, socket_connect_timeout=0.2, socket_timeout=0.2)
    client.ping()
    return {"status": "ok", "detail": "PING respondeu.", "level": "ok"}


def check_celery_status():
    if not getattr(settings, "CELERY_BROKER_URL", ""):
        return {"status": "not configured", "detail": "Broker do Celery ausente.", "level": "warning"}
    from config.celery import app as celery_app

    replies = celery_app.control.ping(timeout=0.2)
    if not replies:
        return {"status": "not checked", "detail": "No worker responded to the short ping.", "level": "unknown"}
    return {"status": "ok", "detail": f"{len(replies)} worker(s) responderam.", "level": "ok"}


def check_bot_status():
    active_users = AuthorizedTelegramUser.objects.filter(is_active=True).count()
    if getattr(settings, "TELEGRAM_BOT_TOKEN", ""):
        return {
            "status": "configured",
            "detail": f"{active_users} user(s) Telegram autorizado(s).",
            "level": "ok",
        }
    return {
        "status": "not configured",
        "detail": f"{active_users} user(s) Telegram autorizado(s).",
        "level": "warning",
    }


def sensitive_setting_values():
    values = []
    for name in SENSITIVE_SETTING_NAMES:
        value = getattr(settings, name, "")
        if isinstance(value, str) and len(value) >= 6:
            values.append(value)
    database_password = settings.DATABASES.get("default", {}).get("PASSWORD", "")
    if isinstance(database_password, str) and len(database_password) >= 6:
        values.append(database_password)
    return values


def sanitize_operational_detail(value, limit=180):
    text = " ".join(str(value or "Sem detalhe.").split())
    text = re.sub(r"\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b", "**.***.***/****-**", text)
    text = re.sub(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b", "***.***.***-**", text)
    text = re.sub(r"(?i)\b(token|secret|password|senha|api[_-]?key)\s*[:=]\s*\S+", r"\1=[removed]", text)
    for secret in sensitive_setting_values():
        text = text.replace(secret, "[removed]")
    if len(text) > limit:
        return f"{text[: limit - 3]}..."
    return text


def recent_operational_errors(limit=6):
    errors = []
    for batch in ExportBatch.objects.filter(status=ExportBatch.Status.ERROR).order_by("-updated_at")[:limit]:
        errors.append(
            {
                "when": batch.updated_at,
                "source": "Export",
                "summary": f"Batch #{batch.pk}",
                "detail": sanitize_operational_detail(batch.error_message),
            }
        )
    for uploaded_file in UploadedFile.objects.filter(status=UploadedFile.Status.ERROR).order_by("-updated_at")[:limit]:
        errors.append(
            {
                "when": uploaded_file.updated_at,
                "source": "File",
                "summary": f"File #{uploaded_file.pk} ({uploaded_file.get_kind_display()})",
                "detail": sanitize_operational_detail(uploaded_file.error_message),
            }
        )
    for payment in Payment.objects.filter(status=Payment.Status.ERROR).order_by("-updated_at")[:limit]:
        errors.append(
            {
                "when": payment.updated_at,
                "source": "Payment",
                "summary": f"Payment #{payment.pk}",
                "detail": sanitize_operational_detail(payment.review_reason),
            }
        )
    for ofx_file in OfxFile.objects.filter(status=OfxFile.Status.ERROR).order_by("-updated_at")[:limit]:
        errors.append(
            {
                "when": ofx_file.updated_at,
                "source": "OFX",
                "summary": f"OFX #{ofx_file.pk}",
                "detail": sanitize_operational_detail(ofx_file.notes),
            }
        )
    return sorted(errors, key=lambda error: error["when"], reverse=True)[:limit]


def build_operational_diagnostics():
    payment_pending_count = Payment.objects.filter(status__in=PENDING_PAYMENT_STATUSES).count()
    counters = {
        "active_drafts": TelegramDraft.objects.filter(status=TelegramDraft.Status.ACTIVE).count(),
        "pending_payments": payment_pending_count,
        "pending_registration": Payment.objects.filter(status=Payment.Status.PENDING_REGISTRATION).count(),
        "pending_confirmation": Payment.objects.filter(status=Payment.Status.PENDING_CONFIRMATION).count(),
        "ofx_pending": OfxTransaction.objects.filter(status=OfxTransaction.Status.PENDING).count(),
        "ofx_divergent": OfxTransaction.objects.filter(status=OfxTransaction.Status.DIVERGENT).count(),
        "ofx_possible_duplicates": OfxTransaction.objects.filter(status=OfxTransaction.Status.POSSIBLE_DUPLICATE).count(),
    }
    settings_module = os.environ.get("DJANGO_SETTINGS_MODULE", "")
    environment = "desenvolvimento" if settings.DEBUG else "producao"
    return {
        "services": [
            service_status("Database", check_database_status),
            service_status("Redis", check_redis_status),
            service_status("Celery/worker", check_celery_status),
            service_status("Bot Telegram", check_bot_status),
        ],
        "counters": counters,
        "last_export_batch": ExportBatch.objects.filter(status=ExportBatch.Status.GENERATED)
        .order_by("-generated_at", "-created_at")
        .first(),
        "last_ofx_file": OfxFile.objects.order_by("-created_at").first(),
        "recent_errors": recent_operational_errors(),
        "environment": environment,
        "settings_module": settings_module,
        "debug": settings.DEBUG,
    }


@login_required
def dashboard(request):
    today = timezone.localdate()
    month = parse_int(request.GET.get("mes"), today.month)
    year = parse_int(request.GET.get("ano"), today.year)
    if month < 1 or month > 12:
        month = today.month
    if year < 1900 or year > 2100:
        year = today.year

    summary = build_dashboard_summary(month=month, year=year)
    financial_center_legend = dashboard_financial_center_legend(summary)
    year_choices = range(min(today.year - 2, year - 1), max(today.year + 2, year + 2))
    context = {
        "summary": summary,
        "selected_month": month,
        "selected_year": year,
        "month_choices": MONTH_CHOICES,
        "year_choices": year_choices,
        "dashboard_cards": dashboard_cards(summary),
        "financial_center_legend": financial_center_legend,
        "category_rows": dashboard_category_rows(summary),
        "work_budget_rows": dashboard_work_budget_rows(summary),
        "financial_center_pie_gradient": dashboard_pie_gradient(financial_center_legend),
        "monthly_evolution": dashboard_monthly_evolution(summary),
        "previous_year_url": dashboard_year_link(month, year - 1),
        "next_year_url": dashboard_year_link(month, year + 1),
        "monthly_evolution_year": year,
        "operational_pendencies": dashboard_operational_pendencies(summary),
        "period_payments_url": dashboard_period_payments_link(summary),
        "pending_payments_url": monthly_payment_link(summary, "pendencias"),
        "pending_ofx_url": monthly_ofx_link(summary, "pendencias"),
        "active_drafts_url": monthly_drafts_link(summary),
    }
    return render(request, "core/dashboard.html", context)


def dashboard_cards(summary):
    return [
        {
            "label": "Period spend",
            "value": format_money_br(summary.realized_amount),
            "detail": f"{summary.realized_payments_count} payment(s) by due/accrual date",
        },
        {
            "label": "Accumulated spend",
            "value": format_money_br(summary.accumulated_realized_amount),
            "detail": "Histórico de payments realizados",
        },
        {
            "label": "Approved without OFX",
            "value": format_money_br(summary.approved_unreconciled_amount),
            "detail": f"{summary.approved_unreconciled_count} payment(s)",
        },
        {
            "label": "Reconciled in period",
            "value": format_money_br(summary.reconciled_amount),
            "detail": f"{summary.reconciled_count} payment(s) with confirmed reconciliation",
        },
        {
            "label": "Pending approval",
            "value": format_money_br(summary.pending_confirmation_amount),
            "detail": "Waiting for approval, correction, or cancellation",
        },
        {
            "label": "OFX without payment",
            "value": summary.ofx_expense_without_payment_count,
            "detail": "Bank expenses still without a related payment",
        },
        {
            "label": "OFX suggestions",
            "value": summary.ofx_suggested_pending_registration_count
            + summary.ofx_suggested_pending_confirmation_count,
            "detail": (
                f"{summary.ofx_suggested_pending_registration_count} registration · "
                f"{summary.ofx_suggested_pending_confirmation_count} approval"
            ),
        },
        {
            "label": "OFX imported",
            "value": "yes" if summary.has_ofx_imported else "no",
            "detail": f"{summary.ofx_imported_count} file(s) in period",
        },
        {
            "label": "Ignored credits",
            "value": summary.ofx_ignored_credit_count,
            "detail": "Income/credits from OFX outside the expense flow",
        },
        {
            "label": "Operational pending items",
            "value": summary.operational_pendency_count,
            "detail": "Drafts, OFX, registrations, and missing dates",
        },
    ]


def dashboard_financial_center_legend(summary):
    groups = list(summary.financial_center_groups)
    hidden_groups = []
    if len(groups) > DASHBOARD_MAX_FINANCIAL_CENTER_SLICES:
        visible_count = DASHBOARD_MAX_FINANCIAL_CENTER_SLICES - 1
        hidden_groups = groups[visible_count:]
        groups = groups[:visible_count]

    legend = []
    for index, group in enumerate(groups):
        legend.append(
            {
                "label": group.label,
                "amount": group.amount,
                "amount_display": format_money_br(group.amount),
                "count": group.count,
                "percentage_raw": group.percentage,
                "percentage": group.percentage.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP),
                "color": DASHBOARD_CHART_COLORS[index % len(DASHBOARD_CHART_COLORS)],
                "url": dashboard_financial_center_link(summary, group),
                "is_other": False,
            }
        )
    if hidden_groups:
        amount = sum((group.amount for group in hidden_groups), Decimal("0.00"))
        count = sum(group.count for group in hidden_groups)
        percentage_raw = dashboard_percent(amount, summary.realized_amount)
        legend.append(
            {
                "label": f"Other ({len(hidden_groups)} groups)",
                "amount": amount,
                "amount_display": format_money_br(amount),
                "count": count,
                "percentage_raw": percentage_raw,
                "percentage": percentage_raw.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP),
                "color": DASHBOARD_CHART_COLORS[(DASHBOARD_MAX_FINANCIAL_CENTER_SLICES - 1) % len(DASHBOARD_CHART_COLORS)],
                "url": dashboard_realized_payments_link(summary),
                "is_other": True,
            }
        )
    return legend


def dashboard_pie_gradient(legend):
    if not legend:
        return "conic-gradient(var(--surface-3) 0deg 360deg)"

    current = Decimal("0")
    segments = []
    for index, item in enumerate(legend):
        color = item["color"]
        if index == len(legend) - 1:
            end = Decimal("360")
        else:
            end = current + (item["percentage_raw"] / Decimal("100") * Decimal("360"))
            end = end.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        segments.append(f"{color} {current}deg {end}deg")
        current = end
    return f"conic-gradient({', '.join(segments)})"


def dashboard_financial_center_link(summary, group):
    params = {
        "status": "realizados",
        "date_inicio": summary.period_start.isoformat(),
        "date_fim": summary.period_end.isoformat(),
    }
    if group.kind == "work" and group.object_id:
        params["work"] = group.object_id
    elif group.kind == "cost_center" and group.object_id:
        params["cost_center"] = group.object_id
        params["work"] = "sem"
    elif group.kind == "company":
        params["cost_center"] = "sem"
        params["work"] = "sem"
    return url_with_query(reverse("internal_pending_payments"), **params)


def dashboard_category_rows(summary):
    rows = []
    for group in summary.category_groups:
        rows.append(
            {
                "label": group.label,
                "amount": group.amount,
                "amount_display": format_money_br(group.amount),
                "count": group.count,
                "percentage": group.percentage.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP),
                "url": dashboard_category_link(summary, group),
            }
        )
    return rows


def dashboard_category_link(summary, group):
    params = {
        "status": "realizados",
        "date_inicio": summary.period_start.isoformat(),
        "date_fim": summary.period_end.isoformat(),
    }
    if group.object_id:
        params["category"] = group.object_id
    else:
        params["category"] = "sem"
    return url_with_query(reverse("internal_pending_payments"), **params)


def dashboard_work_budget_rows(summary):
    rows = []
    for item in summary.work_budget_summaries:
        rows.append(
            {
                "item": item,
                "monthly_spent_display": format_money_br(item.monthly_spent),
                "accumulated_spent_display": format_money_br(item.accumulated_spent),
                "pending_amount_display": format_money_br(item.pending_amount),
                "budget_total_display": format_money_br(item.budget_total) if item.budget_total is not None else "-",
                "estimated_balance_display": (
                    format_money_br(item.estimated_balance) if item.estimated_balance is not None else "-"
                ),
                "payments_url": monthly_work_payments_link(summary, item.work_id),
                "budget_import_url": reverse("internal_work_budget_import", args=[item.work_id]),
            }
        )
    return rows


def dashboard_period_payments_link(summary):
    return url_with_query(
        reverse("internal_pending_payments"),
        status="all",
        date_inicio=summary.period_start.isoformat(),
        date_fim=summary.period_end.isoformat(),
    )


def dashboard_realized_payments_link(summary):
    return url_with_query(
        reverse("internal_pending_payments"),
        status="realizados",
        date_inicio=summary.period_start.isoformat(),
        date_fim=summary.period_end.isoformat(),
    )


def dashboard_monthly_evolution(summary):
    max_amount = max((item.realized_amount for item in summary.monthly_evolution), default=Decimal("0.00"))
    rows = []
    for item in summary.monthly_evolution:
        rows.append(
            {
                "label": f"{MONTH_SHORT_NAMES[item.month]}/{str(item.year)[-2:]}",
                "realized_amount": item.realized_amount,
                "realized_amount_display": format_money_br(item.realized_amount),
                "reconciled_amount": item.reconciled_amount,
                "reconciled_amount_display": format_money_br(item.reconciled_amount),
                "pending_amount": item.pending_amount,
                "pending_amount_display": format_money_br(item.pending_amount),
                "payments_count": item.payments_count,
                "bar_width": dashboard_percent(item.realized_amount, max_amount),
                "url": dashboard_month_payments_link(item.period_start, item.period_end),
            }
        )
    return rows


def dashboard_year_link(month: int, year: int) -> str:
    return url_with_query(reverse("internal_dashboard"), mes=month, ano=year)


def dashboard_month_payments_link(period_start, period_end):
    return url_with_query(
        reverse("internal_pending_payments"),
        status="all",
        date_inicio=period_start.isoformat(),
        date_fim=period_end.isoformat(),
    )


def dashboard_percent(amount: Decimal, total: Decimal) -> Decimal:
    if amount <= Decimal("0") or total <= Decimal("0"):
        return Decimal("0")
    return (amount / total * Decimal("100")).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def format_money_br(value: Decimal | int | str | None) -> str:
    if value in (None, ""):
        return "-"
    amount = Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sign = "-" if amount < 0 else ""
    amount = abs(amount)
    formatted = f"{amount:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    return f"R$ {sign}{formatted}"


def dashboard_operational_pendencies(summary):
    rows = []
    if summary.active_drafts_count:
        rows.append(dashboard_pendency_row(
            "Active drafts",
            summary.active_drafts_count,
            "",
            "Finalize, edit, or cancel active drafts.",
            monthly_drafts_link(summary),
            "Open drafts",
        ))
    if summary.undated_payments_count:
        rows.append(dashboard_pendency_row(
            "Payments without date",
            summary.undated_payments_count,
            format_money_br(summary.undated_payments_amount),
            "They do not enter period totals until the payment date is filled.",
            undated_payments_link(),
            "Complete dates",
        ))
    if summary.pending_registration_count:
        rows.append(dashboard_pendency_row(
            "Pending registration",
            summary.pending_registration_count,
            format_money_br(summary.pending_registration_amount),
            "Complete missing vendor/worker, project, category, or date.",
            monthly_payment_link(summary, Payment.Status.PENDING_REGISTRATION),
            "Resolve registration",
        ))
    if summary.ofx_suggested_pending_registration_count:
        rows.append(dashboard_pendency_row(
            "OFX suggestion pending registration",
            summary.ofx_suggested_pending_registration_count,
            format_money_br(summary.ofx_suggested_pending_registration_amount),
            "Register or link the counterparty suggested by the statement.",
            monthly_ofx_payment_link(summary, Payment.Status.PENDING_REGISTRATION),
            "Review OFX",
        ))
    if summary.pending_confirmation_count:
        rows.append(dashboard_pendency_row(
            "Pending approval",
            summary.pending_confirmation_count,
            format_money_br(summary.pending_confirmation_amount),
            "Review payments and approve them when they are correct.",
            monthly_payment_link(summary, Payment.Status.PENDING_CONFIRMATION),
            "Review",
        ))
    if summary.ofx_suggested_pending_confirmation_count:
        rows.append(dashboard_pendency_row(
            "OFX suggestion pending approval",
            summary.ofx_suggested_pending_confirmation_count,
            format_money_br(summary.ofx_suggested_pending_confirmation_amount),
            "Check the fields suggested by OFX before approving.",
            monthly_ofx_payment_link(summary, Payment.Status.PENDING_CONFIRMATION),
            "Review OFX",
        ))
    if summary.correcting_count:
        rows.append(dashboard_pendency_row(
            "Under correction",
            summary.correcting_count,
            format_money_br(summary.correcting_amount),
            "Review payments and make them ready for approval.",
            monthly_payment_link(summary, Payment.Status.CORRECTING),
            "Open corrections",
        ))
    if summary.approved_unreconciled_count:
        rows.append(dashboard_pendency_row(
            "Approved/posted without OFX",
            summary.approved_unreconciled_count,
            format_money_br(summary.approved_unreconciled_amount),
            "Already counted as realized, but still needs confirmed bank reconciliation.",
            unreconciled_realized_payments_link(summary),
            "Reconcile",
        ))
    if summary.ofx_expense_without_payment_count:
        rows.append(dashboard_pendency_row(
            "OFX expense without payment",
            summary.ofx_expense_without_payment_count,
            "",
            "Expense bank transaction still has no suggested or reconciled payment.",
            monthly_ofx_link(summary, "pendencias"),
            "Create suggestion",
        ))
    if summary.ofx_pending_count:
        rows.append(dashboard_pendency_row(
            "OFX pending",
            summary.ofx_pending_count,
            "",
            "Review OFX transactions still pending.",
            monthly_ofx_link(summary, OfxTransaction.Status.PENDING),
            "Open OFX",
        ))
    if summary.ofx_divergent_count:
        rows.append(dashboard_pendency_row(
            "Divergent OFX",
            summary.ofx_divergent_count,
            "",
            "Review amount, date, or counterparty divergences.",
            monthly_ofx_link(summary, OfxTransaction.Status.DIVERGENT),
            "Resolve OFX",
        ))
    if summary.ofx_possible_duplicate_count:
        rows.append(dashboard_pendency_row(
            "OFX possible duplicate",
            summary.ofx_possible_duplicate_count,
            "",
            "Confirm whether the transaction has already been posted.",
            monthly_ofx_link(summary, OfxTransaction.Status.POSSIBLE_DUPLICATE),
            "Review duplicates",
        ))
    if summary.ofx_ignored_credit_count:
        rows.append(dashboard_pendency_row(
            "Ignored OFX credits",
            summary.ofx_ignored_credit_count,
            "",
            "Credits/income were identified in OFX and do not enter as expenses.",
            monthly_ofx_link(summary, OfxTransaction.Status.IGNORED),
            "View credits",
        ))
    works_without_budget = [
        item
        for item in summary.work_budget_summaries
        if item.status == "sem_orcamento" and item.accumulated_spent > Decimal("0.00")
    ]
    if works_without_budget:
        amount = sum((item.accumulated_spent for item in works_without_budget), Decimal("0.00"))
        first_work = works_without_budget[0]
        rows.append(dashboard_pendency_row(
            "Projects with spend and no budget",
            len(works_without_budget),
            format_money_br(amount),
            "Import the budget to track consumed percentage and estimated balance.",
            reverse("internal_work_budget_import", args=[first_work.work_id]),
            "Import budget",
        ))
    return rows


def dashboard_pendency_row(label, count, amount, detail, url, action_label):
    return {
        "label": label,
        "count": count,
        "amount": amount or "-",
        "detail": detail,
        "url": url,
        "action_label": action_label,
    }


def undated_payments_link():
    return url_with_query(reverse("internal_pending_payments"), status="all", date_status="sem_date")


def unreconciled_realized_payments_link(summary):
    return url_with_query(
        reverse("internal_pending_payments"),
        status="realizados",
        ofx="sem",
        date_inicio=summary.period_start.isoformat(),
        date_fim=summary.period_end.isoformat(),
    )


@login_required
def operational_diagnostics(request):
    return render(
        request,
        "core/operational_diagnostics.html",
        {"diagnostics": build_operational_diagnostics()},
    )


@login_required
def telegram_drafts(request):
    selected_status = request.GET.get("status") or TelegramDraft.Status.ACTIVE
    if selected_status not in TELEGRAM_DRAFT_EXACT_STATUS_VALUES | {"all"}:
        selected_status = TelegramDraft.Status.ACTIVE
    date_start = parse_date(request.GET.get("date_inicio") or "")
    date_end = parse_date(request.GET.get("date_fim") or "")
    drafts = (
        TelegramDraft.objects.select_related("counterparty", "category", "cost_center", "work", "finalized_payment")
        .prefetch_related("uploaded_files")
    )
    if selected_status != "all":
        drafts = drafts.filter(status=selected_status)
    if date_start:
        drafts = drafts.filter(
            Q(payment_date__gte=date_start) | Q(payment_date__isnull=True, updated_at__date__gte=date_start)
        )
    if date_end:
        drafts = drafts.filter(
            Q(payment_date__lte=date_end) | Q(payment_date__isnull=True, updated_at__date__lte=date_end)
        )
    drafts = list(drafts.order_by("-updated_at")[:100])
    enrich_telegram_draft_rows(drafts)
    return render(
        request,
        "core/telegram_drafts.html",
        {
            "drafts": drafts,
            "selected_status": selected_status,
            "status_filters": TELEGRAM_DRAFT_STATUS_FILTERS,
            "date_start": date_start,
            "date_end": date_end,
        },
    )


@login_required
def telegram_draft_detail(request, pk):
    draft = get_telegram_draft(pk)
    enrich_telegram_draft_rows([draft])
    raw_payload_json = json.dumps(draft.raw_payload or {}, ensure_ascii=False, indent=2, default=str)
    draft_pendencies = draft_blocking_pendency_lines(draft)
    detail_url = reverse("internal_telegram_draft_detail", args=[draft.pk])
    can_register_counterparty = can_edit_telegram_draft(draft) and not draft.counterparty_id and bool(
        draft.counterparty_candidate_name or draft.counterparty_candidate_document
    )
    can_register_work = can_edit_telegram_draft(draft) and not draft.work_id and bool(draft.work_candidate_name)
    return render(
        request,
        "core/telegram_draft_detail.html",
        {
            "draft": draft,
            "raw_payload_json": raw_payload_json,
            "can_edit": can_edit_telegram_draft(draft),
            "can_register_counterparty": can_register_counterparty,
            "can_register_work": can_register_work,
            "draft_supplier_create_url": url_with_query(
                reverse("internal_supplier_quick_create"),
                next=detail_url,
                draft=draft.pk,
                name=draft.counterparty_candidate_name,
                primary_document=draft.counterparty_candidate_document,
            ),
            "draft_worker_create_url": url_with_query(
                reverse("internal_worker_quick_create"),
                next=detail_url,
                draft=draft.pk,
                name=draft.counterparty_candidate_name,
                primary_document=draft.counterparty_candidate_document,
            ),
            "draft_work_create_url": url_with_query(
                reverse("internal_work_cost_center_quick_create"),
                next=detail_url,
                draft=draft.pk,
                work_name=draft.work_candidate_name,
                cost_center_name="Project",
            ),
            "budget_import_hint": BUDGET_IMPORT_HINT,
            "budget_import_url": reverse("internal_work_budget_import", args=[draft.work_id])
            if draft.work_id
            else "",
            "draft_pendencies": draft_pendencies,
            "payment_preview": build_telegram_draft_payment_preview(draft),
        },
    )


@login_required
def telegram_draft_update(request, pk):
    draft = get_telegram_draft(pk)
    if not can_edit_telegram_draft(draft):
        messages.error(request, "This draft cannot be edited because it has already been finalized or canceled.")
        return redirect("internal_telegram_draft_detail", pk=draft.pk)
    if request.method == "POST":
        form = TelegramDraftForm(request.POST, instance=draft)
        if form.is_valid():
            form.save()
            messages.success(request, f"Draft #{draft.pk} updated.")
            return redirect("internal_telegram_draft_detail", pk=draft.pk)
    else:
        form = TelegramDraftForm(instance=draft)
    return render(
        request,
        "core/telegram_draft_form.html",
        {
            "form": form,
            "draft": draft,
        },
    )


@login_required
@require_POST
def telegram_draft_action(request, pk, action):
    draft = get_object_or_404(TelegramDraft, pk=pk)
    if draft.status != TelegramDraft.Status.ACTIVE:
        messages.error(request, "This draft is not active.")
        return redirect("internal_telegram_draft_detail", pk=draft.pk)

    service = TelegramIntakeService()
    sender = TelegramSender(
        telegram_user_id=draft.telegram_user_id,
        name=draft.sender_name,
        username=draft.sender_username,
    )
    if action == "finalize":
        result = service.finalize_draft(draft.pk, sender, require_authorization=False)
        if result.payment:
            messages.success(request, f"Draft #{draft.pk} finalized into payment #{result.payment.pk}.")
            return redirect("internal_payment_detail", pk=result.payment.pk)
        messages.error(request, result.reply_text)
        return redirect("internal_telegram_draft_detail", pk=draft.pk)
    if action == "cancel":
        result = service.cancel_draft(draft.pk, sender, require_authorization=False)
        messages.success(request, result.reply_text)
        return redirect("internal_telegram_drafts")
    raise Http404("Invalid action.")


@login_required
def pending_payments(request):
    selected_status = request.GET.get("status") or "all"
    if selected_status not in {"pendencias", "realizados", "all"} | PAYMENT_EXACT_STATUS_VALUES:
        selected_status = "all"
    today = timezone.localdate()
    default_date_start, default_date_end = get_month_period(today.month, today.year)
    date_start = parse_date(request.GET.get("date_inicio") or "") or default_date_start
    date_end = parse_date(request.GET.get("date_fim") or "") or default_date_end
    selected_counterparty_id = parse_int(request.GET.get("counterparty"), 0)
    category_filter = request.GET.get("category") or ""
    cost_center_filter = request.GET.get("cost_center") or ""
    work_filter = request.GET.get("work") or ""
    selected_category_missing = category_filter == "sem"
    selected_cost_center_missing = cost_center_filter == "sem"
    selected_work_missing = work_filter == "sem"
    selected_category_id = 0 if selected_category_missing else parse_int(category_filter, 0)
    selected_cost_center_id = 0 if selected_cost_center_missing else parse_int(cost_center_filter, 0)
    selected_work_id = 0 if selected_work_missing else parse_int(work_filter, 0)
    selected_document = request.GET.get("documento") or "all"
    if selected_document not in {"all", "com", "sem"}:
        selected_document = "all"
    selected_date_status = request.GET.get("date_status") or "all"
    if selected_date_status not in {"all", "com_date", "sem_date"}:
        selected_date_status = "all"
    selected_reconciliation = request.GET.get("ofx") or "all"
    if selected_reconciliation not in {"all", "com", "sem"}:
        selected_reconciliation = "all"

    confirmed_reconciliations = Reconciliation.objects.filter(status=Reconciliation.Status.CONFIRMED).select_related(
        "transaction"
    )
    payments = (
        Payment.objects.select_related("counterparty", "category", "cost_center", "work")
        .prefetch_related(
            Prefetch("reconciliations", queryset=confirmed_reconciliations, to_attr="confirmed_reconciliations")
        )
        .exclude(status=Payment.Status.CANCELED)
        .order_by("-created_at")
    )
    if selected_status == "pendencias":
        payments = payments.filter(status__in=PENDING_PAYMENT_STATUSES)
    elif selected_status == "realizados":
        payments = payments.filter(status__in=DASHBOARD_REALIZED_PAYMENT_STATUSES)
    elif selected_status != "all":
        payments = payments.filter(status=selected_status)
    if selected_date_status == "sem_date":
        payments = payments.filter(due_date__isnull=True, competence_date__isnull=True, payment_date__isnull=True)
    elif selected_date_status == "com_date":
        payments = payments.filter(
            Q(due_date__isnull=False) | Q(competence_date__isnull=False) | Q(payment_date__isnull=False)
        )
    if date_start and date_end:
        payments = payments.filter(
            payment_period_q(date_start, date_end)
            | Q(
                due_date__isnull=True,
                competence_date__isnull=True,
                payment_date__isnull=True,
                updated_at__date__gte=date_start,
                updated_at__date__lte=date_end,
            )
        )
    elif date_start:
        payments = payments.filter(
            Q(due_date__gte=date_start)
            | Q(due_date__isnull=True, competence_date__gte=date_start)
            | Q(due_date__isnull=True, competence_date__isnull=True, payment_date__gte=date_start)
        )
    elif date_end:
        payments = payments.filter(
            Q(due_date__lte=date_end)
            | Q(due_date__isnull=True, competence_date__lte=date_end)
            | Q(due_date__isnull=True, competence_date__isnull=True, payment_date__lte=date_end)
        )
    if selected_counterparty_id:
        payments = payments.filter(counterparty_id=selected_counterparty_id)
    if selected_category_missing:
        payments = payments.filter(category__isnull=True)
    elif selected_category_id:
        payments = payments.filter(category_id=selected_category_id)
    if selected_cost_center_missing:
        payments = payments.filter(cost_center__isnull=True)
    elif selected_cost_center_id:
        payments = payments.filter(cost_center_id=selected_cost_center_id)
    if selected_work_missing:
        payments = payments.filter(work__isnull=True)
    elif selected_work_id:
        payments = payments.filter(work_id=selected_work_id)
    if selected_document == "com":
        payments = payments.exclude(counterparty__isnull=True).exclude(counterparty__primary_document="")
    elif selected_document == "sem":
        payments = payments.filter(Q(counterparty__isnull=True) | Q(counterparty__primary_document=""))
    if selected_reconciliation == "com":
        payments = payments.filter(reconciliations__status=Reconciliation.Status.CONFIRMED)
    elif selected_reconciliation == "sem":
        payments = payments.exclude(reconciliations__status=Reconciliation.Status.CONFIRMED)
    payments = list(payments.distinct())
    enrich_payment_rows(payments)
    bulk_approvable_count = sum(1 for payment in payments if payment.can_bulk_approve)
    context = {
        "payments": payments,
        "bulk_approvable_count": bulk_approvable_count,
        "status_filters": PAYMENT_STATUS_FILTERS,
        "document_filters": DOCUMENT_FILTERS,
        "date_status_filters": DATE_STATUS_FILTERS,
        "reconciliation_filters": RECONCILIATION_FILTERS,
        "counterparties": Counterparty.objects.filter(
            is_active=True,
            payments__status__in=PAYMENT_EXACT_STATUS_VALUES,
        )
        .order_by("name")
        .distinct(),
        "categories": Category.objects.filter(is_active=True).order_by("name"),
        "cost_centers": CostCenter.objects.filter(is_active=True).order_by("name"),
        "works": Work.objects.filter(is_active=True).order_by("name"),
        "selected_status": selected_status,
        "selected_document": selected_document,
        "selected_date_status": selected_date_status,
        "selected_reconciliation": selected_reconciliation,
        "selected_counterparty_id": selected_counterparty_id,
        "selected_category_id": selected_category_id,
        "selected_category_missing": selected_category_missing,
        "selected_cost_center_id": selected_cost_center_id,
        "selected_cost_center_missing": selected_cost_center_missing,
        "selected_work_id": selected_work_id,
        "selected_work_missing": selected_work_missing,
        "date_start": date_start,
        "date_end": date_end,
        "current_url": request.get_full_path(),
    }
    return render(request, "core/pending_payments.html", context)


@login_required
def payment_create(request):
    fallback_url = reverse("internal_pending_payments")
    next_url = safe_return_url(request.POST.get("next") or request.GET.get("next"), request, fallback_url)
    if request.method == "POST":
        form = PaymentManualForm(request.POST)
        if form.is_valid():
            payment = form.save(commit=False)
            payment.created_by = request.user
            payment.save()
            form.save_m2m()
            messages.success(request, f"Payment #{payment.pk} created. Review and approve it to include it in spreadsheets.")
            return redirect(next_url if next_url != fallback_url else reverse("internal_payment_detail", args=[payment.pk]))
    else:
        form = PaymentManualForm(initial=payment_form_initial_from_query(request))
    return render(
        request,
        "core/payment_form.html",
        {
            "form": form,
            "title": "New manual payment",
            "submit_label": "Create payment",
            "payment": None,
            "next_url": next_url,
            **quick_create_urls(request),
        },
    )


@login_required
def payment_update(request, pk):
    payment = get_object_or_404(Payment, pk=pk)
    detail_url = reverse("internal_payment_detail", args=[payment.pk])
    next_url = safe_return_url(request.POST.get("next") or request.GET.get("next"), request, detail_url)
    if not can_edit_payment(payment):
        messages.error(
            request,
            "This payment cannot be edited on the web. Put it under correction or check whether it has already been exported/reconciled.",
        )
        return redirect(detail_url)
    if request.method == "POST":
        form = PaymentManualForm(request.POST, instance=payment)
        if form.is_valid():
            payment = form.save(commit=False)
            payment.save()
            form.save_m2m()
            messages.success(request, f"Payment #{payment.pk} updated and sent for approval.")
            return redirect(next_url)
    else:
        form = PaymentManualForm(instance=payment)
    return render(
        request,
        "core/payment_form.html",
        {
            "form": form,
            "title": f"Edit payment {payment.pk}",
            "submit_label": "Save changes",
            "payment": payment,
            "next_url": next_url,
            **quick_create_urls(request),
        },
    )


@login_required
def counterparty_create(request):
    next_url = safe_next_url(request.POST.get("next") or request.GET.get("next"), request)
    if request.method == "POST":
        form = CounterpartyManualForm(request.POST)
        if form.is_valid():
            counterparty = form.save()
            messages.success(request, f"Record #{counterparty.pk} created: {counterparty.name}.")
            return redirect(url_with_query(next_url, counterparty=counterparty.pk))
    else:
        form = CounterpartyManualForm()
    return render(
        request,
        "core/counterparty_form.html",
        {
            "form": form,
            "next_url": next_url,
        },
    )


@login_required
def supplier_quick_create(request):
    return counterparty_quick_create(
        request,
        kind=Counterparty.Kind.SUPPLIER,
        title="New vendor",
        success_label="Vendor",
    )


@login_required
def worker_quick_create(request):
    return counterparty_quick_create(
        request,
        kind=Counterparty.Kind.WORKER,
        title="New worker",
        success_label="Worker",
    )


def counterparty_quick_create(request, kind: str, title: str, success_label: str):
    next_url = safe_next_url(request.POST.get("next") or request.GET.get("next"), request)
    draft = optional_telegram_draft_from_request(request)
    payment_id = parse_int(request.POST.get("payment") or request.GET.get("payment"), 0)
    ofx_payment = None
    if not draft and payment_id:
        ofx_payment = Payment.objects.filter(pk=payment_id, source=Origin.OFX).first()
        if not ofx_payment:
            messages.error(request, "OFX payment not found for linking this record.")
            return redirect(next_url)
        if not can_link_counterparty_to_ofx_payment(ofx_payment):
            messages.error(request, "This OFX payment cannot receive quick registration in its current state.")
            return redirect(next_url)
    if draft and not can_edit_telegram_draft(draft):
        messages.error(request, "This draft cannot receive registration because it is already finalized or canceled.")
        return redirect("internal_telegram_draft_detail", pk=draft.pk)

    if request.method == "POST":
        form = CounterpartyQuickForm(request.POST, kind=kind, reuse_existing=bool(draft or ofx_payment))
        if form.is_valid():
            counterparty = form.save()
            if draft:
                link_counterparty_to_draft(draft, counterparty)
                messages.success(
                    request,
                    f"{success_label} {counterparty.name} linked to draft #{draft.pk}.",
                )
                return redirect("internal_telegram_draft_detail", pk=draft.pk)
            if ofx_payment:
                linked_payment = link_counterparty_to_ofx_payment(ofx_payment, counterparty)
                messages.success(
                    request,
                    f"{success_label} {counterparty.name} linked to payment #{linked_payment.pk}.",
                )
                return redirect(next_url)
            messages.success(request, f"{success_label} #{counterparty.pk} registered: {counterparty.name}.")
            return redirect(url_with_query(next_url, counterparty=counterparty.pk))
    else:
        form = CounterpartyQuickForm(
            kind=kind,
            reuse_existing=bool(draft or ofx_payment),
            initial=counterparty_quick_initial_from_request(request, draft),
        )
    return render(
        request,
        "core/quick_counterparty_form.html",
        {
            "form": form,
            "title": title,
            "next_url": next_url,
            "draft_id": draft.pk if draft else "",
            "payment_id": ofx_payment.pk if ofx_payment else "",
        },
    )


@login_required
def work_cost_center_quick_create(request):
    next_url = safe_next_url(request.POST.get("next") or request.GET.get("next"), request)
    draft = optional_telegram_draft_from_request(request)
    if draft and not can_edit_telegram_draft(draft):
        messages.error(request, "This draft cannot receive a project because it is already finalized or canceled.")
        return redirect("internal_telegram_draft_detail", pk=draft.pk)

    if request.method == "POST":
        data = request.POST.copy()
        if draft:
            data["cost_center_name"] = "Project"
        form = WorkCostCenterQuickForm(data, reuse_existing=bool(draft))
        if form.is_valid():
            cost_center, work = form.save()
            if draft:
                link_work_to_draft(draft, cost_center, work)
                messages.success(request, f"Project {work.name} linked to draft #{draft.pk}.")
                return redirect("internal_telegram_draft_detail", pk=draft.pk)
            messages.success(request, f"Project #{work.pk} registered: {work.name}.")
            return redirect(url_with_query(next_url, cost_center=cost_center.pk, work=work.pk))
    else:
        form = WorkCostCenterQuickForm(
            reuse_existing=bool(draft),
            initial=work_quick_initial_from_request(request, draft),
        )
    return render(
        request,
        "core/quick_work_cost_center_form.html",
        {
            "form": form,
            "next_url": next_url,
            "draft_id": draft.pk if draft else "",
        },
    )


@login_required
def work_budget_import(request, pk):
    work = get_object_or_404(Work, pk=pk, is_active=True)
    batch = None
    if request.method == "POST":
        form = BudgetImportForm(request.POST, request.FILES)
        if form.is_valid():
            uploaded = form.cleaned_data["budget_file"]
            uploaded_file = save_budget_uploaded_file(uploaded, request.user)
            try:
                with transaction.atomic():
                    report = import_budget_workbook_for_work(uploaded_file.file.path, work)
                    uploaded_file.status = UploadedFile.Status.PROCESSED
                    uploaded_file.save(update_fields=["status", "updated_at"])
                    batch = BudgetImportBatch.objects.create(
                        work=work,
                        uploaded_file=uploaded_file,
                        uploaded_by=request.user,
                        status=BudgetImportBatch.Status.PROCESSED,
                        rows_read=report.rows_read,
                        rows_skipped=report.rows_skipped,
                        items_created=report.created,
                        items_updated=report.updated,
                        items_unchanged=report.unchanged,
                        conflicts=report.as_dict()["conflicts"],
                    )
            except Exception as exc:
                uploaded_file.status = UploadedFile.Status.ERROR
                uploaded_file.error_message = "Budget import failed."
                uploaded_file.save(update_fields=["status", "error_message", "updated_at"])
                batch = BudgetImportBatch.objects.create(
                    work=work,
                    uploaded_file=uploaded_file,
                    uploaded_by=request.user,
                    status=BudgetImportBatch.Status.ERROR,
                    error_message=exc.__class__.__name__,
                )
                messages.error(request, f"Could not import the budget: {exc.__class__.__name__}.")
            else:
                messages.success(
                    request,
                    "Budget imported. "
                    f"Items created: {batch.items_created}; "
                    f"updated: {batch.items_updated}; "
                    f"ignored: {batch.rows_skipped}; "
                    f"conflicts: {len(batch.conflicts)}.",
                )
    else:
        form = BudgetImportForm()
    recent_batches = BudgetImportBatch.objects.filter(work=work).select_related("uploaded_file", "uploaded_by")[:5]
    return render(
        request,
        "core/work_budget_import.html",
        {
            "work": work,
            "form": form,
            "batch": batch,
            "recent_batches": recent_batches,
        },
    )


def save_budget_uploaded_file(uploaded, user):
    original_filename = get_valid_filename(uploaded.name or "budget.xlsx")
    content = b"".join(uploaded.chunks())
    digest = hashlib.sha256(content).hexdigest()
    uploaded_file = (
        UploadedFile.objects.filter(sha256=digest, kind=UploadedFile.Kind.SPREADSHEET).order_by("created_at").first()
    )
    if uploaded_file is None:
        uploaded_file = UploadedFile(
            original_filename=original_filename,
            content_type=uploaded.content_type or "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            size_bytes=len(content),
            sha256=digest,
            source=UploadedFile.Source.IMPORT,
            kind=UploadedFile.Kind.SPREADSHEET,
            status=UploadedFile.Status.RECEIVED,
            uploaded_by=user,
        )
        uploaded_file.file.save(original_filename, ContentFile(content), save=False)
        uploaded_file.save()
    elif not uploaded_file.file:
        uploaded_file.file.save(original_filename, ContentFile(content), save=False)
        uploaded_file.status = UploadedFile.Status.RECEIVED
        uploaded_file.save(update_fields=["file", "status", "updated_at"])
    return uploaded_file


@login_required
def payment_delete(request, pk):
    payment = get_object_or_404(Payment.objects.select_related("counterparty"), pk=pk)
    detail_url = reverse("internal_payment_detail", args=[payment.pk])
    next_url = safe_return_url(
        request.POST.get("next") or request.GET.get("next"),
        request,
        reverse("internal_pending_payments"),
    )
    if not can_delete_payment(payment):
        messages.error(
            request,
            "This payment cannot be deleted because it has already been approved, exported, or reconciled. Use cancel when you need to preserve history.",
        )
        return redirect(detail_url)
    if request.method == "POST":
        payment_id = payment.pk
        payment.delete()
        messages.success(request, f"Payment #{payment_id} deleted.")
        return redirect(next_url)
    return render(request, "core/payment_confirm_delete.html", {"payment": payment, "next_url": next_url})


@login_required
@require_POST
def payment_bulk_action(request):
    next_url = safe_return_url(request.POST.get("next"), request, reverse("internal_pending_payments"))
    action = request.POST.get("action")
    if action != "approve":
        messages.error(request, "Invalid bulk action.")
        return redirect(next_url)

    payment_ids = unique_ints(request.POST.getlist("payment_ids"))
    if not payment_ids:
        messages.warning(request, "Select at least one payment to approve.")
        return redirect(next_url)

    selected_payments = Payment.objects.select_related("counterparty", "category", "cost_center", "work").filter(
        pk__in=payment_ids
    )
    selected_count = selected_payments.count()
    missing_count = len(payment_ids) - selected_count
    approvable_payments = []
    blocked_reasons = []
    for payment in selected_payments.order_by("id"):
        blockers = bulk_payment_approval_blockers(payment)
        if blockers:
            blocked_reasons.append(f"#{payment.pk}: {', '.join(blockers)}")
        else:
            approvable_payments.append(payment)

    approved_count = 0
    errors = []
    for payment in approvable_payments:
        try:
            approve_payment(payment.pk, user=request.user, message="Internal panel - bulk approval")
        except PaymentConfirmationError as exc:
            errors.append(f"#{payment.pk}: {exc}")
        else:
            approved_count += 1

    if approved_count:
        messages.success(request, f"{approved_count} payment(s) approved in bulk.")
    if blocked_reasons:
        visible_blockers = "; ".join(blocked_reasons[:4])
        if len(blocked_reasons) > 4:
            visible_blockers += f"; and {len(blocked_reasons) - 4} more."
        messages.warning(request, f"{len(blocked_reasons)} payment(s) ignored: {visible_blockers}")
    if missing_count:
        messages.warning(request, f"{missing_count} payment(s) not found.")
    if errors:
        visible_errors = "; ".join(errors[:3])
        if len(errors) > 3:
            visible_errors += f"; and {len(errors) - 3} more."
        messages.error(request, f"Some payments could not be approved: {visible_errors}")
    if not approved_count and not blocked_reasons and not errors:
        messages.warning(request, "No payment was approved.")
    return redirect(next_url)


@login_required
def monthly_closing(request):
    today = timezone.localdate()
    month = parse_int(request.GET.get("mes"), today.month)
    year = parse_int(request.GET.get("ano"), today.year)
    if request.method == "POST":
        month = parse_int(request.POST.get("mes"), month)
        year = parse_int(request.POST.get("ano"), year)
    if month < 1 or month > 12:
        month = today.month
    summary = build_monthly_closing(month=month, year=year)
    blockers = monthly_closing_blockers(summary)
    if request.method == "POST":
        if blockers:
            messages.error(
                request,
                "Close blocked. Resolve pending items before generating the spreadsheets: "
                f"{'; '.join(blockers)}.",
            )
            return redirect(f"{reverse('internal_monthly_closing')}?mes={month}&ano={year}")
        try:
            batch = export_approved_payments_for_period(
                summary.period_start,
                summary.period_end,
                user=request.user,
            )
        except ExportValidationError as exc:
            messages.error(request, f"Could not generate close spreadsheets: {exc}")
        else:
            messages.success(
                request,
                f"Close spreadsheets for {month:02d}/{year} generated in batch #{batch.pk}: "
                f"{batch.records_count} payment(s).",
            )
        return redirect(f"{reverse('internal_monthly_closing')}?mes={month}&ano={year}")
    period_batches = (
        ExportBatch.objects.filter(
            status=ExportBatch.Status.GENERATED,
        )
        .filter(
            Q(period_start=summary.period_start, period_end=summary.period_end)
            | Q(payments__payment_date__gte=summary.period_start, payments__payment_date__lte=summary.period_end)
        )
        .distinct()
        .order_by("-generated_at", "-created_at")[:5]
    )
    period_batches = list(period_batches)
    checklist = monthly_closing_checklist(summary, period_batches)
    context = {
        "summary": summary,
        "blockers": blockers,
        "checklist": checklist,
        "period_batches": period_batches,
        "blocking_issue_count": monthly_closing_blocking_issue_count(summary),
        "can_generate_spreadsheets": not blockers,
        "budget_import_hint": BUDGET_IMPORT_HINT,
        "selected_month": month,
        "selected_year": year,
        "month_choices": MONTH_CHOICES,
        "year_choices": range(today.year - 2, today.year + 2),
    }
    return render(request, "core/monthly_closing.html", context)


@login_required
def payment_detail(request, pk):
    return_url = safe_return_url(request.GET.get("next"), request, reverse("internal_pending_payments"))
    confirmed_reconciliations = Reconciliation.objects.filter(status=Reconciliation.Status.CONFIRMED).select_related(
        "transaction"
    )
    visible_reconciliations = (
        Reconciliation.objects.exclude(status=Reconciliation.Status.REJECTED)
        .select_related("transaction", "transaction__counterparty")
        .order_by("-created_at")
    )
    payment = get_object_or_404(
        Payment.objects.select_related(
            "counterparty",
            "category",
            "cost_center",
            "work",
            "uploaded_file",
            "created_by",
            "confirmed_by",
        ).prefetch_related(
            "counterparty__documents",
            "confirmations__user",
            Prefetch("reconciliations", queryset=confirmed_reconciliations, to_attr="confirmed_reconciliations"),
            Prefetch("reconciliations", queryset=visible_reconciliations, to_attr="visible_reconciliations"),
        ),
        pk=pk,
    )
    enrich_payment_rows([payment])
    context = {
        "payment": payment,
        "raw_payload_json": json.dumps(payment.raw_payload or {}, ensure_ascii=False, indent=2, default=str),
        "confirmations": payment.confirmations.all().order_by("-created_at"),
        "reconciliations": getattr(payment, "visible_reconciliations", []),
        "can_web_edit": can_edit_payment(payment),
        "can_web_delete": can_delete_payment(payment),
        "budget_import_hint": BUDGET_IMPORT_HINT,
        "budget_import_url": reverse("internal_work_budget_import", args=[payment.work_id])
        if payment.work_id
        else "",
        "return_url": return_url,
    }
    return render(request, "core/payment_detail.html", context)


@login_required
@require_POST
def payment_action(request, pk, action):
    next_url = safe_return_url(request.POST.get("next"), request, reverse("internal_payment_detail", args=[pk]))
    try:
        if action == "approve":
            result = approve_payment(pk, user=request.user, message="Painel interno")
        elif action == "correct":
            result = request_payment_correction(pk, user=request.user, message="Painel interno")
        elif action == "cancel":
            result = cancel_payment(pk, user=request.user, message="Painel interno")
        else:
            raise Http404("Invalid action.")
    except PaymentConfirmationError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, result.message)
    return redirect(next_url)


@login_required
def unreconciled_ofx_transactions(request):
    if request.method == "POST":
        return upload_ofx_file(request)

    today = timezone.localdate()
    month = parse_int(request.GET.get("mes"), today.month)
    year = parse_int(request.GET.get("ano"), today.year)
    if month < 1 or month > 12:
        month = today.month
    if year < 1900 or year > 2100:
        year = today.year
    period_start, period_end = get_month_period(month, year)
    selected_status = request.GET.get("status") or "pendencias"
    if selected_status not in {"pendencias", "all"} | OFX_EXACT_STATUS_VALUES:
        selected_status = "pendencias"
    selected_payment_filter = request.GET.get("payment") or "all"
    if selected_payment_filter not in OFX_PAYMENT_FILTER_VALUES:
        selected_payment_filter = "all"
    selected_counterparty_id = parse_int(request.GET.get("counterparty"), 0)
    selected_cost_center_id = parse_int(request.GET.get("cost_center"), 0)
    selected_work_id = parse_int(request.GET.get("work"), 0)

    reconciliations = (
        Reconciliation.objects.exclude(status=Reconciliation.Status.REJECTED)
        .select_related("payment", "payment__counterparty", "payment__category", "payment__cost_center", "payment__work")
        .order_by("-created_at")
    )
    transactions = (
        OfxTransaction.objects.select_related("counterparty", "ofx_file")
        .prefetch_related(Prefetch("reconciliations", queryset=reconciliations, to_attr="visible_reconciliations"))
        .filter(posted_at__gte=period_start, posted_at__lte=period_end)
        .order_by("-posted_at", "-created_at")
    )
    if selected_status == "pendencias":
        transactions = transactions.filter(status__in=UNRECONCILED_OFX_STATUSES)
    elif selected_status != "all":
        transactions = transactions.filter(status=selected_status)
    transactions = list(transactions)
    enrich_ofx_review_rows(transactions, request)
    transactions = filter_ofx_review_rows(
        transactions,
        payment_filter=selected_payment_filter,
        counterparty_id=selected_counterparty_id,
        cost_center_id=selected_cost_center_id,
        work_id=selected_work_id,
    )

    context = {
        "transactions": transactions,
        "period_start": period_start,
        "period_end": period_end,
        "selected_month": month,
        "selected_year": year,
        "selected_status": selected_status,
        "selected_payment_filter": selected_payment_filter,
        "selected_counterparty_id": selected_counterparty_id,
        "selected_cost_center_id": selected_cost_center_id,
        "selected_work_id": selected_work_id,
        "month_choices": MONTH_CHOICES,
        "year_choices": range(today.year - 2, today.year + 2),
        "status_filters": OFX_STATUS_FILTERS,
        "payment_filters": OFX_PAYMENT_FILTERS,
        "counterparties": Counterparty.objects.filter(is_active=True).order_by("name"),
        "cost_centers": CostCenter.objects.filter(is_active=True).order_by("name"),
        "works": Work.objects.filter(is_active=True).order_by("name"),
        "monthly_closing_url": url_with_query(reverse("internal_monthly_closing"), mes=month, ano=year),
        "current_url": request.get_full_path(),
        "clear_ofx_period_url": reverse("internal_ofx_clear_period"),
        "ofx_bulk_edit_form": OfxPaymentBulkEditForm(),
        "ofx_bulk_edit_url": reverse("internal_ofx_payment_bulk_edit"),
        "ofx_bulk_editable_count": sum(
            1
            for transaction_record in transactions
            for item in getattr(transaction_record, "related_payments", [])
            if item.get("can_bulk_edit")
        ),
        "ofx_bulk_approvable_count": sum(
            1
            for transaction_record in transactions
            for item in getattr(transaction_record, "related_payments", [])
            if item.get("can_bulk_approve")
        ),
        "ofx_selectable_payment_count": sum(
            1
            for transaction_record in transactions
            for item in getattr(transaction_record, "related_payments", [])
            if item.get("can_bulk_edit") or item.get("can_bulk_approve")
        ),
        "payment_bulk_action_url": reverse("internal_payment_bulk_action"),
    }
    return render(request, "core/unreconciled_ofx.html", context)


@login_required
@require_POST
def clear_ofx_period(request):
    today = timezone.localdate()
    month = parse_int(request.POST.get("mes"), today.month)
    year = parse_int(request.POST.get("ano"), today.year)
    if month < 1 or month > 12:
        month = today.month
    if year < 1900 or year > 2100:
        year = today.year
    period_start, period_end = get_month_period(month, year)
    next_url = safe_return_url(
        request.POST.get("next"),
        request,
        url_with_query(reverse("internal_unreconciled_ofx"), mes=month, ano=year, status="all"),
    )

    report = clear_ofx_date_for_period(period_start, period_end)
    if report["protected_payments"]:
        messages.error(
            request,
            "I did not clear the entire OFX import because some OFX payment(s) were already exported. "
            f"I removed the safe records and preserved {report['protected_payments']} payment(s).",
        )
    else:
        messages.success(
            request,
            "OFX do período zerado: "
            f"{report['ofx_files']} file(s), {report['ofx_transactions']} transaction(s), "
            f"{report['ofx_payments']} sugestão(ões) de payment e "
            f"{report['reconciliations']} reconciliation(s) removidas.",
        )
    if report["restored_payments"]:
        messages.info(
            request,
            f"{report['restored_payments']} reconciled payment(s) returned to approved.",
        )
    return redirect(next_url)


@login_required
@require_POST
def ofx_payment_bulk_edit(request):
    next_url = safe_return_url(request.POST.get("next"), request, reverse("internal_unreconciled_ofx"))
    payment_ids = unique_ints(request.POST.getlist("payment_ids"))
    if not payment_ids:
        messages.warning(request, "Select at least one OFX-suggested payment.")
        return redirect(next_url)

    form = OfxPaymentBulkEditForm(request.POST)
    if not form.is_valid():
        messages.error(request, f"Review the bulk edit: {first_form_error(form)}")
        return redirect(next_url)
    if not form.has_bulk_changes():
        messages.warning(request, "Enter at least one field to apply in bulk.")
        return redirect(next_url)

    selected_payments = Payment.objects.filter(pk__in=payment_ids, source=Origin.OFX)
    selected_count = selected_payments.count()
    ignored_not_found_or_not_ofx = len(payment_ids) - selected_count
    updated_count = 0
    blocked_count = 0
    skipped_status_count = 0

    with transaction.atomic():
        for payment in selected_payments.select_for_update().order_by("id"):
            if not can_bulk_edit_ofx_payment(payment):
                blocked_count += 1
                continue
            changed_fields, skipped_status = apply_ofx_bulk_edit_to_payment(payment, form.cleaned_data, request.user)
            if skipped_status:
                skipped_status_count += 1
            if not changed_fields:
                continue
            payment.save(update_fields=sorted(changed_fields | {"updated_at"}))
            updated_count += 1

    if updated_count:
        messages.success(request, f"{updated_count} OFX-suggested payment(s) updated in bulk.")
    if blocked_count:
        messages.warning(
            request,
            f"{blocked_count} payment(s) ignored because they are already approved, reconciled, canceled, or exported.",
        )
    if skipped_status_count:
        messages.warning(
            request,
            f"{skipped_status_count} payment(s) without counterparty could not move to pending.",
        )
    if ignored_not_found_or_not_ofx:
        messages.warning(request, f"{ignored_not_found_or_not_ofx} selection(s) ignored because they are not OFX suggestions.")
    if not updated_count and not blocked_count and not skipped_status_count and not ignored_not_found_or_not_ofx:
        messages.warning(request, "No payment was changed.")
    return redirect(next_url)


def clear_ofx_date_for_period(period_start, period_end) -> dict[str, int]:
    transactions_qs = OfxTransaction.objects.filter(posted_at__gte=period_start, posted_at__lte=period_end)
    transaction_ids = list(transactions_qs.values_list("pk", flat=True))
    if not transaction_ids:
        return {
            "ofx_files": 0,
            "ofx_transactions": 0,
            "ofx_payments": 0,
            "protected_payments": 0,
            "reconciliations": 0,
            "restored_payments": 0,
        }

    ofx_file_ids = list(transactions_qs.values_list("ofx_file_id", flat=True).distinct())
    reconciliation_count = Reconciliation.objects.filter(transaction_id__in=transaction_ids).count()
    transaction_count = len(transaction_ids)
    file_count = len(ofx_file_ids)
    source_ofx_payments = (
        Payment.objects.filter(source=Origin.OFX)
        .filter(Q(raw_payload__ofx_transaction_id__in=transaction_ids) | Q(reconciliations__transaction_id__in=transaction_ids))
        .distinct()
    )
    source_ofx_payment_count = source_ofx_payments.count()
    protected_payment_ids = list(
        source_ofx_payments.filter(export_batches__isnull=False).values_list("pk", flat=True).distinct()
    )
    if protected_payment_ids:
        return {
            "ofx_files": file_count,
            "ofx_transactions": transaction_count,
            "ofx_payments": source_ofx_payment_count,
            "protected_payments": len(protected_payment_ids),
            "reconciliations": reconciliation_count,
            "restored_payments": 0,
        }
    deletable_ofx_payments = source_ofx_payments.exclude(pk__in=protected_payment_ids)
    manual_reconciled_payment_ids = list(
        Payment.objects.filter(
            reconciliations__transaction_id__in=transaction_ids,
            reconciliations__status=Reconciliation.Status.CONFIRMED,
        )
        .exclude(source=Origin.OFX)
        .values_list("pk", flat=True)
        .distinct()
    )
    uploaded_file_ids = list(
        OfxFile.objects.filter(pk__in=ofx_file_ids, uploaded_file__isnull=False).values_list("uploaded_file_id", flat=True)
    )
    with transaction.atomic():
        deletable_payment_count = deletable_ofx_payments.count()
        deletable_ofx_payments.delete()
        OfxFile.objects.filter(pk__in=ofx_file_ids).delete()
        UploadedFile.objects.filter(pk__in=uploaded_file_ids, kind=UploadedFile.Kind.OFX).update(
            status=UploadedFile.Status.IGNORED,
            notes="OFX zerado para reimport.",
            updated_at=timezone.now(),
        )
        restored_payments = (
            Payment.objects.filter(pk__in=manual_reconciled_payment_ids, status=Payment.Status.RECONCILED)
            .exclude(reconciliations__status=Reconciliation.Status.CONFIRMED)
            .update(
                status=Payment.Status.APPROVED,
                review_reason="Reconciliation OFX removida para reimport do extrato.",
                updated_at=timezone.now(),
            )
        )

    return {
        "ofx_files": file_count,
        "ofx_transactions": transaction_count,
        "ofx_payments": deletable_payment_count,
        "protected_payments": len(protected_payment_ids),
        "reconciliations": reconciliation_count,
        "restored_payments": restored_payments,
    }


@login_required
@require_POST
def ofx_transaction_action(request, pk, action):
    next_url = safe_return_url(request.POST.get("next"), request, reverse("internal_unreconciled_ofx"))
    transaction_record = get_object_or_404(OfxTransaction, pk=pk)
    if action == "ignore":
        if transaction_record.reconciliations.filter(status=Reconciliation.Status.CONFIRMED).exists():
            messages.error(request, "Already reconciled transactions cannot be ignored.")
        else:
            transaction_record.status = OfxTransaction.Status.IGNORED
            transaction_record.save(update_fields=["status", "updated_at"])
            messages.success(request, f"Transaction OFX #{transaction_record.pk} marcada como ignored.")
        return redirect(next_url)

    if action == "mark_duplicate":
        transaction_record.status = OfxTransaction.Status.POSSIBLE_DUPLICATE
        transaction_record.save(update_fields=["status", "updated_at"])
        messages.success(request, f"OFX transaction #{transaction_record.pk} marked as possible duplicate.")
        return redirect(next_url)

    if action == "confirm_reconciliation":
        reconciliation_id = parse_int(request.POST.get("reconciliation_id"), 0)
        reconciliations = transaction_record.reconciliations.select_related("payment").filter(
            status=Reconciliation.Status.SUGGESTED,
        )
        if reconciliation_id:
            reconciliations = reconciliations.filter(pk=reconciliation_id)
        reconciliation = reconciliations.order_by("-confidence", "pk").first()
        if not reconciliation:
            messages.error(request, "No suggested reconciliation was found to confirm.")
            return redirect(next_url)
        try:
            with transaction.atomic():
                reconciliation.status = Reconciliation.Status.CONFIRMED
                reconciliation.created_by = reconciliation.created_by or request.user
                reconciliation.save(update_fields=["status", "created_by", "updated_at"])
                mark_reconciled(reconciliation.payment, transaction_record)
        except IntegrityError:
            messages.error(request, "Could not confirm: this pair already has a confirmed reconciliation.")
        else:
            messages.success(request, f"Reconciliation da transaction OFX #{transaction_record.pk} confirmada.")
        return redirect(next_url)

    raise Http404("Invalid OFX action.")


def upload_ofx_file(request):
    uploaded = request.FILES.get("ofx_file")
    if not uploaded:
        messages.error(request, "Select an OFX file to import.")
        return redirect("internal_unreconciled_ofx")
    original_filename = get_valid_filename(uploaded.name or "extrato.ofx")
    if not original_filename.lower().endswith(".ofx"):
        messages.error(request, "Upload a valid .ofx file.")
        return redirect("internal_unreconciled_ofx")

    content = b"".join(uploaded.chunks())
    digest = hashlib.sha256(content).hexdigest()
    uploaded_file = UploadedFile.objects.filter(sha256=digest, kind=UploadedFile.Kind.OFX).order_by("created_at").first()
    if uploaded_file is None:
        uploaded_file = UploadedFile(
            original_filename=original_filename,
            content_type=uploaded.content_type or "application/x-ofx",
            size_bytes=uploaded.size,
            sha256=digest,
            source=UploadedFile.Source.MANUAL,
            kind=UploadedFile.Kind.OFX,
            status=UploadedFile.Status.RECEIVED,
            uploaded_by=request.user,
        )
        uploaded_file.file.save(original_filename, ContentFile(content), save=False)
        uploaded_file.save()
    elif not uploaded_file.file:
        uploaded_file.file.save(original_filename, ContentFile(content), save=False)
        uploaded_file.status = UploadedFile.Status.RECEIVED
        uploaded_file.save(update_fields=["file", "status", "updated_at"])

    try:
        report = import_uploaded_ofx_file(uploaded_file)
        reconciliation_report = reconcile_ofx_transactions(report.ofx_file.transactions.all(), user=request.user)
        suggestion_report = suggest_payments_from_ofx(report.ofx_file, user=request.user)
        summary = build_ofx_import_summary(report, reconciliation_report, suggestion_report)
    except Exception as exc:
        uploaded_file.status = UploadedFile.Status.ERROR
        uploaded_file.error_message = "OFX import failed."
        uploaded_file.save(update_fields=["status", "error_message", "updated_at"])
        messages.error(request, f"Could not import OFX: {exc.__class__.__name__}.")
        return redirect("internal_unreconciled_ofx")

    messages.success(
        request,
        f"OFX imported and reconciled. {summary.as_sentence()}",
    )
    display_date = report.ofx_file.start_date or report.ofx_file.transactions.order_by("posted_at").values_list(
        "posted_at", flat=True
    ).first()
    if display_date:
        return redirect(f"{reverse('internal_unreconciled_ofx')}?mes={display_date.month}&ano={display_date.year}&status=all")
    return redirect("internal_unreconciled_ofx")


def enrich_ofx_review_rows(transactions, request) -> None:
    transaction_ids = [transaction_record.pk for transaction_record in transactions]
    if not transaction_ids:
        return
    suggested_payments = (
        Payment.objects.select_related("counterparty", "category", "cost_center", "work")
        .prefetch_related("export_batches", "reconciliations")
        .filter(source=Origin.OFX, raw_payload__ofx_transaction_id__in=transaction_ids)
        .order_by("pk")
    )
    suggested_by_transaction: dict[int, list[Payment]] = {}
    for payment in suggested_payments:
        transaction_id = parse_int((payment.raw_payload or {}).get("ofx_transaction_id"), 0)
        if not transaction_id:
            continue
        payment.amount_display = format_money_br(payment.amount)
        suggested_by_transaction.setdefault(transaction_id, []).append(payment)

    current_url = request.get_full_path()
    for transaction_record in transactions:
        related = []
        seen_payment_ids = set()
        for reconciliation in getattr(transaction_record, "visible_reconciliations", []):
            payment = reconciliation.payment
            payment.amount_display = format_money_br(payment.amount)
            related.append(
                {
                    "payment": payment,
                    "reconciliation": reconciliation,
                    "kind": reconciliation.get_status_display(),
                    "can_confirm": reconciliation.status == Reconciliation.Status.SUGGESTED,
                    "can_bulk_edit": can_bulk_edit_ofx_payment(payment),
                    "can_bulk_approve": can_bulk_approve_payment(payment),
                }
            )
            seen_payment_ids.add(payment.pk)
        for payment in suggested_by_transaction.get(transaction_record.pk, []):
            if payment.pk in seen_payment_ids:
                continue
            related.append(
                {
                    "payment": payment,
                    "reconciliation": None,
                    "kind": "OFX suggestion",
                    "can_confirm": False,
                    "can_bulk_edit": can_bulk_edit_ofx_payment(payment),
                    "can_bulk_approve": can_bulk_approve_payment(payment),
                }
            )
            seen_payment_ids.add(payment.pk)

        transaction_record.related_payments = related
        transaction_record.has_suggested_payment = bool(suggested_by_transaction.get(transaction_record.pk))
        transaction_record.needs_counterparty_registration = any(
            item["payment"].status == Payment.Status.PENDING_REGISTRATION for item in related
        )
        transaction_record.amount_display = format_money_br(transaction_record.amount)
        transaction_record.memo_summary = summarize_text(transaction_record.memo, 120)
        transaction_record.name_summary = summarize_text(
            transaction_record.name_extracted
            or (transaction_record.counterparty.name if transaction_record.counterparty_id else ""),
            64,
        )
        transaction_record.counterparty_display = (
            transaction_record.counterparty.name
            if transaction_record.counterparty_id
            else transaction_record.name_extracted or "-"
        )
        transaction_record.can_ignore = not any(
            item["reconciliation"] and item["reconciliation"].status == Reconciliation.Status.CONFIRMED
            for item in related
        )
        transaction_record.ofx_status_label = compact_ofx_status_label(transaction_record.status)
        transaction_record.ofx_status_badge_class = ofx_status_badge_class(transaction_record.status)
        for item in related:
            payment = item["payment"]
            item["kind_badge_class"] = reconciliation_badge_class(
                item["reconciliation"].status if item["reconciliation"] else ""
            )
            item["payment_status_label"] = compact_payment_status_label(payment.status)
            item["payment_status_badge_class"] = payment_status_badge_class(payment.status)
            item["needs_counterparty_registration"] = (
                payment.source == Origin.OFX
                and payment.status == Payment.Status.PENDING_REGISTRATION
                and not payment.counterparty_id
            )
            item["supplier_create_url"] = (
                ofx_counterparty_create_url(
                    transaction_record,
                    reverse("internal_supplier_quick_create"),
                    current_url,
                    payment,
                )
                if item["needs_counterparty_registration"]
                else ""
            )
            item["worker_create_url"] = (
                ofx_counterparty_create_url(
                    transaction_record,
                    reverse("internal_worker_quick_create"),
                    current_url,
                    payment,
                )
                if item["needs_counterparty_registration"]
                else ""
            )
        primary_related = choose_primary_ofx_related_payment(related)
        transaction_record.primary_related_payment = primary_related
        transaction_record.alternative_related_payments = [
            item for item in related if item is not primary_related
        ]
        transaction_record.related_payments_count = len(related)
        pending_registration_payment = next(
            (
                item["payment"]
                for item in related
                if item["payment"].source == Origin.OFX
                and item["payment"].status == Payment.Status.PENDING_REGISTRATION
                and not item["payment"].counterparty_id
            ),
            None,
        )
        transaction_record.supplier_create_url = ofx_counterparty_create_url(
            transaction_record,
            reverse("internal_supplier_quick_create"),
            current_url,
            pending_registration_payment,
        )
        transaction_record.worker_create_url = ofx_counterparty_create_url(
            transaction_record,
            reverse("internal_worker_quick_create"),
            current_url,
            pending_registration_payment,
        )


def filter_ofx_review_rows(
    transactions,
    *,
    payment_filter: str,
    counterparty_id: int,
    cost_center_id: int,
    work_id: int,
):
    filtered = []
    for transaction_record in transactions:
        related_payments = [item["payment"] for item in getattr(transaction_record, "related_payments", [])]
        if payment_filter == "com_sugestao" and not getattr(transaction_record, "has_suggested_payment", False):
            continue
        if payment_filter in {Payment.Status.PENDING_REGISTRATION, Payment.Status.PENDING_CONFIRMATION} and not any(
            payment.status == payment_filter for payment in related_payments
        ):
            continue
        if counterparty_id and not ofx_row_has_counterparty(transaction_record, related_payments, counterparty_id):
            continue
        if cost_center_id and not any(payment.cost_center_id == cost_center_id for payment in related_payments):
            continue
        if work_id and not any(payment.work_id == work_id for payment in related_payments):
            continue
        filtered.append(transaction_record)
    return filtered


def ofx_row_has_counterparty(transaction_record, related_payments, counterparty_id: int) -> bool:
    if transaction_record.counterparty_id == counterparty_id:
        return True
    return any(payment.counterparty_id == counterparty_id for payment in related_payments)


def ofx_counterparty_create_url(transaction_record, base_url: str, next_url: str, payment: Payment | None = None) -> str:
    query = {
        "next": next_url,
        "name": transaction_record.name_extracted,
        "primary_document": transaction_record.document_extracted,
    }
    if payment:
        query["payment"] = payment.pk
    return url_with_query(base_url, **query)


def choose_primary_ofx_related_payment(related: list[dict]) -> dict | None:
    if not related:
        return None
    return min(
        related,
        key=lambda item: (
            ofx_related_payment_priority(item),
            item["payment"].pk or 0,
        ),
    )


def ofx_related_payment_priority(item: dict) -> int:
    reconciliation = item.get("reconciliation")
    payment = item["payment"]
    if reconciliation and reconciliation.status == Reconciliation.Status.CONFIRMED:
        return 0
    if payment.source == Origin.OFX:
        return 1
    if reconciliation and reconciliation.status == Reconciliation.Status.SUGGESTED:
        return 2
    return 3


def compact_payment_status_label(status: str) -> str:
    labels = {
        Payment.Status.RECEIVED: "Received",
        Payment.Status.PROCESSING: "Processing",
        Payment.Status.PENDING_REGISTRATION: "Registration",
        Payment.Status.PENDING_CONFIRMATION: "Pending",
        Payment.Status.CORRECTING: "Correction",
        Payment.Status.APPROVED: "Approved",
        Payment.Status.CANCELED: "Canceled",
        Payment.Status.POSTED: "Posted",
        Payment.Status.RECONCILED: "Reconciled",
        Payment.Status.POSSIBLE_DUPLICATE: "Duplicate?",
        Payment.Status.ERROR: "Error",
        Payment.Status.IGNORED: "Ignored",
    }
    return labels.get(status, status or "-")


def payment_status_badge_class(status: str) -> str:
    if status in {Payment.Status.APPROVED, Payment.Status.POSTED, Payment.Status.RECONCILED}:
        return "ok"
    if status in {
        Payment.Status.PENDING_REGISTRATION,
        Payment.Status.PENDING_CONFIRMATION,
        Payment.Status.CORRECTING,
        Payment.Status.RECEIVED,
        Payment.Status.PROCESSING,
    }:
        return "warning"
    if status in {Payment.Status.CANCELED, Payment.Status.ERROR, Payment.Status.POSSIBLE_DUPLICATE}:
        return "danger"
    if status == Payment.Status.IGNORED:
        return "muted"
    return ""


def compact_ofx_status_label(status: str) -> str:
    labels = {
        OfxTransaction.Status.PENDING: "Pending",
        OfxTransaction.Status.RECONCILED: "Reconciled",
        OfxTransaction.Status.POSSIBLE_DUPLICATE: "Duplicate?",
        OfxTransaction.Status.MISSING_PAYMENT: "Missing payment",
        OfxTransaction.Status.DIVERGENT: "Divergent",
        OfxTransaction.Status.IGNORED: "Ignored",
    }
    return labels.get(status, status or "-")


def ofx_status_badge_class(status: str) -> str:
    if status == OfxTransaction.Status.RECONCILED:
        return "ok"
    if status in {OfxTransaction.Status.PENDING, OfxTransaction.Status.MISSING_PAYMENT}:
        return "warning"
    if status in {OfxTransaction.Status.DIVERGENT, OfxTransaction.Status.POSSIBLE_DUPLICATE}:
        return "danger"
    if status == OfxTransaction.Status.IGNORED:
        return "muted"
    return ""


def reconciliation_badge_class(status: str) -> str:
    if status == Reconciliation.Status.CONFIRMED:
        return "ok"
    if status == Reconciliation.Status.SUGGESTED:
        return "warning"
    if status == Reconciliation.Status.REJECTED:
        return "danger"
    return "info"


def summarize_text(value: str, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text or "-"
    return f"{text[: max(0, limit - 1)].rstrip()}…"


def unique_ints(values) -> list[int]:
    selected = []
    for value in values:
        parsed = parse_int(value, 0)
        if parsed and parsed not in selected:
            selected.append(parsed)
    return selected


def first_form_error(form) -> str:
    for errors in form.errors.values():
        if errors:
            return errors[0]
    return "Invalid data."


def can_bulk_edit_ofx_payment(payment: Payment) -> bool:
    if payment.source != Origin.OFX:
        return False
    if payment.status in OFX_BULK_EDIT_BLOCKED_STATUSES:
        return False
    if payment_has_generated_export(payment):
        return False
    if payment_has_confirmed_reconciliation(payment):
        return False
    return True


def can_bulk_approve_payment(payment: Payment) -> bool:
    return not bulk_payment_approval_blockers(payment)


def bulk_payment_approval_blockers(payment: Payment) -> list[str]:
    blockers = []
    if payment.status == Payment.Status.PENDING_REGISTRATION:
        blockers.append("pending registration")
    elif payment.status == Payment.Status.POSSIBLE_DUPLICATE:
        blockers.append("possible duplicate")
    elif payment.status not in BULK_APPROVABLE_PAYMENT_STATUSES:
        blockers.append(f"status {payment.get_status_display()}")

    if not payment.counterparty_id:
        blockers.append("missing vendor/worker")
    if payment_has_generated_export(payment):
        blockers.append("already exported")
    if ofx_payment_is_possible_duplicate(payment):
        blockers.append("possible OFX duplicate")

    missing_fields = payment_missing_required_fields(payment)
    if missing_fields:
        blockers.append(f"missing required fields ({', '.join(missing_fields)})")
    return blockers


def ofx_payment_is_possible_duplicate(payment: Payment) -> bool:
    if payment.source != Origin.OFX:
        return False
    transaction_id = parse_int((payment.raw_payload or {}).get("ofx_transaction_id"), 0)
    if not transaction_id:
        return False
    return OfxTransaction.objects.filter(pk=transaction_id, status=OfxTransaction.Status.POSSIBLE_DUPLICATE).exists()


def apply_ofx_bulk_edit_to_payment(payment: Payment, cleaned_data: dict, user):
    changed_fields = set()
    changed_labels = []
    skipped_status = False
    category = cleaned_data.get("category")
    cost_center = cleaned_data.get("cost_center")
    work = cleaned_data.get("work")

    if category and payment.category_id != category.pk:
        payment.category = category
        changed_fields.add("category")
        changed_labels.append("category")

    if work and payment.work_id != work.pk:
        payment.work = work
        changed_fields.add("work")
        changed_labels.append("project")

    if work and not cost_center:
        cost_center = ensure_default_work_cost_center()

    if cost_center and payment.cost_center_id != cost_center.pk:
        payment.cost_center = cost_center
        changed_fields.add("cost_center")
        changed_labels.append("cost center")

    if cleaned_data.get("clear_work_if_company") and cost_center and is_company_cost_center(cost_center):
        if payment.work_id:
            payment.work = None
            changed_fields.add("work")
            changed_labels.append("project removed")
        if payment.work_item_index:
            payment.work_item_index = ""
            changed_fields.add("work_item_index")

    for field_name, label in [
        ("payment_method", "payment method"),
        ("payer", "payer"),
        ("bank_account", "bank account"),
    ]:
        value = cleaned_data.get(field_name)
        if value and getattr(payment, field_name) != value:
            setattr(payment, field_name, value)
            changed_fields.add(field_name)
            changed_labels.append(label)

    target_status = cleaned_data.get("payment_status")
    if target_status == Payment.Status.PENDING_CONFIRMATION and not payment.counterparty_id:
        skipped_status = True
    elif target_status and payment.status != target_status:
        payment.status = target_status
        payment.needs_review = True
        payment.review_reason = "Bulk edit from OFX review. Waiting for review/approval."
        payment.confirmed_at = None
        payment.confirmed_by = None
        changed_fields.update({"status", "needs_review", "review_reason", "confirmed_at", "confirmed_by"})
        changed_labels.append("status")

    if changed_fields:
        append_payment_bulk_edit_history(payment, changed_labels, user)
        changed_fields.add("raw_payload")
    return changed_fields, skipped_status


def ensure_default_work_cost_center() -> CostCenter:
    cost_center, _created = CostCenter.objects.get_or_create(
        normalized_name=normalize_text("Project"),
        defaults={"name": "Project"},
    )
    if not cost_center.is_active:
        cost_center.is_active = True
        cost_center.save(update_fields=["is_active", "updated_at"])
    return cost_center


def is_company_cost_center(cost_center: CostCenter) -> bool:
    return normalize_text(cost_center.normalized_name or cost_center.name) == normalize_text("Company")


def append_payment_bulk_edit_history(payment: Payment, changed_labels: list[str], user) -> None:
    payload = dict(payment.raw_payload or {})
    history = list(payload.get("bulk_edits") or [])
    history.append(
        {
            "type": "ofx_review_bulk_edit",
            "at": timezone.now().isoformat(),
            "user_id": getattr(user, "pk", None),
            "username": user.get_username() if user else "",
            "changed_fields": changed_labels,
        }
    )
    payload["bulk_edits"] = history[-25:]
    payment.raw_payload = payload


@login_required
def export_batches(request):
    if request.method == "POST":
        try:
            batch = export_approved_payments(user=request.user)
        except ExportValidationError as exc:
            messages.error(request, f"Could not generate spreadsheets: {exc}")
        else:
            messages.success(
                request,
                f"Generated spreadsheets in batch #{batch.pk}: {batch.records_count} payment(s).",
            )
        return redirect("internal_export_batches")
    batches = (
        ExportBatch.objects.select_related("generated_by")
        .filter(status__in=[ExportBatch.Status.GENERATED, ExportBatch.Status.ERROR])
        .order_by("-generated_at", "-created_at")
    )
    return render(request, "core/export_batches.html", {"batches": batches})


@login_required
def export_download(request, pk, file_kind="importacao"):
    batch = get_object_or_404(ExportBatch, pk=pk, status=ExportBatch.Status.GENERATED)
    export_file = export_file_for_kind(batch, file_kind)
    if not export_file:
        raise Http404("Spreadsheet not found.")
    try:
        return FileResponse(
            export_file.open("rb"),
            as_attachment=True,
            filename=export_file.name.rsplit("/", 1)[-1],
        )
    except FileNotFoundError as exc:
        raise Http404("Spreadsheet not found.") from exc


def export_file_for_kind(batch: ExportBatch, file_kind: str):
    if file_kind == "exportacao":
        return batch.accounting_file
    if file_kind == "importacao":
        return batch.import_file or batch.file
    raise Http404("Type de spreadsheet inválido.")


def parse_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def payment_form_initial_from_query(request) -> dict:
    initial = {}
    for query_key, field_name in [
        ("counterparty", "counterparty"),
        ("category", "category"),
        ("cost_center", "cost_center"),
        ("work", "work"),
    ]:
        value = parse_int(request.GET.get(query_key), 0)
        if value:
            initial[field_name] = value
    return initial


def optional_telegram_draft_from_request(request) -> TelegramDraft | None:
    draft_id = parse_int(request.POST.get("draft") or request.GET.get("draft"), 0)
    if not draft_id:
        return None
    return TelegramDraft.objects.filter(pk=draft_id).first()


def counterparty_quick_initial_from_request(request, draft: TelegramDraft | None) -> dict:
    initial = {
        "name": request.GET.get("name", ""),
        "primary_document": request.GET.get("primary_document", ""),
    }
    category_id = parse_int(request.GET.get("default_category"), 0)
    if category_id:
        initial["default_category"] = category_id
    if draft:
        candidate = (draft.raw_payload or {}).get("counterparty_candidate") or {}
        initial["name"] = initial["name"] or candidate.get("name", "")
        initial["primary_document"] = initial["primary_document"] or candidate.get("document", "")
        category_name = candidate.get("category_name", "")
        if category_name and "default_category" not in initial:
            category = Category.objects.filter(normalized_name=normalize_text(category_name)).first()
            if category:
                initial["default_category"] = category.pk
    return {key: value for key, value in initial.items() if value}


def work_quick_initial_from_request(request, draft: TelegramDraft | None) -> dict:
    initial = {
        "work_name": request.GET.get("work_name", ""),
        "cost_center_name": request.GET.get("cost_center_name") or "Project",
        "city": request.GET.get("city", ""),
        "state": request.GET.get("state", ""),
    }
    if draft:
        candidate = (draft.raw_payload or {}).get("work_candidate") or {}
        initial["work_name"] = initial["work_name"] or candidate.get("name", "")
        initial["cost_center_name"] = "Project"
    return {key: value for key, value in initial.items() if value}


def link_counterparty_to_draft(draft: TelegramDraft, counterparty: Counterparty) -> TelegramDraft:
    with transaction.atomic():
        locked_draft = TelegramDraft.objects.select_for_update().get(pk=draft.pk)
        locked_draft.counterparty = counterparty
        if not locked_draft.category_id and counterparty.default_category_id:
            locked_draft.category = counterparty.default_category
        clear_draft_counterparty_candidate(locked_draft)
        locked_draft.save(update_fields=["counterparty", "category", "raw_payload", "updated_at"])
        return locked_draft


def can_link_counterparty_to_ofx_payment(payment: Payment) -> bool:
    return (
        payment.source == Origin.OFX
        and payment.status == Payment.Status.PENDING_REGISTRATION
        and not payment.counterparty_id
        and not payment.export_batches.exists()
    )


def link_counterparty_to_ofx_payment(payment: Payment, counterparty: Counterparty) -> Payment:
    with transaction.atomic():
        locked_payment = Payment.objects.select_for_update().get(pk=payment.pk)
        if not can_link_counterparty_to_ofx_payment(locked_payment):
            return locked_payment
        payload = locked_payment.raw_payload or {}
        candidate = payload.pop("counterparty_candidate", None)
        if candidate:
            payload["resolved_counterparty_candidate"] = candidate
        payload["counterparty_quick_link"] = {
            "counterparty_id": counterparty.pk,
            "source": "ofx_review",
            "linked_at": timezone.now().isoformat(),
        }
        locked_payment.counterparty = counterparty
        if not locked_payment.category_id and counterparty.default_category_id:
            locked_payment.category = counterparty.default_category
        if not locked_payment.chart_account_id and counterparty.default_chart_account_id:
            locked_payment.chart_account = counterparty.default_chart_account
        if not locked_payment.cost_center_id and counterparty.default_cost_center_id:
            locked_payment.cost_center = counterparty.default_cost_center
        if not locked_payment.work_id and counterparty.default_work_id:
            locked_payment.work = counterparty.default_work
        locked_payment.status = Payment.Status.PENDING_CONFIRMATION
        locked_payment.needs_review = True
        locked_payment.review_reason = "Registration linked from OFX review. Check and approve the payment."
        locked_payment.raw_payload = payload
        locked_payment.save(
            update_fields=[
                "counterparty",
                "category",
                "chart_account",
                "cost_center",
                "work",
                "status",
                "needs_review",
                "review_reason",
                "raw_payload",
                "updated_at",
            ]
        )
        return locked_payment


def link_work_to_draft(draft: TelegramDraft, cost_center: CostCenter, work: Work) -> TelegramDraft:
    with transaction.atomic():
        locked_draft = TelegramDraft.objects.select_for_update().get(pk=draft.pk)
        locked_draft.work = work
        locked_draft.cost_center = cost_center
        locked_draft.work_item_index = ""
        clear_draft_work_candidate(locked_draft)
        locked_draft.save(update_fields=["work", "cost_center", "work_item_index", "raw_payload", "updated_at"])
        return locked_draft


def quick_create_urls(request) -> dict:
    next_url = request.get_full_path()
    return {
        "supplier_create_url": url_with_query(reverse("internal_supplier_quick_create"), next=next_url),
        "worker_create_url": url_with_query(reverse("internal_worker_quick_create"), next=next_url),
        "work_cost_center_create_url": url_with_query(
            reverse("internal_work_cost_center_quick_create"),
            next=next_url,
        ),
        "counterparty_create_url": url_with_query(reverse("internal_counterparty_create"), next=next_url),
    }


def safe_next_url(value: str | None, request) -> str:
    if value and url_has_allowed_host_and_scheme(value, allowed_hosts={request.get_host()}):
        return value
    return reverse("internal_payment_create")


def safe_return_url(value: str | None, request, fallback: str) -> str:
    if value and url_has_allowed_host_and_scheme(value, allowed_hosts={request.get_host()}):
        return value
    return fallback


def url_with_query(url: str, **params) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update({key: value for key, value in params.items() if value not in {None, ""}})
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def enrich_payment_rows(payments):
    mark_work_budget_status(payments)
    for payment in payments:
        confirmed_reconciliations = list(getattr(payment, "confirmed_reconciliations", []))
        has_confirmed_reconciliation = bool(confirmed_reconciliations) or payment.status == Payment.Status.RECONCILED
        indicators = []
        if not payment.payment_date:
            indicators.append("Without date")
        if not payment.category_id:
            indicators.append("No category")
        if not payment.cost_center_id:
            indicators.append("No cost center")
        if not payment.counterparty_id:
            indicators.append("No counterparty")
        elif not payment.counterparty.primary_document:
            indicators.append("Without CPF/CNPJ")
        if payment.status == Payment.Status.PENDING_REGISTRATION:
            indicators.append("Pending registration")
        if payment.status in {Payment.Status.APPROVED, Payment.Status.POSTED} and not has_confirmed_reconciliation:
            indicators.append("Pending de OFX")
        if payment.work_without_budget:
            indicators.append("Project without budget")
        payment.amount_display = format_money_br(payment.amount)
        payment.has_confirmed_reconciliation = has_confirmed_reconciliation
        payment.row_indicators = indicators
        payment.can_bulk_approve = can_bulk_approve_payment(payment)
        payment.can_quick_approve = payment.status not in {
            Payment.Status.APPROVED,
            Payment.Status.CANCELED,
            Payment.Status.RECONCILED,
        }
        payment.can_quick_change = payment.status not in {Payment.Status.CANCELED, Payment.Status.RECONCILED}
        payment.can_web_edit = can_edit_payment(payment)
        payment.can_web_delete = can_delete_payment(payment)


def enrich_telegram_draft_rows(drafts):
    mark_work_budget_status(drafts)
    for draft in drafts:
        payload = draft.raw_payload or {}
        counterparty_candidate = payload.get("counterparty_candidate") or {}
        work_candidate = payload.get("work_candidate") or {}
        draft.counterparty_candidate_name = counterparty_candidate.get("name", "")
        draft.counterparty_candidate_document = counterparty_candidate.get("document", "")
        draft.work_candidate_name = work_candidate.get("name", "")
        draft.counterparty_display = (
            draft.counterparty.name if draft.counterparty_id else draft.counterparty_candidate_name
        )
        draft.work_display = draft.work.name if draft.work_id else draft.work_candidate_name
        draft.uploaded_file_count = draft.uploaded_files.count()


def mark_work_budget_status(records):
    work_ids = {record.work_id for record in records if getattr(record, "work_id", None)}
    works_with_budget = set(
        BudgetItem.objects.filter(work_id__in=work_ids, is_active=True).values_list("work_id", flat=True).distinct()
    )
    for record in records:
        work_id = getattr(record, "work_id", None)
        record.work_without_budget = bool(work_id and work_id not in works_with_budget)


def build_telegram_draft_payment_preview(draft: TelegramDraft) -> str:
    payment = Payment(
        competence_date=draft.payment_date,
        due_date=draft.payment_date,
        payment_date=draft.payment_date,
        amount=draft.amount or 0,
        counterparty=draft.counterparty,
        description=draft.description or draft.text_content[:255],
        category=draft.category,
        cost_center=draft.cost_center,
        work=draft.work,
        work_item_index=draft.work_item_index,
        payment_method=draft.payment_method,
        source="telegram",
        status=Payment.Status.RECEIVED,
        confidence=draft.confidence,
        needs_review=True,
    )
    return format_payment_suggestion(payment)


def get_telegram_draft(pk):
    return get_object_or_404(
        TelegramDraft.objects.select_related(
            "counterparty",
            "category",
            "cost_center",
            "work",
            "finalized_payment",
        ).prefetch_related("uploaded_files"),
        pk=pk,
    )


def can_edit_telegram_draft(draft: TelegramDraft) -> bool:
    return draft.status == TelegramDraft.Status.ACTIVE


def monthly_closing_checklist(summary, period_batches):
    payment_link = monthly_payment_link
    ofx_link = monthly_ofx_link
    missing_fields_count = len(summary.payments_missing_required_fields)
    return [
        {
            "label": "Selected period",
            "status": "ok",
            "status_label": "ok",
            "detail": f"{summary.period_start:%d/%m/%Y} to {summary.period_end:%d/%m/%Y}",
            "count": "",
            "link": "",
            "action_label": "",
            "blocking": False,
        },
        checklist_row(
            "Active drafts in period",
            summary.active_drafts_count,
            "Finalize, edit, or cancel drafts before closing.",
            monthly_drafts_link(),
            "Open drafts",
            blocking=summary.active_drafts_count > 0,
        ),
        checklist_row(
            "Payments pending registration",
            summary.pending_registration_count,
            "Complete missing vendor/worker, project, category, or date.",
            payment_link(summary, Payment.Status.PENDING_REGISTRATION),
            "Resolve payments",
            blocking=summary.pending_registration_count > 0,
        ),
        checklist_row(
            "Payments pending approval",
            summary.pending_confirmation_count,
            "Review payments and approve them when they are correct.",
            payment_link(summary, Payment.Status.PENDING_CONFIRMATION),
            "Resolve payments",
            blocking=summary.pending_confirmation_count > 0,
        ),
        checklist_row(
            "Payments under correction",
            summary.correcting_count,
            "Review payments and make them ready for approval.",
            payment_link(summary, Payment.Status.CORRECTING),
            "Resolve corrections",
            blocking=summary.correcting_count > 0,
        ),
        checklist_row(
            "Approved payments",
            summary.approved_count,
            f"Total approved: R$ {summary.approved_amount}",
            payment_link(summary, Payment.Status.APPROVED),
            "View approved",
            blocking=False,
            warning_if_zero=False,
        ),
        checklist_row(
            "Reconciled payments",
            summary.reconciled_count,
            f"Total reconciled: R$ {summary.reconciled_amount}",
            payment_link(summary, Payment.Status.RECONCILED),
            "View reconciled",
            blocking=False,
            warning_if_zero=False,
        ),
        {
            "label": "OFX imported in period",
            "status": "ok" if summary.has_ofx_imported else "warning",
            "status_label": "ok" if summary.has_ofx_imported else "attention",
            "detail": (
                "OFX found for the period."
                if summary.has_ofx_imported
                else "Import the month's OFX to validate the close when the statement is available."
            ),
            "count": "yes" if summary.has_ofx_imported else "no",
            "link": ofx_link(summary, "all"),
            "action_label": "Open OFX",
            "blocking": False,
        },
        checklist_row(
            "OFX expenses without payment",
            summary.ofx_expense_without_payment_count,
            "Create or link a payment for each bank expense before closing.",
            ofx_link(summary, "pendencias"),
            "Resolve OFX",
            blocking=summary.ofx_expense_without_payment_count > 0,
        ),
        checklist_row(
            "OFX suggestions pending registration",
            summary.ofx_suggested_pending_registration_count,
            "Register or link the vendor/worker from OFX suggestions.",
            monthly_ofx_payment_link(summary, Payment.Status.PENDING_REGISTRATION),
            "Review OFX",
            blocking=summary.ofx_suggested_pending_registration_count > 0,
        ),
        checklist_row(
            "Pending OFX suggestions",
            summary.ofx_suggested_pending_confirmation_count,
            "Review suggestions created by OFX and approve them when they are correct.",
            monthly_ofx_payment_link(summary, Payment.Status.PENDING_CONFIRMATION),
            "Review OFX",
            blocking=summary.ofx_suggested_pending_confirmation_count > 0,
        ),
        checklist_row(
            "OFX pending",
            summary.ofx_pending_count,
            "Informational alert: pending transactions appear here, but blocking depends on expense without payment, registration, approval, divergence, or duplicate.",
            ofx_link(summary, OfxTransaction.Status.PENDING),
            "Resolve OFX",
            blocking=False,
            warning=summary.ofx_pending_count > 0,
        ),
        checklist_row(
            "Divergent OFX",
            summary.ofx_divergent_count,
            "Review amount, date, or counterparty divergences.",
            ofx_link(summary, OfxTransaction.Status.DIVERGENT),
            "Resolve OFX",
            blocking=summary.ofx_divergent_count > 0,
        ),
        checklist_row(
            "OFX possible duplicate",
            summary.ofx_possible_duplicate_count,
            "Confirm whether there is a duplicate before exporting.",
            ofx_link(summary, OfxTransaction.Status.POSSIBLE_DUPLICATE),
            "Resolve OFX",
            blocking=summary.ofx_possible_duplicate_count > 0,
        ),
        checklist_row(
            "Ignored OFX credits",
            summary.ofx_ignored_credit_count,
            "Credits/income identified in OFX do not block the expense close.",
            ofx_link(summary, OfxTransaction.Status.IGNORED),
            "View credits",
            blocking=False,
            warning=False,
            warning_if_zero=False,
        ),
        checklist_row(
            "Approved without reconciliation",
            summary.approved_unreconciled_count,
            "Alert: approved payments can already be exported, but still have no confirmed reconciliation.",
            url_with_query(
                reverse("internal_pending_payments"),
                status="realizados",
                date_inicio=summary.period_start.isoformat(),
                date_fim=summary.period_end.isoformat(),
                ofx="sem",
            ),
            "View approved",
            blocking=False,
            warning=summary.approved_unreconciled_count > 0,
            warning_if_zero=False,
        ),
        checklist_row(
            "Missing required fields",
            missing_fields_count,
            "Complete the required fields for exportable payments.",
            payment_link(summary, "all"),
            "View payments",
            blocking=missing_fields_count > 0,
        ),
        checklist_row(
            "Projects without imported budget",
            len(summary.payments_with_work_without_budget),
            "Informational notice: does not block payment or export; keep index/item empty until a reliable budget is imported.",
            payment_link(summary, "all"),
            "View payments",
            blocking=False,
            warning=bool(summary.payments_with_work_without_budget),
            warning_if_zero=False,
        ),
        {
            "label": "Spreadsheets already generated for the period",
            "status": "ok" if period_batches else "warning",
            "status_label": "ok" if period_batches else "attention",
            "detail": "Downloads are available below." if period_batches else "No generated spreadsheet for this period.",
            "count": len(period_batches),
            "link": reverse("internal_export_batches"),
            "action_label": "View spreadsheets",
            "blocking": False,
        },
    ]


def checklist_row(
    label,
    count,
    detail,
    link,
    action_label,
    *,
    blocking: bool,
    warning: bool = False,
    warning_if_zero: bool = False,
):
    if blocking:
        status = "danger"
        status_label = "blocked"
    elif warning:
        status = "warning"
        status_label = "attention"
    elif warning_if_zero and not count:
        status = "warning"
        status_label = "attention"
    else:
        status = "ok"
        status_label = "ok"
    return {
        "label": label,
        "status": status,
        "status_label": status_label,
        "detail": detail,
        "count": count,
        "link": link,
        "action_label": action_label,
        "blocking": blocking,
    }


def monthly_payment_link(summary, status: str) -> str:
    return url_with_query(
        reverse("internal_pending_payments"),
        status=status,
        date_inicio=summary.period_start.isoformat(),
        date_fim=summary.period_end.isoformat(),
    )


def monthly_work_payments_link(summary, work_id: int) -> str:
    return url_with_query(
        reverse("internal_pending_payments"),
        status="all",
        date_inicio=summary.period_start.isoformat(),
        date_fim=summary.period_end.isoformat(),
        work=work_id,
    )


def monthly_ofx_link(summary, status: str) -> str:
    return url_with_query(
        reverse("internal_unreconciled_ofx"),
        mes=summary.month,
        ano=summary.year,
        status=status,
    )


def monthly_ofx_payment_link(summary, payment_status: str) -> str:
    return url_with_query(
        reverse("internal_unreconciled_ofx"),
        mes=summary.month,
        ano=summary.year,
        status="all",
        payment=payment_status,
    )


def monthly_drafts_link(summary=None) -> str:
    params = {"status": TelegramDraft.Status.ACTIVE}
    if summary:
        params["date_inicio"] = summary.period_start.isoformat()
        params["date_fim"] = summary.period_end.isoformat()
    return url_with_query(reverse("internal_telegram_drafts"), **params)


def monthly_closing_blocking_issue_count(summary) -> int:
    return (
        summary.active_drafts_count
        + summary.pending_registration_count
        + summary.pending_confirmation_count
        + summary.correcting_count
        + len(summary.payments_missing_required_fields)
        + summary.ofx_expense_without_payment_count
        + summary.ofx_divergent_count
        + summary.ofx_possible_duplicate_count
    )


def monthly_closing_blockers(summary):
    blockers = []
    if summary.active_drafts_count:
        blockers.append(f"{summary.active_drafts_count} active draft(s) in period")
    if summary.pending_registration_count:
        blockers.append(f"{summary.pending_registration_count} payment(s) pending registration")
    if summary.pending_confirmation_count:
        blockers.append(f"{summary.pending_confirmation_count} payment(s) pending approval")
    if summary.correcting_count:
        blockers.append(f"{summary.correcting_count} payment(s) under correction")
    if summary.payments_missing_required_fields:
        blockers.append(
            f"{len(summary.payments_missing_required_fields)} exportable payment(s) with missing required fields"
        )
    if summary.ofx_expense_without_payment_count:
        blockers.append(f"{summary.ofx_expense_without_payment_count} OFX expense(s) without payment")
    if summary.ofx_divergent_count:
        blockers.append(f"{summary.ofx_divergent_count} divergent OFX transaction(s)")
    if summary.ofx_possible_duplicate_count:
        blockers.append(f"{summary.ofx_possible_duplicate_count} possibly duplicated OFX transaction(s)")
    return blockers


def can_edit_payment(payment: Payment) -> bool:
    if payment_has_generated_export(payment):
        return False
    if payment_has_confirmed_reconciliation(payment):
        return False
    return payment.status not in {
        Payment.Status.APPROVED,
        Payment.Status.POSTED,
        Payment.Status.RECONCILED,
        Payment.Status.CANCELED,
    }


def can_delete_payment(payment: Payment) -> bool:
    if payment_has_generated_export(payment):
        return False
    if payment.reconciliations.exists():
        return False
    return payment.status not in {
        Payment.Status.APPROVED,
        Payment.Status.POSTED,
        Payment.Status.RECONCILED,
    }


def payment_has_generated_export(payment: Payment) -> bool:
    return payment.export_batches.filter(status=ExportBatch.Status.GENERATED).exists()


def payment_has_confirmed_reconciliation(payment: Payment) -> bool:
    return payment.reconciliations.filter(status=Reconciliation.Status.CONFIRMED).exists()
