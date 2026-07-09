from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
import logging
import re

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Q
from openai import OpenAI

from apps.core.log_safety import mask_document, sanitize_log_payload, sanitize_log_text
from apps.counterparties.importers import digits_only, document_type, normalize_text
from apps.counterparties.models import (
    BudgetItem,
    Category,
    CostCenter,
    Counterparty,
    CounterpartyAlias,
    CounterpartyDocument,
    Origin,
    Work,
)
from apps.payments.counterparty_resolution import (
    AmbiguousCounterpartyError,
    CounterpartyCandidate,
    find_existing_counterparty,
)
from apps.payments.extraction import extract_payment_method
from apps.payments.models import Payment

from .models import OfxFile, OfxTransaction, Reconciliation


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PaymentSuggestionConflict:
    transaction_id: int
    fitid: str
    reason: str
    detail: str = ""


@dataclass
class PaymentSuggestionReport:
    transactions_analyzed: int = 0
    payments_created: int = 0
    payments_reused: int = 0
    transactions_ignored: int = 0
    pending_registration: int = 0
    pending_confirmation: int = 0
    conflicts: list[PaymentSuggestionConflict] = field(default_factory=list)
    created_payment_ids: list[int] = field(default_factory=list)
    reused_payment_ids: list[int] = field(default_factory=list)
    ignored_transaction_ids: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class PaymentSuggestionOutcome:
    action: str
    payment: Payment | None = None
    conflict: PaymentSuggestionConflict | None = None


@dataclass(frozen=True)
class WorkResolution:
    work: Work | None = None
    cost_center: CostCenter | None = None
    candidate_name: str = ""
    conflict: str = ""


@dataclass
class LocalPaymentClassification:
    counterparty: Counterparty | None
    category: Category | None
    cost_center: CostCenter | None
    work: Work | None
    budget_item: BudgetItem | None
    work_item_index: str
    payment_method: str
    description: str
    confidence: Decimal
    needs_user_review_reason: str
    ai_classification: "OFXAIClassification | None" = None


@dataclass(frozen=True)
class OFXAIClassification:
    counterparty_name: str
    counterparty_document: str
    category: str
    cost_center: str
    work: str
    work_item_index: str
    payment_method: str
    description: str
    confidence: Decimal
    needs_user_review_reason: str

    def as_dict(self) -> dict:
        return {
            "counterparty_name": self.counterparty_name,
            "counterparty_document": self.counterparty_document,
            "category": self.category,
            "cost_center": self.cost_center,
            "work": self.work,
            "work_item_index": self.work_item_index,
            "payment_method": self.payment_method,
            "description": self.description,
            "confidence": str(self.confidence),
            "needs_user_review_reason": self.needs_user_review_reason,
        }


class OFXAIClassificationError(Exception):
    pass


OFX_AI_CLASSIFICATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "counterparty_name",
        "counterparty_document",
        "category",
        "cost_center",
        "work",
        "work_item_index",
        "payment_method",
        "description",
        "confidence",
        "needs_user_review_reason",
    ],
    "properties": {
        "counterparty_name": {"type": "string"},
        "counterparty_document": {"type": "string"},
        "category": {"type": "string"},
        "cost_center": {"type": "string"},
        "work": {"type": "string"},
        "work_item_index": {"type": "string"},
        "payment_method": {"type": "string"},
        "description": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "needs_user_review_reason": {"type": "string"},
    },
}


class OpenAIOFXPaymentClassifier:
    def __init__(self, client=None, model: str | None = None, timeout: float | None = None):
        self.client = client
        self.model = model or settings.OPENAI_MODEL
        self.timeout = timeout if timeout is not None else settings.OPENAI_REQUEST_TIMEOUT_SECONDS

    def classify(
        self,
        transaction_record: OfxTransaction,
        local_classification: LocalPaymentClassification,
    ) -> OFXAIClassification:
        if self.client is None:
            if not settings.OPENAI_API_KEY:
                raise OFXAIClassificationError("OPENAI_API_KEY is not configured.")
            self.client = OpenAI(api_key=settings.OPENAI_API_KEY, timeout=self.timeout)
        try:
            response = self.client.responses.create(
                model=self.model,
                store=False,
                input=build_openai_ofx_classification_input(transaction_record, local_classification),
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "ofx_payment_classification",
                        "schema": OFX_AI_CLASSIFICATION_SCHEMA,
                        "strict": True,
                    }
                },
            )
        except Exception as exc:
            logger.warning(
                "OpenAI OFX classification failed: %s",
                sanitize_log_payload(
                    {
                        "transaction_id": transaction_record.pk,
                        "fitid": transaction_record.fitid,
                        "error_class": exc.__class__.__name__,
                        "error": str(exc),
                    }
                ),
            )
            raise OFXAIClassificationError(f"AI classification failed: {exc.__class__.__name__}") from exc
        return parse_ai_classification_response(response.output_text)


