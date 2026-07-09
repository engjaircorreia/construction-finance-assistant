import json
import logging
from pathlib import Path
from io import StringIO
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.test import TestCase, override_settings

from apps.accounts.models import AuthorizedTelegramUser
from apps.core.log_safety import SafeFormatter, SafeLogFilter
from apps.counterparties.models import BudgetItem, Category, Counterparty, CounterpartyDocument, CostCenter, Origin, Work
from apps.documents.models import UploadedFile

from .ai_extraction import (
    AIExtractionError,
    AIExtractionValidationError,
    OpenAIPaymentExtractor,
    apply_ai_extraction_to_payment,
    build_minimal_context,
    build_openai_input,
    parse_ai_extraction_response,
)
from .models import Payment


VALID_AI_JSON = json.dumps(
    {
        "amount": 123.45,
        "payment_date": "2026-06-24",
        "counterparty_name": "ACME Materiais",
        "counterparty_id": None,
        "counterparty_document": "12345678000199",
        "document_number": "NF-123",
        "payment_method": "PIX",
        "description": "Compra de materiais",
        "category_name": "Materiais",
        "cost_center_name": "Project",
        "work_name": "Sertãozinho",
        "work_item_index": "3.4",
        "confidence": 0.92,
        "needs_review": True,
        "notes": "Extraído de receipt",
    }
)


