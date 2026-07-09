from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from django.db import transaction
from django.db.models import Q

from apps.counterparties.importers import digits_only, document_type, normalize_text
from apps.counterparties.matching import choose_best_counterparty
from apps.counterparties.models import Category, Counterparty, CounterpartyAlias, CounterpartyDocument, Origin

from .models import Payment


class CounterpartyResolutionError(Exception):
    pass


class MissingCounterpartyCandidateError(CounterpartyResolutionError):
    pass


class AmbiguousCounterpartyError(CounterpartyResolutionError):
    pass


@dataclass(frozen=True)
class CounterpartyCandidate:
    name: str = ""
    document: str = ""
    alias: str = ""
    category_name: str = ""
    source: str = ""

    @property
    def normalized_name(self) -> str:
        return normalize_text(self.name)


@dataclass(frozen=True)
class CounterpartyRegistrationResult:
    payment: Payment
    counterparty: Counterparty
    created: bool
    reused: bool
    message: str


def candidate_from_payment(payment: Payment) -> CounterpartyCandidate:
    payload = payment.raw_payload or {}
    candidate = payload.get("counterparty_candidate") or {}
    ai_extraction = payload.get("ai_extraction") or {}
    initial_extraction = payload.get("initial_extraction") or {}

    name = clean_value(
        candidate.get("name")
        or ai_extraction.get("counterparty_name")
        or initial_extraction.get("counterparty_name")
    )
    document = digits_only(
        candidate.get("document")
        or candidate.get("cpf_cnpj")
        or ai_extraction.get("counterparty_document")
        or ai_extraction.get("counterparty_cpf_cnpj")
    )
    alias = clean_value(candidate.get("alias") or ai_extraction.get("counterparty_alias"))
    category_name = clean_value(candidate.get("category_name") or ai_extraction.get("category_name"))
    source = clean_value(candidate.get("source") or ("ia" if ai_extraction else "telegram"))
    return CounterpartyCandidate(
        name=name,
        document=document,
        alias=alias,
        category_name=category_name,
        source=source,
    )


def mark_payment_pending_counterparty(payment: Payment, reason: str | None = None) -> Payment:
    payment.status = Payment.Status.PENDING_REGISTRATION
    payment.needs_review = True
    payment.review_reason = reason or "Vendor/worker still needs to be confirmed before approval."
    payment.save(update_fields=["status", "needs_review", "review_reason", "updated_at"])
    return payment


def prepare_counterparty_review(payment: Payment) -> Payment:
    if payment.counterparty_id:
        return payment
    candidate = candidate_from_payment(payment)
    if not candidate.name and not candidate.document:
        return mark_payment_pending_counterparty(
            payment,
            "Could not identify vendor/worker. Request a correction before approving.",
        )
    try:
        existing = find_existing_counterparty(candidate)
    except AmbiguousCounterpartyError as exc:
        mark_payment_pending_counterparty(payment, f"Ambiguous counterparty: {exc}")
        raise
    if existing:
        apply_counterparty_defaults(payment, existing)
        payment.status = Payment.Status.PENDING_CONFIRMATION
        payment.needs_review = True
        payment.review_reason = "Existing counterparty found. Awaiting payment confirmation."
        payment.save()
        return payment
    persist_candidate(payment, candidate)
    return mark_payment_pending_counterparty(
        payment,
        "Vendor/worker novo. Confirme o type e os date antes de aprovar.",
    )


def confirm_counterparty_for_payment(
    payment_id: int,
    kind: str,
    name: str = "",
    document: str = "",
    category_name: str = "",
    alias: str = "",
) -> CounterpartyRegistrationResult:
    if kind not in {Counterparty.Kind.SUPPLIER, Counterparty.Kind.WORKER}:
        raise CounterpartyResolutionError("Invalid counterparty type.")

    error = None
    result = None
    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(pk=payment_id)
        if payment.counterparty_id:
            result = CounterpartyRegistrationResult(
                payment=payment,
                counterparty=payment.counterparty,
                created=False,
                reused=True,
                message="Vendor/worker was already linked to the payment.",
            )
        else:
            candidate = candidate_from_payment(payment)
            confirmed_candidate = CounterpartyCandidate(
                name=clean_value(name) or candidate.name,
                document=digits_only(document) or candidate.document,
                alias=clean_value(alias) or candidate.alias or candidate.name,
                category_name=clean_value(category_name) or candidate.category_name,
                source=candidate.source or "telegram",
            )
            if not confirmed_candidate.name and not confirmed_candidate.document:
                mark_payment_pending_counterparty(
                    payment,
                    "Enter the name and, if available, CPF/CNPJ before registering the vendor/worker.",
                )
                error = MissingCounterpartyCandidateError("Counterparty name or CPF/CNPJ is required.")
            else:
                try:
                    existing = find_existing_counterparty(confirmed_candidate)
                except AmbiguousCounterpartyError as exc:
                    mark_payment_pending_counterparty(payment, f"Ambiguous counterparty: {exc}")
                    error = exc
                else:
                    if existing:
                        apply_counterparty_defaults(payment, existing)
                        payment.status = Payment.Status.PENDING_CONFIRMATION
                        payment.needs_review = True
                        payment.review_reason = "Existing counterparty reused. Awaiting payment confirmation."
                        persist_candidate(payment, confirmed_candidate)
                        payment.save()
                        result = CounterpartyRegistrationResult(
                            payment=payment,
                            counterparty=existing,
                            created=False,
                            reused=True,
                            message="Cadastro existente reutilizado. Confira o payment antes de aprovar.",
                        )
                    else:
                        counterparty = Counterparty.objects.create(
                            name=confirmed_candidate.name or confirmed_candidate.document,
                            normalized_name=confirmed_candidate.normalized_name or confirmed_candidate.document,
                            kind=kind,
                            person_type=person_type_for_document(confirmed_candidate.document),
                            primary_document=confirmed_candidate.document,
                            default_category=find_category(confirmed_candidate.category_name),
                            source=Origin.TELEGRAM,
                            confidence=Decimal("1.00") if confirmed_candidate.document else Decimal("0.70"),
                            notes="Criado a partir de payment received pelo Telegram.",
                        )
                        ensure_document(counterparty, confirmed_candidate.document)
                        ensure_alias(counterparty, confirmed_candidate.alias, Origin.TELEGRAM)
                        ensure_alias(counterparty, candidate.name, Origin.TELEGRAM)

                        apply_counterparty_defaults(payment, counterparty)
                        payment.status = Payment.Status.PENDING_CONFIRMATION
                        payment.needs_review = True
                        payment.review_reason = "Counterparty registered. Awaiting payment confirmation."
                        persist_candidate(payment, confirmed_candidate)
                        payment.save()
                        result = CounterpartyRegistrationResult(
                            payment=payment,
                            counterparty=counterparty,
                            created=True,
                            reused=False,
                            message="Registration created. Review the payment before approving it.",
                        )
    if error is not None:
        raise error
    return result


