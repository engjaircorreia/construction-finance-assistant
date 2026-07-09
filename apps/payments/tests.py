import csv
from datetime import date
from decimal import Decimal
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from django.contrib import admin
from django.core.files.base import ContentFile
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.test import TestCase, override_settings
from openpyxl import Workbook

from apps.counterparties.models import (
    BudgetItem,
    Category,
    ChartOfAccount,
    Counterparty,
    CounterpartyAlias,
    CounterpartyDocument,
    CostCenter,
    Origin,
    Work,
)
from apps.documents.models import UploadedFile

from .confirmation import (
    PaymentConfirmationError,
    approve_payment,
    cancel_payment,
    format_payment_suggestion,
    request_payment_correction,
)
from .counterparty_resolution import (
    AmbiguousCounterpartyError,
    confirm_counterparty_for_payment,
    prepare_counterparty_review,
)
from .corrections import apply_text_correction_to_payment
from .extraction import (
    apply_extraction_to_payment,
    extract_from_text,
    extract_from_uploaded_file,
    extract_text_from_pdf_bytes,
)
from .importers import import_payment_history
from .models import Payment, PaymentConfirmation


class PaymentModelTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(name="Services", normalized_name="servicos")
        self.cost_center = CostCenter.objects.create(name="Jaurez Távora", normalized_name="jaurez tavora")
        self.work = Work.objects.create(name="Jaurez Távora", normalized_name="project-jaurez-tavora")
        self.counterparty = Counterparty.objects.create(
            name="Worker Teste",
            normalized_name="worker teste",
            kind=Counterparty.Kind.WORKER,
            person_type=Counterparty.PersonType.INDIVIDUAL,
        )
        self.uploaded_file = UploadedFile.objects.create(
            original_filename="recibo.jpg",
            source=UploadedFile.Source.TELEGRAM,
            kind=UploadedFile.Kind.IMAGE,
        )

    def test_payment_models_are_admin_registered(self):
        self.assertTrue(admin.site.is_registered(Payment))
        self.assertTrue(admin.site.is_registered(PaymentConfirmation))

    def test_payment_starts_pending_review_when_created_from_telegram(self):
        payment = Payment.objects.create(
            payment_date=date(2026, 6, 24),
            amount=Decimal("150.50"),
            counterparty=self.counterparty,
            category=self.category,
            cost_center=self.cost_center,
            work=self.work,
            work_item_index="3.4",
            uploaded_file=self.uploaded_file,
        )

        self.assertEqual(payment.source, "telegram")
        self.assertEqual(payment.status, Payment.Status.RECEIVED)
        self.assertTrue(payment.needs_review)

    def test_payment_confirmation_records_user_action_without_changing_payment_implicitly(self):
        payment = Payment.objects.create(amount=Decimal("99.90"), counterparty=self.counterparty)

        confirmation = PaymentConfirmation.objects.create(
            payment=payment,
            telegram_user_id=123456,
            action=Payment.ConfirmationAction.APPROVE,
            message="ok",
        )
        payment.refresh_from_db()

        self.assertEqual(confirmation.action, Payment.ConfirmationAction.APPROVE)
        self.assertEqual(payment.status, Payment.Status.RECEIVED)
        self.assertEqual(payment.user_action, "")

    def test_referenced_counterparty_is_protected_from_deletion(self):
        Payment.objects.create(amount=Decimal("20.00"), counterparty=self.counterparty)

        with self.assertRaises(ProtectedError):
            self.counterparty.delete()

    def test_payment_amount_cannot_be_negative(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Payment.objects.create(amount=Decimal("-1.00"), counterparty=self.counterparty)

    def test_payment_confidence_must_be_between_zero_and_one(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Payment.objects.create(
                amount=Decimal("10.00"),
                counterparty=self.counterparty,
                confidence=Decimal("1.25"),
            )

    def test_invalid_status_is_rejected_by_model_validation(self):
        payment = Payment(amount=Decimal("10.00"), status="status_inexistente")

        with self.assertRaises(ValidationError):
            payment.full_clean()

    def test_invalid_confirmation_action_is_rejected_by_model_validation(self):
        payment = Payment.objects.create(amount=Decimal("10.00"), counterparty=self.counterparty)
        confirmation = PaymentConfirmation(payment=payment, action="acao_inexistente")

        with self.assertRaises(ValidationError):
            confirmation.full_clean()


class PaymentExtractionTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(name="Materiais", normalized_name="materiais")
        self.cost_center = CostCenter.objects.create(name="Project", normalized_name="project")
        self.company_cost_center = CostCenter.objects.create(name="Company", normalized_name="empresa")
        self.work = Work.objects.create(name="Sertãozinho", normalized_name="sertaozinho")
        self.budget_item = BudgetItem.objects.create(
            work=self.work,
            index="3.4",
            item_type=BudgetItem.ItemType.SUBSTAGE,
            description="CALÇADA",
            normalized_description="calcada",
        )
        self.counterparty = Counterparty.objects.create(
            name="ACME Materiais",
            normalized_name="acme materiais",
            kind=Counterparty.Kind.SUPPLIER,
            default_category=self.category,
            default_cost_center=self.cost_center,
            default_work=self.work,
        )
        self.tempdir = TemporaryDirectory()
        self.override = override_media_root(self.tempdir.name)
        self.override.enable()

    def tearDown(self):
        self.override.disable()
        self.tempdir.cleanup()

    def test_extracts_simple_text_payment_date(self):
        extraction = extract_from_text("Paguei R$ 1.250,50 para ACME Materiais via PIX em 05/06/2026")

        self.assertEqual(extraction.amount, Decimal("1250.50"))
        self.assertEqual(extraction.payment_date, date(2026, 6, 5))
        self.assertEqual(extraction.counterparty, self.counterparty)
        self.assertEqual(extraction.payment_method, "PIX")
        self.assertFalse(extraction.needs_ai)
        self.assertGreaterEqual(extraction.confidence, Decimal("0.80"))

    def test_pix_receipt_uses_recipient_not_requester_as_counterparty(self):
        requester = Counterparty.objects.create(
            name="Tiago Marcelo Araujo de Oliveira",
            normalized_name="tiago marcelo araujo de oliveira",
            kind=Counterparty.Kind.SUPPLIER,
        )
        recipient = Counterparty.objects.create(
            name="Anita Jakeline Alves Fields",
            normalized_name="anita jakeline alves campos",
            kind=Counterparty.Kind.SUPPLIER,
        )
        text = (
            "Receipt de Payment Pix Amount: R$ 2.000,00 Realized em: 23/06/2026 "
            "Solicitante: TIAGO MARCELO ARAUJO DE OLIVEIRA "
            "Name do destinatário: Anita Jakeline Alves Fields "
            "Name do pagador: Inplant Engenharia E Planejamento Ltda"
        )

        extraction = extract_from_text(text, source_kind=UploadedFile.Kind.PDF)

        self.assertEqual(extraction.counterparty, recipient)
        self.assertNotEqual(extraction.counterparty, requester)

    def test_pix_receipt_uses_beneficiary_not_requester_as_counterparty(self):
        requester = Counterparty.objects.create(
            name="Tiago Marcelo Araujo de Oliveira",
            normalized_name="tiago marcelo araujo de oliveira",
            kind=Counterparty.Kind.SUPPLIER,
        )
        beneficiary = Counterparty.objects.create(
            name="Anita Jakeline Alves Fields",
            normalized_name="anita jakeline alves campos",
            kind=Counterparty.Kind.SUPPLIER,
        )
        text = (
            "Receipt de Payment Pix Amount: R$ 2.000,00 Realized em: 23/06/2026 "
            "Solicitante: TIAGO MARCELO ARAUJO DE OLIVEIRA "
            "Name do beneficiário: Anita Jakeline Alves Fields"
        )

        extraction = extract_from_text(text, source_kind=UploadedFile.Kind.PDF)

        self.assertEqual(extraction.counterparty, beneficiary)
        self.assertNotEqual(extraction.counterparty, requester)

    def test_counterparty_match_prefers_documented_duplicate(self):
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
        text = (
            "Receipt de Payment Pix Amount: R$ 2.000,00 "
            "Name do destinatário: Anita Jakeline Alves Fields"
        )

        extraction = extract_from_text(text, source_kind=UploadedFile.Kind.PDF)

        self.assertEqual(extraction.counterparty, documented)

    def test_counterparty_match_ignores_requester_when_no_recipient_is_known(self):
        Counterparty.objects.create(
            name="Tiago Marcelo Araujo de Oliveira",
            normalized_name="tiago marcelo araujo de oliveira",
            kind=Counterparty.Kind.SUPPLIER,
        )
        text = (
            "Receipt de Payment Pix Amount: R$ 2.000,00 Realized em: 23/06/2026 "
            "Solicitante: TIAGO MARCELO ARAUJO DE OLIVEIRA "
            "Name do destinatário: Pessoa Ainda Nao Cadastrada"
        )

        extraction = extract_from_text(text, source_kind=UploadedFile.Kind.PDF)

        self.assertIsNone(extraction.counterparty)
        self.assertEqual(extraction.counterparty_candidate_name, "Pessoa Ainda Nao Cadastrada")

    def test_pix_receipt_keeps_unknown_recipient_as_registration_candidate(self):
        text = (
            "Receipt de Payment Pix Amount: R$ 1.100,00 Realized em: 26/06/2026 "
            "Solicitante: TIAGO MARCELO ARAUJO DE OLIVEIRA "
            "Name do destinatário: IVALDO MARTINS DE FREITAS "
            "CPF do destinatário: ***.425.464-** "
            "Name do pagador: Inplant Engenharia E Planejamento Ltda"
        )

        extraction = extract_from_text(text, source_kind=UploadedFile.Kind.PDF)

        self.assertIsNone(extraction.counterparty)
        self.assertEqual(extraction.counterparty_candidate_name, "IVALDO MARTINS DE FREITAS")

    def test_apply_extraction_keeps_payment_pending_review(self):
        payment = Payment.objects.create(source="telegram", status=Payment.Status.RECEIVED, needs_review=True)
        extraction = extract_from_text("Paguei R$ 200 para ACME Materiais via pix em 05/06/2026")

        apply_extraction_to_payment(payment, extraction)
        payment.refresh_from_db()

        self.assertEqual(payment.amount, Decimal("200.00"))
        self.assertEqual(payment.payment_date, date(2026, 6, 5))
        self.assertEqual(payment.counterparty, self.counterparty)
        self.assertEqual(payment.category, self.category)
        self.assertEqual(payment.cost_center, self.company_cost_center)
        self.assertIsNone(payment.work)
        self.assertEqual(payment.payment_method, "PIX")
        self.assertEqual(payment.status, Payment.Status.RECEIVED)
        self.assertTrue(payment.needs_review)
        self.assertEqual(payment.user_action, "")
        self.assertIn("initial_extraction", payment.raw_payload)

    def test_extracts_searchable_pdf_text(self):
        pdf_bytes = minimal_pdf_with_text("Payment R$ 300,00 ACME Materiais PIX 06/06/2026")

        text = extract_text_from_pdf_bytes(pdf_bytes)

        self.assertIn("ACME Materiais", text)
        extraction = extract_from_text(text, source_kind=UploadedFile.Kind.PDF)
        self.assertEqual(extraction.amount, Decimal("300.00"))
        self.assertEqual(extraction.payment_date, date(2026, 6, 6))
        self.assertEqual(extraction.counterparty, self.counterparty)

    def test_extract_from_uploaded_searchable_pdf_updates_extracted_text(self):
        pdf_bytes = minimal_pdf_with_text("Payment R$ 450,00 ACME Materiais PIX 07/06/2026")
        uploaded_file = UploadedFile.objects.create(
            original_filename="receipt.pdf",
            source=UploadedFile.Source.TELEGRAM,
            kind=UploadedFile.Kind.PDF,
            content_type="application/pdf",
        )
        uploaded_file.file.save("receipt.pdf", ContentFile(pdf_bytes), save=True)

        extraction = extract_from_uploaded_file(uploaded_file)
        uploaded_file.refresh_from_db()

        self.assertFalse(extraction.needs_ai)
        self.assertEqual(extraction.amount, Decimal("450.00"))
        self.assertIn("ACME Materiais", uploaded_file.extracted_text)

    def test_image_extraction_is_prepared_for_ai(self):
        uploaded_file = UploadedFile.objects.create(
            original_filename="foto.jpg",
            source=UploadedFile.Source.TELEGRAM,
            kind=UploadedFile.Kind.IMAGE,
            content_type="image/jpeg",
        )
        uploaded_file.file.save("foto.jpg", ContentFile(b"fake-image"), save=True)

        extraction = extract_from_uploaded_file(uploaded_file)

        self.assertTrue(extraction.needs_ai)
        self.assertIsNone(extraction.amount)


class PaymentConfirmationFlowTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(name="Materiais", normalized_name="materiais")
        self.cost_center = CostCenter.objects.create(name="Project", normalized_name="project")
        self.work = Work.objects.create(name="Sertãozinho", normalized_name="sertaozinho")
        self.budget_item = BudgetItem.objects.create(
            work=self.work,
            index="3.4",
            item_type=BudgetItem.ItemType.SUBSTAGE,
            description="CALÇADA",
            normalized_description="calcada",
        )
        self.counterparty = Counterparty.objects.create(
            name="ACME Materiais",
            normalized_name="acme materiais",
            primary_document="12345678000199",
            default_category=self.category,
            default_cost_center=self.cost_center,
            default_work=self.work,
        )

    def make_payment(self):
        return Payment.objects.create(
            payment_date=date(2026, 6, 24),
            amount=Decimal("123.45"),
            counterparty=self.counterparty,
            category=self.category,
            cost_center=self.cost_center,
            work=self.work,
            payment_method="PIX",
            description="Compra de materiais",
            work_item_index="3.4",
            source="telegram",
            status=Payment.Status.RECEIVED,
            needs_review=True,
        )

    def test_approve_changes_status_and_records_confirmation(self):
        payment = self.make_payment()

        result = approve_payment(payment.pk, telegram_user_id=123, message="ok")
        payment.refresh_from_db()

        self.assertEqual(payment.status, Payment.Status.APPROVED)
        self.assertFalse(payment.needs_review)
        self.assertEqual(payment.user_action, Payment.ConfirmationAction.APPROVE)
        self.assertIsNotNone(payment.confirmed_at)
        self.assertEqual(result.confirmation.action, Payment.ConfirmationAction.APPROVE)
        self.assertEqual(result.confirmation.telegram_user_id, 123)

    def test_correct_keeps_payment_pending_until_new_confirmation(self):
        payment = self.make_payment()

        result = request_payment_correction(payment.pk, telegram_user_id=123, message="corrigir")
        payment.refresh_from_db()

        self.assertEqual(payment.status, Payment.Status.CORRECTING)
        self.assertTrue(payment.needs_review)
        self.assertEqual(payment.user_action, Payment.ConfirmationAction.CORRECT)
        self.assertIsNone(payment.confirmed_at)
        self.assertEqual(result.confirmation.action, Payment.ConfirmationAction.CORRECT)

    def test_cancel_changes_status_and_records_confirmation(self):
        payment = self.make_payment()

        result = cancel_payment(payment.pk, telegram_user_id=123, message="cancelar")
        payment.refresh_from_db()

        self.assertEqual(payment.status, Payment.Status.CANCELED)
        self.assertFalse(payment.needs_review)
        self.assertEqual(payment.user_action, Payment.ConfirmationAction.CANCEL)
        self.assertIsNotNone(payment.confirmed_at)
        self.assertEqual(result.confirmation.action, Payment.ConfirmationAction.CANCEL)

    def test_payment_suggestion_contains_required_fields(self):
        payment = self.make_payment()

        suggestion = format_payment_suggestion(payment)

        self.assertIn("Date: 24/06/2026", suggestion)
        self.assertIn("Amount: R$ 123.45", suggestion)
        self.assertIn("Vendor/Worker: ACME Materiais", suggestion)
        self.assertIn("CPF/CNPJ: 12345678000199", suggestion)
        self.assertIn("Description: Compra de materiais", suggestion)
        self.assertIn("Category: Materiais", suggestion)
        self.assertIn("Payment method: PIX", suggestion)
        self.assertIn("Cost center: Project", suggestion)
        self.assertIn("Project: Sertãozinho", suggestion)
        self.assertIn("Budget item index: 3.4", suggestion)
        self.assertIn("Service/Item: CALÇADA", suggestion)


class PaymentTextCorrectionTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(name="Materiais", normalized_name="materiais")
        self.cost_center = CostCenter.objects.create(name="Project", normalized_name="project")
        self.work = Work.objects.create(name="Sertãozinho", normalized_name="sertaozinho")
        self.budget_item = BudgetItem.objects.create(
            work=self.work,
            index="3.4.6",
            parent_index="3.4",
            item_type=BudgetItem.ItemType.ITEM,
            description="ALVENARIA DE CALÇADA",
            normalized_description="alvenaria de calcada",
        )
        self.wrong_counterparty = Counterparty.objects.create(
            name="Tiago Marcelo Araujo de Oliveira",
            normalized_name="tiago marcelo araujo de oliveira",
            kind=Counterparty.Kind.SUPPLIER,
        )
        self.right_counterparty = Counterparty.objects.create(
            name="Anita Jakeline Alves Fields",
            normalized_name="anita jakeline alves campos",
            kind=Counterparty.Kind.SUPPLIER,
            default_category=self.category,
            default_cost_center=self.cost_center,
            default_work=self.work,
        )

    def make_payment(self):
        return Payment.objects.create(
            amount=Decimal("100.00"),
            counterparty=self.wrong_counterparty,
            status=Payment.Status.CORRECTING,
            source="telegram",
            raw_payload={"telegram_user_id": 123},
            needs_review=True,
        )

    def test_text_correction_updates_existing_counterparty_and_fields(self):
        payment = self.make_payment()

        result = apply_text_correction_to_payment(
            payment.pk,
            "vendor correto é Anita Jakeline Alves Fields, amount R$ 2.000,00, project Sertãozinho, item 3.4.6, category Materiais, forma pix",
            telegram_user_id=123,
        )
        payment.refresh_from_db()

        self.assertEqual(payment.counterparty, self.right_counterparty)
        self.assertEqual(payment.amount, Decimal("2000.00"))
        self.assertEqual(payment.work, self.work)
        self.assertEqual(payment.work_item_index, "3.4.6")
        self.assertEqual(payment.category, self.category)
        self.assertEqual(payment.payment_method, "PIX")
        self.assertEqual(payment.status, Payment.Status.PENDING_CONFIRMATION)
        self.assertIn("vendor/worker", result.changed_fields)
        self.assertEqual(payment.raw_payload["text_corrections"][0]["telegram_user_id"], 123)

    def test_text_correction_with_new_counterparty_sets_candidate_for_registration(self):
        payment = self.make_payment()

        apply_text_correction_to_payment(payment.pk, "beneficiário é Novo Vendor Ltda", telegram_user_id=123)
        payment.refresh_from_db()

        self.assertIsNone(payment.counterparty)
        self.assertEqual(payment.status, Payment.Status.PENDING_REGISTRATION)
        self.assertEqual(payment.raw_payload["counterparty_candidate"]["name"], "Novo Vendor Ltda")

    def test_text_correction_matches_existing_counterparty_by_partial_name(self):
        documented = Counterparty.objects.create(
            name="Ivaldo Martins de Freitas",
            normalized_name="ivaldo martins de freitas",
            kind=Counterparty.Kind.SUPPLIER,
            primary_document="02542546401",
            source=Origin.IMPORT,
        )
        payment = self.make_payment()

        apply_text_correction_to_payment(payment.pk, "Worker Ivaldo Martins project de Sertãozinho", telegram_user_id=123)
        payment.refresh_from_db()

        self.assertEqual(payment.counterparty, documented)
        self.assertEqual(payment.work, self.work)
        self.assertEqual(payment.status, Payment.Status.PENDING_CONFIRMATION)


class CounterpartyRegistrationFlowTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(name="Materiais", normalized_name="materiais")

    def make_payment(self, candidate=None):
        payload = {}
        if candidate is not None:
            payload["counterparty_candidate"] = candidate
        return Payment.objects.create(
            amount=Decimal("100.00"),
            source="telegram",
            status=Payment.Status.RECEIVED,
            needs_review=True,
            raw_payload=payload,
        )

    def test_new_supplier_or_worker_requires_registration_before_approval(self):
        payment = self.make_payment({"name": "Novo Vendor", "document": "12345678000199"})

        with self.assertRaises(PaymentConfirmationError):
            approve_payment(payment.pk, telegram_user_id=123, message="aprovar")
        payment.refresh_from_db()

        self.assertEqual(payment.status, Payment.Status.PENDING_REGISTRATION)
        self.assertTrue(payment.needs_review)
        self.assertEqual(PaymentConfirmation.objects.count(), 0)
        self.assertEqual(Counterparty.objects.count(), 0)

    def test_existing_cpf_cnpj_is_reused_instead_of_creating_duplicate(self):
        counterparty = Counterparty.objects.create(
            name="Vendor Existente",
            normalized_name="vendor existente",
            kind=Counterparty.Kind.SUPPLIER,
            primary_document="12345678000199",
        )
        CounterpartyDocument.objects.create(
            counterparty=counterparty,
            document_type=CounterpartyDocument.DocumentType.CNPJ,
            number="12345678000199",
            source=Origin.IMPORT,
            is_primary=True,
        )
        payment = self.make_payment({"name": "Vendor Existente LTDA", "document": "12.345.678/0001-99"})

        result = confirm_counterparty_for_payment(payment.pk, Counterparty.Kind.SUPPLIER)
        payment.refresh_from_db()

        self.assertFalse(result.created)
        self.assertTrue(result.reused)
        self.assertEqual(payment.counterparty, counterparty)
        self.assertEqual(Counterparty.objects.count(), 1)
        self.assertEqual(payment.status, Payment.Status.PENDING_CONFIRMATION)

    def test_same_name_as_supplier_and_worker_generates_ambiguity(self):
        Counterparty.objects.create(
            name="Name Ambíguo",
            normalized_name="name ambiguo",
            kind=Counterparty.Kind.SUPPLIER,
        )
        Counterparty.objects.create(
            name="Name Ambíguo",
            normalized_name="name ambiguo",
            kind=Counterparty.Kind.WORKER,
        )
        payment = self.make_payment({"name": "Name Ambíguo"})

        with self.assertRaises(AmbiguousCounterpartyError):
            prepare_counterparty_review(payment)
        payment.refresh_from_db()

        self.assertIsNone(payment.counterparty)
        self.assertEqual(payment.status, Payment.Status.PENDING_REGISTRATION)
        self.assertIn("Ambiguous", payment.review_reason)

    def test_new_counterparty_creates_alias_for_future_classification(self):
        payment = self.make_payment(
            {
                "name": "ACME Materiais LTDA",
                "document": "12345678000199",
                "alias": "ACME Mat",
                "category_name": "Materiais",
            }
        )

        result = confirm_counterparty_for_payment(payment.pk, Counterparty.Kind.SUPPLIER)
        payment.refresh_from_db()

        self.assertTrue(result.created)
        self.assertEqual(payment.counterparty.name, "ACME Materiais LTDA")
        self.assertEqual(payment.counterparty.kind, Counterparty.Kind.SUPPLIER)
        self.assertEqual(payment.counterparty.default_category, self.category)
        self.assertEqual(payment.status, Payment.Status.PENDING_CONFIRMATION)
        self.assertTrue(
            CounterpartyAlias.objects.filter(
                counterparty=payment.counterparty,
                normalized_name="acme mat",
            ).exists()
        )


class PaymentHistoryImporterTests(TestCase):
    headers = [
        "Índice",
        "Date de Competência",
        "Date de Vencimento",
        "Date de Payment",
        "Amount da Parcela",
        "Amount em Aberto",
        "Amount Pago da Parcela",
        "Juros / Multas",
        "Descontos",
        "Amount Total Pago",
        "Vendor",
        "CNPJ / CPF do Vendor",
        "Dados Bancários do Vendor",
        "Description",
        "Document number",
        "Category",
        "Plano de Contas",
        "Grupo",
        "Condição de Payment",
        "Payment method",
        "Payer",
        "Bank account",
        "Centro de Custo",
        "Project",
        "Índice Step / Item",
        "Step / Item",
        "Ordem de Compra",
        "Comentários",
    ]

    def setUp(self):
        self.tempdir = TemporaryDirectory()
        self.base_path = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_history_import_is_idempotent(self):
        workbook = self.write_history(
            "historico.xlsx",
            [
                self.row("1", "ACME Materiais", "12345678000199", "Materiais", "Materiais", 100),
                self.row(
                    "2",
                    "João Pedreiro",
                    "11122233344",
                    "Labor Terceirizada",
                    "Labor Terceirizada",
                    250,
                ),
            ],
        )

        first_report = import_payment_history(workbook)
        second_report = import_payment_history(workbook)

        self.assertEqual(first_report.payments_created, 2)
        self.assertEqual(second_report.payments_created, 0)
        self.assertEqual(second_report.payments_unchanged, 2)
        self.assertEqual(Payment.objects.count(), 2)

    def test_history_import_creates_dimensions_without_duplicates(self):
        workbook = self.write_history(
            "historico.xlsx",
            [
                self.row("1", "ACME Materiais", "12345678000199", "Materiais", "Materiais", 100),
                self.row("2", "ACME Materiais", "12345678000199", "Materiais", "Materiais", 200),
            ],
        )

        import_payment_history(workbook)
        import_payment_history(workbook)

        self.assertEqual(Category.objects.filter(normalized_name="materiais").count(), 1)
        self.assertEqual(ChartOfAccount.objects.filter(normalized_name="materiais").count(), 1)
        self.assertEqual(CostCenter.objects.filter(normalized_name="project").count(), 1)
        self.assertEqual(Work.objects.filter(normalized_name="sertaozinho").count(), 1)

    def test_history_import_creates_missing_counterparty_and_classification_defaults(self):
        workbook = self.write_history(
            "historico.xlsx",
            [
                self.row(
                    "1",
                    "João Pedreiro",
                    "11122233344",
                    "Labor Terceirizada",
                    "Labor Terceirizada",
                    300,
                    work="Jaurez Távora",
                ),
            ],
        )

        import_payment_history(workbook)

        counterparty = Counterparty.objects.get(primary_document="11122233344")
        self.assertEqual(counterparty.kind, Counterparty.Kind.WORKER)
        self.assertEqual(counterparty.source, Origin.HISTORICAL)
        self.assertEqual(counterparty.default_category.normalized_name, "mao de obra terceirizada")
        self.assertEqual(counterparty.default_chart_account.normalized_name, "mao de obra terceirizada")
        self.assertEqual(counterparty.default_cost_center.normalized_name, "project")
        self.assertEqual(counterparty.default_work.normalized_name, "jaurez tavora")

    def test_history_import_without_document_reuses_documented_counterparty_by_name(self):
        counterparty = Counterparty.objects.create(
            name="Anita Jakeline Alves Fields",
            normalized_name="anita jakeline alves campos",
            kind=Counterparty.Kind.SUPPLIER,
            person_type=Counterparty.PersonType.INDIVIDUAL,
            primary_document="05783796433",
            source=Origin.IMPORT,
        )
        CounterpartyDocument.objects.create(
            counterparty=counterparty,
            document_type=CounterpartyDocument.DocumentType.CPF,
            number="05783796433",
            source=Origin.IMPORT,
            is_primary=True,
        )
        workbook = self.write_history(
            "historico.xlsx",
            [self.row("1", "ANITA JAKELINE ALVES CAMPOS", "", "Materiais", "Materiais", 2000)],
        )

        report = import_payment_history(workbook)

        self.assertEqual(report.counterparties_created, 0)
        self.assertEqual(Counterparty.objects.filter(normalized_name="anita jakeline alves campos").count(), 1)
        self.assertEqual(Payment.objects.get().counterparty, counterparty)

    def test_counterparty_kind_uses_aggregated_history_occurrences(self):
        Counterparty.objects.create(
            name="Pessoa Mista",
            normalized_name="pessoa mista",
            kind=Counterparty.Kind.WORKER,
            person_type=Counterparty.PersonType.INDIVIDUAL,
            primary_document="11122233344",
            source=Origin.IMPORT,
        )
        workbook = self.write_history(
            "historico.xlsx",
            [
                self.row("1", "Pessoa Mista", "11122233344", "Materiais", "Materiais", 100),
                self.row("2", "Pessoa Mista", "11122233344", "Materiais", "Materiais", 100),
                self.row(
                    "3",
                    "Pessoa Mista",
                    "11122233344",
                    "Labor Terceirizada",
                    "Labor Terceirizada",
                    100,
                ),
            ],
        )

        first_report = import_payment_history(workbook)
        second_report = import_payment_history(workbook)

        counterparty = Counterparty.objects.get(primary_document="11122233344")
        self.assertEqual(counterparty.kind, Counterparty.Kind.SUPPLIER)
        self.assertEqual(first_report.counterparties_updated, 0)
        self.assertEqual(first_report.classification_rules_updated, 1)
        self.assertEqual(second_report.counterparties_updated, 0)
        self.assertEqual(second_report.classification_rules_updated, 0)

    def test_historical_payments_are_marked_with_historical_origin(self):
        workbook = self.write_history(
            "historico.xlsx",
            [self.row("1", "ACME Materiais", "12345678000199", "Materiais", "Materiais", 100)],
        )

        import_payment_history(workbook)

        payment = Payment.objects.get()
        self.assertEqual(payment.source, Origin.HISTORICAL)
        self.assertEqual(payment.status, Payment.Status.POSTED)
        self.assertFalse(payment.needs_review)
        self.assertEqual(payment.raw_payload["source_file"], "historico.xlsx")
        self.assertIn("history_key", payment.raw_payload)

    def test_history_import_does_not_create_payments_for_unpaid_or_non_expense_rows(self):
        workbook = self.write_history(
            "historico.xlsx",
            [
                self.row("1", "ACME Materiais", "12345678000199", "Materiais", "Materiais", 0),
                self.row("2", "Income Indevida", "11122233344", "Other Expenses", "Other Expenses", -50),
            ],
        )

        report = import_payment_history(workbook)

        self.assertEqual(report.payments_created, 0)
        self.assertEqual(report.unpaid_or_non_expense_skipped, 2)
        self.assertEqual(Payment.objects.count(), 0)
        self.assertEqual(Counterparty.objects.count(), 2)

    def test_management_command_imports_history(self):
        workbook = self.write_history(
            "historico.xlsx",
            [self.row("1", "ACME Materiais", "12345678000199", "Materiais", "Materiais", 100)],
        )
        out = StringIO()

        call_command("import_payment_history", "--path", workbook, stdout=out)

        self.assertIn("Payment history import completed", out.getvalue())
        self.assertEqual(Payment.objects.count(), 1)

    def test_dry_run_does_not_persist_history(self):
        workbook = self.write_history(
            "historico.xlsx",
            [self.row("1", "ACME Materiais", "12345678000199", "Materiais", "Materiais", 100)],
        )

        report = import_payment_history(workbook, dry_run=True)

        self.assertEqual(report.payments_created, 1)
        self.assertEqual(Payment.objects.count(), 0)
        self.assertEqual(Counterparty.objects.count(), 0)

    def write_history(self, filename, rows):
        path = self.base_path / filename
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Payments"
        sheet.append(self.headers)
        for row in rows:
            sheet.append(row)
        workbook.save(path)
        return path

    def row(
        self,
        index,
        supplier,
        document,
        category,
        chart_account,
        paid_amount,
        cost_center="Project",
        work="Sertãozinho",
    ):
        return [
            index,
            "01/06/2026",
            "05/06/2026",
            "05/06/2026",
            paid_amount,
            0,
            paid_amount,
            0,
            0,
            paid_amount,
            supplier,
            document,
            "",
            f"Payment {supplier}",
            f"DOC-{index}",
            category,
            chart_account,
            "",
            "",
            "PIX",
            "Company",
            "Main Account",
            cost_center,
            work,
            "3.4",
            "",
            "",
            "",
        ]


class ReplacePeriodPaymentsFromCsvCommandTests(TestCase):
    def setUp(self):
        self.tempdir = TemporaryDirectory()
        self.base_path = Path(self.tempdir.name)
        self.company_center = CostCenter.objects.create(name="Company", normalized_name="empresa")
        self.work_center = CostCenter.objects.create(name="Project", normalized_name="project")
        self.old_counterparty = Counterparty.objects.create(
            name="Histórico Duplicado",
            normalized_name="historico duplicado",
            kind=Counterparty.Kind.SUPPLIER,
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def test_dry_run_does_not_change_existing_payments(self):
        self.make_payment(amount=Decimal("999.00"), payment_date=date(2026, 6, 5))
        csv_path = self.write_correction_csv(
            [
                {
                    "Vencimento": "05/06/2026",
                    "Status": "Pago",
                    "Amount": "R$ 250,00",
                    "Payee": "José do Egito Marinho",
                    "Category": "Labor Terceirizada",
                    "Centro de Custo": "Sertãozinho",
                    "Pago": "05/06/2026",
                }
            ]
        )
        out = StringIO()

        call_command(
            "replace_period_payments_from_csv",
            "--path",
            csv_path,
            "--start-date",
            "2026-06-01",
            "--end-date",
            "2026-06-30",
            stdout=out,
        )

        self.assertIn("Dry-run", out.getvalue())
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(Payment.objects.get().status, Payment.Status.POSTED)

    def test_apply_ignores_period_payments_and_creates_canonical_rows(self):
        june_duplicate = self.make_payment(amount=Decimal("999.00"), payment_date=date(2026, 6, 5))
        outside_period = self.make_payment(amount=Decimal("111.00"), payment_date=date(2026, 7, 1))
        csv_path = self.write_correction_csv(
            [
                {
                    "Vencimento": "05/06/2026",
                    "Status": "Pago",
                    "Amount": "R$ 250,00",
                    "Payee": "José do Egito Marinho",
                    "Category": "Labor Terceirizada",
                    "Centro de Custo": "Sertãozinho",
                    "Pago": "05/06/2026",
                },
                {
                    "Vencimento": "18/06/2026",
                    "Status": "Vencido",
                    "Amount": "R$ 278,00",
                    "Payee": "MARIA DO SOCORRO DE LIMA SILVA",
                    "Category": "Other Expenses",
                    "Centro de Custo": "Company",
                    "Pago": "",
                },
            ]
        )
        out = StringIO()

        call_command(
            "replace_period_payments_from_csv",
            "--path",
            csv_path,
            "--start-date",
            "2026-06-01",
            "--end-date",
            "2026-06-30",
            "--apply",
            stdout=out,
        )

        june_duplicate.refresh_from_db()
        outside_period.refresh_from_db()
        self.assertEqual(june_duplicate.status, Payment.Status.IGNORED)
        self.assertEqual(outside_period.status, Payment.Status.POSTED)
        self.assertEqual(Payment.objects.filter(source=Origin.IMPORT).count(), 2)
        paid = Payment.objects.get(amount=Decimal("250.00"))
        overdue = Payment.objects.get(amount=Decimal("278.00"))
        self.assertEqual(paid.status, Payment.Status.POSTED)
        self.assertEqual(paid.payment_date, date(2026, 6, 5))
        self.assertEqual(paid.counterparty.kind, Counterparty.Kind.WORKER)
        self.assertEqual(paid.cost_center, self.work_center)
        self.assertEqual(paid.work.normalized_name, "sertaozinho")
        self.assertEqual(overdue.status, Payment.Status.PENDING_CONFIRMATION)
        self.assertIsNone(overdue.payment_date)
        self.assertEqual(overdue.due_date, date(2026, 6, 18))
        self.assertEqual(overdue.cost_center, self.company_center)
        self.assertIsNone(overdue.work)
        self.assertIn("Replacement applied", out.getvalue())

    def test_apply_uses_due_date_before_payment_date_for_existing_period(self):
        due_may_paid_june = self.make_payment(
            amount=Decimal("100.00"),
            payment_date=date(2026, 6, 5),
            due_date=date(2026, 5, 31),
        )
        due_june_paid_may = self.make_payment(
            amount=Decimal("200.00"),
            payment_date=date(2026, 5, 31),
            due_date=date(2026, 6, 5),
        )
        csv_path = self.write_correction_csv(
            [
                {
                    "Vencimento": "05/06/2026",
                    "Status": "Pago",
                    "Amount": "R$ 250,00",
                    "Payee": "José do Egito Marinho",
                    "Category": "Labor Terceirizada",
                    "Centro de Custo": "Sertãozinho",
                    "Pago": "05/06/2026",
                }
            ]
        )

        call_command(
            "replace_period_payments_from_csv",
            "--path",
            csv_path,
            "--start-date",
            "2026-06-01",
            "--end-date",
            "2026-06-30",
            "--apply",
            stdout=StringIO(),
        )

        due_may_paid_june.refresh_from_db()
        due_june_paid_may.refresh_from_db()
        self.assertEqual(due_may_paid_june.status, Payment.Status.POSTED)
        self.assertEqual(due_june_paid_may.status, Payment.Status.IGNORED)

    def make_payment(self, *, amount, payment_date, due_date=None):
        return Payment.objects.create(
            payment_date=payment_date,
            due_date=due_date or payment_date,
            amount=amount,
            counterparty=self.old_counterparty,
            cost_center=self.company_center,
            source=Origin.HISTORICAL,
            status=Payment.Status.POSTED,
            needs_review=False,
        )

    def write_correction_csv(self, rows):
        path = self.base_path / "junho_2026_corrigido.csv"
        headers = ["Vencimento", "Status", "Amount", "Payee", "Description", "Category", "Centro de Custo", "Pago"]
        with path.open("w", encoding="utf-8", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=headers)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
        return path


def override_media_root(path):
    return override_settings(MEDIA_ROOT=Path(path))


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