class OpenAIPaymentExtractionTests(TestCase):
    def setUp(self):
        self.tempdir = TemporaryDirectory()
        self.override = override_settings(
            MEDIA_ROOT=Path(self.tempdir.name),
            OPENAI_API_KEY="test-openai-secret-key",
            TELEGRAM_BOT_TOKEN="test-telegram-secret-token",
            DJANGO_SECRET_KEY="test-django-secret-key",
        )
        self.override.enable()
        self.category = Category.objects.create(name="Materiais", normalized_name="materiais")
        self.cost_center = CostCenter.objects.create(name="Project", normalized_name="project")
        self.company_cost_center = CostCenter.objects.create(name="Company", normalized_name="empresa")
        self.work = Work.objects.create(name="Sertãozinho", normalized_name="sertaozinho")
        self.budget_item = BudgetItem.objects.create(
            work=self.work,
            index="3.4.6",
            parent_index="3.4",
            item_type=BudgetItem.ItemType.ITEM,
            description="ALVENARIA DE CALÇADA",
            normalized_description="alvenaria de calcada",
        )
        self.counterparty = Counterparty.objects.create(
            name="ACME Materiais",
            normalized_name="acme materiais",
            kind=Counterparty.Kind.SUPPLIER,
            default_category=self.category,
            default_cost_center=self.cost_center,
            default_work=self.work,
        )

    def tearDown(self):
        self.override.disable()
        self.tempdir.cleanup()

    def test_parser_accepts_valid_json(self):
        extraction = parse_ai_extraction_response(VALID_AI_JSON)

        self.assertEqual(extraction.amount.as_tuple().digits, (1, 2, 3, 4, 5))
        self.assertEqual(extraction.payment_date.isoformat(), "2026-06-24")
        self.assertEqual(extraction.counterparty_name, "ACME Materiais")
        self.assertEqual(extraction.payment_method, "PIX")
        self.assertTrue(extraction.needs_review)

    def test_parser_rejects_invalid_json(self):
        with self.assertRaises(AIExtractionValidationError):
            parse_ai_extraction_response("{not-json")

    def test_parser_rejects_incomplete_json(self):
        with self.assertRaises(AIExtractionValidationError):
            parse_ai_extraction_response(json.dumps({"amount": 100}))

    def test_ai_response_never_approves_payment_automatically(self):
        payment = Payment.objects.create(status=Payment.Status.RECEIVED, source="telegram", needs_review=True)
        extraction = parse_ai_extraction_response(VALID_AI_JSON)

        apply_ai_extraction_to_payment(payment, extraction)
        payment.refresh_from_db()

        self.assertEqual(payment.amount, extraction.amount)
        self.assertEqual(payment.counterparty, self.counterparty)
        self.assertEqual(payment.status, Payment.Status.RECEIVED)
        self.assertTrue(payment.needs_review)
        self.assertEqual(payment.user_action, "")
        self.assertIsNone(payment.confirmed_at)
        self.assertIn("ai_extraction", payment.raw_payload)

    def test_ai_response_cannot_leave_payment_approved(self):
        payment = Payment.objects.create(status=Payment.Status.APPROVED, source="telegram", needs_review=False)
        extraction = parse_ai_extraction_response(VALID_AI_JSON)

        apply_ai_extraction_to_payment(payment, extraction)
        payment.refresh_from_db()

        self.assertEqual(payment.status, Payment.Status.PENDING_CONFIRMATION)
        self.assertTrue(payment.needs_review)

    def test_openai_payload_does_not_include_environment_secrets(self):
        AuthorizedTelegramUser.objects.create(telegram_user_id=123, name="Jair Correia", username="jair")
        uploaded_file = UploadedFile.objects.create(
            original_filename="receipt.txt",
            source=UploadedFile.Source.TELEGRAM,
            kind=UploadedFile.Kind.TEXT,
            content_type="text/plain",
            extracted_text="Paguei R$ 100 para ACME Materiais via PIX",
            telegram_user_id=123,
            notes="Telegram ID: 123; Name: Jair Correia; Username: @jair",
        )
        uploaded_file.file.save("receipt.txt", ContentFile(b"Paguei R$ 100"), save=True)
        payment = Payment.objects.create(uploaded_file=uploaded_file, source="telegram")

        payload = json.dumps(build_openai_input(payment), ensure_ascii=False)

        self.assertNotIn("test-openai-secret-key", payload)
        self.assertNotIn("test-telegram-secret-token", payload)
        self.assertNotIn("test-django-secret-key", payload)
        self.assertIn("ACME Materiais", payload)
        self.assertIn("receiver/beneficiary/recipient", payload)
        self.assertIn("is not the vendor/worker", payload)
        self.assertIn("Jair Correia", payload)

    def test_minimal_context_includes_budget_items(self):
        context = build_minimal_context()

        self.assertIn(
            {
                "work": "Sertãozinho",
                "index": "3.4.6",
                "parent_index": "3.4",
                "type": BudgetItem.ItemType.ITEM,
                "description": "ALVENARIA DE CALÇADA",
            },
            context["budget_items"],
        )

    def test_minimal_context_prefers_documented_duplicate_counterparty(self):
        Counterparty.objects.create(
            name="ANITA JAKELINE ALVES CAMPOS",
            normalized_name="anita jakeline alves campos",
            kind=Counterparty.Kind.SUPPLIER,
            source=Origin.HISTORICAL,
        )
        documented = Counterparty.objects.create(
            name="Anita Jakeline Alves Fields",
            normalized_name="anita jakeline alves campos",
            kind=Counterparty.Kind.SUPPLIER,
            person_type=Counterparty.PersonType.INDIVIDUAL,
            primary_document="05783796433",
            source=Origin.IMPORT,
        )
        CounterpartyDocument.objects.create(
            counterparty=documented,
            document_type=CounterpartyDocument.DocumentType.CPF,
            number="05783796433",
            source=Origin.IMPORT,
            is_primary=True,
        )

        context = build_minimal_context()
        anita_entries = [
            counterparty
            for counterparty in context["counterparties"]
            if counterparty["name"].casefold() == "anita jakeline alves fields"
        ]

        self.assertEqual(len(anita_entries), 1)
        self.assertEqual(anita_entries[0]["id"], documented.pk)

    def test_ai_extraction_prefers_documented_duplicate_even_when_wrong_id_is_returned(self):
        undocumented = Counterparty.objects.create(
            name="ANITA JAKELINE ALVES CAMPOS",
            normalized_name="anita jakeline alves campos",
            kind=Counterparty.Kind.SUPPLIER,
            source=Origin.HISTORICAL,
        )
        documented = Counterparty.objects.create(
            name="Anita Jakeline Alves Fields",
            normalized_name="anita jakeline alves campos",
            kind=Counterparty.Kind.SUPPLIER,
            person_type=Counterparty.PersonType.INDIVIDUAL,
            primary_document="05783796433",
            source=Origin.IMPORT,
        )
        CounterpartyDocument.objects.create(
            counterparty=documented,
            document_type=CounterpartyDocument.DocumentType.CPF,
            number="05783796433",
            source=Origin.IMPORT,
            is_primary=True,
        )
        payment = Payment.objects.create(status=Payment.Status.RECEIVED, source="telegram", needs_review=True)
        extraction = parse_ai_extraction_response(
            json.dumps(
                {
                    "amount": 2000,
                    "payment_date": "2026-06-23",
                    "counterparty_name": "Anita Jakeline Alves Fields",
                    "counterparty_id": undocumented.pk,
                    "counterparty_document": "",
                    "document_number": "",
                    "payment_method": "PIX",
                    "description": "Payment Pix",
                    "category_name": "Materiais",
                    "cost_center_name": "Project",
                    "work_name": "Sertãozinho",
                    "work_item_index": "",
                    "confidence": 0.80,
                    "needs_review": True,
                    "notes": "Cadastro duplicado sem documento foi retornado.",
                }
            )
        )

        apply_ai_extraction_to_payment(payment, extraction)
        payment.refresh_from_db()

        self.assertEqual(payment.counterparty, documented)

    def test_ai_extraction_defaults_to_company_cost_center_when_work_is_omitted(self):
        payment = Payment.objects.create(status=Payment.Status.RECEIVED, source="telegram", needs_review=True)
        extraction = parse_ai_extraction_response(
            json.dumps(
                {
                    "amount": 180,
                    "payment_date": "2026-06-24",
                    "counterparty_name": "ACME Materiais",
                    "counterparty_id": self.counterparty.pk,
                    "counterparty_document": "",
                    "document_number": "",
                    "payment_method": "PIX",
                    "description": "Payment de imposto sem project informada",
                    "category_name": "Other Expenses",
                    "cost_center_name": "",
                    "work_name": "",
                    "work_item_index": "",
                    "confidence": 0.80,
                    "needs_review": True,
                    "notes": "No project específica foi citada.",
                }
            )
        )

        apply_ai_extraction_to_payment(payment, extraction)
        payment.refresh_from_db()

        self.assertEqual(payment.counterparty, self.counterparty)
        self.assertEqual(payment.cost_center, self.company_cost_center)
        self.assertIsNone(payment.work)
        self.assertEqual(payment.work_item_index, "")

    def test_ai_extraction_ignores_requester_as_counterparty_when_text_marks_non_counterparty_role(self):
        requester = Counterparty.objects.create(
            name="Tiago Marcelo Araujo de Oliveira",
            normalized_name="tiago marcelo araujo de oliveira",
            kind=Counterparty.Kind.SUPPLIER,
        )
        uploaded_file = UploadedFile.objects.create(
            original_filename="receipt.pdf",
            source=UploadedFile.Source.TELEGRAM,
            kind=UploadedFile.Kind.PDF,
            content_type="application/pdf",
            extracted_text=(
                "Receipt de Payment Pix Amount: R$ 2.000,00 "
                "Solicitante: TIAGO MARCELO ARAUJO DE OLIVEIRA "
                "Name do destinatário: Anita Jakeline Alves Fields"
            ),
        )
        payment = Payment.objects.create(uploaded_file=uploaded_file, source="telegram")
        extraction = parse_ai_extraction_response(
            json.dumps(
                {
                    "amount": 2000,
                    "payment_date": "2026-06-23",
                    "counterparty_name": requester.name,
                    "counterparty_id": requester.pk,
                    "counterparty_document": "10146708458",
                    "document_number": "",
                    "payment_method": "PIX",
                    "description": "Payment Pix",
                    "category_name": "Materiais",
                    "cost_center_name": "Project",
                    "work_name": "Sertãozinho",
                    "work_item_index": "",
                    "confidence": 0.80,
                    "needs_review": True,
                    "notes": "Name retornado aparece como solicitante.",
                }
            )
        )

        apply_ai_extraction_to_payment(payment, extraction)
        payment.refresh_from_db()

        self.assertIsNone(payment.counterparty)
        self.assertNotIn("counterparty_candidate", payment.raw_payload)

    def test_extractor_uses_structured_outputs_and_parses_client_response(self):
        fake_client = FakeOpenAIClient(VALID_AI_JSON)
        payment = Payment.objects.create(source="telegram")

        extraction = OpenAIPaymentExtractor(client=fake_client, model="test-model").extract(payment)

        self.assertEqual(extraction.amount, payment_decimal("123.45"))
        kwargs = fake_client.responses.last_kwargs
        self.assertEqual(kwargs["model"], "test-model")
        self.assertFalse(kwargs["store"])
        self.assertEqual(kwargs["text"]["format"]["type"], "json_schema")
        self.assertTrue(kwargs["text"]["format"]["strict"])

    def test_extractor_configures_openai_client_timeout(self):
        payment = Payment.objects.create(source="telegram")
        with override_settings(OPENAI_REQUEST_TIMEOUT_SECONDS=3), patch("apps.payments.ai_extraction.OpenAI") as openai:
            openai.return_value = FakeOpenAIClient(VALID_AI_JSON)

            OpenAIPaymentExtractor().extract(payment)

        openai.assert_called_once_with(api_key="test-openai-secret-key", timeout=3)

    def test_extractor_wraps_openai_client_errors(self):
        payment = Payment.objects.create(source="telegram")
        fake_client = FakeOpenAIClient(VALID_AI_JSON, error=RuntimeError("boom"))

        with self.assertRaises(AIExtractionError):
            OpenAIPaymentExtractor(client=fake_client).extract(payment)

    def test_openai_failure_log_does_not_expose_api_key_or_document(self):
        payment = Payment.objects.create(source="telegram")
        fake_client = FakeOpenAIClient(
            VALID_AI_JSON,
            error=RuntimeError("boom test-openai-secret-key 123.456.789-01"),
        )
        stream = StringIO()
        handler = logging.StreamHandler(stream)
        handler.addFilter(SafeLogFilter())
        handler.setFormatter(SafeFormatter("%(message)s"))
        logger = logging.getLogger("apps.payments.ai_extraction")
        old_handlers = logger.handlers[:]
        old_level = logger.level
        old_propagate = logger.propagate
        try:
            logger.handlers = [handler]
            logger.setLevel(logging.WARNING)
            logger.propagate = False
            with self.assertRaises(AIExtractionError):
                OpenAIPaymentExtractor(client=fake_client).extract(payment)
        finally:
            logger.handlers = old_handlers
            logger.setLevel(old_level)
            logger.propagate = old_propagate

        output = stream.getvalue()
        self.assertIn("OpenAI extraction failed", output)
        self.assertNotIn("test-openai-secret-key", output)
        self.assertNotIn("123.456.789-01", output)
        self.assertIn("***.***.***-**", output)


class FakeResponses:
    def __init__(self, output_text, error=None):
        self.output_text = output_text
        self.error = error
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        if self.error:
            raise self.error
        return SimpleNamespace(output_text=self.output_text)


class FakeOpenAIClient:
    def __init__(self, output_text, error=None):
        self.responses = FakeResponses(output_text, error=error)


def payment_decimal(value):
    from decimal import Decimal

    return Decimal(value)