def find_existing_counterparty(candidate: CounterpartyCandidate) -> Counterparty | None:
    if candidate.document:
        document_record = (
            CounterpartyDocument.objects.select_related("counterparty")
            .filter(number=candidate.document, counterparty__is_active=True)
            .first()
        )
        if document_record:
            return document_record.counterparty
        counterparty = Counterparty.objects.filter(primary_document=candidate.document, is_active=True).first()
        if counterparty:
            return counterparty

    if not candidate.normalized_name:
        return None

    matches = list(
        Counterparty.objects.filter(normalized_name=candidate.normalized_name, is_active=True).prefetch_related(
            "documents"
        )[:5]
    )
    if len(matches) > 1:
        kinds = ", ".join(sorted({match.kind for match in matches}))
        if len({match.kind for match in matches}) > 1:
            raise AmbiguousCounterpartyError(f"Name found in more than one record ({kinds}).")
        return choose_best_counterparty(matches)
    if len(matches) == 1:
        return matches[0]

    alias_matches = list(
        CounterpartyAlias.objects.select_related("counterparty").filter(
            normalized_name=candidate.normalized_name,
            counterparty__is_active=True,
        )[:3]
    )
    counterparties = {alias.counterparty_id: alias.counterparty for alias in alias_matches}
    if len(counterparties) > 1:
        if len({counterparty.kind for counterparty in counterparties.values()}) > 1:
            raise AmbiguousCounterpartyError("Alias found in more than one record.")
        return choose_best_counterparty(counterparties.values())
    return next(iter(counterparties.values()), None)


def apply_counterparty_defaults(payment: Payment, counterparty: Counterparty) -> None:
    payment.counterparty = counterparty
    payment.category = payment.category or counterparty.default_category
    payment.chart_account = payment.chart_account or counterparty.default_chart_account


def persist_candidate(payment: Payment, candidate: CounterpartyCandidate) -> None:
    payload = payment.raw_payload or {}
    payload["counterparty_candidate"] = {
        "name": candidate.name,
        "document": candidate.document,
        "alias": candidate.alias,
        "category_name": candidate.category_name,
        "source": candidate.source,
    }
    payment.raw_payload = payload


def ensure_document(counterparty: Counterparty, document: str) -> None:
    if not document:
        return
    CounterpartyDocument.objects.get_or_create(
        number=document,
        defaults={
            "counterparty": counterparty,
            "document_type": document_type(document),
            "source": Origin.TELEGRAM,
            "confidence": Decimal("1.00"),
            "is_primary": counterparty.primary_document == document,
        },
    )


def ensure_alias(counterparty: Counterparty, name: str, source: str) -> None:
    normalized_name = normalize_text(name)
    if not name or not normalized_name or normalized_name == counterparty.normalized_name:
        return
    CounterpartyAlias.objects.get_or_create(
        counterparty=counterparty,
        normalized_name=normalized_name,
        defaults={"name": name, "source": source},
    )


def find_category(name: str) -> Category | None:
    normalized = normalize_text(name)
    if not normalized:
        return None
    return Category.objects.filter(Q(normalized_name=normalized) | Q(name__iexact=name), is_active=True).first()


def person_type_for_document(document: str) -> str:
    if len(document) == 11:
        return Counterparty.PersonType.INDIVIDUAL
    if len(document) == 14:
        return Counterparty.PersonType.COMPANY
    return Counterparty.PersonType.UNKNOWN


def clean_value(value: object) -> str:
    return " ".join(str(value or "").strip().split())
