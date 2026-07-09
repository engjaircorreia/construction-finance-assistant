from datetime import date
from decimal import Decimal

from django.test import TestCase

from apps.banking.models import OfxFile, OfxTransaction, Reconciliation
from apps.counterparties.models import Category, CostCenter, Origin, Work
from apps.payments.models import Payment
from apps.telegrambot.models import TelegramDraft

from .monthly_closing import build_monthly_closing, get_month_period


class MonthlyClosingTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(name="Materiais", normalized_name="materiais")
        self.cost_center = CostCenter.objects.create(name="Company", normalized_name="empresa")

    def test_calculates_month_period(self):
        start, end = get_month_period(2, 2028)

        self.assertEqual(start, date(2028, 2, 1))
        self.assertEqual(end, date(2028, 2, 29))

    def test_counts_payments_by_status_inside_period(self):
        self.make_payment(Payment.Status.RECEIVED)
        self.make_payment(Payment.Status.PROCESSING)
        self.make_payment(Payment.Status.PENDING_REGISTRATION)
        self.make_payment(Payment.Status.PENDING_CONFIRMATION)
        self.make_payment(Payment.Status.CORRECTING)
        self.make_payment(Payment.Status.APPROVED)
        self.make_payment(Payment.Status.RECONCILED)
        self.make_payment(Payment.Status.CANCELED)
        self.make_payment(Payment.Status.APPROVED, payment_date=date(2026, 7, 1))

        summary = build_monthly_closing(month=6, year=2026)

        self.assertEqual(summary.total_payments, 8)
        self.assertEqual(summary.received_count, 1)
        self.assertEqual(summary.pending_total, 5)
        self.assertEqual(summary.pending_registration_count, 1)
        self.assertEqual(summary.pending_confirmation_count, 1)
        self.assertEqual(summary.correcting_count, 1)
        self.assertEqual(summary.approved_count, 1)
        self.assertEqual(summary.reconciled_count, 1)
        self.assertEqual(summary.canceled_count, 1)

    def test_sums_approved_and_reconciled_amounts_inside_period(self):
        self.make_payment(Payment.Status.APPROVED, amount=Decimal("100.00"))
        self.make_payment(Payment.Status.APPROVED, amount=Decimal("25.45"))
        self.make_payment(Payment.Status.RECONCILED, amount=Decimal("10.00"))
        self.make_payment(Payment.Status.APPROVED, amount=Decimal("999.00"), payment_date=date(2026, 7, 1))

        summary = build_monthly_closing(month=6, year=2026)

        self.assertEqual(summary.approved_amount, Decimal("125.45"))
        self.assertEqual(summary.reconciled_amount, Decimal("10.00"))

    def test_detects_exportable_payments_with_missing_required_fields(self):
        valid = self.make_payment(
            Payment.Status.APPROVED,
            category=self.category,
            cost_center=self.cost_center,
        )
        missing = self.make_payment(Payment.Status.APPROVED, category=None, cost_center=None)
        self.make_payment(Payment.Status.RECEIVED, category=None, cost_center=None)

        summary = build_monthly_closing(month=6, year=2026)

        self.assertEqual(summary.exportable_payments_count, 1)
        self.assertEqual(summary.payments_missing_required_fields[0].payment_id, missing.pk)
        self.assertEqual(
            summary.payments_missing_required_fields[0].missing_fields,
            ["Category", "Cost center"],
        )
        self.assertNotEqual(summary.payments_missing_required_fields[0].payment_id, valid.pk)

    def test_detects_payments_with_work_without_budget_as_informational_issue(self):
        work = Work.objects.create(name="Tacima", normalized_name="tacima")
        payment = self.make_payment(
            Payment.Status.APPROVED,
            category=self.category,
            cost_center=self.cost_center,
            work=work,
        )

        summary = build_monthly_closing(month=6, year=2026)

        self.assertEqual(len(summary.payments_with_work_without_budget), 1)
        self.assertEqual(summary.payments_with_work_without_budget[0].payment_id, payment.pk)
        self.assertEqual(summary.payments_with_work_without_budget[0].work_name, "Tacima")

    def test_counts_active_drafts_inside_period(self):
        TelegramDraft.objects.create(telegram_user_id=123, payment_date=date(2026, 6, 15))
        TelegramDraft.objects.create(telegram_user_id=123, payment_date=date(2026, 7, 1))
        TelegramDraft.objects.create(
            telegram_user_id=123,
            payment_date=date(2026, 6, 20),
            status=TelegramDraft.Status.CANCELED,
        )

        summary = build_monthly_closing(month=6, year=2026)

        self.assertEqual(summary.active_drafts_count, 1)

    def test_detects_ofx_pending_divergent_and_duplicate_transactions_inside_period(self):
        ofx_file = OfxFile.objects.create(
            original_filename="extrato-junho.ofx",
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 30),
        )
        self.make_ofx_transaction(ofx_file, "FIT-1", OfxTransaction.Status.PENDING)
        self.make_ofx_transaction(ofx_file, "FIT-2", OfxTransaction.Status.DIVERGENT)
        self.make_ofx_transaction(ofx_file, "FIT-3", OfxTransaction.Status.POSSIBLE_DUPLICATE)
        self.make_ofx_transaction(
            ofx_file,
            "FIT-4",
            OfxTransaction.Status.PENDING,
            posted_at=date(2026, 7, 1),
        )

        summary = build_monthly_closing(month=6, year=2026)

        self.assertTrue(summary.has_ofx_imported)
        self.assertEqual(summary.ofx_pending_count, 1)
        self.assertEqual(summary.ofx_divergent_count, 1)
        self.assertEqual(summary.ofx_possible_duplicate_count, 1)

    def test_counts_ofx_validation_flow_without_treating_credit_as_blocker(self):
        ofx_file = OfxFile.objects.create(
            original_filename="extrato-junho.ofx",
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 30),
        )
        self.make_ofx_transaction(ofx_file, "FIT-MISSING", OfxTransaction.Status.MISSING_PAYMENT)
        registration_transaction = self.make_ofx_transaction(
            ofx_file,
            "FIT-CADASTRO",
            OfxTransaction.Status.PENDING,
        )
        confirmation_transaction = self.make_ofx_transaction(
            ofx_file,
            "FIT-CONFIRMACAO",
            OfxTransaction.Status.PENDING,
        )
        duplicate_transaction = self.make_ofx_transaction(
            ofx_file,
            "FIT-DUP",
            OfxTransaction.Status.POSSIBLE_DUPLICATE,
        )
        divergent_transaction = self.make_ofx_transaction(
            ofx_file,
            "FIT-DIV",
            OfxTransaction.Status.DIVERGENT,
        )
        reconciled_transaction = self.make_ofx_transaction(
            ofx_file,
            "FIT-RECONCILED",
            OfxTransaction.Status.RECONCILED,
        )
        self.make_ofx_transaction(
            ofx_file,
            "FIT-CREDIT",
            OfxTransaction.Status.IGNORED,
            amount=Decimal("300.00"),
        )
        self.make_payment(
            Payment.Status.PENDING_REGISTRATION,
            source=Origin.OFX,
            raw_payload={"ofx_transaction_id": registration_transaction.pk},
        )
        self.make_payment(
            Payment.Status.PENDING_CONFIRMATION,
            source=Origin.OFX,
            raw_payload={"ofx_transaction_id": confirmation_transaction.pk},
        )
        self.make_payment(
            Payment.Status.POSSIBLE_DUPLICATE,
            source=Origin.OFX,
            raw_payload={"ofx_transaction_id": duplicate_transaction.pk},
        )
        self.make_payment(
            Payment.Status.APPROVED,
            source=Origin.OFX,
            raw_payload={"ofx_transaction_id": divergent_transaction.pk},
        )
        reconciled_payment = self.make_payment(Payment.Status.APPROVED)
        Reconciliation.objects.create(
            payment=reconciled_payment,
            transaction=reconciled_transaction,
            status=Reconciliation.Status.CONFIRMED,
        )

        summary = build_monthly_closing(month=6, year=2026)

        self.assertTrue(summary.has_ofx_imported)
        self.assertEqual(summary.ofx_expense_without_payment_count, 1)
        self.assertEqual(summary.ofx_suggested_pending_registration_count, 1)
        self.assertEqual(summary.ofx_suggested_pending_confirmation_count, 1)
        self.assertEqual(summary.ofx_possible_duplicate_count, 1)
        self.assertEqual(summary.ofx_divergent_count, 1)
        self.assertEqual(summary.ofx_ignored_credit_count, 1)
        self.assertEqual(summary.approved_unreconciled_count, 1)

    def make_payment(
        self,
        status,
        amount=Decimal("100.00"),
        payment_date=date(2026, 6, 15),
        category=None,
        cost_center=None,
        work=None,
        source="telegram",
        raw_payload=None,
    ):
        return Payment.objects.create(
            payment_date=payment_date,
            amount=amount,
            category=category,
            cost_center=cost_center,
            work=work,
            status=status,
            source=source,
            raw_payload=raw_payload or {},
        )

    def make_ofx_transaction(
        self,
        ofx_file,
        fitid,
        status,
        posted_at=date(2026, 6, 15),
        amount=Decimal("-100.00"),
    ):
        return OfxTransaction.objects.create(
            ofx_file=ofx_file,
            fitid=fitid,
            posted_at=posted_at,
            amount=amount,
            status=status,
        )