def build_openai_ofx_classification_input(
    transaction_record: OfxTransaction,
    local_classification: LocalPaymentClassification,
) -> list[dict]:
    context = {
        "local_classification": {
            "counterparty_known": bool(local_classification.counterparty),
            "counterparty_name": local_classification.counterparty.name if local_classification.counterparty else "",
            "category": local_classification.category.name if local_classification.category else "",
            "cost_center": local_classification.cost_center.name if local_classification.cost_center else "",
            "work": local_classification.work.name if local_classification.work else "",
            "payment_method": local_classification.payment_method,
        },
        "categories": list(Category.objects.filter(is_active=True).values_list("name", flat=True)[:80]),
        "cost_centers": list(CostCenter.objects.filter(is_active=True).values_list("name", flat=True)[:80]),
        "works": list(Work.objects.filter(is_active=True).values_list("name", flat=True)[:80]),
        "budget_items": [
            {
                "work": item.work.name,
                "index": item.index,
                "description": item.description[:180],
            }
            for item in BudgetItem.objects.filter(is_active=True)
            .select_related("work")
            .order_by("work__name", "index")[:250]
        ],
    }
    transaction_payload = {
        "posted_at": transaction_record.posted_at.isoformat(),
        "amount_is_debit": transaction_record.amount < 0,
        "transaction_type": transaction_record.transaction_type,
        "memo": sanitize_log_text(clean_description(transaction_record.memo), limit=200),
        "name_extracted": sanitize_log_text(mask_document(clean_name(transaction_record.name_extracted)), limit=120),
        "document_extracted_present": bool(digits_only(transaction_record.document_extracted)),
    }
    return [
        {
            "role": "developer",
            "content": [
                {
                    "type": "input_text",
                    "text": (
                        "Classify an OFX-imported expense for human review. "
                        "Return only JSON in the schema. Do not approve payments. "
                        "Use AI only as a suggestion: if the local classification already has a counterparty, category, "
                        "project, or cost center, do not contradict those data. "
                        "If confidence is low, fill needs_user_review_reason."
                    ),
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": (
                        f"Transaction OFX:\n{json.dumps(transaction_payload, ensure_ascii=False)}\n\n"
                        f"Minimum context:\n{json.dumps(context, ensure_ascii=False)}"
                    ),
                }
            ],
        },
    ]


def parse_ai_classification_response(content: str) -> OFXAIClassification:
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise OFXAIClassificationError("AI response is not valid JSON.") from exc
    missing = set(OFX_AI_CLASSIFICATION_SCHEMA["required"]) - set(data)
    if missing:
        raise OFXAIClassificationError(f"AI response is missing required fields: {sorted(missing)}")
    confidence = parse_ai_confidence(data["confidence"])
    return OFXAIClassification(
        counterparty_name=parse_ai_string(data["counterparty_name"]),
        counterparty_document=digits_only(data["counterparty_document"]),
        category=parse_ai_string(data["category"]),
        cost_center=parse_ai_string(data["cost_center"]),
        work=parse_ai_string(data["work"]),
        work_item_index=parse_ai_string(data["work_item_index"]),
        payment_method=parse_ai_string(data["payment_method"]),
        description=parse_ai_string(data["description"])[:200],
        confidence=confidence,
        needs_user_review_reason=parse_ai_string(data["needs_user_review_reason"])[:255],
    )


def parse_ai_string(value) -> str:
    if not isinstance(value, str):
        raise OFXAIClassificationError("AI response contains an invalid text field.")
    return " ".join(value.strip().split())


def parse_ai_confidence(value) -> Decimal:
    try:
        confidence = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError) as exc:
        raise OFXAIClassificationError("Invalid confidence field.") from exc
    if confidence < 0 or confidence > 1:
        raise OFXAIClassificationError("Campo confidence fora do intervalo 0..1.")
    return confidence


def suggest_payments_from_ofx(source, *, user=None, ai_classifier=None) -> PaymentSuggestionReport:
    """Create reviewable Payment suggestions for debit OFX transactions without payments."""

    report = PaymentSuggestionReport()
    for transaction_record in ofx_transactions_for_source(source):
        report.transactions_analyzed += 1
        try:
            result = suggest_payment_from_transaction(transaction_record, user=user, ai_classifier=ai_classifier)
        except Exception as exc:
            report.conflicts.append(
                PaymentSuggestionConflict(
                    transaction_id=transaction_record.pk,
                    fitid=transaction_record.fitid,
                    reason="error_sugestao",
                    detail=exc.__class__.__name__,
                )
            )
            continue
        if result.action == "created" and result.payment:
            report.payments_created += 1
            report.created_payment_ids.append(result.payment.pk)
            increment_pending_count(report, result.payment)
        elif result.action == "reused" and result.payment:
            report.payments_reused += 1
            report.reused_payment_ids.append(result.payment.pk)
            increment_pending_count(report, result.payment)
        elif result.action == "ignored":
            report.transactions_ignored += 1
            report.ignored_transaction_ids.append(transaction_record.pk)
        elif result.action == "conflict" and result.conflict:
            report.conflicts.append(result.conflict)
    return report


