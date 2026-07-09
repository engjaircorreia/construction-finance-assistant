import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import TestCase, override_settings
from telegram import Update

from apps.accounts.models import AuthorizedTelegramUser
from apps.banking.models import OfxFile, OfxTransaction
from apps.counterparties.models import BudgetItem, Category, CostCenter, Counterparty, Origin, Work
from apps.documents.models import UploadedFile
from apps.payments.ai_extraction import AIExtractionError, parse_ai_extraction_response
from apps.payments.models import Payment

from .models import TelegramDraft
from .services import TelegramAttachment, TelegramIntakeService, TelegramSender, infer_budget_item_index
from .handlers import (
    confirmation_keyboard,
    counterparty_registration_keyboard,
    draft_keyboard,
    parse_callback_data,
    parse_counterparty_callback_data,
    parse_draft_callback_data,
)
from .management.commands.run_telegram_bot import DOCUMENT_FILTER


class TelegramBotCommandTests(TestCase):
    def test_document_filter_forwards_ofx_documents_to_handler(self):
        update = Update.de_json(
            {
                "update_id": 1,
                "message": {
                    "message_id": 1,
                    "date": 1782295123,
                    "chat": {"id": 123, "type": "private", "first_name": "Jair"},
                    "from": {"id": 123, "is_bot": False, "first_name": "Jair"},
                    "document": {
                        "file_id": "ofx-file-id",
                        "file_unique_id": "ofx-unique-id",
                        "file_name": "extrato.ofx",
                        "mime_type": "application/x-ofx",
                        "file_size": 123,
                    },
                },
            },
            bot=None,
        )

        self.assertTrue(DOCUMENT_FILTER.check_update(update))


