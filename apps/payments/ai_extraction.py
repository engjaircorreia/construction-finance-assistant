from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import base64
import json
import logging

from django.conf import settings
from django.db.models import Q
from openai import OpenAI

from apps.accounts.models import AuthorizedTelegramUser
from apps.core.log_safety import sanitize_log_payload
from apps.counterparties.matching import (
    context_counterparties,
    counterparty_rank,
    find_best_counterparty_by_name,
)
from apps.counterparties.models import BudgetItem, Category, CostCenter, Counterparty, Work
from apps.documents.models import UploadedFile

from .defaults import apply_cost_center_default
from .extraction import COUNTERPARTY_LABELS, NON_COUNTERPARTY_LABELS, appears_after_any_label
from .models import Payment


logger = logging.getLogger(__name__)


class AIExtractionError(Exception):
    pass


class AIExtractionValidationError(AIExtractionError):
    pass


class OpenAIConfigurationError(AIExtractionError):
    pass


@dataclass(frozen=True)
class AIPaymentExtraction:
    amount: Decimal | None
    payment_date: date | None
    counterparty_name: str
    counterparty_id: int | None
    counterparty_document: str
    document_number: str
    payment_method: str
    description: str
    category_name: str
    cost_center_name: str
    work_name: str
    work_item_index: str
    confidence: Decimal
    needs_review: bool
    notes: str

    def as_dict(self) -> dict:
        return {
            "amount": str(self.amount) if self.amount is not None else None,
            "payment_date": self.payment_date.isoformat() if self.payment_date else None,
            "counterparty_name": self.counterparty_name,
            "counterparty_id": self.counterparty_id,
            "counterparty_document": self.counterparty_document,
            "document_number": self.document_number,
            "payment_method": self.payment_method,
            "description": self.description,
            "category_name": self.category_name,
            "cost_center_name": self.cost_center_name,
            "work_name": self.work_name,
            "work_item_index": self.work_item_index,
            "confidence": str(self.confidence),
            "needs_review": self.needs_review,
            "notes": self.notes,
        }


AI_EXTRACTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "amount",
        "payment_date",
        "counterparty_name",
        "counterparty_id",
        "counterparty_document",
        "document_number",
        "payment_method",
        "description",
        "category_name",
        "cost_center_name",
        "work_name",
        "work_item_index",
        "confidence",
        "needs_review",
        "notes",
    ],
    "properties": {
        "amount": {"type": ["number", "null"]},
        "payment_date": {"type": ["string", "null"], "description": "Date ISO YYYY-MM-DD."},
        "counterparty_name": {"type": "string"},
        "counterparty_id": {"type": ["integer", "null"]},
        "counterparty_document": {"type": "string", "description": "CPF/CNPJ da counterparty, se visível."},
        "document_number": {"type": "string"},
        "payment_method": {"type": "string"},
        "description": {"type": "string"},
        "category_name": {"type": "string"},
        "cost_center_name": {"type": "string"},
        "work_name": {"type": "string"},
        "work_item_index": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "needs_review": {"type": "boolean"},
        "notes": {"type": "string"},
    },
}


class OpenAIPaymentExtractor:
    def __init__(self, client=None, model: str | None = None, timeout: float | None = None):
        self.client = client
        self.model = model or settings.OPENAI_MODEL
        self.timeout = timeout if timeout is not None else settings.OPENAI_REQUEST_TIMEOUT_SECONDS

    def extract(self, payment: Payment) -> AIPaymentExtraction:
        if self.client is None:
            if not settings.OPENAI_API_KEY:
                raise OpenAIConfigurationError("OPENAI_API_KEY is not configured.")
            self.client = OpenAI(api_key=settings.OPENAI_API_KEY, timeout=self.timeout)

        try:
            response = self.client.responses.create(
                model=self.model,
                store=False,
                input=build_openai_input(payment),
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "payment_extraction",
                        "schema": AI_EXTRACTION_SCHEMA,
                        "strict": True,
                    }
                },
            )
        except Exception as exc:
            logger.warning(
                "OpenAI extraction failed: %s",
                sanitize_log_payload(
                    {
                        "payment_id": payment.pk,
                        "uploaded_file_id": payment.uploaded_file_id,
                        "error_class": exc.__class__.__name__,
                        "error": str(exc),
                    }
                ),
            )
            raise AIExtractionError(f"OpenAI call failed: {exc.__class__.__name__}") from exc
        return parse_ai_extraction_response(response.output_text)