def increment_pending_count(report: PaymentSuggestionReport, payment: Payment) -> None:
    if payment.status == Payment.Status.PENDING_REGISTRATION:
        report.pending_registration += 1
    elif payment.status == Payment.Status.PENDING_CONFIRMATION:
        report.pending_confirmation += 1


def ofx_transactions_for_source(source):
    if isinstance(source, OfxFile):
        queryset = source.transactions.all()
    elif isinstance(source, OfxTransaction):
        queryset = OfxTransaction.objects.filter(pk=source.pk)
    elif hasattr(source, "select_related"):
        queryset = source
    else:
        queryset = OfxTransaction.objects.filter(pk__in=[transaction_record.pk for transaction_record in source])
    return queryset.select_related("ofx_file", "counterparty").order_by("posted_at", "id")


@transaction.atomic
def suggest_payment_from_transaction(transaction_record: OfxTransaction, *, user=None, ai_classifier=None):
    transaction_record = (
        OfxTransaction.objects.select_for_update(of=("self",))
        .select_related("ofx_file", "counterparty")
        .get(pk=transaction_record.pk)
    )

    if transaction_record.amount >= 0:
        return PaymentSuggestionOutcome(action="ignored")
    if has_confirmed_reconciliation(transaction_record):
        return PaymentSuggestionOutcome(action="ignored")
    existing_reconciliation_payment = find_existing_reconciliation_payment(transaction_record)
    if existing_reconciliation_payment:
        return PaymentSuggestionOutcome(action="reused", payment=existing_reconciliation_payment)

    existing_ofx_payment = find_existing_ofx_payment(transaction_record)
    if existing_ofx_payment:
        return PaymentSuggestionOutcome(action="reused", payment=existing_ofx_payment)

    counterparty_result = resolve_counterparty(transaction_record)
    if isinstance(counterparty_result, PaymentSuggestionConflict):
        return PaymentSuggestionOutcome(action="conflict", conflict=counterparty_result)
    counterparty = counterparty_result
    if not counterparty and not should_defer_auto_counterparty_to_ai(ai_classifier):
        counterparty = auto_create_ofx_supplier(transaction_record)

    existing_payment = find_existing_business_payment(transaction_record, counterparty)
    if existing_payment:
        suggest_existing_payment_reconciliation(existing_payment, transaction_record, user=user)
        return PaymentSuggestionOutcome(action="reused", payment=existing_payment)

    work_resolution = resolve_work(transaction_record)
    if work_resolution.conflict:
        return PaymentSuggestionOutcome(
            action="conflict",
            conflict=PaymentSuggestionConflict(
                transaction_id=transaction_record.pk,
                fitid=transaction_record.fitid,
                reason="project_ambigua",
                detail=work_resolution.conflict,
            ),
        )

    payment = build_payment_suggestion(
        transaction_record,
        counterparty,
        work_resolution,
        user=user,
        ai_classifier=ai_classifier,
    )
    payment.save()
    return PaymentSuggestionOutcome(action="created", payment=payment)


def has_confirmed_reconciliation(transaction_record: OfxTransaction) -> bool:
    return transaction_record.reconciliations.filter(status=Reconciliation.Status.CONFIRMED).exists()


def find_existing_reconciliation_payment(transaction_record: OfxTransaction) -> Payment | None:
    reconciliation = (
        transaction_record.reconciliations.exclude(status=Reconciliation.Status.REJECTED)
        .select_related("payment")
        .order_by("pk")
        .first()
    )
    if not reconciliation:
        return None
    payment = reconciliation.payment
    if payment_has_other_ofx_link(payment, transaction_record):
        return None
    return payment


def find_existing_ofx_payment(transaction_record: OfxTransaction) -> Payment | None:
    return (
        Payment.objects.filter(
            source=Origin.OFX,
            raw_payload__ofx_transaction_id=transaction_record.pk,
        )
        .order_by("pk")
        .first()
    )


def resolve_counterparty(transaction_record: OfxTransaction) -> Counterparty | PaymentSuggestionConflict | None:
    if transaction_record.counterparty_id:
        return transaction_record.counterparty

    candidate = CounterpartyCandidate(
        name=clean_name(transaction_record.name_extracted),
        document=transaction_record.document_extracted,
        alias=clean_name(transaction_record.name_extracted),
        source=Origin.OFX,
    )
    try:
        counterparty = find_existing_counterparty(candidate)
    except AmbiguousCounterpartyError as exc:
        return PaymentSuggestionConflict(
            transaction_id=transaction_record.pk,
            fitid=transaction_record.fitid,
            reason="counterparty_ambigua",
            detail=str(exc),
        )
    if counterparty:
        ensure_ofx_counterparty_defaults(counterparty, candidate.name, digits_only(candidate.document))
    return counterparty


