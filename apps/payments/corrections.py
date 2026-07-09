from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
import re

from django.db import transaction

from apps.counterparties.importers import digits_only, normalize_text
from apps.counterparties.matching import choose_best_counterparty
from apps.counterparties.models import BudgetItem, Category, CostCenter, Counterparty, CounterpartyAlias, Work

from .counterparty_resolution import (
    CounterpartyCandidate,
    apply_counterparty_defaults,
    find_existing_counterparty,
    persist_candidate,
)
from .defaults import apply_cost_center_default
from .extraction import extract_amount, extract_date, extract_payment_method
from .models import Payment


class PaymentCorrectionError(Exception):
    pass


@dataclass(frozen=True)
class PaymentCorrectionResult:
    payment: Payment
    changed_fields: list[str] = field(default_factory=list)
    message: str = ""


def apply_text_correction_to_payment(payment_id: int, text: str, telegram_user_id: int | None = None) -> PaymentCorrectionResult:
    cleaned = " ".join(str(text or "").split())
    if not cleaned:
        raise PaymentCorrectionError("Send the correction text.")

    with transaction.atomic():
        payment = Payment.objects.select_for_update(of=("self",)).select_related(
            "counterparty",
            "category",
            "cost_center",
            "work",
        ).get(pk=payment_id)
        if payment.status not in {Payment.Status.CORRECTING, Payment.Status.PENDING_REGISTRATION, Payment.Status.RECEIVED}:
            raise PaymentCorrectionError("This payment is not waiting for text correction.")

        changed = []
        updates = parse_payment_correction(cleaned, payment)
        if "amount" in updates and payment.amount != updates["amount"]:
            payment.amount = updates["amount"]
            changed.append("amount")
        if "payment_date" in updates and payment.payment_date != updates["payment_date"]:
            payment.payment_date = updates["payment_date"]
            payment.competence_date = payment.competence_date or updates["payment_date"]
            payment.due_date = payment.due_date or updates["payment_date"]
            changed.append("data")
        if "payment_method" in updates and payment.payment_method != updates["payment_method"]:
            payment.payment_method = updates["payment_method"]
            changed.append("payment method")
        if "description" in updates and payment.description != updates["description"]:
            payment.description = updates["description"][:255]
            changed.append("description")
        if "category" in updates and payment.category_id != updates["category"].pk:
            payment.category = updates["category"]
            changed.append("category")
        if "cost_center" in updates and payment.cost_center_id != updates["cost_center"].pk:
            payment.cost_center = updates["cost_center"]
            changed.append("cost center")
        if "work" in updates and payment.work_id != updates["work"].pk:
            payment.work = updates["work"]
            changed.append("project")
        if "work_item_index" in updates and payment.work_item_index != updates["work_item_index"]:
            payment.work_item_index = updates["work_item_index"]
            changed.append("budget item index")
        if "counterparty" in updates and payment.counterparty_id != updates["counterparty"].pk:
            apply_counterparty_defaults(payment, updates["counterparty"])
            changed.append("vendor/worker")
        elif "counterparty_candidate" in updates:
            payment.counterparty = None
            persist_candidate(payment, updates["counterparty_candidate"])
            changed.append("suggested vendor/worker")
        apply_cost_center_default(payment)

        payload = payment.raw_payload or {}
        corrections = payload.setdefault("text_corrections", [])
        corrections.append(
            {
                "telegram_user_id": telegram_user_id,
                "text": cleaned,
                "changed_fields": changed,
            }
        )
        payment.raw_payload = payload

        if payment.counterparty_id:
            payment.status = Payment.Status.PENDING_CONFIRMATION
            payment.review_reason = "Correction applied through Telegram. Awaiting new confirmation."
        else:
            payment.status = Payment.Status.PENDING_REGISTRATION
            payment.review_reason = "Correction applied through Telegram. Confirm or register the counterparty."
        payment.needs_review = True
        payment.confirmed_at = None
        payment.confirmed_by = None
        payment.user_action = ""
        payment.save()

    message = (
        "Correction applied. Review the new suggestion."
        if changed
        else "I received the correction, but did not identify fields to change. Review the suggestion."
    )
    return PaymentCorrectionResult(payment=payment, changed_fields=changed, message=message)