def build_openai_input(payment: Payment) -> list[dict]:
    uploaded_file = payment.uploaded_file
    context = build_minimal_context()
    submission_context = build_submission_context(uploaded_file)
    user_content = [
        {
            "type": "input_text",
            "text": (
                "Interpret the receipt or text below and return only the schema fields. "
                "Do not approve payments. When in doubt, use needs_review=true.\n\n"
                "Rules to identify the vendor/worker:\n"
                "- The payment counterparty is the person or entity that received the money or provided/sold the service.\n"
                "- In Pix/bank receipts, prioritize fields such as nome do destinatario, recebedor, "
                "beneficiario, favorecido, or pago a.\n"
                "- Do not use names from requester, payer, source account, account holder, issued by, "
                "or Telegram user fields as the vendor/worker.\n"
                "- If requester/payer and recipient/receiver both appear, use recipient/receiver.\n"
                "- If the receiver is not registered, fill counterparty_name/document with the visible receiver "
                "and leave counterparty_id=null.\n\n"
                "Cost center rules:\n"
                "- If a specific project is identified, use Project cost center and fill work_name.\n"
                "- If no specific project is identified, use Company cost center.\n"
                "- Taxes, fees, loans, financing, and other general expenses stay under Company "
                "when no explicit project appears.\n\n"
                f"Submission context:\n{json.dumps(submission_context, ensure_ascii=False)}\n\n"
                f"Additional draft text:\n{safe_text((payment.raw_payload or {}).get('draft_text_content', ''))}\n\n"
                f"Minimum master-data context:\n{json.dumps(context, ensure_ascii=False)}\n\n"
                f"Current extracted text:\n{safe_text(uploaded_file.extracted_text if uploaded_file else '')}"
            ),
        }
    ]
    if uploaded_file and uploaded_file.file and uploaded_file.kind in {UploadedFile.Kind.PDF, UploadedFile.Kind.IMAGE}:
        user_content.append(build_file_content(uploaded_file))
    return [
        {
            "role": "developer",
            "content": [
                {
                    "type": "input_text",
                    "text": (
                        "You extract project expense data for human review. "
                        "This call must be fast for Telegram interaction; reason directly "
                        "and do not prolong analysis when the main fields are clear. "
                        "Use only data visible in the file/text and the minimum master-data context. "
                        "The counterparty is the receiver/beneficiary/recipient of the payment, never the requester, "
                        "payer, source-account holder, or Telegram sender. "
                        "For step/item index, use the budget-item list: "
                        "if the service mentioned is generic, choose the substep; "
                        "if the service is detailed, choose the specific item. "
                        "Do not use a vendor default project when the message does not mention a specific project; "
                        "in that case, the cost center is Company. "
                        "Do not invent CPF/CNPJ, vendor, project, or category. "
                        "Do not include secrets, tokens, or environment variables."
                    ),
                }
            ],
        },
        {"role": "user", "content": user_content},
    ]


def build_file_content(uploaded_file: UploadedFile) -> dict:
    uploaded_file.file.open("rb")
    try:
        encoded = base64.b64encode(uploaded_file.file.read()).decode("ascii")
    finally:
        uploaded_file.file.close()
    if uploaded_file.kind == UploadedFile.Kind.IMAGE:
        return {
            "type": "input_image",
            "image_url": f"data:{uploaded_file.content_type or 'image/jpeg'};base64,{encoded}",
        }
    return {
        "type": "input_file",
        "filename": uploaded_file.original_filename or "receipt.pdf",
        "file_data": f"data:{uploaded_file.content_type or 'application/pdf'};base64,{encoded}",
    }