def auto_create_ofx_supplier(transaction_record: OfxTransaction) -> Counterparty | None:
    candidate_name = clean_auto_counterparty_name(transaction_record.name_extracted)
    document = digits_only(transaction_record.document_extracted)
    if not is_safe_auto_counterparty_name(candidate_name):
        return None

    candidate = CounterpartyCandidate(
        name=candidate_name,
        document=document,
        alias=candidate_name,
        category_name="Other Expenses",
        source=Origin.OFX,
    )
    try:
        existing = find_existing_counterparty(candidate)
    except AmbiguousCounterpartyError:
        return None
    if existing:
        ensure_ofx_counterparty_defaults(existing, candidate_name, document)
        return existing

    category = ensure_other_expenses_category()
    counterparty = Counterparty.objects.create(
        name=candidate.name,
        normalized_name=candidate.normalized_name,
        kind=Counterparty.Kind.SUPPLIER,
        person_type=person_type_for_document(document),
        primary_document=document,
        default_category=category,
        source=Origin.OFX,
        confidence=Decimal("0.85") if document else Decimal("0.65"),
        notes="Created automatically from an OFX expense. Review the record if needed.",
    )
    ensure_ofx_counterparty_document(counterparty, document)
    ensure_ofx_counterparty_alias(counterparty, candidate_name)
    return counterparty


def should_defer_auto_counterparty_to_ai(ai_classifier=None) -> bool:
    if ai_classifier is not None:
        return True
    return bool(settings.OPENAI_API_KEY and getattr(settings, "OPENAI_OFX_CLASSIFICATION_ENABLED", False))


def clean_auto_counterparty_name(value: object) -> str:
    return " ".join(str(value or "").strip().split())[:255]


def is_safe_auto_counterparty_name(value: str) -> bool:
    normalized = normalize_text(value)
    if len(normalized) < 5:
        return False
    generic_names = {
        "pix",
        "ted",
        "doc",
        "debito",
        "credito",
        "payment",
        "payment pix",
        "payment pix pix deb",
        "transferencia",
        "transferencia enviada",
        "debito automatico",
        "compra cartao",
        "saque",
        "tarifa",
    }
    if normalized in generic_names:
        return False
    if len(re.findall(r"[a-z]", normalized)) < 4:
        return False
    return True


def ensure_ofx_counterparty_defaults(counterparty: Counterparty, alias: str, document: str) -> None:
    changed_fields = []
    if document and not counterparty.primary_document:
        counterparty.primary_document = document
        counterparty.person_type = person_type_for_document(document)
        changed_fields.extend(["primary_document", "person_type"])
    if changed_fields:
        changed_fields.append("updated_at")
        counterparty.save(update_fields=changed_fields)
    ensure_ofx_counterparty_document(counterparty, document)
    ensure_ofx_counterparty_alias(counterparty, alias)


def ensure_ofx_counterparty_document(counterparty: Counterparty, document: str) -> None:
    if not document:
        return
    CounterpartyDocument.objects.get_or_create(
        number=document,
        defaults={
            "counterparty": counterparty,
            "document_type": document_type(document),
            "source": Origin.OFX,
            "confidence": Decimal("1.00"),
            "is_primary": counterparty.primary_document == document,
        },
    )


def ensure_ofx_counterparty_alias(counterparty: Counterparty, name: str) -> None:
    normalized = normalize_text(name)
    if not normalized or normalized == counterparty.normalized_name:
        return
    CounterpartyAlias.objects.get_or_create(
        counterparty=counterparty,
        normalized_name=normalized,
        defaults={"name": name, "source": Origin.OFX},
    )


def person_type_for_document(document: str) -> str:
    if len(document) == 11:
        return Counterparty.PersonType.INDIVIDUAL
    if len(document) == 14:
        return Counterparty.PersonType.COMPANY
    return Counterparty.PersonType.UNKNOWN


def find_existing_business_payment(
    transaction_record: OfxTransaction,
    counterparty: Counterparty | None,
) -> Payment | None:
    if not counterparty:
        return None
    candidates = (
        Payment.objects.filter(
            payment_date=transaction_record.posted_at,
            amount=money_abs(transaction_record.amount),
            counterparty=counterparty,
        )
        .exclude(source=Origin.OFX)
        .exclude(status__in=[Payment.Status.CANCELED, Payment.Status.IGNORED])
        .order_by("pk")
    )
    for payment in candidates:
        if not payment_has_other_ofx_link(payment, transaction_record):
            return payment
    return None


def payment_has_other_ofx_link(payment: Payment, transaction_record: OfxTransaction) -> bool:
    payload = payment.raw_payload or {}
    payload_transaction_id = payload.get("ofx_transaction_id") or nested_payload_value(payload, "ofx", "transaction_id")
    if payload_transaction_id and str(payload_transaction_id) != str(transaction_record.pk):
        return True

    payload_fitid = (
        payload.get("ofx_fitid")
        or payload.get("fitid")
        or nested_payload_value(payload, "ofx", "fitid")
        or nested_payload_value(payload, "banking", "fitid")
    )
    if payload_fitid and str(payload_fitid) != str(transaction_record.fitid):
        return True

    return (
        payment.reconciliations.exclude(status=Reconciliation.Status.REJECTED)
        .exclude(transaction=transaction_record)
        .exists()
    )


def nested_payload_value(payload: dict, key: str, nested_key: str):
    nested = payload.get(key)
    if not isinstance(nested, dict):
        return None
    return nested.get(nested_key)


