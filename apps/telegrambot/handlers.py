from __future__ import annotations

from asgiref.sync import sync_to_async
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from apps.documents.models import UploadedFile
from apps.payments.confirmation import (
    PaymentConfirmationError,
    approve_payment,
    cancel_payment,
    get_telegram_user,
    request_payment_correction,
)
from apps.payments.counterparty_resolution import (
    CounterpartyResolutionError,
    confirm_counterparty_for_payment,
)
from apps.counterparties.models import Counterparty

from .services import (
    TelegramAttachment,
    TelegramIntakeService,
    TelegramSender,
    draft_has_pending_counterparty_candidate,
    draft_has_pending_work_candidate,
)


service = TelegramIntakeService()


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or message.text is None:
        return
    result = await sync_to_async(service.process_text)(
        sender=sender_from_update(update),
        message_id=message.message_id,
        text=message.text,
    )
    await reply_with_confirmation(message, result)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or not message.photo:
        return
    sender = sender_from_update(update)
    if not await sync_to_async(service.is_authorized)(sender):
        await message.reply_text(service.unauthorized_reply)
        return
    photo = message.photo[-1]
    telegram_file = await context.bot.get_file(photo.file_id)
    content = bytes(await telegram_file.download_as_bytearray())
    result = await sync_to_async(service.process_attachment)(
        sender=sender,
        message_id=message.message_id,
        attachment=TelegramAttachment(
            file_id=photo.file_id,
            filename=f"telegram-photo-{message.message_id}.jpg",
            content_type="image/jpeg",
            content=content,
            kind=UploadedFile.Kind.IMAGE,
            size_bytes=getattr(photo, "file_size", None),
        ),
    )
    await reply_with_confirmation(message, result)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or message.document is None:
        return
    sender = sender_from_update(update)
    if not await sync_to_async(service.is_authorized)(sender):
        await message.reply_text(service.unauthorized_reply)
        return
    document = message.document
    filename = document.file_name or f"telegram-document-{message.message_id}"
    mime_type = document.mime_type or ""
    is_pdf = mime_type == "application/pdf" or filename.lower().endswith(".pdf")
    is_ofx = filename.lower().endswith(".ofx") or mime_type in {"application/x-ofx", "application/ofx"}
    if not is_pdf and not is_ofx:
        await message.reply_text("For now, send text, image, PDF, or OFX files.")
        return
    telegram_file = await context.bot.get_file(document.file_id)
    content = bytes(await telegram_file.download_as_bytearray())
    result = await sync_to_async(service.process_attachment)(
        sender=sender,
        message_id=message.message_id,
        attachment=TelegramAttachment(
            file_id=document.file_id,
            filename=filename,
            content_type=mime_type or ("application/x-ofx" if is_ofx else "application/pdf"),
            content=content,
            kind=UploadedFile.Kind.OFX if is_ofx else UploadedFile.Kind.PDF,
            size_bytes=document.file_size,
        ),
    )
    await reply_with_confirmation(message, result)