def build_minimal_context(limit: int = 60) -> dict:
    counterparties = []
    for counterparty in context_counterparties(limit):
        counterparties.append(
            {
                "id": counterparty.pk,
                "name": counterparty.name,
                "kind": counterparty.kind,
                "default_category": counterparty.default_category.name if counterparty.default_category else "",
                "default_cost_center": counterparty.default_cost_center.name if counterparty.default_cost_center else "",
                "default_work": counterparty.default_work.name if counterparty.default_work else "",
            }
        )
    budget_items = []
    for item in BudgetItem.objects.filter(is_active=True).select_related("work").order_by(
        "work__name",
        "index",
    )[:500]:
        budget_items.append(
            {
                "work": item.work.name,
                "index": item.index,
                "parent_index": item.parent_index,
                "type": item.item_type,
                "description": item.description[:220],
            }
        )
    return {
        "counterparties": counterparties,
        "launchers_to_ignore": list(
            AuthorizedTelegramUser.objects.filter(is_active=True).values_list("name", flat=True)[:limit]
        ),
        "categories": list(Category.objects.filter(is_active=True).values_list("name", flat=True)[:limit]),
        "cost_centers": list(CostCenter.objects.filter(is_active=True).values_list("name", flat=True)[:limit]),
        "works": list(Work.objects.filter(is_active=True).values_list("name", flat=True)[:limit]),
        "budget_items": budget_items,
    }


def build_submission_context(uploaded_file: UploadedFile | None) -> dict:
    if not uploaded_file:
        return {}
    return {
        "source": uploaded_file.source,
        "kind": uploaded_file.kind,
        "telegram_user_id": uploaded_file.telegram_user_id,
        "sender_note": uploaded_file.notes,
        "instruction": "sender_note identifies who sent/entered the data; it is not the vendor/worker.",
    }


def parse_ai_extraction_response(content: str) -> AIPaymentExtraction:
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise AIExtractionValidationError("AI response is not valid JSON.") from exc
    missing = set(AI_EXTRACTION_SCHEMA["required"]) - set(data)
    if missing:
        raise AIExtractionValidationError(f"AI response is missing required fields: {sorted(missing)}")
    confidence = parse_confidence(data["confidence"])
    return AIPaymentExtraction(
        amount=parse_amount(data["amount"]),
        payment_date=parse_date(data["payment_date"]),
        counterparty_name=parse_string(data["counterparty_name"]),
        counterparty_id=parse_optional_int(data["counterparty_id"]),
        counterparty_document=parse_string(data["counterparty_document"]),
        document_number=parse_string(data["document_number"]),
        payment_method=parse_string(data["payment_method"]),
        description=parse_string(data["description"])[:255],
        category_name=parse_string(data["category_name"]),
        cost_center_name=parse_string(data["cost_center_name"]),
        work_name=parse_string(data["work_name"]),
        work_item_index=parse_string(data["work_item_index"]),
        confidence=confidence,
        needs_review=parse_bool(data["needs_review"]),
        notes=parse_string(data["notes"]),
    )