def suggest_existing_payment_reconciliation(payment: Payment, transaction_record: OfxTransaction, *, user=None) -> None:
    Reconciliation.objects.get_or_create(
        payment=payment,
        transaction=transaction_record,
        defaults={
            "status": Reconciliation.Status.SUGGESTED,
            "confidence": Decimal("0.90"),
            "notes": "Existing payment with the same date, amount, and counterparty.",
            "created_by": user,
        },
    )
    if transaction_record.status != OfxTransaction.Status.POSSIBLE_DUPLICATE:
        transaction_record.status = OfxTransaction.Status.POSSIBLE_DUPLICATE
        transaction_record.save(update_fields=["status", "updated_at"])


def build_payment_suggestion(
    transaction_record: OfxTransaction,
    counterparty: Counterparty | None,
    work_resolution: WorkResolution,
    *,
    user=None,
    ai_classifier=None,
) -> Payment:
    payment_date = transaction_record.posted_at
    classification = classify_payment_suggestion(
        transaction_record,
        counterparty,
        work_resolution,
        ai_classifier=ai_classifier,
    )
    raw_payload = build_payment_raw_payload(transaction_record, classification, work_resolution)
    status = (
        Payment.Status.PENDING_CONFIRMATION
        if classification.counterparty
        else Payment.Status.PENDING_REGISTRATION
    )
    review_reason = (
        classification.needs_user_review_reason or "Suggestion created from OFX. Review before approving."
        if classification.counterparty
        else "Counterparty is not registered. Confirm vendor/worker before approving."
    )
    return Payment(
        competence_date=payment_date,
        due_date=payment_date,
        payment_date=payment_date,
        amount=money_abs(transaction_record.amount),
        counterparty=classification.counterparty,
        description=classification.description,
        category=classification.category,
        payment_method=classification.payment_method,
        cost_center=classification.cost_center or ensure_company_cost_center(),
        work=classification.work,
        work_item_index=classification.work_item_index,
        source=Origin.OFX,
        status=status,
        confidence=classification.confidence,
        needs_review=True,
        review_reason=review_reason,
        created_by=user,
        raw_payload=raw_payload,
    )


def classify_payment_suggestion(
    transaction_record: OfxTransaction,
    counterparty: Counterparty | None,
    work_resolution: WorkResolution,
    *,
    ai_classifier=None,
) -> LocalPaymentClassification:
    historical = infer_from_history(counterparty)
    work = work_resolution.work or historical.get("work")
    category = infer_category(counterparty) or historical.get("category")
    cost_center = (
        work_resolution.cost_center
        or (ensure_work_cost_center() if work else None)
        or historical.get("cost_center")
        or ensure_company_cost_center()
    )
    payment_method = infer_payment_method(transaction_record) or historical.get("payment_method") or ""
    description = clean_description(transaction_record.memo)
    budget_item = infer_budget_item(work, transaction_record.memo) if work else None
    classification = LocalPaymentClassification(
        counterparty=counterparty,
        category=category,
        cost_center=cost_center,
        work=work,
        budget_item=budget_item,
        work_item_index=budget_item.index if budget_item else "",
        payment_method=payment_method,
        description=description,
        confidence=Decimal("0.85") if counterparty else Decimal("0.55"),
        needs_user_review_reason="Suggestion created from OFX. Review before approving.",
    )
    if should_use_ai_classification(classification, work_resolution, ai_classifier):
        classification = apply_ai_classification(
            classification,
            transaction_record,
            work_resolution,
            ai_classifier=ai_classifier,
        )
    if not classification.category:
        classification.category = ensure_other_expenses_category()
    if classification.work and not classification.cost_center:
        classification.cost_center = ensure_work_cost_center()
    if not classification.work and not classification.cost_center:
        classification.cost_center = ensure_company_cost_center()
    return classification


def infer_from_history(counterparty: Counterparty | None) -> dict:
    if not counterparty:
        return {}
    return {
        "category": most_common_payment_fk(counterparty, "category"),
        "cost_center": most_common_payment_fk(counterparty, "cost_center"),
        "work": most_common_payment_fk(counterparty, "work"),
        "payment_method": most_common_payment_value(counterparty, "payment_method"),
    }


def most_common_payment_fk(counterparty: Counterparty, field_name: str):
    id_field = f"{field_name}_id"
    row = (
        Payment.objects.filter(counterparty=counterparty)
        .exclude(status__in=[Payment.Status.CANCELED, Payment.Status.IGNORED, Payment.Status.ERROR])
        .exclude(**{id_field: None})
        .values(id_field)
        .annotate(total=Count("id"))
        .order_by("-total", id_field)
        .first()
    )
    if not row:
        return None
    models_by_field = {
        "category": Category,
        "cost_center": CostCenter,
        "work": Work,
    }
    return models_by_field[field_name].objects.filter(pk=row[id_field]).first()