async def handle_unsupported(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is not None:
        await message.reply_text("For now, send text, image, PDF, or OFX files.")


async def handle_confirmation_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or not query.data:
        return
    sender = sender_from_update(update)
    if not await sync_to_async(service.is_authorized)(sender):
        await query.answer(service.unauthorized_reply, show_alert=True)
        return

    try:
        action, payment_id = parse_callback_data(query.data)
        user = await sync_to_async(get_telegram_user)(sender.telegram_user_id)
        if action == "approve":
            result = await sync_to_async(approve_payment)(payment_id, sender.telegram_user_id, user, "Telegram")
        elif action == "correct":
            result = await sync_to_async(request_payment_correction)(
                payment_id,
                sender.telegram_user_id,
                user,
                "Telegram",
            )
        elif action == "cancel":
            result = await sync_to_async(cancel_payment)(payment_id, sender.telegram_user_id, user, "Telegram")
        else:
            raise ValueError("invalid action")
    except (ValueError, PaymentConfirmationError):
        await query.answer("Could not process this action.", show_alert=True)
        return

    await query.answer(result.message)
    if query.message is not None:
        if action == "correct":
            await query.message.reply_text(
                f"{result.message}\n\n"
                "Send the correction in a message. Examples:\n"
                "- correct vendor is Anita Jakeline Alves Fields\n"
                "- amount R$ 2.000,00, project Sertaozinho, item 3.4.6\n"
                "- category Materials, method Pix"
            )
            return
        await query.message.reply_text(result.message)


async def handle_draft_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or not query.data:
        return
    sender = sender_from_update(update)
    if not await sync_to_async(service.is_authorized)(sender):
        await query.answer(service.unauthorized_reply, show_alert=True)
        return

    try:
        action, draft_id = parse_draft_callback_data(query.data)
    except ValueError:
        await query.answer("Could not process this action.", show_alert=True)
        return

    if action == "finalize":
        result = await sync_to_async(service.finalize_draft)(draft_id, sender)
        await query.answer("Draft finalized.")
        if query.message is not None:
            await reply_with_confirmation(query.message, result)
        return
    if action == "new":
        result = await sync_to_async(service.start_new_from_draft)(draft_id, sender)
        await query.answer("New payment.")
        if query.message is not None:
            await query.message.reply_text(result.reply_text)
        return
    if action in {"register_supplier", "register_worker"}:
        kind = Counterparty.Kind.SUPPLIER if action == "register_supplier" else Counterparty.Kind.WORKER
        result = await sync_to_async(service.register_counterparty_from_draft)(draft_id, kind, sender)
        await query.answer("Registration processed.")
        if query.message is not None:
            reply_markup = None
            if result.draft is not None:
                reply_markup = draft_keyboard_for_draft(result.draft)
            await query.message.reply_text(result.reply_text, reply_markup=reply_markup)
        return
    if action == "register_work":
        result = await sync_to_async(service.register_work_from_draft)(draft_id, sender)
        await query.answer("Project registration processed.")
        if query.message is not None:
            reply_markup = None
            if result.draft is not None:
                reply_markup = draft_keyboard_for_draft(result.draft)
            await query.message.reply_text(result.reply_text, reply_markup=reply_markup)
        return
    if action == "leave_company":
        result = await sync_to_async(service.leave_work_candidate_as_company)(draft_id, sender)
        await query.answer("Company cost center kept.")
        if query.message is not None:
            reply_markup = None
            if result.draft is not None:
                reply_markup = draft_keyboard_for_draft(result.draft)
            await query.message.reply_text(result.reply_text, reply_markup=reply_markup)
        return
    if action == "correct":
        await query.answer("Send the correction by message.")
        if query.message is not None:
            await query.message.reply_text(
                "Send the correction by message. Examples:\n"
                "- project Sertaozinho\n"
                "- correct vendor is Name\n"
                "- worker Name"
            )
        return
    if action == "cancel":
        result = await sync_to_async(service.cancel_draft)(draft_id, sender)
        await query.answer("Draft canceled.")
        if query.message is not None:
            await query.message.reply_text(result.reply_text)
        return

    await query.answer("Invalid action.", show_alert=True)


async def handle_counterparty_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or not query.data:
        return
    sender = sender_from_update(update)
    if not await sync_to_async(service.is_authorized)(sender):
        await query.answer(service.unauthorized_reply, show_alert=True)
        return

    try:
        kind, payment_id = parse_counterparty_callback_data(query.data)
        result = await sync_to_async(confirm_counterparty_for_payment)(payment_id, kind)
    except (ValueError, CounterpartyResolutionError):
        await query.answer("I need a correction before registering.", show_alert=True)
        return

    await query.answer(result.message)
    if query.message is not None:
        await query.message.reply_text(
            f"{result.message}\n\n{service.build_confirmation_message(result.payment)}",
            reply_markup=confirmation_keyboard(result.payment.pk),
        )


def sender_from_update(update: Update) -> TelegramSender:
    user = update.effective_user
    if user is None:
        return TelegramSender(telegram_user_id=0)
    full_name = " ".join(part for part in [user.first_name, user.last_name] if part)
    return TelegramSender(
        telegram_user_id=user.id,
        name=full_name,
        username=user.username or "",
    )


async def reply_with_confirmation(message, result) -> None:
    if getattr(result, "draft", None) is not None:
        await message.reply_text(
            result.reply_text,
            reply_markup=draft_keyboard_for_draft(result.draft),
        )
        return
    if result.payment is None:
        await message.reply_text(result.reply_text)
        return
    if result.payment.counterparty_id is None:
        await message.reply_text(
            result.reply_text,
            reply_markup=counterparty_registration_keyboard(result.payment.pk),
        )
        return
    await message.reply_text(
        result.reply_text,
        reply_markup=confirmation_keyboard(result.payment.pk),
    )


def draft_keyboard(
    draft_id: int,
    can_register_counterparty: bool = False,
    can_register_work: bool = False,
) -> InlineKeyboardMarkup:
    rows = []
    if can_register_counterparty:
        rows.append(
            [
                InlineKeyboardButton("Register vendor", callback_data=f"draft:{draft_id}:register_supplier"),
                InlineKeyboardButton("Register worker", callback_data=f"draft:{draft_id}:register_worker"),
            ]
        )
    if can_register_work:
        rows.append(
            [
                InlineKeyboardButton("Register project", callback_data=f"draft:{draft_id}:register_work"),
                InlineKeyboardButton("Keep as Company", callback_data=f"draft:{draft_id}:leave_company"),
            ]
        )
    if can_register_counterparty or can_register_work:
        rows.append([InlineKeyboardButton("Correct", callback_data=f"draft:{draft_id}:correct")])
        rows.append([InlineKeyboardButton("Cancel", callback_data=f"draft:{draft_id}:cancel")])
    else:
        rows.extend(
            [
                [
                    InlineKeyboardButton("Finalize", callback_data=f"draft:{draft_id}:finalize"),
                    InlineKeyboardButton("New payment", callback_data=f"draft:{draft_id}:new"),
                ],
                [InlineKeyboardButton("Cancel", callback_data=f"draft:{draft_id}:cancel")],
            ]
        )
    return InlineKeyboardMarkup(rows)


def draft_keyboard_for_draft(draft) -> InlineKeyboardMarkup:
    return draft_keyboard(
        draft.pk,
        can_register_counterparty=has_pending_counterparty_candidate(draft),
        can_register_work=has_pending_work_candidate(draft),
    )


def counterparty_registration_keyboard(payment_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Vendor", callback_data=f"counterparty:{payment_id}:supplier"),
                InlineKeyboardButton("Worker", callback_data=f"counterparty:{payment_id}:worker"),
                InlineKeyboardButton("Correct", callback_data=f"payment:{payment_id}:correct"),
            ],
            [InlineKeyboardButton("Cancel", callback_data=f"payment:{payment_id}:cancel")],
        ]
    )


