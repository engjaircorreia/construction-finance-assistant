from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction

from apps.counterparties.importers import digits_only, normalize_text
from apps.counterparties.models import CounterpartyAlias, CounterpartyDocument
from apps.payments.models import Payment

from .models import OfxTransaction, Reconciliation


@dataclass(frozen=True)
class MatchCandidate:
    payment: Payment
    confidence: Decimal
    notes: str
    amount_matches: bool
    date_matches: bool
    counterparty_matches: bool


@dataclass
class ReconciliationReport:
    reconciled: int = 0
    possible_duplicates: int = 0
    missing_payments: int = 0
    divergent: int = 0
    ignored_credits: int = 0
    reconciliations_created: int = 0
    reconciliations_updated: int = 0
    processed_transaction_ids: list[int] = field(default_factory=list)


def reconcile_ofx_transactions(queryset=None, user=None) -> ReconciliationReport:
    queryset = queryset or OfxTransaction.objects.all()
    report = ReconciliationReport()
    for transaction_record in queryset.select_related("counterparty").order_by("posted_at", "id"):
        result = reconcile_ofx_transaction(transaction_record, user=user)
        report.processed_transaction_ids.append(transaction_record.pk)
        setattr(report, result["classification"], getattr(report, result["classification"]) + 1)
        report.reconciliations_created += result["created"]
        report.reconciliations_updated += result["updated"]
    return report


@transaction.atomic
def reconcile_ofx_transaction(transaction_record: OfxTransaction, user=None) -> dict:
    transaction_record = OfxTransaction.objects.select_related("counterparty").select_for_update(of=("self",)).get(
        pk=transaction_record.pk
    )
    normalized_memo = normalized_transaction_memo(transaction_record)
    if transaction_record.normalized_memo != normalized_memo:
        transaction_record.normalized_memo = normalized_memo

    if transaction_record.amount >= 0:
        transaction_record.status = OfxTransaction.Status.IGNORED
        transaction_record.save(update_fields=["normalized_memo", "status", "updated_at"])
        return {"classification": "ignored_credits", "created": 0, "updated": 0}

    candidates = find_match_candidates(transaction_record)
    exact_candidates = [
        candidate
        for candidate in candidates
        if candidate.amount_matches and candidate.date_matches and candidate.counterparty_matches
    ]

    if len(exact_candidates) == 1:
        candidate = exact_candidates[0]
        reconciliation, created = upsert_reconciliation(
            candidate.payment,
            transaction_record,
            status=Reconciliation.Status.CONFIRMED,
            confidence=candidate.confidence,
            notes=candidate.notes,
            user=user,
        )
        mark_reconciled(candidate.payment, transaction_record)
        return {"classification": "reconciled", "created": int(created), "updated": int(not created)}

    if len(exact_candidates) > 1:
        created, updated = suggest_candidates(exact_candidates, transaction_record, user=user)
        transaction_record.status = OfxTransaction.Status.POSSIBLE_DUPLICATE
        transaction_record.save(update_fields=["status", "normalized_memo", "updated_at"])
        for candidate in exact_candidates:
            mark_possible_duplicate(candidate.payment)
        return {"classification": "possible_duplicates", "created": created, "updated": updated}

    divergent_candidates = [candidate for candidate in candidates if candidate.counterparty_matches]
    if divergent_candidates:
        created, updated = suggest_candidates(divergent_candidates[:5], transaction_record, user=user)
        transaction_record.status = OfxTransaction.Status.DIVERGENT
        transaction_record.save(update_fields=["status", "normalized_memo", "updated_at"])
        return {"classification": "divergent", "created": created, "updated": updated}

    transaction_record.status = OfxTransaction.Status.MISSING_PAYMENT
    transaction_record.save(update_fields=["status", "normalized_memo", "updated_at"])
    return {"classification": "missing_payments", "created": 0, "updated": 0}


def find_match_candidates(transaction_record: OfxTransaction) -> list[MatchCandidate]:
    payments = (
        Payment.objects.select_related("counterparty")
        .filter(
            status__in=[
                Payment.Status.APPROVED,
                Payment.Status.POSTED,
                Payment.Status.RECONCILED,
                Payment.Status.POSSIBLE_DUPLICATE,
            ]
        )
        .exclude(amount=0)
        .order_by("payment_date", "id")
    )
    candidates = []
    for payment in payments:
        candidate = score_payment(transaction_record, payment)
        if candidate.confidence >= Decimal("0.45") or candidate.counterparty_matches:
            candidates.append(candidate)
    candidates.sort(key=lambda candidate: candidate.confidence, reverse=True)
    return candidates