def apply_ai_extraction_to_payment(payment: Payment, extraction: AIPaymentExtraction) -> Payment:
    if extraction.amount is not None and extraction.amount >= 0:
        payment.amount = extraction.amount
    if extraction.payment_date:
        payment.payment_date = extraction.payment_date
        payment.competence_date = payment.competence_date or extraction.payment_date
        payment.due_date = payment.due_date or extraction.payment_date
    counterparty = find_counterparty(extraction)
    if counterparty and should_ignore_counterparty_from_text(payment, counterparty.name):
        counterparty = None
    if counterparty:
        payment.counterparty = counterparty
        payment.category = payment.category or counterparty.default_category
    payment.category = payment.category or find_by_name(Category, extraction.category_name)
    payment.cost_center = payment.cost_center or find_by_name(CostCenter, extraction.cost_center_name)
    payment.work = payment.work or find_by_name(Work, extraction.work_name)
    if extraction.document_number:
        payment.document_number = extraction.document_number
    if extraction.payment_method:
        payment.payment_method = extraction.payment_method
    if extraction.description:
        payment.description = extraction.description[:255]
    if extraction.work_item_index:
        payment.work_item_index = extraction.work_item_index
    apply_cost_center_default(payment)
    payment.confidence = extraction.confidence
    payment.needs_review = True
    payment.review_reason = "AI extraction completed. Awaiting confirmation."
    payload = payment.raw_payload or {}
    payload["ai_extraction"] = extraction.as_dict()
    if (
        not payment.counterparty_id
        and (extraction.counterparty_name or extraction.counterparty_document)
        and not should_ignore_counterparty_from_text(payment, extraction.counterparty_name)
    ):
        payload["counterparty_candidate"] = {
            "name": extraction.counterparty_name,
            "document": extraction.counterparty_document,
            "alias": extraction.counterparty_name,
            "category_name": extraction.category_name,
            "source": "ia",
        }
    payment.raw_payload = payload
    if payment.status == Payment.Status.APPROVED:
        payment.status = Payment.Status.PENDING_CONFIRMATION
    payment.save()
    return payment


def should_ignore_counterparty_from_text(payment: Payment, counterparty_name: str) -> bool:
    uploaded_file = payment.uploaded_file
    if not uploaded_file or not uploaded_file.extracted_text or not counterparty_name:
        return False
    normalized_text = normalize_for_lookup(uploaded_file.extracted_text)
    normalized_name = normalize_for_lookup(counterparty_name)
    if appears_after_any_label(normalized_text, normalized_name, COUNTERPARTY_LABELS):
        return False
    return appears_after_any_label(normalized_text, normalized_name, NON_COUNTERPARTY_LABELS)


def find_counterparty(extraction: AIPaymentExtraction) -> Counterparty | None:
    counterparty_by_name = find_best_counterparty_by_name(extraction.counterparty_name)
    if extraction.counterparty_id:
        counterparty = Counterparty.objects.filter(pk=extraction.counterparty_id, is_active=True).first()
        if counterparty:
            if (
                counterparty_by_name
                and counterparty_by_name.pk != counterparty.pk
                and counterparty_by_name.normalized_name == counterparty.normalized_name
                and counterparty_rank(counterparty_by_name) > counterparty_rank(counterparty)
            ):
                return counterparty_by_name
            return counterparty
    return counterparty_by_name


def find_by_name(model, name: str):
    normalized = normalize_for_lookup(name)
    if not normalized:
        return None
    return model.objects.filter(Q(normalized_name=normalized) | Q(name__iexact=name)).first()


def parse_amount(value) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        raise AIExtractionValidationError("Invalid amount field.")


def parse_date(value) -> date | None:
    if value in {None, ""}:
        return None
    if not isinstance(value, str):
        raise AIExtractionValidationError("Campo payment_date deve ser string ou null.")
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise AIExtractionValidationError("Invalid payment_date field.") from exc


def parse_confidence(value) -> Decimal:
    try:
        confidence = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        raise AIExtractionValidationError("Invalid confidence field.")
    if confidence < 0 or confidence > 1:
        raise AIExtractionValidationError("Campo confidence fora do intervalo 0..1.")
    return confidence


def parse_string(value) -> str:
    if not isinstance(value, str):
        raise AIExtractionValidationError("Invalid text field.")
    return value.strip()


def parse_optional_int(value) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int):
        raise AIExtractionValidationError("counterparty_id deve ser inteiro ou null.")
    return value


def parse_bool(value) -> bool:
    if not isinstance(value, bool):
        raise AIExtractionValidationError("needs_review deve ser booleano.")
    return value


def normalize_for_lookup(value: str) -> str:
    from apps.counterparties.importers import normalize_text

    return normalize_text(value)


def safe_text(text: str, limit: int = 4000) -> str:
    return str(text or "")[:limit]