def most_common_payment_value(counterparty: Counterparty, field_name: str) -> str:
    row = (
        Payment.objects.filter(counterparty=counterparty)
        .exclude(status__in=[Payment.Status.CANCELED, Payment.Status.IGNORED, Payment.Status.ERROR])
        .exclude(**{field_name: ""})
        .values(field_name)
        .annotate(total=Count("id"))
        .order_by("-total", field_name)
        .first()
    )
    return row[field_name] if row else ""


def should_use_ai_classification(
    classification: LocalPaymentClassification,
    work_resolution: WorkResolution,
    ai_classifier,
) -> bool:
    if not ai_classifier and not (
        settings.OPENAI_API_KEY and getattr(settings, "OPENAI_OFX_CLASSIFICATION_ENABLED", False)
    ):
        return False
    return any(
        [
            classification.counterparty is None,
            classification.category is None,
            not classification.payment_method,
            bool(work_resolution.candidate_name and not classification.work),
            classification.confidence < Decimal("0.70"),
        ]
    )


def apply_ai_classification(
    classification: LocalPaymentClassification,
    transaction_record: OfxTransaction,
    work_resolution: WorkResolution,
    *,
    ai_classifier=None,
) -> LocalPaymentClassification:
    classifier = ai_classifier or OpenAIOFXPaymentClassifier()
    try:
        ai_date = classifier.classify(transaction_record, classification)
    except OFXAIClassificationError:
        raise
    except Exception as exc:
        logger.warning(
            "OFX AI classification failed: %s",
            sanitize_log_payload(
                {
                    "transaction_id": transaction_record.pk,
                    "fitid": transaction_record.fitid,
                    "error_class": exc.__class__.__name__,
                    "error": str(exc),
                }
            ),
        )
        return classification

    classification.ai_classification = ai_date
    if ai_date.confidence < Decimal("0.60"):
        classification.needs_user_review_reason = (
            ai_date.needs_user_review_reason or "AI classification with low confidence. Review manually."
        )
        return classification

    if not classification.counterparty:
        ai_counterparty = resolve_ai_counterparty(ai_date)
        if ai_counterparty:
            classification.counterparty = ai_counterparty
            classification.category = classification.category or ai_counterparty.default_category
            classification.confidence = max(classification.confidence, min(ai_date.confidence, Decimal("0.80")))

    classification.category = classification.category or find_by_normalized(Category, ai_date.category)

    if not classification.work:
        ai_work = find_by_normalized(Work, ai_date.work)
        if ai_work:
            classification.work = ai_work
            classification.cost_center = ensure_work_cost_center()
            classification.budget_item = classification.budget_item or infer_budget_item(ai_work, transaction_record.memo)

    if not classification.cost_center:
        classification.cost_center = find_by_normalized(CostCenter, ai_date.cost_center)
    if classification.cost_center and normalize_text(classification.cost_center.name) == normalize_text("Company"):
        if not classification.work:
            classification.work_item_index = ""

    if not classification.work_item_index and classification.work:
        ai_budget_item = find_budget_item_by_index(classification.work, ai_date.work_item_index)
        if ai_budget_item:
            classification.budget_item = ai_budget_item
            classification.work_item_index = ai_budget_item.index
    if not classification.work_item_index and classification.budget_item:
        classification.work_item_index = classification.budget_item.index

    if not classification.payment_method and ai_date.payment_method:
        classification.payment_method = clean_name(ai_date.payment_method)
    if ai_date.description and classification.description == clean_description(transaction_record.memo):
        classification.description = clean_description(ai_date.description)
    if ai_date.needs_user_review_reason:
        classification.needs_user_review_reason = ai_date.needs_user_review_reason
    classification.confidence = max(classification.confidence, min(ai_date.confidence, Decimal("0.90")))
    return classification


def resolve_ai_counterparty(ai_date: OFXAIClassification) -> Counterparty | None:
    candidate = CounterpartyCandidate(
        name=clean_name(ai_date.counterparty_name),
        document=digits_only(ai_date.counterparty_document),
        alias=clean_name(ai_date.counterparty_name),
        source=Origin.AI,
    )
    try:
        return find_existing_counterparty(candidate)
    except AmbiguousCounterpartyError:
        return None


def find_by_normalized(model, value: str):
    normalized = normalize_text(value)
    if not normalized:
        return None
    return model.objects.filter(Q(normalized_name=normalized) | Q(name__iexact=value), is_active=True).first()