def confirmation_keyboard(payment_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Approve", callback_data=f"payment:{payment_id}:approve"),
                InlineKeyboardButton("Correct", callback_data=f"payment:{payment_id}:correct"),
                InlineKeyboardButton("Cancel", callback_data=f"payment:{payment_id}:cancel"),
            ]
        ]
    )


def parse_callback_data(data: str) -> tuple[str, int]:
    prefix, payment_id, action = data.split(":", 2)
    if prefix != "payment":
        raise ValueError("invalid prefix")
    return action, int(payment_id)


def parse_counterparty_callback_data(data: str) -> tuple[str, int]:
    prefix, payment_id, action = data.split(":", 2)
    if prefix != "counterparty":
        raise ValueError("invalid prefix")
    if action == "supplier":
        return Counterparty.Kind.SUPPLIER, int(payment_id)
    if action == "worker":
        return Counterparty.Kind.WORKER, int(payment_id)
    raise ValueError("invalid type")


def parse_draft_callback_data(data: str) -> tuple[str, int]:
    prefix, draft_id, action = data.split(":", 2)
    if prefix != "draft":
        raise ValueError("invalid prefix")
    if action not in {
        "finalize",
        "new",
        "cancel",
        "register_work",
        "leave_company",
        "register_supplier",
        "register_worker",
        "correct",
    }:
        raise ValueError("invalid action")
    return action, int(draft_id)


def has_pending_work_candidate(draft) -> bool:
    return draft_has_pending_work_candidate(draft)


def has_pending_counterparty_candidate(draft) -> bool:
    return draft_has_pending_counterparty_candidate(draft)