class TelegramIntakeServiceTests(TestCase):
    def setUp(self):
        self.tempdir = TemporaryDirectory()
        self.override = override_settings(
            MEDIA_ROOT=Path(self.tempdir.name),
            TELEGRAM_ALLOWED_USER_IDS=[],
            OPENAI_API_KEY="",
        )
        self.override.enable()
        self.service = TelegramIntakeService()

    def tearDown(self):
        self.override.disable()
        self.tempdir.cleanup()

    def test_unauthorized_user_is_blocked_without_creating_records(self):
        result = self.service.process_text(
            sender=TelegramSender(telegram_user_id=999, name="Intruso"),
            message_id=10,
            text="paguei 100 no cimento",
        )

        self.assertFalse(result.authorized)
        self.assertEqual(result.reply_text, "Unauthorized access.")
        self.assertEqual(UploadedFile.objects.count(), 0)
        self.assertEqual(Payment.objects.count(), 0)

    def test_authorized_text_creates_uploaded_file_and_active_draft(self):
        self.authorize_user(telegram_user_id=123)

        result = self.service.process_text(
            sender=TelegramSender(telegram_user_id=123, name="Jair", username="jair"),
            message_id=11,
            text="paguei 150 de areia na project",
        )

        self.assertTrue(result.authorized)
        uploaded_file = UploadedFile.objects.get()
        draft = TelegramDraft.objects.get()
        self.assertEqual(uploaded_file.kind, UploadedFile.Kind.TEXT)
        self.assertEqual(uploaded_file.source, UploadedFile.Source.TELEGRAM)
        self.assertEqual(uploaded_file.telegram_user_id, 123)
        self.assertEqual(uploaded_file.telegram_message_id, "11")
        self.assertEqual(uploaded_file.extracted_text, "paguei 150 de areia na project")
        self.assertTrue(uploaded_file.file.name.endswith(".txt"))
        self.assertEqual(Payment.objects.count(), 0)
        self.assertEqual(draft.status, TelegramDraft.Status.ACTIVE)
        self.assertEqual(draft.uploaded_files.get(), uploaded_file)
        self.assertEqual(draft.amount, 150)
        self.assertIn("paguei 150 de areia na project", draft.text_content)
        self.assertIn("Draft #", result.reply_text)
        self.assertIn("Data extracted so far", result.reply_text)

    def test_authorized_pdf_creates_uploaded_file_and_active_draft(self):
        self.authorize_user(telegram_user_id=123)

        result = self.service.process_attachment(
            sender=TelegramSender(telegram_user_id=123, name="Jair"),
            message_id=12,
            attachment=TelegramAttachment(
                file_id="pdf-file-id",
                filename="receipt.pdf",
                content_type="application/pdf",
                content=minimal_pdf_with_text("Payment R$ 100,00 via PIX"),
                kind=UploadedFile.Kind.PDF,
            ),
        )

        self.assertTrue(result.authorized)
        uploaded_file = UploadedFile.objects.get()
        draft = TelegramDraft.objects.get()
        self.assertEqual(uploaded_file.kind, UploadedFile.Kind.PDF)
        self.assertEqual(uploaded_file.content_type, "application/pdf")
        self.assertEqual(uploaded_file.telegram_file_id, "pdf-file-id")
        self.assertTrue(uploaded_file.file.name.endswith(".pdf"))
        self.assertEqual(Payment.objects.count(), 0)
        self.assertEqual(draft.uploaded_files.get(), uploaded_file)
        self.assertEqual(draft.amount, 100)
        self.assertIn("Draft #", result.reply_text)

    def test_pdf_recipient_without_registration_stays_as_draft_candidate(self):
        self.authorize_user(telegram_user_id=123)

        result = self.service.process_attachment(
            sender=TelegramSender(telegram_user_id=123, name="Jair"),
            message_id=17,
            attachment=TelegramAttachment(
                file_id="pdf-file-id",
                filename="sicredi.pdf",
                content_type="application/pdf",
                content=minimal_pdf_with_text(
                    "Receipt de Payment Pix Amount: R$ 1.100,00 Realized em: 26/06/2026 "
                    "Solicitante: TIAGO MARCELO ARAUJO DE OLIVEIRA "
                    "Name do destinatário: IVALDO MARTINS DE FREITAS "
                    "CPF do destinatário: ***.425.464-**"
                ),
                kind=UploadedFile.Kind.PDF,
            ),
        )

        draft = TelegramDraft.objects.get()

        self.assertTrue(result.authorized)
        self.assertIsNone(draft.counterparty)
        self.assertEqual(draft.raw_payload["counterparty_candidate"]["name"], "IVALDO MARTINS DE FREITAS")
        self.assertEqual(Payment.objects.count(), 0)
        self.assertIn("Vendor/Worker: IVALDO MARTINS DE FREITAS", result.reply_text)
        self.assertIn("Suggested registration:", result.reply_text)
        self.assertIn("Name: IVALDO MARTINS DE FREITAS", result.reply_text)
        self.assertIn("Source: PDF", result.reply_text)

    @override_settings(OPENAI_API_KEY="test-openai-key", OPENAI_DRAFT_REARRANGE_ENABLED=True)
    def test_pdf_runs_fast_ai_rearrangement_before_telegram_reply(self):
        self.authorize_user(telegram_user_id=123)
        CostCenter.objects.create(name="Company", normalized_name="empresa")
        CostCenter.objects.create(name="Project", normalized_name="project")
        category = Category.objects.create(name="Labor Terceirizada", normalized_name="mao de project terceirizada")
        work = Work.objects.create(name="Tacima", normalized_name="tacima")
        counterparty = Counterparty.objects.create(
            name="Ivaldo Martins de Freitas",
            normalized_name="ivaldo martins de freitas",
            kind=Counterparty.Kind.WORKER,
            primary_document="02542546401",
            default_category=category,
            source=Origin.IMPORT,
        )
        ai = FakeDraftAIExtractor(
            {
                "amount": 1100,
                "payment_date": "2026-06-26",
                "counterparty_name": "Ivaldo Martins de Freitas",
                "counterparty_id": counterparty.pk,
                "counterparty_document": "02542546401",
                "document_number": "",
                "payment_method": "PIX",
                "description": "Payment Pix para Ivaldo Martins de Freitas",
                "category_name": "Labor Terceirizada",
                "cost_center_name": "Project",
                "work_name": "Tacima",
                "work_item_index": "",
                "confidence": 0.92,
                "needs_review": True,
                "notes": "Rearranjo rápido do receipt.",
            }
        )

        with patch("apps.telegrambot.services.OpenAIPaymentExtractor", return_value=ai):
            result = self.service.process_attachment(
                sender=TelegramSender(telegram_user_id=123, name="Jair"),
                message_id=38,
                attachment=TelegramAttachment(
                    file_id="pdf-file-id",
                    filename="sicredi.pdf",
                    content_type="application/pdf",
                    content=minimal_pdf_with_text(
                        "Receipt de Payment Pix Amount: R$ 1.100,00 "
                        "Solicitante: TIAGO MARCELO ARAUJO DE OLIVEIRA "
                        "Name do destinatário: IVALDO MARTINS DE FREITAS"
                    ),
                    kind=UploadedFile.Kind.PDF,
                ),
            )
        draft = TelegramDraft.objects.get()

        self.assertEqual(ai.calls, 1)
        self.assertEqual(Payment.objects.count(), 0)
        self.assertEqual(draft.counterparty, counterparty)
        self.assertEqual(draft.category, category)
        self.assertEqual(draft.work, work)
        self.assertEqual(draft.cost_center.name, "Project")
        self.assertEqual(draft.amount, 1100)
        self.assertEqual(draft.raw_payload["draft_ai_rearrangement"]["counterparty_name"], "Ivaldo Martins de Freitas")
        self.assertIn("Vendor/Worker: Ivaldo Martins de Freitas", result.reply_text)
        self.assertIn("Project: Tacima", result.reply_text)

    @override_settings(OPENAI_API_KEY="test-openai-key", OPENAI_DRAFT_REARRANGE_ENABLED=True)
    def test_ai_rearrangement_failure_does_not_block_draft_reply(self):
        self.authorize_user(telegram_user_id=123)
        ai = FakeDraftAIExtractor(error=AIExtractionError("timeout"))

        with patch("apps.telegrambot.services.OpenAIPaymentExtractor", return_value=ai):
            result = self.service.process_attachment(
                sender=TelegramSender(telegram_user_id=123, name="Jair"),
                message_id=39,
                attachment=TelegramAttachment(
                    file_id="pdf-file-id",
                    filename="sicredi.pdf",
                    content_type="application/pdf",
                    content=minimal_pdf_with_text("Receipt Pix Amount: R$ 100,00 em 26/06/2026"),
                    kind=UploadedFile.Kind.PDF,
                ),
            )
        draft = TelegramDraft.objects.get()

        self.assertEqual(ai.calls, 1)
        self.assertEqual(draft.amount, 100)
        self.assertEqual(draft.raw_payload["draft_ai_rearrangement_error"], "AIExtractionError")
        self.assertIn("Draft #", result.reply_text)

    def test_authorized_image_creates_uploaded_file_and_active_draft(self):
        self.authorize_user(telegram_user_id=123)

        result = self.service.process_attachment(
            sender=TelegramSender(telegram_user_id=123, name="Jair"),
            message_id=13,
            attachment=TelegramAttachment(
                file_id="image-file-id",
                filename="foto.jpg",
                content_type="image/jpeg",
                content=b"fake-image",
                kind=UploadedFile.Kind.IMAGE,
            ),
        )

        self.assertTrue(result.authorized)
        uploaded_file = UploadedFile.objects.get()
        draft = TelegramDraft.objects.get()
        self.assertEqual(uploaded_file.kind, UploadedFile.Kind.IMAGE)
        self.assertEqual(uploaded_file.content_type, "image/jpeg")
        self.assertEqual(uploaded_file.telegram_file_id, "image-file-id")
        self.assertTrue(uploaded_file.file.name.endswith(".jpg"))
        self.assertEqual(Payment.objects.count(), 0)
        self.assertTrue(draft.needs_ai)
        self.assertIn("Draft #", result.reply_text)

    def test_authorized_ofx_imports_transactions_without_creating_draft(self):
        self.authorize_user(telegram_user_id=123)

        result = self.service.process_attachment(
            sender=TelegramSender(telegram_user_id=123, name="Jair"),
            message_id=16,
            attachment=TelegramAttachment(
                file_id="ofx-file-id",
                filename="extrato.ofx",
                content_type="application/x-ofx",
                content=minimal_ofx().encode("utf-8"),
                kind=UploadedFile.Kind.OFX,
            ),
        )

        self.assertTrue(result.authorized)
        self.assertEqual(UploadedFile.objects.get().kind, UploadedFile.Kind.OFX)
        self.assertEqual(OfxFile.objects.count(), 1)
        self.assertEqual(OfxTransaction.objects.count(), 1)
        self.assertEqual(Payment.objects.count(), 1)
        payment = Payment.objects.get()
        self.assertEqual(payment.source, Origin.OFX)
        self.assertEqual(payment.status, Payment.Status.PENDING_CONFIRMATION)
        self.assertTrue(payment.needs_review)
        self.assertEqual(TelegramDraft.objects.count(), 0)
        self.assertIn("OFX received and imported", result.reply_text)
        self.assertIn("Transactions read: 1", result.reply_text)
        self.assertIn("Suggested payments: 1", result.reply_text)
        self.assertIn("Pending registration/confirmation: 0/1", result.reply_text)
        self.assertIn("Review pending payments in the web interface.", result.reply_text)

    def test_allowed_user_ids_setting_can_authorize_without_database_record(self):
        self.override.disable()
        self.override = override_settings(
            MEDIA_ROOT=Path(self.tempdir.name),
            TELEGRAM_ALLOWED_USER_IDS=["555"],
        )
        self.override.enable()

        result = self.service.process_text(
            sender=TelegramSender(telegram_user_id=555, name="Partner"),
            message_id=14,
            text="texto autorizado pelo env",
        )

        self.assertTrue(result.authorized)
        self.assertEqual(Payment.objects.count(), 0)
        self.assertEqual(TelegramDraft.objects.count(), 1)

    def test_multiple_messages_are_grouped_in_same_active_draft(self):
        self.authorize_user(telegram_user_id=123)

        self.service.process_text(
            sender=TelegramSender(telegram_user_id=123, name="Jair"),
            message_id=20,
            text="Compra de material para project de calçada",
        )
        result = self.service.process_text(
            sender=TelegramSender(telegram_user_id=123, name="Jair"),
            message_id=21,
            text="Project Sertãozinho, calçadas da rua projetada 2",
        )

        draft = TelegramDraft.objects.get()
        self.assertEqual(Payment.objects.count(), 0)
        self.assertEqual(UploadedFile.objects.count(), 2)
        self.assertIn("Compra de material", draft.text_content)
        self.assertIn("Project Sertãozinho", draft.text_content)
        self.assertIn("Draft #", result.reply_text)

    def test_text_correction_updates_draft_counterparty_and_work_by_natural_message(self):
        self.authorize_user(telegram_user_id=123)
        CostCenter.objects.create(name="Company", normalized_name="empresa")
        CostCenter.objects.create(name="Project", normalized_name="project")
        category = Category.objects.create(name="Labor Terceirizada", normalized_name="mao de project terceirizada")
        work = Work.objects.create(name="Tacima", normalized_name="tacima")
        counterparty = Counterparty.objects.create(
            name="Ivaldo Martins de Freitas",
            normalized_name="ivaldo martins de freitas",
            kind=Counterparty.Kind.SUPPLIER,
            primary_document="02542546401",
            default_category=category,
            source=Origin.IMPORT,
        )

        self.service.process_attachment(
            sender=TelegramSender(telegram_user_id=123, name="Jair"),
            message_id=18,
            attachment=TelegramAttachment(
                file_id="pdf-file-id",
                filename="sicredi.pdf",
                content_type="application/pdf",
                content=minimal_pdf_with_text(
                    "Receipt de Payment Pix Amount: R$ 1.100,00 Realized em: 26/06/2026 "
                    "Name do destinatário: IVALDO"
                ),
                kind=UploadedFile.Kind.PDF,
            ),
        )
        result = self.service.process_text(
            sender=TelegramSender(telegram_user_id=123, name="Jair"),
            message_id=19,
            text="Worker Ivaldo Martins project de Tacima",
        )
        draft = TelegramDraft.objects.get()

        self.assertEqual(draft.counterparty, counterparty)
        self.assertEqual(draft.category, category)
        self.assertEqual(draft.work, work)
        self.assertEqual(draft.cost_center.name, "Project")
        self.assertIn("Vendor/Worker: Ivaldo Martins de Freitas", result.reply_text)
        self.assertIn("CPF/CNPJ: 02542546401", result.reply_text)
        self.assertIn("Project: Tacima", result.reply_text)

    def test_finalize_draft_with_pending_counterparty_candidate_does_not_create_payment(self):
        self.authorize_user(telegram_user_id=123)
        self.service.process_attachment(
            sender=TelegramSender(telegram_user_id=123, name="Jair"),
            message_id=37,
            attachment=TelegramAttachment(
                file_id="pdf-file-id",
                filename="sicredi.pdf",
                content_type="application/pdf",
                content=minimal_pdf_with_text(
                    "Receipt de Payment Pix Amount: R$ 1.100,00 Realized em: 26/06/2026 "
                    "Name do destinatário: IVALDO MARTINS DE FREITAS"
                ),
                kind=UploadedFile.Kind.PDF,
            ),
        )
        draft = TelegramDraft.objects.get()

        result = self.service.finalize_draft(draft.pk, TelegramSender(telegram_user_id=123, name="Jair"))
        draft.refresh_from_db()

        self.assertEqual(Payment.objects.count(), 0)
        self.assertEqual(draft.status, TelegramDraft.Status.ACTIVE)
        self.assertIn("Resolva as pending items antes de finalizar", result.reply_text)
        self.assertIn("Suggested registration without confirmation: IVALDO MARTINS DE FREITAS", result.reply_text)

    def test_draft_defaults_to_company_cost_center_when_work_is_not_specified(self):
        self.authorize_user(telegram_user_id=123)
        company = CostCenter.objects.create(name="Company", normalized_name="empresa")
        CostCenter.objects.create(name="Project", normalized_name="project")

        result = self.service.process_text(
            sender=TelegramSender(telegram_user_id=123, name="Jair"),
            message_id=23,
            text="Paguei R$ 180,00 de imposto simples nacional via pix",
        )
        draft = TelegramDraft.objects.get()

        self.assertEqual(draft.cost_center, company)
        self.assertIsNone(draft.work)
        self.assertIn("Cost center: Company", result.reply_text)

    def test_draft_uses_work_cost_center_when_work_is_specified_later(self):
        self.authorize_user(telegram_user_id=123)
        CostCenter.objects.create(name="Company", normalized_name="empresa")
        work_cost_center = CostCenter.objects.create(name="Project", normalized_name="project")
        work = Work.objects.create(name="Sertãozinho", normalized_name="sertaozinho")

        self.service.process_text(
            sender=TelegramSender(telegram_user_id=123, name="Jair"),
            message_id=35,
            text="Paguei R$ 180,00 de outras despesas",
        )
        self.service.process_text(
            sender=TelegramSender(telegram_user_id=123, name="Jair"),
            message_id=36,
            text="project Sertãozinho",
        )
        draft = TelegramDraft.objects.get()

        self.assertEqual(draft.work, work)
        self.assertEqual(draft.cost_center, work_cost_center)

    def test_known_work_text_does_not_leave_pending_work_candidate(self):
        self.authorize_user(telegram_user_id=123)
        CostCenter.objects.create(name="Company", normalized_name="empresa")
        work_cost_center = CostCenter.objects.create(name="Project", normalized_name="project")
        work = Work.objects.create(name="Tacima", normalized_name="tacima")

        result = self.service.process_text(
            sender=TelegramSender(telegram_user_id=123, name="Jair"),
            message_id=40,
            text="project Tacima",
        )
        draft = TelegramDraft.objects.get()

        self.assertEqual(draft.work, work)
        self.assertEqual(draft.cost_center, work_cost_center)
        self.assertNotIn("work_candidate", draft.raw_payload)
        self.assertIn("Project: Tacima", result.reply_text)

    def test_unknown_work_text_is_saved_as_pending_work_candidate(self):
        self.authorize_user(telegram_user_id=123)
        company = CostCenter.objects.create(name="Company", normalized_name="empresa")
        CostCenter.objects.create(name="Project", normalized_name="project")

        result = self.service.process_text(
            sender=TelegramSender(telegram_user_id=123, name="Jair"),
            message_id=41,
            text="project Nova City",
        )
        draft = TelegramDraft.objects.get()

        self.assertIsNone(draft.work)
        self.assertEqual(draft.cost_center, company)
        self.assertEqual(draft.raw_payload["work_candidate"]["name"], "Nova City")
        self.assertIn("Suggested project not registered: Nova City", result.reply_text)

    def test_register_work_button_appears_when_draft_has_pending_work_candidate(self):
        keyboard = draft_keyboard(42, can_register_work=True)

        self.assertEqual(keyboard.inline_keyboard[0][0].text, "Register project")
        self.assertEqual(keyboard.inline_keyboard[0][0].callback_data, "draft:42:register_work")
        self.assertEqual(keyboard.inline_keyboard[0][1].text, "Keep as Company")
        self.assertEqual(keyboard.inline_keyboard[0][1].callback_data, "draft:42:leave_company")
        self.assertEqual(keyboard.inline_keyboard[1][0].callback_data, "draft:42:correct")
        self.assertEqual(keyboard.inline_keyboard[2][0].callback_data, "draft:42:cancel")

    def test_register_work_from_draft_creates_work_and_links_to_draft(self):
        self.authorize_user(telegram_user_id=123)
        company = CostCenter.objects.create(name="Company", normalized_name="empresa")

        self.service.process_text(
            sender=TelegramSender(telegram_user_id=123, name="Jair"),
            message_id=44,
            text="project Tacima",
        )
        draft = TelegramDraft.objects.get()
        self.assertEqual(draft.cost_center, company)

        result = self.service.register_work_from_draft(draft.pk, TelegramSender(telegram_user_id=123, name="Jair"))
        draft.refresh_from_db()
        work = Work.objects.get(normalized_name="tacima")

        self.assertEqual(draft.work, work)
        self.assertEqual(draft.cost_center.normalized_name, "project")
        self.assertNotIn("work_candidate", draft.raw_payload)
        self.assertEqual(work.name, "Tacima")
        self.assertIn("Project registered: Tacima", result.reply_text)
        self.assertIn("no imported budget items yet", result.reply_text)

    def test_register_work_from_draft_reuses_existing_work(self):
        self.authorize_user(telegram_user_id=123)
        CostCenter.objects.create(name="Company", normalized_name="empresa")
        existing_work = Work.objects.create(name="Nova City", normalized_name="nova city")

        draft = TelegramDraft.objects.create(
            telegram_user_id=123,
            status=TelegramDraft.Status.ACTIVE,
            raw_payload={"work_candidate": {"name": "Nova City", "source": "telegram"}},
        )

        result = self.service.register_work_from_draft(draft.pk, TelegramSender(telegram_user_id=123, name="Jair"))
        draft.refresh_from_db()

        self.assertEqual(Work.objects.filter(normalized_name="nova city").count(), 1)
        self.assertEqual(draft.work, existing_work)
        self.assertNotIn("work_candidate", draft.raw_payload)
        self.assertIn("Existing project reused: Nova City", result.reply_text)

    def test_register_work_from_draft_does_not_duplicate_by_normalized_name(self):
        self.authorize_user(telegram_user_id=123)
        CostCenter.objects.create(name="Company", normalized_name="empresa")
        first_draft = TelegramDraft.objects.create(
            telegram_user_id=123,
            status=TelegramDraft.Status.ACTIVE,
            raw_payload={"work_candidate": {"name": "Nova City", "source": "telegram"}},
        )
        second_draft = TelegramDraft.objects.create(
            telegram_user_id=123,
            status=TelegramDraft.Status.ACTIVE,
            raw_payload={"work_candidate": {"name": "Nova   City", "source": "telegram"}},
        )

        self.service.register_work_from_draft(first_draft.pk, TelegramSender(telegram_user_id=123, name="Jair"))
        self.service.register_work_from_draft(second_draft.pk, TelegramSender(telegram_user_id=123, name="Jair"))

        self.assertEqual(Work.objects.filter(normalized_name="nova city").count(), 1)

    def test_finalize_with_new_work_without_budget_creates_payment_without_work_item_index(self):
        self.authorize_user(telegram_user_id=123)
        CostCenter.objects.create(name="Company", normalized_name="empresa")
        counterparty = Counterparty.objects.create(
            name="Anita Jakeline Alves Fields",
            normalized_name="anita jakeline alves campos",
        )

        self.service.process_text(
            sender=TelegramSender(telegram_user_id=123, name="Jair"),
            message_id=45,
            text="Paguei R$ 500 para Anita Jakeline Alves Fields project Tacima",
        )
        draft = TelegramDraft.objects.get()
        self.service.register_work_from_draft(draft.pk, TelegramSender(telegram_user_id=123, name="Jair"))
        draft.refresh_from_db()

        result = self.service.finalize_draft(draft.pk, TelegramSender(telegram_user_id=123, name="Jair"))
        payment = Payment.objects.get()

        self.assertEqual(payment.counterparty, counterparty)
        self.assertEqual(payment.work, draft.work)
        self.assertEqual(payment.work.name, "Tacima")
        self.assertEqual(payment.work_item_index, "")
        self.assertIn("Budget item index: -", result.reply_text)

    def test_draft_counterparty_buttons_appear_when_candidate_is_pending(self):
        keyboard = draft_keyboard(42, can_register_counterparty=True)

        self.assertEqual(keyboard.inline_keyboard[0][0].text, "Register vendor")
        self.assertEqual(keyboard.inline_keyboard[0][0].callback_data, "draft:42:register_supplier")
        self.assertEqual(keyboard.inline_keyboard[0][1].text, "Register worker")
        self.assertEqual(keyboard.inline_keyboard[0][1].callback_data, "draft:42:register_worker")
        self.assertEqual(keyboard.inline_keyboard[1][0].text, "Correct")
        self.assertEqual(keyboard.inline_keyboard[1][0].callback_data, "draft:42:correct")
        self.assertEqual(keyboard.inline_keyboard[2][0].callback_data, "draft:42:cancel")

    def test_draft_keyboard_with_counterparty_and_work_pendencies_resolves_one_by_one(self):
        keyboard = draft_keyboard(42, can_register_counterparty=True, can_register_work=True)

        self.assertEqual(keyboard.inline_keyboard[0][0].callback_data, "draft:42:register_supplier")
        self.assertEqual(keyboard.inline_keyboard[0][1].callback_data, "draft:42:register_worker")
        self.assertEqual(keyboard.inline_keyboard[1][0].callback_data, "draft:42:register_work")
        self.assertEqual(keyboard.inline_keyboard[1][1].callback_data, "draft:42:leave_company")
        self.assertEqual(keyboard.inline_keyboard[2][0].callback_data, "draft:42:correct")
        self.assertEqual(keyboard.inline_keyboard[3][0].callback_data, "draft:42:cancel")

    def test_leave_work_candidate_as_company_clears_pending_work(self):
        self.authorize_user(telegram_user_id=123)
        CostCenter.objects.create(name="Project", normalized_name="project")
        draft = TelegramDraft.objects.create(
            telegram_user_id=123,
            status=TelegramDraft.Status.ACTIVE,
            raw_payload={"work_candidate": {"name": "Tacima", "source": "telegram"}},
        )

        result = self.service.leave_work_candidate_as_company(
            draft.pk,
            TelegramSender(telegram_user_id=123, name="Jair"),
        )
        draft.refresh_from_db()

        self.assertIsNone(draft.work)
        self.assertEqual(draft.cost_center.name, "Company")
        self.assertEqual(draft.work_item_index, "")
        self.assertNotIn("work_candidate", draft.raw_payload)
        self.assertIn("Cost center kept as Company", result.reply_text)
        self.assertNotIn("Suggested project not registered", result.reply_text)

    def test_draft_message_lists_all_blocking_pendencies(self):
        draft = TelegramDraft.objects.create(
            telegram_user_id=123,
            status=TelegramDraft.Status.ACTIVE,
            raw_payload={
                "counterparty_candidate": {
                    "name": "Novo Vendor Ltda",
                    "document": "12345678000199",
                    "alias": "Novo Vendor Ltda",
                    "category_name": "",
                    "source": "telegram",
                    "source_kind": "pdf",
                },
                "work_candidate": {"name": "Tacima", "source": "telegram"},
            },
        )

        message = self.service.build_draft_message(draft)

        self.assertIn("Pending items:", message)
        self.assertIn("Suggested registration without confirmation: Novo Vendor Ltda", message)
        self.assertIn("CPF/CNPJ: 12345678000199", message)
        self.assertIn("Source: PDF", message)
        self.assertIn("Suggested project not registered: Tacima", message)

    def test_old_callback_on_finalized_draft_does_not_create_counterparty_or_change_status(self):
        self.authorize_user(telegram_user_id=123)
        payment = Payment.objects.create(source="telegram", status=Payment.Status.PENDING_CONFIRMATION)
        draft = TelegramDraft.objects.create(
            telegram_user_id=123,
            status=TelegramDraft.Status.FINALIZED,
            finalized_payment=payment,
            raw_payload={
                "counterparty_candidate": {
                    "name": "Novo Vendor Ltda",
                    "document": "",
                    "alias": "Novo Vendor Ltda",
                    "category_name": "",
                    "source": "telegram",
                    "source_kind": "texto",
                }
            },
        )

        result = self.service.register_counterparty_from_draft(
            draft.pk,
            Counterparty.Kind.SUPPLIER,
            TelegramSender(telegram_user_id=123, name="Jair"),
        )
        draft.refresh_from_db()

        self.assertEqual(Counterparty.objects.count(), 0)
        self.assertEqual(draft.status, TelegramDraft.Status.FINALIZED)
        self.assertIn("not active", result.reply_text)

    def test_register_supplier_from_draft_creates_counterparty_before_finalization(self):
        self.authorize_user(telegram_user_id=123)

        result = self.service.process_text(
            sender=TelegramSender(telegram_user_id=123, name="Jair"),
            message_id=46,
            text="beneficiário: Novo Vendor Ltda",
        )
        draft = TelegramDraft.objects.get()

        self.assertIsNone(draft.counterparty)
        self.assertEqual(Payment.objects.count(), 0)
        self.assertIn("Suggested registration:", result.reply_text)

        result = self.service.register_counterparty_from_draft(
            draft.pk,
            Counterparty.Kind.SUPPLIER,
            TelegramSender(telegram_user_id=123, name="Jair"),
        )
        draft.refresh_from_db()
        counterparty = Counterparty.objects.get(normalized_name="novo vendor ltda")

        self.assertEqual(counterparty.kind, Counterparty.Kind.SUPPLIER)
        self.assertEqual(counterparty.source, Origin.TELEGRAM)
        self.assertEqual(draft.counterparty, counterparty)
        self.assertNotIn("counterparty_candidate", draft.raw_payload)
        self.assertEqual(Payment.objects.count(), 0)
        self.assertIn("Vendor registered: Novo Vendor Ltda", result.reply_text)
        self.assertIn("Vendor/Worker: Novo Vendor Ltda", result.reply_text)

    def test_register_worker_from_draft_creates_counterparty_before_finalization(self):
        self.authorize_user(telegram_user_id=123)
        category = Category.objects.create(name="Labor Terceirizada", normalized_name="mao de project terceirizada")

        self.service.process_text(
            sender=TelegramSender(telegram_user_id=123, name="Jair"),
            message_id=47,
            text="worker: João da Silva",
        )
        draft = TelegramDraft.objects.get()
        draft.raw_payload["counterparty_candidate"]["category_name"] = category.name
        draft.save(update_fields=["raw_payload", "updated_at"])

        result = self.service.register_counterparty_from_draft(
            draft.pk,
            Counterparty.Kind.WORKER,
            TelegramSender(telegram_user_id=123, name="Jair"),
        )
        draft.refresh_from_db()
        counterparty = Counterparty.objects.get(normalized_name="joao da silva")

        self.assertEqual(counterparty.kind, Counterparty.Kind.WORKER)
        self.assertEqual(counterparty.default_category, category)
        self.assertEqual(draft.counterparty, counterparty)
        self.assertEqual(draft.category, category)
        self.assertNotIn("counterparty_candidate", draft.raw_payload)
        self.assertIn("Worker registered: João da Silva", result.reply_text)
        self.assertIn("Vendor/Worker: João da Silva", result.reply_text)

    def test_register_counterparty_from_draft_reuses_existing_document(self):
        self.authorize_user(telegram_user_id=123)
        existing = Counterparty.objects.create(
            name="Vendor Existente",
            normalized_name="vendor existente",
            kind=Counterparty.Kind.SUPPLIER,
            primary_document="12345678901",
            source=Origin.IMPORT,
        )
        draft = TelegramDraft.objects.create(
            telegram_user_id=123,
            status=TelegramDraft.Status.ACTIVE,
            raw_payload={
                "counterparty_candidate": {
                    "name": "Name Diferente",
                    "document": "12345678901",
                    "alias": "Name Diferente",
                    "category_name": "",
                    "source": "telegram",
                    "source_kind": "pdf",
                }
            },
        )

        result = self.service.register_counterparty_from_draft(
            draft.pk,
            Counterparty.Kind.WORKER,
            TelegramSender(telegram_user_id=123, name="Jair"),
        )
        draft.refresh_from_db()

        self.assertEqual(Counterparty.objects.count(), 1)
        self.assertEqual(draft.counterparty, existing)
        self.assertNotIn("counterparty_candidate", draft.raw_payload)
        self.assertIn("Worker existing record reused: Vendor Existente", result.reply_text)

    def test_register_counterparty_from_draft_with_ambiguous_name_does_not_create_duplicate(self):
        self.authorize_user(telegram_user_id=123)
        Counterparty.objects.create(
            name="Pessoa Ambígua",
            normalized_name="pessoa ambigua",
            kind=Counterparty.Kind.SUPPLIER,
        )
        Counterparty.objects.create(
            name="Pessoa Ambígua",
            normalized_name="pessoa ambigua",
            kind=Counterparty.Kind.WORKER,
        )
        draft = TelegramDraft.objects.create(
            telegram_user_id=123,
            status=TelegramDraft.Status.ACTIVE,
            raw_payload={
                "counterparty_candidate": {
                    "name": "Pessoa Ambígua",
                    "document": "",
                    "alias": "Pessoa Ambígua",
                    "category_name": "",
                    "source": "telegram",
                    "source_kind": "texto",
                }
            },
        )

        result = self.service.register_counterparty_from_draft(
            draft.pk,
            Counterparty.Kind.SUPPLIER,
            TelegramSender(telegram_user_id=123, name="Jair"),
        )
        draft.refresh_from_db()

        self.assertEqual(Counterparty.objects.filter(normalized_name="pessoa ambigua").count(), 2)
        self.assertIsNone(draft.counterparty)
        self.assertIn("counterparty_candidate", draft.raw_payload)
        self.assertIn("more than one possible record", result.reply_text)

    def test_text_without_specific_work_does_not_create_work_candidate(self):
        self.authorize_user(telegram_user_id=123)
        CostCenter.objects.create(name="Company", normalized_name="empresa")
        CostCenter.objects.create(name="Project", normalized_name="project")

        result = self.service.process_text(
            sender=TelegramSender(telegram_user_id=123, name="Jair"),
            message_id=42,
            text="Compra de material para project de calçada",
        )
        draft = TelegramDraft.objects.get()

        self.assertIsNone(draft.work)
        self.assertNotIn("work_candidate", draft.raw_payload)
        self.assertNotIn("Suggested project not registered", result.reply_text)

    def test_ambiguous_work_text_does_not_choose_arbitrary_work(self):
        self.authorize_user(telegram_user_id=123)
        company = CostCenter.objects.create(name="Company", normalized_name="empresa")
        CostCenter.objects.create(name="Project", normalized_name="project")
        Work.objects.create(name="Nova City Norte", normalized_name="nova cidade norte")
        Work.objects.create(name="Nova City Sul", normalized_name="nova cidade sul")

        result = self.service.process_text(
            sender=TelegramSender(telegram_user_id=123, name="Jair"),
            message_id=43,
            text="project Nova City",
        )
        draft = TelegramDraft.objects.get()

        self.assertIsNone(draft.work)
        self.assertEqual(draft.cost_center, company)
        self.assertNotIn("work_candidate", draft.raw_payload)
        self.assertEqual(draft.raw_payload["work_candidate_ambiguous"]["name"], "Nova City")
        self.assertNotIn("Project: Nova City", result.reply_text)

    def test_named_street_correction_overwrites_previous_budget_index(self):
        self.authorize_user(telegram_user_id=123)
        work = Work.objects.create(name="Sertãozinho", normalized_name="sertaozinho")
        self.create_budget_item(work, "2", "Step", "RUA PAULO EMÍDIO TII")
        self.create_budget_item(work, "2.4", "Subetapa", "CALÇADA")
        self.create_budget_item(work, "9", "Step", "RUA PROJETADA 08-TII")
        self.create_budget_item(work, "9.4", "Subetapa", "CALÇADA")
        draft = TelegramDraft.objects.create(
            telegram_user_id=123,
            status=TelegramDraft.Status.ACTIVE,
            work=work,
            work_item_index="9.4",
        )

        self.service.process_text(
            sender=TelegramSender(telegram_user_id=123, name="Jair"),
            message_id=24,
            text="A rua paulo emidio é outro indice",
        )
        draft.refresh_from_db()

        self.assertEqual(draft.work_item_index, "2.4")

    def test_budget_index_uses_accumulated_draft_text(self):
        self.authorize_user(telegram_user_id=123)
        work = Work.objects.create(name="Sertãozinho", normalized_name="sertaozinho")
        self.create_budget_item(work, "7", "Step", "RUA PROJETADA 07")
        self.create_budget_item(work, "7.4", "Subetapa", "CALÇADA")
        TelegramDraft.objects.create(
            telegram_user_id=123,
            status=TelegramDraft.Status.ACTIVE,
            work=work,
        )
        self.assertEqual(
            infer_budget_item_index(work, "Compra de material para calçada da rua projetada 08", ""),
            "",
        )

        self.service.process_text(
            sender=TelegramSender(telegram_user_id=123, name="Jair"),
            message_id=26,
            text="Compra de material para project de calçada",
        )
        self.service.process_text(
            sender=TelegramSender(telegram_user_id=123, name="Jair"),
            message_id=27,
            text="Rua projetada 07",
        )
        draft = TelegramDraft.objects.get()

        self.assertEqual(draft.work_item_index, "7.4")

    def test_receipt_text_does_not_override_budget_index_from_user_text(self):
        self.authorize_user(telegram_user_id=123)
        work = Work.objects.create(name="Sertãozinho", normalized_name="sertaozinho")
        self.create_budget_item(work, "7", "Step", "RUA PROJETADA 07")
        self.create_budget_item(work, "7.1", "Subetapa", "SERVIÇOS PRELIMINARES")
        self.create_budget_item(work, "7.4", "Subetapa", "CALÇADA")
        TelegramDraft.objects.create(
            telegram_user_id=123,
            status=TelegramDraft.Status.ACTIVE,
            work=work,
        )

        self.service.process_text(
            sender=TelegramSender(telegram_user_id=123, name="Jair"),
            message_id=33,
            text="Compra de material para calçada da rua projetada 07",
        )
        self.service.process_attachment(
            sender=TelegramSender(telegram_user_id=123, name="Jair"),
            message_id=34,
            attachment=TelegramAttachment(
                file_id="pdf-file-id",
                filename="receipt.pdf",
                content_type="application/pdf",
                content=minimal_pdf_with_text("Receipt Pix R$ 100,00 Services por telefone 0800"),
                kind=UploadedFile.Kind.PDF,
            ),
        )
        draft = TelegramDraft.objects.get()

        self.assertEqual(draft.work_item_index, "7.4")
        self.assertNotIn("Services por telefone", draft.text_content)

    def test_ambiguous_projected_street_without_phase_does_not_pick_budget_index(self):
        self.authorize_user(telegram_user_id=123)
        work = Work.objects.create(name="Sertãozinho", normalized_name="sertaozinho")
        self.create_budget_item(work, "8", "Step", "RUA PROJETADA 08 - TI")
        self.create_budget_item(work, "8.4", "Subetapa", "CALÇADA")
        self.create_budget_item(work, "9", "Step", "RUA PROJETADA 08-TII")
        self.create_budget_item(work, "9.4", "Subetapa", "CALÇADA")
        TelegramDraft.objects.create(
            telegram_user_id=123,
            status=TelegramDraft.Status.ACTIVE,
            work=work,
        )

        self.service.process_text(
            sender=TelegramSender(telegram_user_id=123, name="Jair"),
            message_id=28,
            text="Compra de material para calçada da rua projetada 08",
        )
        draft = TelegramDraft.objects.get()

        self.assertEqual(draft.work_item_index, "")

    def test_projected_street_with_tii_phase_selects_budget_index(self):
        self.authorize_user(telegram_user_id=123)
        work = Work.objects.create(name="Sertãozinho", normalized_name="sertaozinho")
        self.create_budget_item(work, "8", "Step", "RUA PROJETADA 08 - TI")
        self.create_budget_item(work, "8.4", "Subetapa", "CALÇADA")
        self.create_budget_item(work, "9", "Step", "RUA PROJETADA 08-TII")
        self.create_budget_item(work, "9.4", "Subetapa", "CALÇADA")
        TelegramDraft.objects.create(
            telegram_user_id=123,
            status=TelegramDraft.Status.ACTIVE,
            work=work,
        )

        self.service.process_text(
            sender=TelegramSender(telegram_user_id=123, name="Jair"),
            message_id=29,
            text="Compra de material para calçada da rua projetada 08 TII",
        )
        draft = TelegramDraft.objects.get()

        self.assertEqual(draft.work_item_index, "9.4")

    def test_budget_index_resolution_works_for_new_non_projected_work(self):
        self.authorize_user(telegram_user_id=123)
        work = Work.objects.create(name="Nova Project Municipal", normalized_name="nova project municipal")
        self.create_budget_item(work, "1", "Step", "AVENIDA CENTRAL")
        self.create_budget_item(work, "1.4", "Subetapa", "CALÇADA")
        TelegramDraft.objects.create(
            telegram_user_id=123,
            status=TelegramDraft.Status.ACTIVE,
            work=work,
        )

        self.service.process_text(
            sender=TelegramSender(telegram_user_id=123, name="Jair"),
            message_id=30,
            text="Compra de material para calçada da avenida central",
        )
        draft = TelegramDraft.objects.get()

        self.assertEqual(draft.work, work)
        self.assertEqual(draft.work_item_index, "1.4")

    def test_budget_index_resolution_works_for_new_work_with_sector_names(self):
        self.authorize_user(telegram_user_id=123)
        work = Work.objects.create(name="Escola Nova", normalized_name="escola nova")
        self.create_budget_item(work, "2", "Step", "BLOCO ADMINISTRATIVO")
        self.create_budget_item(work, "2.3", "Subetapa", "PINTURA")
        TelegramDraft.objects.create(
            telegram_user_id=123,
            status=TelegramDraft.Status.ACTIVE,
            work=work,
        )

        self.service.process_text(
            sender=TelegramSender(telegram_user_id=123, name="Jair"),
            message_id=31,
            text="Compra de tinta para pintura do bloco administractive",
        )
        draft = TelegramDraft.objects.get()

        self.assertEqual(draft.work, work)
        self.assertEqual(draft.work_item_index, "2.3")

    def test_new_work_generic_service_without_stage_remains_without_budget_index(self):
        self.authorize_user(telegram_user_id=123)
        work = Work.objects.create(name="Nova Project Municipal", normalized_name="nova project municipal")
        self.create_budget_item(work, "1", "Step", "AVENIDA CENTRAL")
        self.create_budget_item(work, "1.4", "Subetapa", "CALÇADA")
        self.create_budget_item(work, "2", "Step", "PRAÇA PRINCIPAL")
        self.create_budget_item(work, "2.4", "Subetapa", "CALÇADA")
        TelegramDraft.objects.create(
            telegram_user_id=123,
            status=TelegramDraft.Status.ACTIVE,
            work=work,
        )

        self.service.process_text(
            sender=TelegramSender(telegram_user_id=123, name="Jair"),
            message_id=32,
            text="Compra de material para calçada",
        )
        draft = TelegramDraft.objects.get()

        self.assertEqual(draft.work_item_index, "")

    def test_generic_service_without_street_does_not_pick_arbitrary_budget_index(self):
        self.authorize_user(telegram_user_id=123)
        work = Work.objects.create(name="Sertãozinho", normalized_name="sertaozinho")
        self.create_budget_item(work, "2", "Step", "RUA PAULO EMÍDIO TII")
        self.create_budget_item(work, "2.4", "Subetapa", "CALÇADA")
        self.create_budget_item(work, "9", "Step", "RUA PROJETADA 08-TII")
        self.create_budget_item(work, "9.4", "Subetapa", "CALÇADA")
        TelegramDraft.objects.create(
            telegram_user_id=123,
            status=TelegramDraft.Status.ACTIVE,
            work=work,
        )

        self.service.process_text(
            sender=TelegramSender(telegram_user_id=123, name="Jair"),
            message_id=25,
            text="Compra de material para project de calcada",
        )
        draft = TelegramDraft.objects.get()

        self.assertEqual(draft.work_item_index, "")

    def test_finalize_draft_creates_single_payment(self):
        self.authorize_user(telegram_user_id=123)
        counterparty = Counterparty.objects.create(
            name="Anita Jakeline Alves Fields",
            normalized_name="anita jakeline alves campos",
        )
        self.service.process_text(
            sender=TelegramSender(telegram_user_id=123, name="Jair"),
            message_id=22,
            text="Paguei R$ 2000 para Anita Jakeline Alves Fields via pix em 23/06/2026",
        )
        draft = TelegramDraft.objects.get()

        result = self.service.finalize_draft(draft.pk, TelegramSender(telegram_user_id=123, name="Jair"))
        draft.refresh_from_db()

        self.assertEqual(Payment.objects.count(), 1)
        payment = Payment.objects.get()
        self.assertEqual(draft.status, TelegramDraft.Status.FINALIZED)
        self.assertEqual(draft.finalized_payment, payment)
        self.assertEqual(payment.counterparty, counterparty)
        self.assertEqual(payment.amount, 2000)
        self.assertEqual(payment.status, Payment.Status.PENDING_CONFIRMATION)
        self.assertIn("Payment suggestion", result.reply_text)

    def test_text_after_correction_updates_existing_payment_instead_of_creating_new_one(self):
        self.authorize_user(telegram_user_id=123)
        wrong = Counterparty.objects.create(
            name="Tiago Marcelo Araujo de Oliveira",
            normalized_name="tiago marcelo araujo de oliveira",
        )
        right = Counterparty.objects.create(
            name="Anita Jakeline Alves Fields",
            normalized_name="anita jakeline alves campos",
        )
        payment = Payment.objects.create(
            counterparty=wrong,
            status=Payment.Status.CORRECTING,
            source="telegram",
            raw_payload={"telegram_user_id": 123},
            needs_review=True,
        )

        result = self.service.process_text(
            sender=TelegramSender(telegram_user_id=123, name="Jair"),
            message_id=15,
            text="vendor correto é Anita Jakeline Alves Fields",
        )
        payment.refresh_from_db()

        self.assertTrue(result.authorized)
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(UploadedFile.objects.count(), 0)
        self.assertEqual(payment.counterparty, right)
        self.assertEqual(payment.status, Payment.Status.PENDING_CONFIRMATION)
        self.assertIn("Correction applied", result.reply_text)
        self.assertIn("Vendor/Worker: Anita Jakeline Alves Fields", result.reply_text)

    def authorize_user(self, telegram_user_id):
        return AuthorizedTelegramUser.objects.create(
            telegram_user_id=telegram_user_id,
            name="User autorizado",
            username="autorizado",
            is_active=True,
        )

    def create_budget_item(self, work, index, item_type, description):
        item_type_map = {
            "Step": BudgetItem.ItemType.STAGE,
            "Subetapa": BudgetItem.ItemType.SUBSTAGE,
            "Item": BudgetItem.ItemType.ITEM,
        }
        return BudgetItem.objects.create(
            work=work,
            index=index,
            parent_index=index.rsplit(".", 1)[0] if "." in index else "",
            item_type=item_type_map[item_type],
            description=description,
            normalized_description=description.lower(),
        )

    def test_confirmation_keyboard_uses_payment_callback_data(self):
        keyboard = confirmation_keyboard(42)
        buttons = keyboard.inline_keyboard[0]

        self.assertEqual(buttons[0].callback_data, "payment:42:approve")
        self.assertEqual(buttons[1].callback_data, "payment:42:correct")
        self.assertEqual(buttons[2].callback_data, "payment:42:cancel")

    def test_parse_callback_data(self):
        action, payment_id = parse_callback_data("payment:42:approve")

        self.assertEqual(action, "approve")
        self.assertEqual(payment_id, 42)

    def test_counterparty_registration_keyboard_uses_callback_data(self):
        keyboard = counterparty_registration_keyboard(42)
        first_row = keyboard.inline_keyboard[0]
        second_row = keyboard.inline_keyboard[1]

        self.assertEqual(first_row[0].callback_data, "counterparty:42:supplier")
        self.assertEqual(first_row[1].callback_data, "counterparty:42:worker")
        self.assertEqual(first_row[2].callback_data, "payment:42:correct")
        self.assertEqual(second_row[0].callback_data, "payment:42:cancel")

    def test_parse_counterparty_callback_data(self):
        kind, payment_id = parse_counterparty_callback_data("counterparty:42:worker")

        self.assertEqual(kind, Counterparty.Kind.WORKER)
        self.assertEqual(payment_id, 42)

    def test_draft_keyboard_uses_draft_callback_data(self):
        keyboard = draft_keyboard(42)
        first_row = keyboard.inline_keyboard[0]
        second_row = keyboard.inline_keyboard[1]

        self.assertEqual(first_row[0].callback_data, "draft:42:finalize")
        self.assertEqual(first_row[1].callback_data, "draft:42:new")
        self.assertEqual(second_row[0].callback_data, "draft:42:cancel")

    def test_parse_draft_callback_data(self):
        action, draft_id = parse_draft_callback_data("draft:42:finalize")

        self.assertEqual(action, "finalize")
        self.assertEqual(draft_id, 42)

        action, draft_id = parse_draft_callback_data("draft:42:register_work")

        self.assertEqual(action, "register_work")
        self.assertEqual(draft_id, 42)

        action, draft_id = parse_draft_callback_data("draft:42:leave_company")

        self.assertEqual(action, "leave_company")
        self.assertEqual(draft_id, 42)

        action, draft_id = parse_draft_callback_data("draft:42:register_supplier")

        self.assertEqual(action, "register_supplier")
        self.assertEqual(draft_id, 42)

        action, draft_id = parse_draft_callback_data("draft:42:register_worker")

        self.assertEqual(action, "register_worker")
        self.assertEqual(draft_id, 42)

        action, draft_id = parse_draft_callback_data("draft:42:correct")

        self.assertEqual(action, "correct")
        self.assertEqual(draft_id, 42)


