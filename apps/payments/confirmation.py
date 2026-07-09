from __future__ import annotations

from dataclasses import dataclass

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from apps.counterparties.models import BudgetItem

from .models import Payment, PaymentConfirmation
from .counterparty_resolution import mark_payment_pending_counterparty


class PaymentConfirmationError(Exception):
    pass


@dataclass(frozen=True)
class ConfirmationResult:
    payment: Payment
    confirmation: PaymentConfirmation
    message: str


def approve_payment(payment_id: int, telegram_user_id: int | None = None, user=None, message: str = ""):
    return confirm_payment(
        payment_id=payment_id,
        action=Payment.ConfirmationAction.APPROVE,
        telegram_user_id=telegram_user_id,
        user=user,
        message=message,
    )


def request_payment_correction(payment_id: int, telegram_user_id: int | None = None, user=None, message: str = ""):
    return confirm_payment(
        payment_id=payment_id,
        action=Payment.ConfirmationAction.CORRECT,
        telegram_user_id=telegram_user_id,
        user=user,
        message=message,
    )


def cancel_payment(payment_id: int, telegram_user_id: int | None = None, user=None, message: str = ""):
    return confirm_payment(
        payment_id=payment_id,
        action=Payment.ConfirmationAction.CANCEL,
        telegram_user_id=telegram_user_id,
        user=user,
        message=message,
    )


def confirm_payment(
    payment_id: int,
    action: str,
    telegram_user_id: int | None = None,
    user=None,
    message: str = "",
) -> ConfirmationResult:
    error = None
    result = None
    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(pk=payment_id)
        if payment.status in {Payment.Status.CANCELED, Payment.Status.RECONCILED}:
            raise PaymentConfirmationError("This payment can no longer be changed by confirmation.")

        if action == Payment.ConfirmationAction.APPROVE:
            if not payment.counterparty_id:
                mark_payment_pending_counterparty(payment)
                error = PaymentConfirmationError(
                    "Confirm or register the vendor/worker before approving the payment."
                )
            else:
                payment.status = Payment.Status.APPROVED
                payment.needs_review = False
                payment.review_reason = ""
                payment.confirmed_at = timezone.now()
                payment.confirmed_by = user
                result_message = "Payment approved."
        elif action == Payment.ConfirmationAction.CORRECT:
            payment.status = Payment.Status.CORRECTING
            payment.needs_review = True
            payment.review_reason = "User requested a correction through Telegram."
            payment.confirmed_at = None
            payment.confirmed_by = None
            result_message = "Payment kept pending for correction."
        elif action == Payment.ConfirmationAction.CANCEL:
            payment.status = Payment.Status.CANCELED
            payment.needs_review = False
            payment.review_reason = "User cancelou pelo Telegram."
            payment.confirmed_at = timezone.now()
            payment.confirmed_by = user
            result_message = "Payment canceled."
        else:
            raise PaymentConfirmationError("Invalid confirmation action.")

        if error is None:
            payment.user_action = action
            payment.save()
            confirmation = PaymentConfirmation.objects.create(
                payment=payment,
                user=user,
                telegram_user_id=telegram_user_id,
                action=action,
                message=message,
                payload={"payment_status": payment.status},
            )
            result = ConfirmationResult(payment=payment, confirmation=confirmation, message=result_message)
    if error is not None:
        raise error
    return result


def get_telegram_user(telegram_user_id: int):
    User = get_user_model()
    return User.objects.filter(authorized_telegram__telegram_user_id=telegram_user_id).first()


def format_payment_suggestion(payment: Payment) -> str:
    counterparty = payment.counterparty
    document = counterparty.primary_document if counterparty else ""
    budget_item = resolve_budget_item(payment)
    lines = [
        "Payment suggestion",
        f"Date: {payment.payment_date.strftime('%d/%m/%Y') if payment.payment_date else '-'}",
        f"Amount: R$ {payment.amount:.2f}",
        f"Vendor/Worker: {counterparty.name if counterparty else '-'}",
        f"CPF/CNPJ: {document or '-'}",
        f"Description: {payment.description or '-'}",
        f"Category: {payment.category.name if payment.category else '-'}",
        f"Payment method: {payment.payment_method or '-'}",
        f"Cost center: {payment.cost_center.name if payment.cost_center else '-'}",
        f"Project: {payment.work.name if payment.work else '-'}",
        f"Budget item index: {payment.work_item_index or '-'}",
        f"Service/Item: {budget_item.description if budget_item else '-'}",
    ]
    return "\n".join(lines)


def resolve_budget_item(payment: Payment) -> BudgetItem | None:
    if not payment.work_id or not payment.work_item_index:
        return None
    return BudgetItem.objects.filter(
        work=payment.work,
        index=payment.work_item_index,
        is_active=True,
    ).first()