def build_payment_raw_payload(
    transaction_record: OfxTransaction,
    classification: LocalPaymentClassification,
    work_resolution: WorkResolution,
) -> dict:
    counterparty = classification.counterparty
    payload = {
        "ofx_transaction_id": transaction_record.pk,
        "ofx_file_id": transaction_record.ofx_file_id,
        "ofx_fitid": transaction_record.fitid,
        "ofx": {
            "transaction_id": transaction_record.pk,
            "ofx_file_id": transaction_record.ofx_file_id,
            "fitid": transaction_record.fitid,
            "posted_at": transaction_record.posted_at.isoformat(),
            "amount": str(transaction_record.amount),
            "memo": transaction_record.memo,
            "bank_id": transaction_record.ofx_file.bank_id,
            "account_id": transaction_record.ofx_file.account_id,
            "document_extracted": transaction_record.document_extracted,
            "name_extracted": transaction_record.name_extracted,
            "raw_payload": transaction_record.raw_payload or {},
        },
        "classification": {
            "source": "local+ia" if classification.ai_classification else "local",
            "confidence": str(classification.confidence),
            "needs_user_review_reason": classification.needs_user_review_reason,
        },
    }
    if classification.ai_classification:
        payload["ai_classification"] = classification.ai_classification.as_dict()
    if classification.budget_item:
        payload["budget_item_suggestion"] = {
            "id": classification.budget_item.pk,
            "work_id": classification.budget_item.work_id,
            "index": classification.budget_item.index,
            "description": classification.budget_item.description,
            "source": "local",
        }
    ai_date = classification.ai_classification
    candidate_name = clean_name(transaction_record.name_extracted)
    candidate_document = digits_only(transaction_record.document_extracted)
    candidate_source = Origin.OFX
    if not counterparty and ai_date and (ai_date.counterparty_name or ai_date.counterparty_document):
        candidate_name = candidate_name or clean_name(ai_date.counterparty_name)
        candidate_document = candidate_document or ai_date.counterparty_document
        candidate_source = Origin.AI
    if not counterparty and (candidate_name or candidate_document):
        payload["counterparty_candidate"] = {
            "name": candidate_name,
            "document": candidate_document,
            "alias": candidate_name,
            "category_name": "",
            "source": candidate_source,
        }
    work_candidate_name = work_resolution.candidate_name
    work_candidate_source = Origin.OFX
    if not classification.work and ai_date and ai_date.work:
        work_candidate_name = work_candidate_name or ai_date.work
        work_candidate_source = Origin.AI
    if work_candidate_name and not classification.work:
        payload["work_candidate"] = {
            "name": work_candidate_name,
            "source": work_candidate_source,
        }
    return payload


def infer_category(counterparty: Counterparty | None) -> Category | None:
    if not counterparty:
        return None
    if counterparty.default_category_id:
        return counterparty.default_category
    return (
        Category.objects.filter(payments__counterparty=counterparty, payments__category__isnull=False)
        .annotate(payment_count=Count("payments"))
        .order_by("-payment_count", "name")
        .first()
    )


def ensure_other_expenses_category() -> Category:
    category = Category.objects.filter(
        normalized_name__in=[normalize_text("Other Expenses"), normalize_text("Outras despesas")]
    ).first()
    if category is None:
        category = Category.objects.create(
            name="Other Expenses",
            normalized_name=normalize_text("Other Expenses"),
            is_active=True,
        )
    if not category.is_active:
        category.is_active = True
        category.save(update_fields=["is_active", "updated_at"])
    return category


def infer_budget_item(work: Work, text: str) -> BudgetItem | None:
    normalized_text = normalize_text(text)
    if not work or not normalized_text:
        return None
    scored = []
    for item in BudgetItem.objects.filter(work=work, is_active=True).exclude(description=""):
        score = budget_item_match_score(item, normalized_text)
        if score > 0:
            scored.append((score, item))
    if not scored:
        return None
    scored.sort(key=lambda row: (row[0], len(row[1].index)), reverse=True)
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None
    return scored[0][1] if scored[0][0] >= 2 else None


def budget_item_match_score(item: BudgetItem, normalized_text: str) -> int:
    normalized_description = normalize_text(item.description)
    if not normalized_description:
        return 0
    if normalized_description in normalized_text:
        return max(3, len(service_tokens(normalized_description)))
    tokens = service_tokens(normalized_description)
    if len(tokens) < 2:
        return 0
    matched = [token for token in tokens if re.search(rf"(^|\W){re.escape(token)}($|\W)", normalized_text)]
    coverage = Decimal(len(matched)) / Decimal(len(tokens))
    if len(matched) >= 2 and coverage >= Decimal("0.50"):
        return len(matched)
    return 0


def service_tokens(value: str) -> list[str]:
    stopwords = {
        "para",
        "por",
        "com",
        "sem",
        "das",
        "dos",
        "uma",
        "uns",
        "nas",
        "nos",
        "project",
        "servico",
        "servicos",
        "serviço",
        "serviços",
        "material",
        "materiais",
        "payment",
    }
    return [
        token
        for token in re.findall(r"[a-z0-9]+", normalize_text(value))
        if len(token) > 3 and token not in stopwords
    ]


def find_budget_item_by_index(work: Work, index: str) -> BudgetItem | None:
    if not work or not index:
        return None
    return BudgetItem.objects.filter(work=work, index=str(index).strip(), is_active=True).first()


def infer_payment_method(transaction_record: OfxTransaction) -> str:
    payment_method = extract_payment_method(transaction_record.memo)
    if payment_method:
        return payment_method
    transaction_type = normalize_text(transaction_record.transaction_type)
    if "debit" in transaction_type or "debito" in transaction_type:
        return "Debit"
    if "check" in transaction_type or "cheque" in transaction_type:
        return "Cheque"
    return ""