def minimal_pdf_with_text(text):
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    chunks = [b"%PDF-1.4\n"]
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(sum(len(chunk) for chunk in chunks))
        chunks.append(f"{index} 0 obj\n".encode("ascii") + obj + b"\nendobj\n")
    xref_offset = sum(len(chunk) for chunk in chunks)
    chunks.append(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    chunks.append(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        chunks.append(f"{offset:010d} 00000 n \n".encode("ascii"))
    chunks.append(
        f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    return b"".join(chunks)


def minimal_ofx():
    return """
OFXHEADER:100
DATA:OFXSGML

<OFX>
<BANKMSGSRSV1>
<STMTTRNRS>
<STMTRS>
<BANKACCTFROM>
<BANKID>748
<ACCTID>12345
</BANKACCTFROM>
<BANKTRANLIST>
<DTSTART>20260601000000
<DTEND>20260630000000
<STMTTRN>
<TRNTYPE>DEBIT
<DTPOSTED>20260624000000
<TRNAMT>-250.00
<FITID>FIT-1
<MEMO>PAGAMENTO PIX-PIX_DEB   12345678000199 ACME Materiais
</STMTTRN>
</BANKTRANLIST>
</STMTRS>
</STMTTRNRS>
</BANKMSGSRSV1>
</OFX>
"""


class FakeDraftAIExtractor:
    def __init__(self, payload=None, error=None):
        self.payload = payload or {}
        self.error = error
        self.calls = 0
        self.payments = []

    def extract(self, payment):
        self.calls += 1
        self.payments.append(payment)
        if self.error:
            raise self.error
        return parse_ai_extraction_response(json.dumps(self.payload))
