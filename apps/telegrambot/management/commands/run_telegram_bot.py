from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from telegram import Update
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, MessageHandler, filters

from apps.telegrambot.handlers import (
    handle_confirmation_callback,
    handle_counterparty_callback,
    handle_draft_callback,
    handle_document,
    handle_photo,
    handle_text,
    handle_unsupported,
)


DOCUMENT_FILTER = filters.Document.ALL


class Command(BaseCommand):
    help = "Inicia o bot do Telegram em modo polling."

    def handle(self, *args, **options):
        if not settings.TELEGRAM_BOT_TOKEN:
            raise CommandError("TELEGRAM_BOT_TOKEN no configured.")

        application = ApplicationBuilder().token(settings.TELEGRAM_BOT_TOKEN).build()
        application.add_handler(
            CallbackQueryHandler(
                handle_draft_callback,
                pattern=(
                    r"^draft:\d+:"
                    r"(finalize|new|cancel|register_work|leave_company|register_supplier|register_worker|correct)$"
                ),
            )
        )
        application.add_handler(
            CallbackQueryHandler(
                handle_counterparty_callback,
                pattern=r"^counterparty:\d+:(supplier|worker)$",
            )
        )
        application.add_handler(
            CallbackQueryHandler(
                handle_confirmation_callback,
                pattern=r"^payment:\d+:(approve|correct|cancel)$",
            )
        )
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
        application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
        application.add_handler(MessageHandler(DOCUMENT_FILTER, handle_document))
        application.add_handler(MessageHandler(filters.ALL, handle_unsupported))

        self.stdout.write(self.style.SUCCESS("Bot do Telegram iniciado."))
        application.run_polling(allowed_updates=Update.ALL_TYPES)