def score_payment(transaction_record: OfxTransaction, payment: Payment) -> MatchCandidate:
    amount_matches = money(payment.amount) == money(abs(transaction_record.amount))
    date_matches = payment.payment_date == transaction_record.posted_at
    fitid_matches = transaction_fitid_matches_payment(transaction_record, payment)
    document_matches = transaction_document_matches_payment(transaction_record, payment)
    direct_counterparty_matches = (
        bool(transaction_record.counterparty_id)
        and bool(payment.counterparty_id)
        and transaction_record.counterparty_id == payment.counterparty_id
    )
    memo_matches = transaction_memo_matches_payment(transaction_record, payment)
    counterparty_matches = fitid_matches or direct_counterparty_matches or document_matches or memo_matches

    confidence = Decimal("0.00")
    notes = []
    if fitid_matches:
        confidence += Decimal("1.00")
        notes.append("FITID linked to payment.")
    else:
        if amount_matches:
            confidence += Decimal("0.35")
            notes.append("Amount confere.")
        if date_matches:
            confidence += Decimal("0.30")
            notes.append("Date confere.")
        if direct_counterparty_matches:
            confidence += Decimal("0.25")
            notes.append("Counterparty confere.")
        if document_matches:
            confidence += Decimal("0.20")
            notes.append("CPF/CNPJ confere.")
        if memo_matches:
            confidence += Decimal("0.10")
            notes.append("Memo indica counterparty.")

    if counterparty_matches and not amount_matches:
        notes.append("Divergent amount.")
    if counterparty_matches and not date_matches:
        notes.append("Divergent date.")

    return MatchCandidate(
        payment=payment,
        confidence=min(confidence, Decimal("1.00")),
        notes=" ".join(notes),
        amount_matches=amount_matches,
        date_matches=date_matches,
        counterparty_matches=counterparty_matches,
    )


def upsert_reconciliation(payment, transaction_record, status, confidence, notes, user=None) -> tuple[Reconciliation, bool]:
    reconciliation, created = Reconciliation.objects.get_or_create(
        payment=payment,
        transaction=transaction_record,
        defaults={
            "status": status,
            "confidence": confidence,
            "notes": notes,
            "created_by": user,
        },
    )
    if not created:
        changed = False
        if reconciliation.status != status:
            reconciliation.status = status
            changed = True
        if reconciliation.confidence != confidence:
            reconciliation.confidence = confidence
            changed = True
        if notes and reconciliation.notes != notes:
            reconciliation.notes = notes
            changed = True
        if user and reconciliation.created_by_id is None:
            reconciliation.created_by = user
            changed = True
        if changed:
            reconciliation.save()
    return reconciliation, created


def suggest_candidates(candidates: list[MatchCandidate], transaction_record: OfxTransaction, user=None) -> tuple[int, int]:
    created_count = 0
    updated_count = 0
    for candidate in candidates:
        _, created = upsert_reconciliation(
            candidate.payment,
            transaction_record,
            status=Reconciliation.Status.SUGGESTED,
            confidence=candidate.confidence,
            notes=candidate.notes,
            user=user,
        )
        created_count += int(created)
        updated_count += int(not created)
    return created_count, updated_count


def mark_reconciled(payment: Payment, transaction_record: OfxTransaction) -> None:
    if transaction_record.counterparty_id is None and payment.counterparty_id:
        transaction_record.counterparty = payment.counterparty
    transaction_record.status = OfxTransaction.Status.RECONCILED
    transaction_record.save(update_fields=["counterparty", "status", "normalized_memo", "updated_at"])
    if payment.status != Payment.Status.RECONCILED:
        payment.status = Payment.Status.RECONCILED
        payment.needs_review = False
        payment.review_reason = "Reconciled com transaction OFX."
        payload = payment.raw_payload or {}
        payload["ofx_fitid"] = transaction_record.fitid
        payment.raw_payload = payload
        payment.save(update_fields=["status", "needs_review", "review_reason", "raw_payload", "updated_at"])


def mark_possible_duplicate(payment: Payment) -> None:
    if payment.status in {Payment.Status.APPROVED, Payment.Status.POSTED}:
        payment.status = Payment.Status.POSSIBLE_DUPLICATE
        payment.needs_review = True
        payment.review_reason = "More than one payment may match the same OFX transaction."
        payment.save(update_fields=["status", "needs_review", "review_reason", "updated_at"])


def transaction_fitid_matches_payment(transaction_record: OfxTransaction, payment: Payment) -> bool:
    if not transaction_record.fitid:
        return False
    payload = payment.raw_payload or {}
    candidates = [
        payload.get("ofx_fitid"),
        payload.get("fitid"),
        (payload.get("banking") or {}).get("fitid") if isinstance(payload.get("banking"), dict) else None,
    ]
    return any(str(candidate) == transaction_record.fitid for candidate in candidates if candidate)


def transaction_document_matches_payment(transaction_record: OfxTransaction, payment: Payment) -> bool:
    document = digits_only(transaction_record.document_extracted)
    if not document or not payment.counterparty_id:
        return False
    if digits_only(payment.counterparty.primary_document) == document:
        return True
    return CounterpartyDocument.objects.filter(counterparty=payment.counterparty, number=document).exists()


def transaction_memo_matches_payment(transaction_record: OfxTransaction, payment: Payment) -> bool:
    if not payment.counterparty_id:
        return False
    memo = normalized_transaction_memo(transaction_record)
    if not memo:
        return False
    names = [payment.counterparty.normalized_name]
    names.extend(
        CounterpartyAlias.objects.filter(counterparty=payment.counterparty).values_list("normalized_name", flat=True)
    )
    return any(name and name in memo for name in names)


def normalized_transaction_memo(transaction_record: OfxTransaction) -> str:
    return normalize_text(transaction_record.normalized_memo or transaction_record.memo or "")


def money(value) -> Decimal:
    return Decimal(value or 0).copy_abs().quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
