from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from io import BytesIO
import re

from pypdf import PdfReader

from apps.counterparties.importers import digits_only, normalize_text
from apps.counterparties.matching import pick_counterparty_candidate
from apps.counterparties.models import Counterparty, CounterpartyAlias
from apps.documents.models import UploadedFile

from .defaults import apply_cost_center_default
from .models import Payment


COUNTERPARTY_LABELS = (
    "nome do destinatario",
    "name do destinatario",
    "destinatario",
    "nome do recebedor",
    "name do recebedor",
    "recebedor",
    "nome do beneficiario",
    "name do beneficiario",
    "beneficiario",
    "beneficiario final",
    "dados do beneficiario",
    "favorecido",
    "pago a",
    "para",
)
NON_COUNTERPARTY_LABELS = (
    "solicitante",
    "nome do pagador",
    "name do pagador",
    "pagador",
    "conta origem",
    "origem",
    "payer",
    "emitido por",
)


@dataclass(frozen=True)
class ExtractedPaymentDate:
    source_kind: str
    raw_text: str = ""
    amount: Decimal | None = None
    payment_date: date | None = None
    counterparty: Counterparty | None = None
    counterparty_candidate_name: str = ""
    counterparty_candidate_document: str = ""
    payment_method: str = ""
    description: str = ""
    needs_ai: bool = False
    confidence: Decimal = Decimal("0.00")
    errors: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "source_kind": self.source_kind,
            "raw_text": self.raw_text,
            "amount": str(self.amount) if self.amount is not None else None,
            "payment_date": self.payment_date.isoformat() if self.payment_date else None,
            "counterparty_id": self.counterparty_id,
            "counterparty_name": self.counterparty.name if self.counterparty else "",
            "counterparty_candidate_name": self.counterparty_candidate_name,
            "counterparty_candidate_document": self.counterparty_candidate_document,
            "payment_method": self.payment_method,
            "description": self.description,
            "needs_ai": self.needs_ai,
            "confidence": str(self.confidence),
            "errors": list(self.errors),
        }

    @property
    def counterparty_id(self) -> int | None:
        return self.counterparty.pk if self.counterparty else None


def extract_from_uploaded_file(uploaded_file: UploadedFile) -> ExtractedPaymentDate:
    if uploaded_file.kind == UploadedFile.Kind.TEXT:
        text = uploaded_file.extracted_text
        if not text and uploaded_file.file:
            text = uploaded_file.file.read().decode("utf-8", errors="ignore")
        return extract_from_text(text, source_kind=uploaded_file.kind)

    if uploaded_file.kind == UploadedFile.Kind.PDF:
        try:
            content = uploaded_file.file.read()
            text = extract_text_from_pdf_bytes(content)
        except Exception as exc:  # pypdf can fail on partial or scanned files.
            return ExtractedPaymentDate(
                source_kind=uploaded_file.kind,
                needs_ai=True,
                errors=(f"pdf_text_extraction_failed: {exc.__class__.__name__}",),
            )
        if not text.strip():
            return ExtractedPaymentDate(source_kind=uploaded_file.kind, needs_ai=True)
        uploaded_file.extracted_text = text
        uploaded_file.save(update_fields=["extracted_text", "updated_at"])
        return extract_from_text(text, source_kind=uploaded_file.kind)

    if uploaded_file.kind == UploadedFile.Kind.IMAGE:
        return ExtractedPaymentDate(source_kind=uploaded_file.kind, needs_ai=True)

    return ExtractedPaymentDate(source_kind=uploaded_file.kind, needs_ai=True)


def extract_from_text(text: str, source_kind: str = UploadedFile.Kind.TEXT) -> ExtractedPaymentDate:
    cleaned = clean_text(text)
    amount = extract_amount(cleaned)
    payment_date = extract_date(cleaned)
    payment_method = extract_payment_method(cleaned)
    counterparty = match_counterparty(cleaned)
    counterparty_candidate_name = "" if counterparty else extract_labeled_counterparty_name(cleaned)
    counterparty_candidate_document = "" if counterparty else extract_labeled_counterparty_document(cleaned)
    confidence = calculate_confidence(amount, payment_date, payment_method, counterparty)
    return ExtractedPaymentDate(
        source_kind=source_kind,
        raw_text=cleaned,
        amount=amount,
        payment_date=payment_date,
        counterparty=counterparty,
        counterparty_candidate_name=counterparty_candidate_name,
        counterparty_candidate_document=counterparty_candidate_document,
        payment_method=payment_method,
        description=cleaned[:200],
        needs_ai=False,
        confidence=confidence,
    )


