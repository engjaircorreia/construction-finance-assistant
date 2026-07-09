from datetime import date
from decimal import Decimal
from io import StringIO
import json
from unittest.mock import patch

from django.contrib import admin
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings

from apps.counterparties.models import (
    BudgetItem,
    Category,
    CostCenter,
    Counterparty,
    CounterpartyAlias,
    CounterpartyDocument,
    Origin,
    Work,
)
from apps.documents.models import UploadedFile
from apps.payments.models import Payment

from .models import OfxFile, OfxTransaction, Reconciliation
from .ofx_import import import_ofx_content
from .payment_suggestions import (
    LocalPaymentClassification,
    OFXAIClassificationError,
    build_openai_ofx_classification_input,
    parse_ai_classification_response,
    suggest_payments_from_ofx,
)
from .reconciliation import reconcile_ofx_transaction, reconcile_ofx_transactions
from .reports import build_ofx_import_summary


class BankingModelTests(TestCase):
    def setUp(self):
        self.ofx_file = OfxFile.objects.create(
            original_filename="sicredi.ofx",
            bank_id="748",
            account_id="12345",
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 24),
        )
        self.counterparty = Counterparty.objects.create(
            name="Vendor OFX",
            normalized_name="vendor ofx",
            primary_document="12345678000199",
        )

    def test_banking_models_are_admin_registered(self):
        self.assertTrue(admin.site.is_registered(OfxFile))
        self.assertTrue(admin.site.is_registered(OfxTransaction))
        self.assertTrue(admin.site.is_registered(Reconciliation))

    def test_fitid_is_unique_inside_the_same_ofx_file(self):
        OfxTransaction.objects.create(
            ofx_file=self.ofx_file,
            fitid="FIT-1",
            posted_at=date(2026, 6, 24),
            amount=Decimal("-250.00"),
            memo="PIX ENVIADO",
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            OfxTransaction.objects.create(
                ofx_file=self.ofx_file,
                fitid="FIT-1",
                posted_at=date(2026, 6, 24),
                amount=Decimal("-250.00"),
            )

    def test_same_fitid_can_exist_in_different_ofx_files(self):
        other_file = OfxFile.objects.create(original_filename="outra-conta.ofx")
        OfxTransaction.objects.create(
            ofx_file=self.ofx_file,
            fitid="FIT-1",
            posted_at=date(2026, 6, 24),
            amount=Decimal("-250.00"),
        )
        OfxTransaction.objects.create(
            ofx_file=other_file,
            fitid="FIT-1",
            posted_at=date(2026, 6, 24),
            amount=Decimal("-250.00"),
        )

        self.assertEqual(OfxTransaction.objects.filter(fitid="FIT-1").count(), 2)

    def test_reconciliation_pair_is_unique(self):
        payment = Payment.objects.create(amount=Decimal("250.00"), counterparty=self.counterparty)
        transaction_record = OfxTransaction.objects.create(
            ofx_file=self.ofx_file,
            fitid="FIT-2",
            posted_at=date(2026, 6, 24),
            amount=Decimal("-250.00"),
            counterparty=self.counterparty,
        )
        Reconciliation.objects.create(payment=payment, transaction=transaction_record)

        with self.assertRaises(IntegrityError), transaction.atomic():
            Reconciliation.objects.create(payment=payment, transaction=transaction_record)

    def test_fitid_cannot_be_blank(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            OfxTransaction.objects.create(
                ofx_file=self.ofx_file,
                fitid="",
                posted_at=date(2026, 6, 24),
                amount=Decimal("-50.00"),
            )

    def test_only_one_confirmed_reconciliation_per_payment(self):
        payment = Payment.objects.create(amount=Decimal("250.00"), counterparty=self.counterparty)
        transaction_1 = OfxTransaction.objects.create(
            ofx_file=self.ofx_file,
            fitid="FIT-3",
            posted_at=date(2026, 6, 24),
            amount=Decimal("-250.00"),
        )
        transaction_2 = OfxTransaction.objects.create(
            ofx_file=self.ofx_file,
            fitid="FIT-4",
            posted_at=date(2026, 6, 24),
            amount=Decimal("-250.00"),
        )
        Reconciliation.objects.create(
            payment=payment,
            transaction=transaction_1,
            status=Reconciliation.Status.CONFIRMED,
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            Reconciliation.objects.create(
                payment=payment,
                transaction=transaction_2,
                status=Reconciliation.Status.CONFIRMED,
            )

    def test_only_one_confirmed_reconciliation_per_transaction(self):
        payment_1 = Payment.objects.create(amount=Decimal("250.00"), counterparty=self.counterparty)
        payment_2 = Payment.objects.create(amount=Decimal("250.00"), counterparty=self.counterparty)
        transaction_record = OfxTransaction.objects.create(
            ofx_file=self.ofx_file,
            fitid="FIT-5",
            posted_at=date(2026, 6, 24),
            amount=Decimal("-250.00"),
        )
        Reconciliation.objects.create(
            payment=payment_1,
            transaction=transaction_record,
            status=Reconciliation.Status.CONFIRMED,
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            Reconciliation.objects.create(
                payment=payment_2,
                transaction=transaction_record,
                status=Reconciliation.Status.CONFIRMED,
            )

    def test_multiple_suggested_reconciliations_can_exist_for_review(self):
        payment_1 = Payment.objects.create(amount=Decimal("250.00"), counterparty=self.counterparty)
        payment_2 = Payment.objects.create(amount=Decimal("250.00"), counterparty=self.counterparty)
        transaction_record = OfxTransaction.objects.create(
            ofx_file=self.ofx_file,
            fitid="FIT-6",
            posted_at=date(2026, 6, 24),
            amount=Decimal("-250.00"),
        )
        Reconciliation.objects.create(payment=payment_1, transaction=transaction_record)
        Reconciliation.objects.create(payment=payment_2, transaction=transaction_record)

        self.assertEqual(transaction_record.reconciliations.count(), 2)

    def test_reconciliation_confidence_must_be_between_zero_and_one(self):
        payment = Payment.objects.create(amount=Decimal("250.00"), counterparty=self.counterparty)
        transaction_record = OfxTransaction.objects.create(
            ofx_file=self.ofx_file,
            fitid="FIT-7",
            posted_at=date(2026, 6, 24),
            amount=Decimal("-250.00"),
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            Reconciliation.objects.create(
                payment=payment,
                transaction=transaction_record,
                confidence=Decimal("1.10"),
            )


class OfxReconciliationTests(TestCase):
    def setUp(self):
        self.ofx_file = OfxFile.objects.create(
            original_filename="sicredi.ofx",
            bank_id="748",
            account_id="12345",
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 30),
        )
        self.counterparty = Counterparty.objects.create(
            name="ACME Materiais",
            normalized_name="acme materiais",
            primary_document="12345678000199",
        )

    def test_exact_reconciliation_by_amount_date_and_counterparty(self):
        payment = self.make_payment()
        transaction_record = self.make_transaction(counterparty=self.counterparty)

        result = reconcile_ofx_transaction(transaction_record)
        payment.refresh_from_db()
        transaction_record.refresh_from_db()
        reconciliation = Reconciliation.objects.get(payment=payment, transaction=transaction_record)

        self.assertEqual(result["classification"], "reconciled")
        self.assertEqual(transaction_record.status, OfxTransaction.Status.RECONCILED)
        self.assertEqual(payment.status, Payment.Status.RECONCILED)
        self.assertEqual(reconciliation.status, Reconciliation.Status.CONFIRMED)
        self.assertEqual(reconciliation.confidence, Decimal("1.00"))

    def test_suggests_match_by_document_when_name_is_different(self):
        payment = self.make_payment()
        transaction_record = self.make_transaction(
            memo="PIX ENVIADO NOME BANCARIO DIFERENTE",
            document_extracted="12.345.678/0001-99",
            name_extracted="Name Bancário Diferente",
        )

        reconcile_ofx_transaction(transaction_record)
        transaction_record.refresh_from_db()
        reconciliation = Reconciliation.objects.get(payment=payment, transaction=transaction_record)

        self.assertEqual(transaction_record.status, OfxTransaction.Status.RECONCILED)
        self.assertEqual(transaction_record.counterparty, self.counterparty)
        self.assertEqual(reconciliation.status, Reconciliation.Status.CONFIRMED)
        self.assertIn("CPF/CNPJ confere", reconciliation.notes)

    def test_marks_divergence_when_amount_or_date_does_not_match(self):
        payment = self.make_payment(amount=Decimal("300.00"))
        transaction_record = self.make_transaction(amount=Decimal("-250.00"), counterparty=self.counterparty)

        result = reconcile_ofx_transaction(transaction_record)
        payment.refresh_from_db()
        transaction_record.refresh_from_db()
        reconciliation = Reconciliation.objects.get(payment=payment, transaction=transaction_record)

        self.assertEqual(result["classification"], "divergent")
        self.assertEqual(transaction_record.status, OfxTransaction.Status.DIVERGENT)
        self.assertEqual(payment.status, Payment.Status.APPROVED)
        self.assertEqual(reconciliation.status, Reconciliation.Status.SUGGESTED)
        self.assertIn("Divergent amount", reconciliation.notes)

    def test_does_not_create_duplicate_reconciliation_for_same_pair(self):
        payment = self.make_payment()
        transaction_record = self.make_transaction(counterparty=self.counterparty)

        first = reconcile_ofx_transaction(transaction_record)
        second = reconcile_ofx_transaction(transaction_record)

        self.assertEqual(first["created"], 1)
        self.assertEqual(second["created"], 0)
        self.assertEqual(second["updated"], 1)
        self.assertEqual(Reconciliation.objects.filter(payment=payment, transaction=transaction_record).count(), 1)

    def test_credits_are_ignored_as_revenue(self):
        transaction_record = self.make_transaction(amount=Decimal("500.00"), memo="PIX RECEBIDO CLIENTE")

        result = reconcile_ofx_transaction(transaction_record)
        transaction_record.refresh_from_db()

        self.assertEqual(result["classification"], "ignored_credits")
        self.assertEqual(transaction_record.status, OfxTransaction.Status.IGNORED)
        self.assertEqual(Reconciliation.objects.count(), 0)

    def make_payment(self, amount=Decimal("250.00")):
        return Payment.objects.create(
            payment_date=date(2026, 6, 24),
            amount=amount,
            counterparty=self.counterparty,
            status=Payment.Status.APPROVED,
            source="telegram",
            needs_review=False,
        )

    def make_transaction(
        self,
        amount=Decimal("-250.00"),
        posted_at=date(2026, 6, 24),
        memo="PIX ENVIADO ACME MATERIAIS",
        counterparty=None,
        document_extracted="",
        name_extracted="",
    ):
        return OfxTransaction.objects.create(
            ofx_file=self.ofx_file,
            fitid=f"FIT-{OfxTransaction.objects.count() + 1}",
            posted_at=posted_at,
            amount=amount,
            memo=memo,
            counterparty=counterparty,
            document_extracted=document_extracted,
            name_extracted=name_extracted,
        )


class OfxImportTests(TestCase):
    def test_imports_ofx_transactions_and_matches_counterparty_document(self):
        counterparty = Counterparty.objects.create(
            name="ACME Materiais",
            normalized_name="acme materiais",
            primary_document="12345678000199",
            source=Origin.IMPORT,
        )
        CounterpartyDocument.objects.create(
            counterparty=counterparty,
            document_type=CounterpartyDocument.DocumentType.CNPJ,
            number="12345678000199",
            source=Origin.IMPORT,
            is_primary=True,
        )

        report = import_ofx_content(minimal_ofx())
        transaction_record = OfxTransaction.objects.get()

        self.assertEqual(report.transactions_created, 1)
        self.assertEqual(report.ofx_file.bank_id, "748")
        self.assertEqual(transaction_record.fitid, "FIT-1")
        self.assertEqual(transaction_record.posted_at, date(2026, 6, 24))
        self.assertEqual(transaction_record.amount, Decimal("-250.00"))
        self.assertEqual(transaction_record.document_extracted, "12345678000199")
        self.assertEqual(transaction_record.name_extracted, "ACME Materiais")
        self.assertEqual(transaction_record.counterparty, counterparty)
        self.assertEqual(report.transactions_read, 1)
        self.assertEqual(report.debit_transactions, 1)
        self.assertEqual(report.credit_transactions, 0)
        self.assertEqual(report.transactions_existing, 0)
        self.assertEqual(report.fallback_fitids, 0)

    def test_import_summary_reports_new_expense_without_creating_payment(self):
        report = import_ofx_content(minimal_ofx(fitid="FIT-EXPENSE", memo="PAGAMENTO PIX FORNECEDOR NOVO"))
        reconciliation_report = reconcile_ofx_transactions(report.ofx_file.transactions.all())
        summary = build_ofx_import_summary(report, reconciliation_report)
        transaction_record = OfxTransaction.objects.get(fitid="FIT-EXPENSE")

        self.assertEqual(Payment.objects.count(), 0)
        self.assertEqual(transaction_record.status, OfxTransaction.Status.MISSING_PAYMENT)
        self.assertIn("Transactions read: 1", summary.as_text())
        self.assertIn("New transactions: 1", summary.as_text())
        self.assertIn("Imported expenses: 1", summary.as_text())
        self.assertIn("Expenses without payment: 1", summary.as_text())
        self.assertIn("Period: 01/06/2026 to 30/06/2026", summary.as_text())
        self.assertIn("Bank/account: 748 / 12345", summary.as_text())

    def test_import_is_idempotent_for_same_uploaded_file(self):
        uploaded_file = UploadedFile.objects.create(
            original_filename="extrato.ofx",
            content_type="application/x-ofx",
            sha256="abc123",
            kind=UploadedFile.Kind.OFX,
            source=UploadedFile.Source.MANUAL,
        )
        content = minimal_ofx(fitid="FIT-IDEMPOTENT")

        first_report = import_ofx_content(content, uploaded_file=uploaded_file)
        second_report = import_ofx_content(content, uploaded_file=uploaded_file)
        summary = build_ofx_import_summary(
            second_report,
            reconcile_ofx_transactions(second_report.ofx_file.transactions.all()),
        )

        self.assertEqual(first_report.transactions_created, 1)
        self.assertEqual(second_report.transactions_created, 0)
        self.assertEqual(second_report.transactions_existing, 1)
        self.assertEqual(OfxFile.objects.count(), 1)
        self.assertEqual(OfxTransaction.objects.filter(fitid="FIT-IDEMPOTENT").count(), 1)
        self.assertIn("Existing transactions: 1", summary.as_text())

    def test_credit_is_ignored_and_reported_without_creating_payment(self):
        report = import_ofx_content(
            minimal_ofx(
                fitid="FIT-CREDIT",
                amount="500.00",
                memo="PIX RECEBIDO CLIENTE",
            )
        )
        reconciliation_report = reconcile_ofx_transactions(report.ofx_file.transactions.all())
        summary = build_ofx_import_summary(report, reconciliation_report)
        transaction_record = OfxTransaction.objects.get(fitid="FIT-CREDIT")

        self.assertEqual(report.credit_transactions, 1)
        self.assertEqual(report.debit_transactions, 0)
        self.assertEqual(reconciliation_report.ignored_credits, 1)
        self.assertEqual(transaction_record.status, OfxTransaction.Status.IGNORED)
        self.assertEqual(Payment.objects.count(), 0)
        self.assertIn("Ignored credits/income: 1", summary.as_text())

    def test_missing_fitid_uses_block_hash_fallback_and_reports_it(self):
        report = import_ofx_content(minimal_ofx(fitid=""))
        transaction_record = OfxTransaction.objects.get()

        self.assertEqual(report.fallback_fitids, 1)
        self.assertEqual(transaction_record.fitid, transaction_record.raw_payload["ofx_block_hash"])
        self.assertEqual(transaction_record.raw_payload["fitid_source"], "fallback_block_hash")

    @override_settings(
        TELEGRAM_BOT_TOKEN="telegram-secret-token",
        OPENAI_API_KEY="openai-secret-key",
        SECRET_KEY="django-secret-key",
        DATABASE_URL="postgres://user:database-secret-password@db:5432/app",
    )
    def test_import_log_does_not_include_known_secrets(self):
        with self.assertLogs("apps.banking.ofx_import", level="INFO") as captured:
            import_ofx_content(minimal_ofx(fitid="FIT-LOG"))

        output = "\n".join(captured.output)
        self.assertNotIn("telegram-secret-token", output)
        self.assertNotIn("openai-secret-key", output)
        self.assertNotIn("django-secret-key", output)
        self.assertNotIn("database-secret-password", output)


class OfxPaymentSuggestionTests(TestCase):
    def setUp(self):
        self.company_center = CostCenter.objects.create(name="Company", normalized_name="empresa")
        self.work_center = CostCenter.objects.create(name="Project", normalized_name="project")
        self.category = Category.objects.create(name="Materiais", normalized_name="materiais")
        self.ofx_file = OfxFile.objects.create(
            original_filename="sicredi.ofx",
            bank_id="748",
            account_id="12345",
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 30),
        )
        self.counterparty = Counterparty.objects.create(
            name="ACME Materiais",
            normalized_name="acme materiais",
            primary_document="12345678000199",
            default_category=self.category,
            source=Origin.IMPORT,
        )
        CounterpartyDocument.objects.create(
            counterparty=self.counterparty,
            document_type=CounterpartyDocument.DocumentType.CNPJ,
            number="12345678000199",
            source=Origin.IMPORT,
            is_primary=True,
        )

    def test_debit_creates_pending_confirmation_payment_when_counterparty_exists_by_document(self):
        transaction_record = self.make_transaction(
            document_extracted="12345678000199",
            name_extracted="Name Bancário ACME",
            memo="PAGAMENTO PIX 12345678000199 Name Bancário ACME",
        )

        report = suggest_payments_from_ofx(self.ofx_file)
        payment = Payment.objects.get()

        self.assertEqual(report.transactions_analyzed, 1)
        self.assertEqual(report.payments_created, 1)
        self.assertEqual(report.created_payment_ids, [payment.pk])
        self.assertEqual(payment.source, Origin.OFX)
        self.assertEqual(payment.status, Payment.Status.PENDING_CONFIRMATION)
        self.assertNotEqual(payment.status, Payment.Status.APPROVED)
        self.assertEqual(payment.counterparty, self.counterparty)
        self.assertEqual(payment.category, self.category)
        self.assertEqual(payment.amount, Decimal("250.00"))
        self.assertEqual(payment.payment_date, transaction_record.posted_at)
        self.assertEqual(payment.competence_date, transaction_record.posted_at)
        self.assertEqual(payment.due_date, transaction_record.posted_at)
        self.assertEqual(payment.payment_method, "PIX")
        self.assertEqual(payment.cost_center, self.company_center)
        self.assertEqual(payment.raw_payload["ofx_transaction_id"], transaction_record.pk)
        self.assertEqual(payment.raw_payload["ofx_fitid"], transaction_record.fitid)

    def test_debit_auto_creates_supplier_when_unknown_counterparty_name_is_safe(self):
        transaction_record = self.make_transaction(
            fitid="FIT-UNKNOWN",
            memo="PAGAMENTO PIX FORNECEDOR NOVO LTDA",
            name_extracted="Vendor Novo LTDA",
            document_extracted="11222333000144",
        )

        report = suggest_payments_from_ofx([transaction_record])
        payment = Payment.objects.get()
        counterparty = Counterparty.objects.get(name="Vendor Novo LTDA")
        category = Category.objects.get(normalized_name="other expenses")

        self.assertEqual(report.payments_created, 1)
        self.assertEqual(payment.counterparty, counterparty)
        self.assertEqual(payment.status, Payment.Status.PENDING_CONFIRMATION)
        self.assertEqual(payment.category, category)
        self.assertEqual(counterparty.kind, Counterparty.Kind.SUPPLIER)
        self.assertEqual(counterparty.source, Origin.OFX)
        self.assertEqual(counterparty.default_category, category)
        self.assertEqual(counterparty.primary_document, "11222333000144")
        self.assertTrue(CounterpartyDocument.objects.filter(counterparty=counterparty, number="11222333000144").exists())
        self.assertFalse(Payment.objects.filter(status=Payment.Status.APPROVED).exists())

    def test_debit_creates_pending_registration_when_unknown_counterparty_name_is_generic(self):
        transaction_record = self.make_transaction(
            fitid="FIT-UNKNOWN-GENERIC",
            memo="PAGAMENTO PIX",
            name_extracted="PIX",
        )

        report = suggest_payments_from_ofx([transaction_record])
        payment = Payment.objects.get()

        self.assertEqual(report.payments_created, 1)
        self.assertIsNone(payment.counterparty)
        self.assertEqual(payment.status, Payment.Status.PENDING_REGISTRATION)
        self.assertEqual(payment.raw_payload["counterparty_candidate"]["name"], "PIX")
        self.assertEqual(payment.raw_payload["counterparty_candidate"]["source"], Origin.OFX)

    def test_auto_created_supplier_is_reused_by_document_without_duplicate(self):
        existing = Counterparty.objects.create(
            name="Vendor Existente",
            normalized_name="vendor existente",
            primary_document="11222333000144",
            source=Origin.IMPORT,
        )
        CounterpartyDocument.objects.create(
            counterparty=existing,
            document_type=CounterpartyDocument.DocumentType.CNPJ,
            number="11222333000144",
            source=Origin.IMPORT,
            is_primary=True,
        )
        transaction_record = self.make_transaction(
            fitid="FIT-REUSE-DOCUMENT",
            memo="PAGAMENTO PIX NOME BANCARIO DIFERENTE",
            name_extracted="Name Bancario Diferente",
            document_extracted="11222333000144",
        )

        suggest_payments_from_ofx([transaction_record])
        payment = Payment.objects.get()

        self.assertEqual(payment.counterparty, existing)
        self.assertEqual(Counterparty.objects.filter(primary_document="11222333000144").count(), 1)
        self.assertEqual(payment.category.normalized_name, "other expenses")
        self.assertTrue(
            CounterpartyAlias.objects.filter(
                counterparty=existing,
                normalized_name="name bancario diferente",
            ).exists()
        )

    def test_credit_does_not_create_payment(self):
        transaction_record = self.make_transaction(fitid="FIT-CREDIT", amount=Decimal("500.00"))

        report = suggest_payments_from_ofx(transaction_record)

        self.assertEqual(report.transactions_analyzed, 1)
        self.assertEqual(report.transactions_ignored, 1)
        self.assertEqual(report.ignored_transaction_ids, [transaction_record.pk])
        self.assertEqual(Payment.objects.count(), 0)

    def test_reprocessing_same_transaction_does_not_duplicate_payment(self):
        transaction_record = self.make_transaction(counterparty=self.counterparty)

        first_report = suggest_payments_from_ofx([transaction_record])
        second_report = suggest_payments_from_ofx([transaction_record])

        self.assertEqual(first_report.payments_created, 1)
        self.assertEqual(second_report.payments_created, 0)
        self.assertEqual(second_report.payments_reused, 1)
        self.assertEqual(Payment.objects.count(), 1)

    def test_repeated_ofx_transactions_with_distinct_fitids_create_distinct_payments(self):
        for index in range(3):
            self.make_transaction(
                fitid=f"FIT-REPEATED-{index}",
                posted_at=date(2026, 6, 29),
                amount=Decimal("-559.99"),
                memo="PAGAMENTO DARF RECEITA FEDERAL",
                counterparty=self.counterparty,
            )

        report = suggest_payments_from_ofx(self.ofx_file)
        payments = Payment.objects.filter(source=Origin.OFX).order_by("raw_payload__ofx_fitid")

        self.assertEqual(report.payments_created, 3)
        self.assertEqual(report.payments_reused, 0)
        self.assertEqual(payments.count(), 3)
        self.assertEqual({payment.raw_payload["ofx_fitid"] for payment in payments}, {
            "FIT-REPEATED-0",
            "FIT-REPEATED-1",
            "FIT-REPEATED-2",
        })

    def test_confirmed_reconciliation_does_not_create_payment(self):
        transaction_record = self.make_transaction(counterparty=self.counterparty)
        payment = Payment.objects.create(
            payment_date=transaction_record.posted_at,
            amount=Decimal("250.00"),
            counterparty=self.counterparty,
            status=Payment.Status.RECONCILED,
            source=Origin.TELEGRAM,
        )
        Reconciliation.objects.create(
            payment=payment,
            transaction=transaction_record,
            status=Reconciliation.Status.CONFIRMED,
        )

        report = suggest_payments_from_ofx([transaction_record])

        self.assertEqual(report.transactions_ignored, 1)
        self.assertEqual(Payment.objects.count(), 1)

    def test_existing_payment_same_date_amount_and_counterparty_is_reused(self):
        transaction_record = self.make_transaction(counterparty=self.counterparty)
        payment = Payment.objects.create(
            payment_date=transaction_record.posted_at,
            amount=Decimal("250.00"),
            counterparty=self.counterparty,
            status=Payment.Status.PENDING_CONFIRMATION,
            source=Origin.TELEGRAM,
        )

        report = suggest_payments_from_ofx([transaction_record])
        transaction_record.refresh_from_db()

        self.assertEqual(report.payments_created, 0)
        self.assertEqual(report.payments_reused, 1)
        self.assertEqual(report.reused_payment_ids, [payment.pk])
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(transaction_record.status, OfxTransaction.Status.POSSIBLE_DUPLICATE)
        self.assertTrue(
            Reconciliation.objects.filter(
                payment=payment,
                transaction=transaction_record,
                status=Reconciliation.Status.SUGGESTED,
            ).exists()
        )

    def test_existing_business_payment_is_reused_only_once_for_repeated_ofx_transactions(self):
        payment = Payment.objects.create(
            payment_date=date(2026, 6, 29),
            amount=Decimal("559.99"),
            counterparty=self.counterparty,
            status=Payment.Status.PENDING_CONFIRMATION,
            source=Origin.TELEGRAM,
        )
        for index in range(3):
            self.make_transaction(
                fitid=f"FIT-ALLOCATED-{index}",
                posted_at=date(2026, 6, 29),
                amount=Decimal("-559.99"),
                memo="PAGAMENTO DARF RECEITA FEDERAL",
                counterparty=self.counterparty,
            )

        report = suggest_payments_from_ofx(self.ofx_file)

        self.assertEqual(report.payments_reused, 1)
        self.assertEqual(report.reused_payment_ids, [payment.pk])
        self.assertEqual(report.payments_created, 2)
        self.assertEqual(Payment.objects.count(), 3)
        self.assertEqual(
            Reconciliation.objects.filter(payment=payment, status=Reconciliation.Status.SUGGESTED).count(),
            1,
        )

    def test_suggested_reconciliation_is_reused_without_creating_payment(self):
        transaction_record = self.make_transaction(counterparty=self.counterparty)
        payment = Payment.objects.create(
            payment_date=date(2026, 6, 20),
            amount=Decimal("300.00"),
            counterparty=self.counterparty,
            status=Payment.Status.APPROVED,
            source=Origin.TELEGRAM,
        )
        Reconciliation.objects.create(
            payment=payment,
            transaction=transaction_record,
            status=Reconciliation.Status.SUGGESTED,
        )

        report = suggest_payments_from_ofx([transaction_record])

        self.assertEqual(report.payments_created, 0)
        self.assertEqual(report.payments_reused, 1)
        self.assertEqual(report.reused_payment_ids, [payment.pk])
        self.assertEqual(Payment.objects.count(), 1)

    def test_existing_work_in_memo_is_linked_with_work_cost_center(self):
        work = Work.objects.create(name="Tacima", normalized_name="tacima")
        transaction_record = self.make_transaction(
            fitid="FIT-WORK",
            counterparty=self.counterparty,
            memo="PAGAMENTO PIX OBRA Tacima MATERIAL",
        )

        suggest_payments_from_ofx([transaction_record])
        payment = Payment.objects.get()

        self.assertEqual(payment.work, work)
        self.assertEqual(payment.cost_center, self.work_center)
        self.assertNotIn("work_candidate", payment.raw_payload)

    def test_unknown_work_in_memo_is_saved_as_candidate(self):
        transaction_record = self.make_transaction(
            fitid="FIT-WORK-CANDIDATE",
            counterparty=self.counterparty,
            memo="PAGAMENTO PIX OBRA Nova City",
        )

        suggest_payments_from_ofx([transaction_record])
        payment = Payment.objects.get()

        self.assertIsNone(payment.work)
        self.assertEqual(payment.cost_center, self.company_center)
        self.assertEqual(payment.raw_payload["work_candidate"]["name"], "Nova City")
        self.assertEqual(payment.raw_payload["work_candidate"]["source"], Origin.OFX)

    def test_budget_item_is_suggested_when_service_text_is_reliable(self):
        work = Work.objects.create(name="Tacima", normalized_name="tacima")
        BudgetItem.objects.create(
            work=work,
            index="3.4.1",
            description="Alvenaria de calçada",
            normalized_description="alvenaria de calcada",
            item_type=BudgetItem.ItemType.ITEM,
        )
        transaction_record = self.make_transaction(
            fitid="FIT-BUDGET-ITEM",
            counterparty=self.counterparty,
            memo="PAGAMENTO PIX OBRA Tacima alvenaria de calcada",
        )

        suggest_payments_from_ofx([transaction_record])
        payment = Payment.objects.get()

        self.assertEqual(payment.work, work)
        self.assertEqual(payment.work_item_index, "3.4.1")
        self.assertEqual(payment.raw_payload["budget_item_suggestion"]["index"], "3.4.1")

    def test_historical_category_is_used_when_counterparty_has_no_default(self):
        historical_category = Category.objects.create(name="Rent", normalized_name="aluguel")
        counterparty = Counterparty.objects.create(
            name="Vendor Historico",
            normalized_name="vendor historico",
            primary_document="11122233344455",
            source=Origin.IMPORT,
        )
        Payment.objects.create(
            payment_date=date(2026, 5, 20),
            amount=Decimal("100.00"),
            counterparty=counterparty,
            category=historical_category,
            status=Payment.Status.APPROVED,
            source=Origin.HISTORICAL,
        )
        transaction_record = self.make_transaction(
            fitid="FIT-HISTORY-CATEGORY",
            document_extracted="11122233344455",
            name_extracted="Vendor Historico",
            memo="PAGAMENTO FORNECEDOR HISTORICO",
        )

        suggest_payments_from_ofx([transaction_record])
        payment = Payment.objects.get(amount=Decimal("250.00"))

        self.assertEqual(payment.category, historical_category)

    def test_local_document_match_wins_over_ai_counterparty(self):
        self.counterparty.default_category = None
        self.counterparty.save(update_fields=["default_category", "updated_at"])
        ai_category = Category.objects.create(name="Other Expenses", normalized_name="outras despesas")
        transaction_record = self.make_transaction(
            fitid="FIT-AI-DOC-WINS",
            document_extracted="12345678000199",
            name_extracted="Name Bancario",
            memo="PAGAMENTO SEM METODO CLARO",
        )
        ai_classifier = FakeOFXAIClassifier(
            {
                "counterparty_name": "Outro Vendor",
                "counterparty_document": "99999999000199",
                "category": ai_category.name,
                "cost_center": "Company",
                "work": "",
                "work_item_index": "",
                "payment_method": "PIX",
                "description": "Payment classificado por IA",
                "confidence": 0.95,
                "needs_user_review_reason": "",
            }
        )

        suggest_payments_from_ofx([transaction_record], ai_classifier=ai_classifier)
        payment = Payment.objects.get()

        self.assertEqual(ai_classifier.calls, 1)
        self.assertEqual(payment.counterparty, self.counterparty)
        self.assertNotEqual(payment.counterparty.name, "Outro Vendor")
        self.assertEqual(payment.category, ai_category)
        self.assertEqual(payment.payment_method, "Debit")
        self.assertEqual(payment.status, Payment.Status.PENDING_CONFIRMATION)

    def test_valid_ai_json_is_accepted_as_suggestion_without_approval(self):
        ai_category = Category.objects.create(name="Consulting", normalized_name="consultoria")
        transaction_record = self.make_transaction(
            fitid="FIT-AI-VALID",
            memo="PAGAMENTO SERVICO ESPECIAL",
            name_extracted="Vendor IA",
        )
        transaction_record.transaction_type = ""
        transaction_record.save(update_fields=["transaction_type", "updated_at"])
        ai_classifier = FakeOFXAIClassifier(
            {
                "counterparty_name": "Vendor IA",
                "counterparty_document": "",
                "category": ai_category.name,
                "cost_center": "Company",
                "work": "",
                "work_item_index": "",
                "payment_method": "PIX",
                "description": "Service especial",
                "confidence": 0.82,
                "needs_user_review_reason": "New counterparty must be registered.",
            }
        )

        suggest_payments_from_ofx([transaction_record], ai_classifier=ai_classifier)
        payment = Payment.objects.get()

        self.assertEqual(payment.category, ai_category)
        self.assertEqual(payment.payment_method, "PIX")
        self.assertEqual(payment.description, "Service especial")
        self.assertEqual(payment.status, Payment.Status.PENDING_REGISTRATION)
        self.assertNotEqual(payment.status, Payment.Status.APPROVED)
        self.assertEqual(payment.raw_payload["counterparty_candidate"]["source"], Origin.AI)
        self.assertEqual(payment.raw_payload["ai_classification"]["confidence"], "0.82")

    def test_invalid_or_incomplete_ai_json_is_rejected(self):
        with self.assertRaises(OFXAIClassificationError):
            parse_ai_classification_response("{invalid-json")
        with self.assertRaises(OFXAIClassificationError):
            parse_ai_classification_response(json.dumps({"counterparty_name": "Vendor"}))

    @override_settings(
        TELEGRAM_BOT_TOKEN="telegram-secret-token",
        OPENAI_API_KEY="openai-secret-key",
        SECRET_KEY="django-secret-key",
        DATABASE_URL="postgres://user:database-secret-password@db:5432/app",
    )
    def test_ai_payload_does_not_include_environment_secrets(self):
        transaction_record = self.make_transaction(
            fitid="FIT-AI-PAYLOAD",
            memo="PAGAMENTO PIX 12345678000199 ACME Materiais token=telegram-secret-token",
            document_extracted="12345678000199",
        )
        local_classification = LocalPaymentClassification(
            counterparty=None,
            category=None,
            cost_center=self.company_center,
            work=None,
            budget_item=None,
            work_item_index="",
            payment_method="",
            description="",
            confidence=Decimal("0.55"),
            needs_user_review_reason="",
        )

        payload = json.dumps(
            build_openai_ofx_classification_input(transaction_record, local_classification),
            ensure_ascii=False,
        )

        self.assertNotIn("telegram-secret-token", payload)
        self.assertNotIn("openai-secret-key", payload)
        self.assertNotIn("django-secret-key", payload)
        self.assertNotIn("database-secret-password", payload)
        self.assertNotIn("12345678000199", payload)

    def test_partial_suggestion_error_preserves_transactions_and_never_approves_payment(self):
        error_transaction = self.make_transaction(
            fitid="FIT-ERROR",
            counterparty=self.counterparty,
            memo="PAGAMENTO PIX COM ERRO SIMULADO",
        )
        ok_transaction = self.make_transaction(
            fitid="FIT-OK",
            counterparty=self.counterparty,
            memo="PAGAMENTO PIX ACME MATERIAIS",
        )

        from apps.banking import payment_suggestions

        original_builder = payment_suggestions.build_payment_suggestion

        def builder_with_error(transaction_record, *args, **kwargs):
            if transaction_record.fitid == "FIT-ERROR":
                raise RuntimeError("error parcial")
            return original_builder(transaction_record, *args, **kwargs)

        with patch("apps.banking.payment_suggestions.build_payment_suggestion", side_effect=builder_with_error):
            report = suggest_payments_from_ofx([error_transaction, ok_transaction])

        self.assertEqual(report.payments_created, 1)
        self.assertEqual(len(report.conflicts), 1)
        self.assertEqual(report.conflicts[0].fitid, "FIT-ERROR")
        self.assertEqual(OfxTransaction.objects.filter(pk__in=[error_transaction.pk, ok_transaction.pk]).count(), 2)
        self.assertFalse(Payment.objects.filter(status=Payment.Status.APPROVED).exists())
        self.assertEqual(Payment.objects.get().status, Payment.Status.PENDING_CONFIRMATION)

    def test_reconcile_management_command_creates_payment_suggestions(self):
        self.make_transaction(
            fitid="FIT-COMMAND",
            counterparty=self.counterparty,
            memo="PAGAMENTO PIX ACME MATERIAIS",
        )
        output = StringIO()

        call_command("reconcile_ofx_transactions", stdout=output)
        payment = Payment.objects.get()

        self.assertEqual(payment.source, Origin.OFX)
        self.assertEqual(payment.status, Payment.Status.PENDING_CONFIRMATION)
        self.assertIn("suggested_payments=1", output.getvalue())
        self.assertIn("pending_confirmation=1", output.getvalue())

    def make_transaction(
        self,
        *,
        fitid="FIT-SUGGESTION",
        amount=Decimal("-250.00"),
        posted_at=date(2026, 6, 24),
        memo="PAGAMENTO PIX ACME MATERIAIS",
        counterparty=None,
        document_extracted="",
        name_extracted="",
    ):
        return OfxTransaction.objects.create(
            ofx_file=self.ofx_file,
            fitid=fitid,
            transaction_type="DEBIT" if amount < 0 else "CREDIT",
            posted_at=posted_at,
            amount=amount,
            memo=memo,
            counterparty=counterparty,
            document_extracted=document_extracted,
            name_extracted=name_extracted,
        )


class FakeOFXAIClassifier:
    def __init__(self, payload: dict):
        self.payload = payload
        self.calls = 0

    def classify(self, transaction_record, local_classification):
        self.calls += 1
        return parse_ai_classification_response(json.dumps(self.payload))


def minimal_ofx(
    *,
    fitid: str = "FIT-1",
    amount: str = "-250.00",
    memo: str = "PAGAMENTO PIX-PIX_DEB   12345678000199 ACME Materiais",
):
    fitid_line = f"<FITID>{fitid}\n" if fitid else ""
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
<TRNAMT>{amount}
{fitid_line}<MEMO>{memo}
</STMTTRN>
</BANKTRANLIST>
</STMTRS>
</STMTTRNRS>
</BANKMSGSRSV1>
</OFX>
""".format(amount=amount, fitid_line=fitid_line, memo=memo)