def resolve_work(transaction_record: OfxTransaction) -> WorkResolution:
    memo = transaction_record.memo or ""
    direct_matches = find_work_matches_in_text(memo)
    if len(direct_matches) == 1:
        return WorkResolution(work=direct_matches[0], cost_center=ensure_work_cost_center())
    if len(direct_matches) > 1:
        names = ", ".join(work.name for work in direct_matches)
        return WorkResolution(conflict=f"More than one project found in OFX: {names}.")

    candidate_name = extract_work_candidate_name(memo)
    if not candidate_name:
        return WorkResolution(cost_center=ensure_company_cost_center())

    candidate_matches = find_work_matches_by_candidate(candidate_name)
    if len(candidate_matches) == 1:
        return WorkResolution(work=candidate_matches[0], cost_center=ensure_work_cost_center())
    if len(candidate_matches) > 1:
        names = ", ".join(work.name for work in candidate_matches)
        return WorkResolution(candidate_name=candidate_name, conflict=f"Ambiguous project candidate: {names}.")
    return WorkResolution(cost_center=ensure_company_cost_center(), candidate_name=candidate_name)


def find_work_matches_in_text(text: str) -> list[Work]:
    normalized = normalize_text(text)
    matches: list[tuple[int, Work]] = []
    for work in Work.objects.filter(is_active=True):
        names = possible_work_names(work)
        for name in names:
            if name and re.search(rf"(^|\W){re.escape(name)}($|\W)", normalized):
                matches.append((len(name), work))
                break
    if not matches:
        return []
    max_length = max(length for length, _work in matches)
    return [work for length, work in matches if length == max_length]


def find_work_matches_by_candidate(candidate_name: str) -> list[Work]:
    normalized_candidate = normalize_text(candidate_name)
    if not normalized_candidate:
        return []
    exact_matches = []
    partial_matches = []
    for work in Work.objects.filter(is_active=True):
        names = possible_work_names(work)
        if normalized_candidate in names:
            exact_matches.append(work)
        elif any(normalized_candidate in name or name in normalized_candidate for name in names if name):
            partial_matches.append(work)
    return exact_matches or partial_matches


def possible_work_names(work: Work) -> list[str]:
    names = [normalize_text(work.name)]
    aliases = work.aliases or ""
    names.extend(normalize_text(alias) for alias in re.split(r"[,;\n]+", aliases) if alias.strip())
    return [name for name in names if name]


def extract_work_candidate_name(text: str) -> str:
    cleaned = " ".join(str(text or "").split())
    if not cleaned:
        return ""
    patterns = (
        r"\bobra\s+(?:de\s+|da\s+|do\s+|em\s+)?(?P<name>[^,;\n]+)",
        r"\bproject\s+(?:de\s+|da\s+|do\s+|em\s+)?(?P<name>[^,;\n]+)",
        r"(?P<name>[^,;\n]+)\s+project\b",
        r"(?P<name>[^,;\n]+)\s+obra\b",
    )
    for pattern in patterns:
        match = re.search(pattern, cleaned, flags=re.IGNORECASE)
        if match:
            candidate = cleanup_work_candidate_name(match.group("name"))
            if is_meaningful_work_candidate(candidate):
                return candidate
    return ""


def cleanup_work_candidate_name(value: str) -> str:
    value = re.sub(
        r"\b(?:amount|date|vendor|worker|beneficiario|beneficiário|destinatario|destinatário|"
        r"recebedor|category|cost center|forma|payment|item|subitem|indice|índice|cpf|cnpj|via)\b.*$",
        "",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(r"^(?:de|da|do|em)\s+", "", value, flags=re.IGNORECASE)
    return " ".join(value.strip(" .,:;-").split())


def is_meaningful_work_candidate(candidate: str) -> bool:
    normalized = normalize_text(candidate)
    if not normalized:
        return False
    return normalized not in {
        "calcada",
        "calçada",
        "material",
        "materiais",
        "servico",
        "servicos",
        "serviço",
        "serviços",
        "imposto",
        "taxa",
        "despesa",
        "despesas",
        "payment",
        "construcao",
        "construção",
    }


def ensure_company_cost_center() -> CostCenter:
    cost_center = CostCenter.objects.filter(
        normalized_name__in=[normalize_text("Company"), normalize_text("Empresa")]
    ).first()
    if cost_center is None:
        cost_center = CostCenter.objects.create(name="Company", normalized_name=normalize_text("Company"))
    return cost_center


def ensure_work_cost_center() -> CostCenter:
    cost_center = CostCenter.objects.filter(
        normalized_name__in=[normalize_text("Project"), normalize_text("Obra")]
    ).first()
    if cost_center is None:
        cost_center = CostCenter.objects.create(name="Project", normalized_name=normalize_text("Project"))
    return cost_center


def clean_description(value: str) -> str:
    return " ".join(str(value or "").split())[:200]


def clean_name(value: str) -> str:
    return " ".join(str(value or "").strip(" .,:;-").split())


def money_abs(value) -> Decimal:
    return Decimal(value or 0).copy_abs().quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