def apply_extraction_to_payment(payment: Payment, extraction: ExtractedPaymentDate) -> Payment:
    if extraction.amount is not None and extraction.amount >= 0:
        payment.amount = extraction.amount
    if extraction.payment_date:
        payment.payment_date = extraction.payment_date
        payment.competence_date = payment.competence_date or extraction.payment_date
        payment.due_date = payment.due_date or extraction.payment_date
    if extraction.counterparty:
        payment.counterparty = extraction.counterparty
        payment.category = payment.category or extraction.counterparty.default_category
        payment.chart_account = payment.chart_account or extraction.counterparty.default_chart_account
    if extraction.payment_method:
        payment.payment_method = extraction.payment_method
    if extraction.description:
        payment.description = extraction.description[:255]
    apply_cost_center_default(payment)
    payment.confidence = extraction.confidence
    payment.needs_review = True
    payment.review_reason = (
        "Initial extraction completed. Awaiting confirmation."
        if not extraction.needs_ai
        else "File has no searchable text. Awaiting AI extraction."
    )
    payload = payment.raw_payload or {}
    payload["initial_extraction"] = extraction.as_dict()
    if not payment.counterparty_id and (extraction.counterparty_candidate_name or extraction.counterparty_candidate_document):
        payload["counterparty_candidate"] = {
            "name": extraction.counterparty_candidate_name,
            "document": extraction.counterparty_candidate_document,
            "alias": extraction.counterparty_candidate_name,
            "category_name": "",
            "source": "telegram",
        }
    payment.raw_payload = payload
    payment.save()
    return payment