def parse_payment_correction(text: str, payment: Payment) -> dict:
    updates = {}
    amount = extract_amount(text)
    if amount is not None:
        updates["amount"] = amount
    payment_date = extract_date(text)
    if payment_date:
        updates["payment_date"] = payment_date
    payment_method = extract_payment_method(text)
    if payment_method:
        updates["payment_method"] = payment_method

    description = extract_labeled_value(text, ("description", "descricao", "histórico", "historico"))
    if description:
        updates["description"] = description

    category = match_named_model(Category, text)
    if not category and mentions_worker(text):
        category = match_default_worker_category()
    if category:
        updates["category"] = category
    cost_center = match_named_model(CostCenter, text)
    if cost_center:
        updates["cost_center"] = cost_center
    work = match_named_model(Work, text)
    if work:
        updates["work"] = work

    work_for_item = updates.get("work") or payment.work
    work_item_index = extract_work_item_index(text, work_for_item)
    if work_item_index:
        updates["work_item_index"] = work_item_index

    counterparty_update = extract_counterparty_update(text)
    if counterparty_update:
        updates.update(counterparty_update)
    return updates


def extract_counterparty_update(text: str) -> dict:
    kind_hint = detect_counterparty_kind(text)
    name = extract_labeled_value(
        text,
        (
            "vendor correto",
            "vendor",
            "worker correto",
            "worker",
            "beneficiário",
            "beneficiario",
            "destinatário",
            "destinatario",
            "recebedor",
            "favorecido",
            "pago a",
        ),
    )
    if not name:
        match = re.search(r"\b(?:no é|nao e)\s+.+?\b(?:é|e)\s+(.+)$", text, flags=re.IGNORECASE)
        if match:
            name = match.group(1)
    name = cleanup_extracted_name(name)
    document = extract_labeled_document(text)
    if not name and not document:
        return {}

    candidate = CounterpartyCandidate(name=name, document=document, alias=name, source="telegram")
    existing = find_existing_counterparty(candidate)
    if not existing and name:
        existing = match_counterparty_by_partial_name(name, kind_hint=kind_hint)
    if existing:
        return {"counterparty": existing}
    return {"counterparty_candidate": candidate}


def extract_work_item_index(text: str, work: Work | None) -> str:
    normalized_text = normalize_text(text)
    match = re.search(r"\b(?:índice|indice|item|subitem)\s*(?:é|e|:)?\s*(\d+(?:\.\d+)+|\d+)\b", text, flags=re.IGNORECASE)
    if match:
        candidate = match.group(1)
        if is_projected_street_number(normalized_text, candidate):
            return ""
        return candidate
    match = re.search(r"\b(\d+(?:\.\d+)+)\b", text)
    if match:
        return match.group(1)
    if not work:
        return ""

    candidates = []
    for item in BudgetItem.objects.filter(work=work, is_active=True):
        tokens = meaningful_tokens(item.normalized_description)
        if len(tokens) >= 2 and all(token in normalized_text for token in tokens[:4]):
            candidates.append((len(tokens), item.index))
    if not candidates:
        return ""
    candidates.sort(reverse=True)
    return candidates[0][1]


def is_projected_street_number(normalized_text: str, candidate: str) -> bool:
    if "." in candidate or not candidate.isdigit():
        return False
    return bool(re.search(rf"\brua\s+projetada\s+0*{int(candidate)}\b", normalized_text))


def extract_labeled_document(text: str) -> str:
    match = re.search(
        r"\b(?:cpf|cnpj|cpf/cnpj|documento)\s*(?:é|e|:)?\s*([0-9.\-\/ ]{11,22})",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return ""
    document = digits_only(match.group(1))
    if len(document) in {11, 14}:
        return document
    return ""


def match_named_model(model, text: str):
    normalized = normalize_text(text)
    matches = []
    for record in model.objects.filter(is_active=True):
        for record_name in possible_model_names(record):
            if record_name and record_name in normalized:
                matches.append((len(record_name), record))
                break
    if not matches:
        return None
    matches.sort(key=lambda item: item[0], reverse=True)
    return matches[0][1]


def possible_model_names(record) -> list[str]:
    names = [normalize_text(record.name)]
    aliases = getattr(record, "aliases", "")
    if aliases:
        names.extend(normalize_text(alias) for alias in re.split(r"[,;\n]+", aliases) if alias.strip())
    return names


def extract_labeled_value(text: str, labels: tuple[str, ...]) -> str:
    alternatives = "|".join(re.escape(label) for label in labels)
    pattern = rf"(?:{alternatives})\s*(?:correto\s*)?(?:é|e|:|-)?\s*([^,;\n]+)"
    match = re.search(pattern, text, flags=re.IGNORECASE)
    return match.group(1).strip() if match else ""


def cleanup_extracted_name(value: str) -> str:
    value = re.sub(
        r"\b(?:amount|date|project|category|cost center|payment method|payment|"
        r"cpf|cnpj|cpf/cnpj|documento|item|índice|indice)\b.*$",
        "",
        value,
        flags=re.IGNORECASE,
    )
    return " ".join(value.strip(" .,:;-").split())


def meaningful_tokens(value: str) -> list[str]:
    ignored = {"de", "da", "do", "das", "dos", "em", "com", "e", "ou", "para", "a", "o"}
    normalized = normalize_text(value).replace("administractive", "administrativo")
    return [token for token in normalized.split() if len(token) > 2 and token not in ignored]


def detect_counterparty_kind(text: str) -> str:
    normalized = normalize_text(text)
    if re.search(r"\b(?:worker|funcionario|pedreiro|servente|mao de project|mestre de project)\b", normalized):
        return Counterparty.Kind.WORKER
    if re.search(r"\b(?:vendor|loja|empresa)\b", normalized):
        return Counterparty.Kind.SUPPLIER
    return ""


def mentions_worker(text: str) -> bool:
    return detect_counterparty_kind(text) == Counterparty.Kind.WORKER


def match_default_worker_category() -> Category | None:
    normalized_names = (
        "mao de project terceirizada",
        "mao de project",
        "workers",
        "servicos",
    )
    for normalized_name in normalized_names:
        category = Category.objects.filter(normalized_name=normalized_name, is_active=True).first()
        if category:
            return category
    return None


def match_counterparty_by_partial_name(name: str, *, kind_hint: str = "") -> Counterparty | None:
    tokens = meaningful_tokens(name)
    if len(tokens) < 2:
        return None

    matches = []
    for counterparty in Counterparty.objects.filter(is_active=True).prefetch_related("documents"):
        for record_name in {normalize_text(counterparty.normalized_name), normalize_text(counterparty.name)}:
            score = partial_name_score(tokens, record_name)
            if score:
                matches.append((score, counterparty))
                break
    for alias in CounterpartyAlias.objects.select_related("counterparty").filter(counterparty__is_active=True):
        score = partial_name_score(tokens, alias.normalized_name)
        if score:
            matches.append((score, alias.counterparty))
    if not matches:
        return None

    best_score = max(score for score, _counterparty in matches)
    best = {counterparty.pk: counterparty for score, counterparty in matches if score == best_score}
    if kind_hint:
        same_kind = [counterparty for counterparty in best.values() if counterparty.kind == kind_hint]
        if same_kind:
            return choose_best_counterparty(same_kind)
    return choose_best_counterparty(best.values())


def partial_name_score(tokens: list[str], normalized_name: str) -> int:
    if not normalized_name:
        return 0
    record_tokens = set(meaningful_tokens(normalized_name))
    if not record_tokens or not set(tokens).issubset(record_tokens):
        return 0
    return len(tokens) * 10 + len(record_tokens)