def extract_text_from_pdf_bytes(content: bytes) -> str:
    reader = PdfReader(BytesIO(content))
    pages = []
    for page in reader.pages:
        pages.append(page.extract_text() or "")
    return clean_text("\n".join(pages))


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def extract_amount(text: str) -> Decimal | None:
    patterns = [
        r"R\$\s*([0-9]{1,3}(?:\.[0-9]{3})*,[0-9]{2}|[0-9]+(?:[,.][0-9]{2})?)",
        r"\b(?:amount|paguei|payment|pago|total)\s+(?:de\s+)?([0-9]{1,3}(?:\.[0-9]{3})*,[0-9]{2}|[0-9]+(?:[,.][0-9]{2})?)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return parse_decimal(match.group(1))
    return None


def parse_decimal(value: str) -> Decimal | None:
    number = value.strip()
    if "," in number:
        number = number.replace(".", "").replace(",", ".")
    try:
        return Decimal(number).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None


def extract_date(text: str) -> date | None:
    match = re.search(r"\b([0-3]?\d)[/-]([01]?\d)[/-]((?:20)?\d{2})\b", text)
    if not match:
        return None
    day, month, year = match.groups()
    if len(year) == 2:
        year = f"20{year}"
    try:
        return datetime(int(year), int(month), int(day)).date()
    except ValueError:
        return None


def extract_payment_method(text: str) -> str:
    normalized = normalize_text(text)
    method_keywords = (
        ("pix", "PIX"),
        ("transferencia", "Transfer"),
        ("ted", "TED"),
        ("boleto", "Bank slip"),
        ("dinheiro", "Cash"),
        ("cartao", "Card"),
        ("credito", "Card"),
        ("debito", "Card"),
    )
    for keyword, label in method_keywords:
        if keyword in normalized:
            return label
    return ""


def match_counterparty(text: str) -> Counterparty | None:
    normalized_text = normalize_text(text)
    explicit_counterparty = match_labeled_counterparty(normalized_text)
    if explicit_counterparty:
        return explicit_counterparty

    candidates = []
    for counterparty in Counterparty.objects.filter(is_active=True):
        for name in searchable_counterparty_names(counterparty):
            if is_valid_counterparty_occurrence(normalized_text, name):
                candidates.append((len(name), counterparty))
                break
    for alias in CounterpartyAlias.objects.select_related("counterparty").filter(counterparty__is_active=True):
        if is_valid_counterparty_occurrence(normalized_text, alias.normalized_name):
            candidates.append((len(alias.normalized_name), alias.counterparty))
    return pick_counterparty_candidate(candidates)


def match_labeled_counterparty(normalized_text: str) -> Counterparty | None:
    candidates = []
    for counterparty in Counterparty.objects.filter(is_active=True):
        for name in searchable_counterparty_names(counterparty):
            if appears_after_any_label(normalized_text, name, COUNTERPARTY_LABELS):
                candidates.append((len(name), counterparty))
                break
    for alias in CounterpartyAlias.objects.select_related("counterparty").filter(counterparty__is_active=True):
        if appears_after_any_label(normalized_text, alias.normalized_name, COUNTERPARTY_LABELS):
            candidates.append((len(alias.normalized_name), alias.counterparty))
    return pick_counterparty_candidate(candidates)


def searchable_counterparty_names(counterparty: Counterparty) -> tuple[str, ...]:
    names = {
        normalize_text(counterparty.normalized_name),
        normalize_text(counterparty.name),
    }
    return tuple(name for name in names if name)


def extract_labeled_counterparty_name(text: str) -> str:
    labels = (
        "nome do destinatário",
        "nome do destinatario",
        "name do destinatário",
        "name do destinatario",
        "destinatário",
        "destinatario",
        "nome do recebedor",
        "name do recebedor",
        "recebedor",
        "nome do beneficiário",
        "nome do beneficiario",
        "name do beneficiário",
        "name do beneficiario",
        "beneficiário",
        "beneficiario",
        "favorecido",
        "pago a",
    )
    alternatives = "|".join(re.escape(label) for label in labels)
    match = re.search(rf"(?:{alternatives})\s*(?:é|e|:|-)?\s*([^,;\n]+)", text, flags=re.IGNORECASE)
    if not match:
        return ""
    return cleanup_counterparty_candidate_name(match.group(1))


def extract_labeled_counterparty_document(text: str) -> str:
    match = re.search(
        r"\b(?:cpf|cnpj|cpf/cnpj)\s+(?:do|da)\s+"
        r"(?:destinatário|destinatario|beneficiário|beneficiario|recebedor|favorecido)"
        r"\s*(?:é|e|:|-)?\s*([0-9.*\-\/ ]{11,24})",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return ""
    document = digits_only(match.group(1))
    if len(document) in {11, 14}:
        return document
    return ""


def cleanup_counterparty_candidate_name(value: str) -> str:
    value = re.sub(
        r"\b(?:cpf|cnpj|cpf/cnpj|documento|institui(?:ç|c)[ãa]o|ag(?:ê|e)ncia|conta|"
        r"nome do pagador|name do pagador|cnpj do pagador|cpf do pagador|id da transa(?:ç|c)[ãa]o|"
        r"autentica(?:ç|c)[ãa]o|n[úu]mero de controle|emitido em|solicitante|cooperativa)\b.*$",
        "",
        value,
        flags=re.IGNORECASE,
    )
    return " ".join(value.strip(" .,:;-").split())


def is_valid_counterparty_occurrence(normalized_text: str, normalized_name: str) -> bool:
    if not normalized_name or normalized_name not in normalized_text:
        return False
    if appears_after_any_label(normalized_text, normalized_name, COUNTERPARTY_LABELS):
        return True
    return not appears_after_any_label(normalized_text, normalized_name, NON_COUNTERPARTY_LABELS)


def appears_after_any_label(normalized_text: str, normalized_name: str, labels: tuple[str, ...]) -> bool:
    if not normalized_name:
        return False
    for match in re.finditer(re.escape(normalized_name), normalized_text):
        before = normalized_text[max(0, match.start() - 80) : match.start()]
        if any(label in before for label in labels):
            return True
    return False


def calculate_confidence(amount, payment_date, payment_method, counterparty) -> Decimal:
    score = Decimal("0.10")
    if amount is not None:
        score += Decimal("0.30")
    if payment_date:
        score += Decimal("0.20")
    if payment_method:
        score += Decimal("0.15")
    if counterparty:
        score += Decimal("0.25")
    return min(score, Decimal("1.00"))
