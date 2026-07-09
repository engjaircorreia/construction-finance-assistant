from datetime import date
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.banking.models import OfxFile, OfxTransaction, Reconciliation
from apps.core.dashboard import build_dashboard_summary, work_budget_totals
from apps.counterparties.models import BudgetItem, Category, CostCenter, Counterparty, Origin, Work
from apps.payments.models import Payment
from apps.telegrambot.models import TelegramDraft


class DashboardSelectorTests(TestCase):
    def setUp(self):
        self.company_center = CostCenter.objects.create(name="Company", normalized_name="empresa")
        self.work_center = CostCenter.objects.create(name="Project", normalized_name="project")
        self.category = Category.objects.create(name="Materiais", normalized_name="materiais")
        self.services = Category.objects.create(name="Services", normalized_name="servicos")
        self.counterparty = Counterparty.objects.create(
            name="ACME Materiais",
            normalized_name="acme materiais",
            kind=Counterparty.Kind.SUPPLIER,
            source=Origin.MANUAL,
        )
        self.worker = Counterparty.objects.create(
            name="Joao Worker",
            normalized_name="joao worker",
            kind=Counterparty.Kind.WORKER,
            source=Origin.MANUAL,
        )
        self.work = Work.objects.create(name="Tacima", normalized_name="tacima")
        self.other_work = Work.objects.create(name="Sertaozinho", normalized_name="sertaozinho")

    def make_payment(self, **overrides):
        data = {
            "payment_date": date(2026, 6, 15),
            "amount": Decimal("100.00"),
            "counterparty": self.counterparty,
            "category": self.category,
            "cost_center": self.company_center,
            "status": Payment.Status.APPROVED,
            "source": Origin.MANUAL,
            "description": "Payment de teste",
        }
        data.update(overrides)
        return Payment.objects.create(**data)

    def make_confirmed_reconciliation(self, payment, *, fitid="FIT-1", amount=Decimal("-100.00")):
        ofx_file = OfxFile.objects.create(original_filename=f"{fitid}.ofx")
        transaction = OfxTransaction.objects.create(
            ofx_file=ofx_file,
            fitid=fitid,
            posted_at=payment.payment_date or date(2026, 6, 15),
            amount=amount,
            memo="PIX ENVIADO ACME",
            status=OfxTransaction.Status.RECONCILED,
        )
        return Reconciliation.objects.create(
            payment=payment,
            transaction=transaction,
            status=Reconciliation.Status.CONFIRMED,
            confidence=Decimal("1.00"),
        )

    def make_budget_item(self, *, work=None, index="1", parent_index="", total_cost="100.00"):
        work = work or self.work
        return BudgetItem.objects.create(
            work=work,
            index=index,
            parent_index=parent_index,
            item_type=BudgetItem.ItemType.ITEM,
            description=f"Item {index}",
            normalized_description=f"item {index}",
            total_cost=Decimal(total_cost),
        )

    def test_default_period_uses_current_month(self):
        summary = build_dashboard_summary(today=date(2026, 6, 27))

        self.assertEqual(summary.month, 6)
        self.assertEqual(summary.year, 2026)
        self.assertEqual(summary.period_start, date(2026, 6, 1))
        self.assertEqual(summary.period_end, date(2026, 6, 30))

    def test_filter_uses_payment_date_and_tracks_undated_as_pendency(self):
        self.make_payment(amount=Decimal("100.00"), payment_date=date(2026, 6, 5))
        self.make_payment(amount=Decimal("200.00"), payment_date=date(2026, 5, 31))
        self.make_payment(amount=Decimal("300.00"), payment_date=None)

        summary = build_dashboard_summary(month=6, year=2026)

        self.assertEqual(summary.realized_amount, Decimal("100.00"))
        self.assertEqual(summary.payments_count, 1)
        self.assertEqual(summary.undated_payments_count, 1)
        self.assertGreaterEqual(summary.operational_pendency_count, 1)

    def test_period_uses_due_date_before_payment_date(self):
        self.make_payment(
            amount=Decimal("100.00"),
            due_date=date(2026, 6, 5),
            payment_date=date(2026, 5, 13),
        )
        self.make_payment(
            amount=Decimal("200.00"),
            due_date=date(2026, 5, 31),
            payment_date=date(2026, 6, 1),
        )

        summary = build_dashboard_summary(month=6, year=2026)

        self.assertEqual(summary.realized_amount, Decimal("100.00"))
        self.assertEqual(summary.realized_payments_count, 1)

    def test_realized_statuses_enter_totals_and_cancelled_or_pending_do_not(self):
        self.make_payment(amount=Decimal("100.00"), status=Payment.Status.APPROVED)
        self.make_payment(amount=Decimal("200.00"), status=Payment.Status.RECONCILED)
        self.make_payment(amount=Decimal("300.00"), status=Payment.Status.POSTED)
        self.make_payment(amount=Decimal("400.00"), status=Payment.Status.CANCELED)
        self.make_payment(amount=Decimal("500.00"), status=Payment.Status.PENDING_CONFIRMATION)

        summary = build_dashboard_summary(month=6, year=2026)

        self.assertEqual(summary.realized_amount, Decimal("600.00"))
        self.assertEqual(summary.realized_payments_count, 3)
        self.assertEqual(summary.pending_confirmation_amount, Decimal("500.00"))
        self.assertEqual(summary.pending_total_amount, Decimal("500.00"))
        self.assertEqual(summary.pending_total_count, 1)

    def test_zero_payment_amounts_do_not_enter_realized_totals_or_groups(self):
        self.make_payment(amount=Decimal("0.00"), status=Payment.Status.APPROVED, work=self.work)

        summary = build_dashboard_summary(month=6, year=2026)

        self.assertEqual(summary.realized_amount, Decimal("0.00"))
        self.assertEqual(summary.realized_payments_count, 0)
        self.assertEqual(summary.approved_unreconciled_amount, Decimal("0.00"))
        self.assertEqual(summary.approved_unreconciled_count, 0)
        self.assertEqual(summary.financial_center_groups, [])
        self.assertEqual(summary.work_budget_summaries, [])

    def test_approved_payment_with_confirmed_reconciliation_counts_as_reconciled(self):
        reconciled_by_ofx = self.make_payment(amount=Decimal("100.00"), status=Payment.Status.APPROVED)
        self.make_confirmed_reconciliation(reconciled_by_ofx, fitid="FIT-RECONCILED")
        self.make_payment(amount=Decimal("75.00"), status=Payment.Status.APPROVED)

        summary = build_dashboard_summary(month=6, year=2026)

        self.assertEqual(summary.realized_amount, Decimal("175.00"))
        self.assertEqual(summary.reconciled_amount, Decimal("100.00"))
        self.assertEqual(summary.approved_unreconciled_amount, Decimal("75.00"))
        self.assertEqual(summary.approved_unreconciled_count, 1)

    def test_operational_pendencies_include_pending_drafts_and_ofx_issues(self):
        self.make_payment(amount=Decimal("100.00"), status=Payment.Status.PENDING_CONFIRMATION)
        self.make_payment(amount=Decimal("50.00"), status=Payment.Status.APPROVED)
        TelegramDraft.objects.create(
            telegram_user_id=327694327,
            sender_name="Jair",
            payment_date=date(2026, 6, 20),
            amount=Decimal("10.00"),
        )
        ofx_file = OfxFile.objects.create(original_filename="pendencias.ofx")
        OfxTransaction.objects.create(
            ofx_file=ofx_file,
            fitid="PEND-1",
            posted_at=date(2026, 6, 21),
            amount=Decimal("-50.00"),
            memo="PIX",
            status=OfxTransaction.Status.DIVERGENT,
        )

        summary = build_dashboard_summary(month=6, year=2026)

        self.assertEqual(summary.active_drafts_count, 1)
        self.assertEqual(summary.ofx_issue_count, 1)
        self.assertGreaterEqual(summary.operational_pendency_count, 4)

    def test_dashboard_counts_ofx_validation_flow(self):
        ofx_file = OfxFile.objects.create(
            original_filename="extrato-junho.ofx",
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 30),
        )
        OfxTransaction.objects.create(
            ofx_file=ofx_file,
            fitid="OFX-MISSING",
            posted_at=date(2026, 6, 10),
            amount=Decimal("-10.00"),
            status=OfxTransaction.Status.MISSING_PAYMENT,
        )
        registration_transaction = OfxTransaction.objects.create(
            ofx_file=ofx_file,
            fitid="OFX-CADASTRO",
            posted_at=date(2026, 6, 11),
            amount=Decimal("-20.00"),
            status=OfxTransaction.Status.PENDING,
        )
        confirmation_transaction = OfxTransaction.objects.create(
            ofx_file=ofx_file,
            fitid="OFX-CONFIRMACAO",
            posted_at=date(2026, 6, 12),
            amount=Decimal("-30.00"),
            status=OfxTransaction.Status.PENDING,
        )
        duplicate_transaction = OfxTransaction.objects.create(
            ofx_file=ofx_file,
            fitid="OFX-DUP",
            posted_at=date(2026, 6, 13),
            amount=Decimal("-40.00"),
            status=OfxTransaction.Status.POSSIBLE_DUPLICATE,
        )
        divergent_transaction = OfxTransaction.objects.create(
            ofx_file=ofx_file,
            fitid="OFX-DIV",
            posted_at=date(2026, 6, 14),
            amount=Decimal("-50.00"),
            status=OfxTransaction.Status.DIVERGENT,
        )
        OfxTransaction.objects.create(
            ofx_file=ofx_file,
            fitid="OFX-CREDIT",
            posted_at=date(2026, 6, 15),
            amount=Decimal("100.00"),
            status=OfxTransaction.Status.IGNORED,
        )
        reconciled_payment = self.make_payment(amount=Decimal("60.00"), status=Payment.Status.APPROVED)
        self.make_confirmed_reconciliation(reconciled_payment, fitid="OFX-RECONCILED", amount=Decimal("-60.00"))
        self.make_payment(
            amount=Decimal("20.00"),
            status=Payment.Status.PENDING_REGISTRATION,
            source=Origin.OFX,
            raw_payload={"ofx_transaction_id": registration_transaction.pk},
        )
        self.make_payment(
            amount=Decimal("30.00"),
            status=Payment.Status.PENDING_CONFIRMATION,
            source=Origin.OFX,
            raw_payload={"ofx_transaction_id": confirmation_transaction.pk},
        )
        self.make_payment(
            amount=Decimal("40.00"),
            status=Payment.Status.POSSIBLE_DUPLICATE,
            source=Origin.OFX,
            raw_payload={"ofx_transaction_id": duplicate_transaction.pk},
        )
        self.make_payment(
            amount=Decimal("50.00"),
            status=Payment.Status.APPROVED,
            source=Origin.OFX,
            raw_payload={"ofx_transaction_id": divergent_transaction.pk},
        )

        summary = build_dashboard_summary(month=6, year=2026)

        self.assertTrue(summary.has_ofx_imported)
        self.assertEqual(summary.ofx_imported_count, 2)
        self.assertEqual(summary.ofx_expense_without_payment_count, 1)
        self.assertEqual(summary.ofx_suggested_pending_registration_count, 1)
        self.assertEqual(summary.ofx_suggested_pending_registration_amount, Decimal("20.00"))
        self.assertEqual(summary.ofx_suggested_pending_confirmation_count, 1)
        self.assertEqual(summary.ofx_suggested_pending_confirmation_amount, Decimal("30.00"))
        self.assertEqual(summary.ofx_possible_duplicate_count, 1)
        self.assertEqual(summary.ofx_divergent_count, 1)
        self.assertEqual(summary.ofx_ignored_credit_count, 1)
        self.assertEqual(summary.reconciled_count, 1)
        self.assertEqual(summary.reconciled_amount, Decimal("60.00"))
        self.assertEqual(summary.approved_unreconciled_count, 1)

    def test_financial_center_group_prioritizes_work_over_cost_center(self):
        self.make_payment(amount=Decimal("300.00"), work=self.work, cost_center=self.company_center)
        self.make_payment(amount=Decimal("200.00"), work=None, cost_center=self.work_center)
        self.make_payment(amount=Decimal("100.00"), work=None, cost_center=None)

        summary = build_dashboard_summary(month=6, year=2026)
        groups = {group.label: group for group in summary.financial_center_groups}

        self.assertEqual(groups["Project: Tacima"].amount, Decimal("300.00"))
        self.assertEqual(groups["Project: Tacima"].kind, "work")
        self.assertEqual(groups["Project"].amount, Decimal("200.00"))
        self.assertEqual(groups["Company"].amount, Decimal("100.00"))

    def test_category_and_counterparty_rankings_are_limited_and_ordered(self):
        self.make_payment(amount=Decimal("100.00"), category=self.category, counterparty=self.counterparty)
        self.make_payment(amount=Decimal("250.00"), category=self.services, counterparty=self.worker)
        self.make_payment(amount=Decimal("50.00"), category=None, counterparty=None)

        summary = build_dashboard_summary(month=6, year=2026, ranking_limit=2)

        self.assertEqual([group.label for group in summary.category_groups], ["Services", "Materiais"])
        self.assertEqual([group.label for group in summary.counterparty_groups], ["Joao Worker", "ACME Materiais"])

    def test_monthly_evolution_returns_selected_year_months_with_totals(self):
        self.make_payment(amount=Decimal("120.00"), payment_date=date(2025, 7, 10), status=Payment.Status.APPROVED)
        self.make_payment(amount=Decimal("220.00"), payment_date=date(2026, 6, 10), status=Payment.Status.APPROVED)
        self.make_payment(amount=Decimal("90.00"), payment_date=date(2026, 6, 11), status=Payment.Status.PENDING_CONFIRMATION)

        summary = build_dashboard_summary(month=6, year=2026)

        self.assertEqual(len(summary.monthly_evolution), 12)
        self.assertEqual((summary.monthly_evolution[0].month, summary.monthly_evolution[0].year), (1, 2026))
        self.assertEqual((summary.monthly_evolution[-1].month, summary.monthly_evolution[-1].year), (12, 2026))
        self.assertFalse(any(item.year == 2025 for item in summary.monthly_evolution))
        june = next(item for item in summary.monthly_evolution if item.month == 6)
        self.assertEqual(june.realized_amount, Decimal("220.00"))
        self.assertEqual(june.pending_amount, Decimal("90.00"))

    def test_monthly_evolution_keeps_months_without_payments_as_zero(self):
        self.make_payment(amount=Decimal("220.00"), payment_date=date(2026, 6, 10), status=Payment.Status.APPROVED)

        summary = build_dashboard_summary(month=6, year=2026)
        may = next(item for item in summary.monthly_evolution if item.month == 5 and item.year == 2026)

        self.assertEqual(may.realized_amount, Decimal("0.00"))
        self.assertEqual(may.reconciled_amount, Decimal("0.00"))
        self.assertEqual(may.pending_amount, Decimal("0.00"))
        self.assertEqual(may.payments_count, 0)

    def test_monthly_evolution_separates_realized_reconciled_and_pending(self):
        self.make_payment(amount=Decimal("100.00"), payment_date=date(2026, 6, 10), status=Payment.Status.APPROVED)
        self.make_payment(amount=Decimal("60.00"), payment_date=date(2026, 6, 11), status=Payment.Status.RECONCILED)
        self.make_payment(
            amount=Decimal("40.00"),
            payment_date=date(2026, 6, 12),
            status=Payment.Status.PENDING_CONFIRMATION,
        )

        summary = build_dashboard_summary(month=6, year=2026)
        june = next(item for item in summary.monthly_evolution if item.month == 6)

        self.assertEqual(june.realized_amount, Decimal("160.00"))
        self.assertEqual(june.reconciled_amount, Decimal("60.00"))
        self.assertEqual(june.pending_amount, Decimal("40.00"))
        self.assertEqual(june.payments_count, 3)

    def test_work_budget_sums_leaf_items_without_double_counting_parent(self):
        self.make_budget_item(work=self.work, index="1", total_cost="1000.00")
        self.make_budget_item(work=self.work, index="1.1", parent_index="1", total_cost="300.00")
        self.make_budget_item(work=self.work, index="1.2", parent_index="1", total_cost="200.00")
        self.make_payment(amount=Decimal("250.00"), work=self.work, cost_center=self.work_center)

        summary = build_dashboard_summary(month=6, year=2026)
        tacima = next(item for item in summary.work_budget_summaries if item.work_id == self.work.id)

        self.assertEqual(work_budget_totals()[self.work.id], Decimal("500.0000"))
        self.assertEqual(tacima.budget_total, Decimal("500.0000"))
        self.assertEqual(tacima.accumulated_spent, Decimal("250.00"))
        self.assertEqual(tacima.consumed_percentage, Decimal("50.0"))
        self.assertEqual(tacima.estimated_balance, Decimal("250.0000"))
        self.assertEqual(tacima.status, "ok")
        self.assertEqual(tacima.payments_count, 1)

    def test_work_without_budget_appears_with_alert_and_does_not_break_percentage(self):
        self.make_payment(amount=Decimal("100.00"), work=self.other_work, cost_center=self.work_center)

        summary = build_dashboard_summary(month=6, year=2026)
        work_summary = next(item for item in summary.work_budget_summaries if item.work_id == self.other_work.id)

        self.assertFalse(work_summary.has_budget)
        self.assertIsNone(work_summary.budget_total)
        self.assertIsNone(work_summary.consumed_percentage)
        self.assertIsNone(work_summary.estimated_balance)
        self.assertEqual(work_summary.status, "sem_orcamento")

    def test_work_above_budget_is_marked_as_alert(self):
        self.make_budget_item(work=self.work, index="1", total_cost="100.00")
        self.make_payment(amount=Decimal("150.00"), work=self.work, cost_center=self.work_center)

        summary = build_dashboard_summary(month=6, year=2026)
        work_summary = next(item for item in summary.work_budget_summaries if item.work_id == self.work.id)

        self.assertEqual(work_summary.budget_total, Decimal("100.0000"))
        self.assertEqual(work_summary.accumulated_spent, Decimal("150.00"))
        self.assertEqual(work_summary.estimated_balance, Decimal("-50.0000"))
        self.assertEqual(work_summary.status, "acima_orcamento")

    def test_pending_payments_do_not_enter_work_accumulated_spent(self):
        self.make_budget_item(work=self.work, index="1", total_cost="500.00")
        self.make_payment(
            amount=Decimal("100.00"),
            payment_date=date(2026, 5, 10),
            work=self.work,
            cost_center=self.work_center,
            status=Payment.Status.APPROVED,
        )
        self.make_payment(
            amount=Decimal("200.00"),
            payment_date=date(2026, 6, 10),
            work=self.work,
            cost_center=self.work_center,
            status=Payment.Status.PENDING_CONFIRMATION,
        )

        summary = build_dashboard_summary(month=6, year=2026)
        work_summary = next(item for item in summary.work_budget_summaries if item.work_id == self.work.id)

        self.assertEqual(work_summary.monthly_spent, Decimal("0.00"))
        self.assertEqual(work_summary.accumulated_spent, Decimal("100.00"))
        self.assertEqual(work_summary.pending_amount, Decimal("200.00"))
        self.assertEqual(work_summary.pending_count, 1)
        self.assertEqual(work_summary.estimated_balance, Decimal("400.0000"))

    def test_work_without_budget_and_only_pending_does_not_divide_by_zero(self):
        self.make_payment(
            amount=Decimal("200.00"),
            work=self.other_work,
            cost_center=self.work_center,
            status=Payment.Status.PENDING_CONFIRMATION,
        )

        summary = build_dashboard_summary(month=6, year=2026)
        work_summary = next(item for item in summary.work_budget_summaries if item.work_id == self.other_work.id)

        self.assertFalse(work_summary.has_budget)
        self.assertIsNone(work_summary.consumed_percentage)
        self.assertIsNone(work_summary.estimated_balance)
        self.assertEqual(work_summary.pending_amount, Decimal("200.00"))
        self.assertEqual(work_summary.status, "sem_orcamento")

    def test_percentages_do_not_break_when_realized_total_is_zero(self):
        summary = build_dashboard_summary(month=6, year=2026)

        self.assertEqual(summary.realized_amount, Decimal("0.00"))
        self.assertEqual(summary.financial_center_groups, [])
        self.assertEqual(summary.category_groups, [])
        self.assertEqual(summary.counterparty_groups, [])


class DashboardViewTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="jair", password="senha-forte")
        self.cost_center = CostCenter.objects.create(name="Company", normalized_name="empresa")
        self.work_center = CostCenter.objects.create(name="Project", normalized_name="project")
        self.category = Category.objects.create(name="Materiais", normalized_name="materiais")
        self.counterparty = Counterparty.objects.create(
            name="ACME Materiais",
            normalized_name="acme materiais",
            kind=Counterparty.Kind.SUPPLIER,
        )

    def make_payment(self, **overrides):
        data = {
            "payment_date": date(2026, 6, 15),
            "amount": Decimal("100.00"),
            "counterparty": self.counterparty,
            "category": self.category,
            "cost_center": self.cost_center,
            "status": Payment.Status.APPROVED,
            "source": Origin.MANUAL,
            "description": "Payment de teste",
        }
        data.update(overrides)
        return Payment.objects.create(**data)

    def make_confirmed_reconciliation(self, payment, *, fitid="FIT-VIEW", amount=Decimal("-100.00")):
        ofx_file = OfxFile.objects.create(original_filename=f"{fitid}.ofx")
        transaction = OfxTransaction.objects.create(
            ofx_file=ofx_file,
            fitid=fitid,
            posted_at=payment.payment_date or date(2026, 6, 15),
            amount=amount,
            memo="PIX CONCILIADO",
            status=OfxTransaction.Status.RECONCILED,
        )
        return Reconciliation.objects.create(
            payment=payment,
            transaction=transaction,
            status=Reconciliation.Status.CONFIRMED,
            confidence=Decimal("1.00"),
        )

    def test_anonymous_user_is_redirected_to_login(self):
        response = self.client.get(reverse("internal_dashboard"))

        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])

    def test_authenticated_user_can_access_dashboard(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("internal_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "core/dashboard.html")

    def test_home_redirects_to_dashboard(self):
        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("internal_dashboard"))

    def test_menu_contains_dashboard_link(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("internal_dashboard"))

        self.assertContains(response, "Dashboard")
        self.assertContains(response, reverse("internal_dashboard"))

    def test_month_and_year_filter_changes_period(self):
        self.client.force_login(self.user)
        self.make_payment(payment_date=date(2026, 5, 10), amount=Decimal("250.00"))
        self.make_payment(payment_date=date(2026, 6, 10), amount=Decimal("100.00"))

        response = self.client.get(reverse("internal_dashboard"), {"mes": "5", "ano": "2026"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "01/05/2026 to 31/05/2026")
        self.assertEqual(response.context["summary"].realized_amount, Decimal("250.00"))

    def test_main_cards_are_rendered(self):
        self.client.force_login(self.user)
        self.make_payment(amount=Decimal("1234.56"))

        response = self.client.get(reverse("internal_dashboard"), {"mes": "6", "ano": "2026"})

        self.assertContains(response, "Period spend")
        self.assertContains(response, "R$ 1.234,56")
        self.assertContains(response, "Accumulated spend")
        self.assertContains(response, "Pending approval")
        self.assertContains(response, "Operational pending items")

    def test_monthly_evolution_section_shows_separated_values(self):
        self.client.force_login(self.user)
        self.make_payment(amount=Decimal("100.00"), payment_date=date(2026, 6, 10), status=Payment.Status.APPROVED)
        self.make_payment(amount=Decimal("70.00"), payment_date=date(2026, 6, 11), status=Payment.Status.RECONCILED)
        self.make_payment(
            amount=Decimal("30.00"),
            payment_date=date(2026, 6, 12),
            status=Payment.Status.PENDING_CONFIRMATION,
        )

        response = self.client.get(reverse("internal_dashboard"), {"mes": "6", "ano": "2026"})
        june = next(item for item in response.context["monthly_evolution"] if item["label"] == "Jun/26")

        self.assertContains(response, "Year 2026 by due/accrual date.")
        self.assertContains(response, "Pending amounts are shown separately and do not compose realized spend.")
        self.assertContains(response, "Realized")
        self.assertContains(response, "Reconciled")
        self.assertContains(response, "Pending")
        self.assertEqual(june["realized_amount"], Decimal("170.00"))
        self.assertEqual(june["reconciled_amount"], Decimal("70.00"))
        self.assertEqual(june["pending_amount"], Decimal("30.00"))
        self.assertEqual(june["payments_count"], 3)

    def test_monthly_evolution_links_point_to_filtered_payments_by_month(self):
        self.client.force_login(self.user)
        self.make_payment(amount=Decimal("250.00"), payment_date=date(2026, 5, 10))

        response = self.client.get(reverse("internal_dashboard"), {"mes": "6", "ano": "2026"})
        may = next(item for item in response.context["monthly_evolution"] if item["label"] == "May/26")
        query = parse_qs(urlsplit(may["url"]).query)

        self.assertEqual(urlsplit(may["url"]).path, reverse("internal_pending_payments"))
        self.assertEqual(query["status"], ["all"])
        self.assertEqual(query["date_inicio"], ["2026-05-01"])
        self.assertEqual(query["date_fim"], ["2026-05-31"])

    def test_monthly_evolution_year_navigation_links_keep_selected_month(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("internal_dashboard"), {"mes": "6", "ano": "2026"})
        previous_query = parse_qs(urlsplit(response.context["previous_year_url"]).query)
        next_query = parse_qs(urlsplit(response.context["next_year_url"]).query)

        self.assertContains(response, "← 2025")
        self.assertContains(response, "2027 →")
        self.assertEqual(previous_query["mes"], ["6"])
        self.assertEqual(previous_query["ano"], ["2025"])
        self.assertEqual(next_query["mes"], ["6"])
        self.assertEqual(next_query["ano"], ["2027"])

    def test_dashboard_action_links_preserve_selected_period(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("internal_dashboard"), {"mes": "6", "ano": "2026"})

        period_query = parse_qs(urlsplit(response.context["period_payments_url"]).query)
        pending_query = parse_qs(urlsplit(response.context["pending_payments_url"]).query)
        ofx_query = parse_qs(urlsplit(response.context["pending_ofx_url"]).query)
        drafts_query = parse_qs(urlsplit(response.context["active_drafts_url"]).query)
        self.assertEqual(period_query["status"], ["all"])
        self.assertEqual(period_query["date_inicio"], ["2026-06-01"])
        self.assertEqual(period_query["date_fim"], ["2026-06-30"])
        self.assertEqual(pending_query["status"], ["pendencias"])
        self.assertEqual(pending_query["date_inicio"], ["2026-06-01"])
        self.assertEqual(pending_query["date_fim"], ["2026-06-30"])
        self.assertEqual(ofx_query["status"], ["pendencias"])
        self.assertEqual(ofx_query["mes"], ["6"])
        self.assertEqual(ofx_query["ano"], ["2026"])
        self.assertEqual(drafts_query["status"], [TelegramDraft.Status.ACTIVE])
        self.assertEqual(drafts_query["date_inicio"], ["2026-06-01"])
        self.assertEqual(drafts_query["date_fim"], ["2026-06-30"])

    def test_dashboard_does_not_break_without_payments(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("internal_dashboard"), {"mes": "6", "ano": "2026"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No realized expense in the period.")
        self.assertEqual(response.context["summary"].realized_amount, Decimal("0.00"))

    def test_dashboard_renders_long_names_and_large_values(self):
        long_work_name = "Project " + "Tacima-Setor-Extremamente-Longo-" * 5 + "<Especial>"
        long_category_name = "Category " + "Materials-De-Construcao-Super-Detalhado-" * 3
        work = Work.objects.create(name=long_work_name, normalized_name="project longa")
        category = Category.objects.create(name=long_category_name, normalized_name="category longa")
        BudgetItem.objects.create(
            work=work,
            index="1",
            item_type=BudgetItem.ItemType.ITEM,
            description="Budget alto",
            normalized_description="orcamento alto",
            total_cost=Decimal("9876543210.1234"),
        )
        self.make_payment(
            amount=Decimal("9876543210.98"),
            work=work,
            cost_center=self.work_center,
            category=category,
        )
        self.client.force_login(self.user)

        response = self.client.get(reverse("internal_dashboard"), {"mes": "6", "ano": "2026"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Tacima-Setor-Extremamente-Longo")
        self.assertNotContains(response, "<Especial>")
        self.assertContains(response, "&lt;Especial&gt;")
        self.assertContains(response, "R$ 9.876.543.210,98")
        self.assertContains(response, "R$ 9.876.543.210,12")
        self.assertContains(response, "role=\"img\"")
        self.assertContains(response, "text legend")
        self.assertContains(response, "legend-item no-color")
        self.assertContains(response, "table-scroll")

    def test_dashboard_renders_many_works_and_categories_without_expanding_links_indefinitely(self):
        self.client.force_login(self.user)
        for index in range(12):
            work = Work.objects.create(
                name=f"Project com name operacional muito longo {index} " + ("Trecho-" * 8),
                normalized_name=f"project longa {index}",
            )
            category = Category.objects.create(
                name=f"Category muito detalhada {index} " + ("Insumo-" * 8),
                normalized_name=f"category longa {index}",
            )
            self.make_payment(
                amount=Decimal(f"{1000 + index}.00"),
                work=work,
                cost_center=self.work_center,
                category=category,
            )

        response = self.client.get(reverse("internal_dashboard"), {"mes": "6", "ano": "2026"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Other")
        self.assertContains(response, "Expenses by category")
        self.assertLessEqual(len(response.context["financial_center_legend"]), 8)
        self.assertLessEqual(len(response.context["category_rows"]), 10)

    def test_dashboard_ignores_zero_payment_values_without_breaking_empty_chart(self):
        self.make_payment(amount=Decimal("0.00"), status=Payment.Status.APPROVED)
        self.client.force_login(self.user)

        response = self.client.get(reverse("internal_dashboard"), {"mes": "6", "ano": "2026"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["summary"].realized_amount, Decimal("0.00"))
        self.assertEqual(response.context["financial_center_legend"], [])
        self.assertContains(response, "No realized expense in the period.")

    def test_zero_operational_pendencies_do_not_pollute_dashboard(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("internal_dashboard"), {"mes": "6", "ano": "2026"})

        self.assertContains(response, "No main operational pending item for the period.")
        self.assertNotContains(response, "Pending registration")
        self.assertEqual(response.context["operational_pendencies"], [])

    def test_operational_pendencies_show_each_existing_type(self):
        self.client.force_login(self.user)
        work = Work.objects.create(name="Tacima", normalized_name="tacima")
        TelegramDraft.objects.create(
            telegram_user_id=327694327,
            sender_name="Jair",
            payment_date=date(2026, 6, 10),
            amount=Decimal("10.00"),
        )
        self.make_payment(amount=Decimal("11.00"), payment_date=None, status=Payment.Status.RECEIVED)
        self.make_payment(amount=Decimal("12.00"), status=Payment.Status.PENDING_REGISTRATION)
        self.make_payment(amount=Decimal("13.00"), status=Payment.Status.PENDING_CONFIRMATION)
        self.make_payment(amount=Decimal("14.00"), status=Payment.Status.CORRECTING)
        self.make_payment(amount=Decimal("15.00"), status=Payment.Status.POSTED)
        self.make_payment(amount=Decimal("16.00"), work=work, cost_center=self.work_center, status=Payment.Status.APPROVED)
        ofx_file = OfxFile.objects.create(original_filename="pendencias.ofx")
        OfxTransaction.objects.create(
            ofx_file=ofx_file,
            fitid="OFX-PENDING",
            posted_at=date(2026, 6, 10),
            amount=Decimal("-1.00"),
            status=OfxTransaction.Status.PENDING,
        )
        registration_transaction = OfxTransaction.objects.create(
            ofx_file=ofx_file,
            fitid="OFX-REGISTRATION",
            posted_at=date(2026, 6, 11),
            amount=Decimal("-12.00"),
            status=OfxTransaction.Status.MISSING_PAYMENT,
        )
        confirmation_transaction = OfxTransaction.objects.create(
            ofx_file=ofx_file,
            fitid="OFX-CONFIRMATION",
            posted_at=date(2026, 6, 12),
            amount=Decimal("-13.00"),
            status=OfxTransaction.Status.MISSING_PAYMENT,
        )
        OfxTransaction.objects.create(
            ofx_file=ofx_file,
            fitid="OFX-CREDIT",
            posted_at=date(2026, 6, 13),
            amount=Decimal("99.00"),
            status=OfxTransaction.Status.IGNORED,
        )
        self.make_payment(
            amount=Decimal("12.00"),
            status=Payment.Status.PENDING_REGISTRATION,
            source=Origin.OFX,
            raw_payload={"ofx_transaction_id": registration_transaction.pk},
        )
        self.make_payment(
            amount=Decimal("13.00"),
            status=Payment.Status.PENDING_CONFIRMATION,
            source=Origin.OFX,
            raw_payload={"ofx_transaction_id": confirmation_transaction.pk},
        )
        OfxTransaction.objects.create(
            ofx_file=ofx_file,
            fitid="OFX-DIVERGENT",
            posted_at=date(2026, 6, 11),
            amount=Decimal("-2.00"),
            status=OfxTransaction.Status.DIVERGENT,
        )
        OfxTransaction.objects.create(
            ofx_file=ofx_file,
            fitid="OFX-DUP",
            posted_at=date(2026, 6, 12),
            amount=Decimal("-3.00"),
            status=OfxTransaction.Status.POSSIBLE_DUPLICATE,
        )

        response = self.client.get(reverse("internal_dashboard"), {"mes": "6", "ano": "2026"})
        labels = {item["label"]: item for item in response.context["operational_pendencies"]}

        for label in (
            "Active drafts",
            "Payments without date",
            "Pending registration",
            "Pending approval",
            "Under correction",
            "Approved/posted without OFX",
            "OFX suggestion pending registration",
            "OFX suggestion pending approval",
            "OFX expense without payment",
            "OFX pending",
            "Divergent OFX",
            "OFX possible duplicate",
            "Ignored OFX credits",
            "Projects with spend and no budget",
        ):
            with self.subTest(label=label):
                self.assertIn(label, labels)
                self.assertContains(response, label)
        self.assertEqual(labels["Pending registration"]["amount"], "R$ 24,00")
        self.assertEqual(labels["OFX suggestion pending registration"]["amount"], "R$ 12,00")
        self.assertEqual(labels["OFX suggestion pending approval"]["amount"], "R$ 13,00")
        self.assertEqual(labels["Under correction"]["amount"], "R$ 14,00")
        self.assertEqual(labels["Projects with spend and no budget"]["amount"], "R$ 16,00")

    def test_operational_pendency_links_point_to_resolution_pages(self):
        self.client.force_login(self.user)
        work = Work.objects.create(name="Tacima", normalized_name="tacima")
        TelegramDraft.objects.create(telegram_user_id=327694327, sender_name="Jair", payment_date=date(2026, 6, 10))
        self.make_payment(amount=Decimal("12.00"), status=Payment.Status.PENDING_REGISTRATION)
        self.make_payment(amount=Decimal("13.00"), status=Payment.Status.PENDING_CONFIRMATION)
        self.make_payment(amount=Decimal("14.00"), status=Payment.Status.CORRECTING)
        self.make_payment(amount=Decimal("15.00"), status=Payment.Status.POSTED)
        self.make_payment(amount=Decimal("16.00"), work=work, cost_center=self.work_center, status=Payment.Status.APPROVED)
        self.make_payment(amount=Decimal("11.00"), payment_date=None, status=Payment.Status.RECEIVED)
        ofx_file = OfxFile.objects.create(original_filename="pendencias.ofx")
        OfxTransaction.objects.create(
            ofx_file=ofx_file,
            fitid="OFX-PENDING",
            posted_at=date(2026, 6, 10),
            amount=Decimal("-1.00"),
            status=OfxTransaction.Status.PENDING,
        )
        registration_transaction = OfxTransaction.objects.create(
            ofx_file=ofx_file,
            fitid="OFX-REGISTRATION",
            posted_at=date(2026, 6, 11),
            amount=Decimal("-12.00"),
            status=OfxTransaction.Status.MISSING_PAYMENT,
        )
        confirmation_transaction = OfxTransaction.objects.create(
            ofx_file=ofx_file,
            fitid="OFX-CONFIRMATION",
            posted_at=date(2026, 6, 12),
            amount=Decimal("-13.00"),
            status=OfxTransaction.Status.MISSING_PAYMENT,
        )
        OfxTransaction.objects.create(
            ofx_file=ofx_file,
            fitid="OFX-CREDIT",
            posted_at=date(2026, 6, 13),
            amount=Decimal("99.00"),
            status=OfxTransaction.Status.IGNORED,
        )
        self.make_payment(
            amount=Decimal("12.00"),
            status=Payment.Status.PENDING_REGISTRATION,
            source=Origin.OFX,
            raw_payload={"ofx_transaction_id": registration_transaction.pk},
        )
        self.make_payment(
            amount=Decimal("13.00"),
            status=Payment.Status.PENDING_CONFIRMATION,
            source=Origin.OFX,
            raw_payload={"ofx_transaction_id": confirmation_transaction.pk},
        )

        response = self.client.get(reverse("internal_dashboard"), {"mes": "6", "ano": "2026"})
        rows = {item["label"]: item for item in response.context["operational_pendencies"]}

        self.assertEqual(urlsplit(rows["Active drafts"]["url"]).path, reverse("internal_telegram_drafts"))
        self.assertEqual(parse_qs(urlsplit(rows["Active drafts"]["url"]).query)["date_inicio"], ["2026-06-01"])
        self.assertEqual(parse_qs(urlsplit(rows["Active drafts"]["url"]).query)["date_fim"], ["2026-06-30"])
        self.assertEqual(urlsplit(rows["Payments without date"]["url"]).path, reverse("internal_pending_payments"))
        self.assertEqual(parse_qs(urlsplit(rows["Payments without date"]["url"]).query)["date_status"], ["sem_date"])
        self.assertEqual(
            parse_qs(urlsplit(rows["Pending registration"]["url"]).query)["status"],
            [Payment.Status.PENDING_REGISTRATION],
        )
        self.assertEqual(
            parse_qs(urlsplit(rows["Pending approval"]["url"]).query)["status"],
            [Payment.Status.PENDING_CONFIRMATION],
        )
        self.assertEqual(
            parse_qs(urlsplit(rows["Under correction"]["url"]).query)["status"],
            [Payment.Status.CORRECTING],
        )
        self.assertEqual(parse_qs(urlsplit(rows["Approved/posted without OFX"]["url"]).query)["ofx"], ["sem"])
        self.assertEqual(
            parse_qs(urlsplit(rows["Approved/posted without OFX"]["url"]).query)["status"],
            ["realizados"],
        )
        self.assertEqual(urlsplit(rows["OFX pending"]["url"]).path, reverse("internal_unreconciled_ofx"))
        self.assertEqual(
            parse_qs(urlsplit(rows["OFX pending"]["url"]).query)["status"],
            [OfxTransaction.Status.PENDING],
        )
        self.assertEqual(
            parse_qs(urlsplit(rows["OFX expense without payment"]["url"]).query)["status"],
            ["pendencias"],
        )
        self.assertEqual(
            parse_qs(urlsplit(rows["OFX suggestion pending registration"]["url"]).query)["payment"],
            [Payment.Status.PENDING_REGISTRATION],
        )
        self.assertEqual(
            parse_qs(urlsplit(rows["OFX suggestion pending approval"]["url"]).query)["payment"],
            [Payment.Status.PENDING_CONFIRMATION],
        )
        self.assertEqual(
            parse_qs(urlsplit(rows["Ignored OFX credits"]["url"]).query)["status"],
            [OfxTransaction.Status.IGNORED],
        )
        self.assertEqual(
            urlsplit(rows["Projects with spend and no budget"]["url"]).path,
            reverse("internal_work_budget_import", args=[work.pk]),
        )

    def test_confirmed_reconciliation_removes_payment_from_ofx_pendency(self):
        self.client.force_login(self.user)
        payment = self.make_payment(amount=Decimal("100.00"), status=Payment.Status.APPROVED)
        self.make_confirmed_reconciliation(payment, fitid="CONFIRMED-OFX")

        response = self.client.get(reverse("internal_dashboard"), {"mes": "6", "ano": "2026"})
        labels = [item["label"] for item in response.context["operational_pendencies"]]

        self.assertNotIn("Approved/posted without OFX", labels)

    def test_posted_payment_without_confirmed_reconciliation_appears_as_ofx_pendency(self):
        self.client.force_login(self.user)
        self.make_payment(amount=Decimal("100.00"), status=Payment.Status.POSTED)

        response = self.client.get(reverse("internal_dashboard"), {"mes": "6", "ano": "2026"})
        rows = {item["label"]: item for item in response.context["operational_pendencies"]}

        self.assertIn("Approved/posted without OFX", rows)
        self.assertEqual(rows["Approved/posted without OFX"]["count"], 1)

    def test_sem_date_filter_on_payments_lists_only_undated_records(self):
        self.client.force_login(self.user)
        undated = self.make_payment(amount=Decimal("11.00"), payment_date=None, status=Payment.Status.RECEIVED)
        self.make_payment(amount=Decimal("12.00"), payment_date=date(2026, 6, 10), status=Payment.Status.RECEIVED)

        response = self.client.get(reverse("internal_pending_payments"), {"date_status": "sem_date"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual([payment.pk for payment in response.context["payments"]], [undated.pk])

    def test_period_filter_on_payments_uses_due_date_before_payment_date(self):
        self.client.force_login(self.user)
        june_due = self.make_payment(
            amount=Decimal("100.00"),
            due_date=date(2026, 6, 5),
            payment_date=date(2026, 5, 13),
        )
        self.make_payment(
            amount=Decimal("200.00"),
            due_date=date(2026, 5, 31),
            payment_date=date(2026, 6, 1),
        )

        response = self.client.get(
            reverse("internal_pending_payments"),
            {"date_inicio": "2026-06-01", "date_fim": "2026-06-30", "status": "realizados"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual([payment.pk for payment in response.context["payments"]], [june_due.pk])

    @override_settings(
        TELEGRAM_BOT_TOKEN="telegram-secret-token-for-dashboard-test",
        OPENAI_API_KEY="openai-secret-key-for-dashboard-test",
        SECRET_KEY="django-secret-key-for-dashboard-test",
    )
    def test_dashboard_does_not_expose_sensitive_raw_data_or_tokens(self):
        self.client.force_login(self.user)
        payment = self.make_payment(
            raw_payload={
                "receipt_texto": "conteudo completo sensivel telegram-secret-token-for-dashboard-test",
                "openai": "openai-secret-key-for-dashboard-test",
            }
        )
        ofx_file = OfxFile.objects.create(original_filename="extrato.ofx")
        OfxTransaction.objects.create(
            ofx_file=ofx_file,
            fitid="FIT-SENSITIVE",
            posted_at=payment.payment_date,
            amount=Decimal("-100.00"),
            memo="MEMO com dado sensivel telegram-secret-token-for-dashboard-test",
            status=OfxTransaction.Status.PENDING,
        )

        response = self.client.get(reverse("internal_dashboard"), {"mes": "6", "ano": "2026"})

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "telegram-secret-token-for-dashboard-test")
        self.assertNotContains(response, "openai-secret-key-for-dashboard-test")
        self.assertNotContains(response, "django-secret-key-for-dashboard-test")
        self.assertNotContains(response, "conteudo completo sensivel")
        self.assertNotContains(response, "MEMO com dado sensivel")

    def test_financial_center_percentages_sum_close_to_100_when_there_are_expenses(self):
        work = Work.objects.create(name="Tacima", normalized_name="tacima")
        other_work = Work.objects.create(name="Sertãozinho", normalized_name="sertaozinho")
        self.make_payment(amount=Decimal("100.00"), work=work, cost_center=self.work_center)
        self.make_payment(amount=Decimal("200.00"), work=other_work, cost_center=self.work_center)
        self.make_payment(amount=Decimal("300.00"), work=None, cost_center=self.cost_center)
        self.client.force_login(self.user)

        response = self.client.get(reverse("internal_dashboard"), {"mes": "6", "ano": "2026"})

        total_percentage = sum(item["percentage"] for item in response.context["financial_center_legend"])
        self.assertGreaterEqual(total_percentage, Decimal("99.9"))
        self.assertLessEqual(total_percentage, Decimal("100.1"))

    def test_zero_amount_groups_do_not_appear_in_chart_legend(self):
        work = Work.objects.create(name="Grupo Zero", normalized_name="grupo zero")
        self.make_payment(amount=Decimal("0.00"), work=work, cost_center=self.work_center)
        self.make_payment(amount=Decimal("100.00"), work=None, cost_center=self.cost_center)
        self.client.force_login(self.user)

        response = self.client.get(reverse("internal_dashboard"), {"mes": "6", "ano": "2026"})
        labels = [item["label"] for item in response.context["financial_center_legend"]]

        self.assertNotIn("Project: Grupo Zero", labels)
        self.assertIn("Company", labels)

    def test_financial_center_legend_links_to_filtered_payments(self):
        work = Work.objects.create(name="Tacima", normalized_name="tacima")
        self.make_payment(amount=Decimal("100.00"), work=work, cost_center=self.work_center)
        self.client.force_login(self.user)

        response = self.client.get(reverse("internal_dashboard"), {"mes": "6", "ano": "2026"})
        legend_item = next(item for item in response.context["financial_center_legend"] if item["label"] == "Project: Tacima")
        query = parse_qs(urlsplit(legend_item["url"]).query)

        self.assertEqual(urlsplit(legend_item["url"]).path, reverse("internal_pending_payments"))
        self.assertEqual(query["status"], ["realizados"])
        self.assertEqual(query["date_inicio"], ["2026-06-01"])
        self.assertEqual(query["date_fim"], ["2026-06-30"])
        self.assertEqual(query["work"], [str(work.pk)])

    def test_category_rows_link_to_filtered_payments(self):
        self.make_payment(amount=Decimal("100.00"), category=self.category)
        self.client.force_login(self.user)

        response = self.client.get(reverse("internal_dashboard"), {"mes": "6", "ano": "2026"})
        category_row = next(item for item in response.context["category_rows"] if item["label"] == "Materiais")
        query = parse_qs(urlsplit(category_row["url"]).query)

        self.assertEqual(urlsplit(category_row["url"]).path, reverse("internal_pending_payments"))
        self.assertEqual(query["status"], ["realizados"])
        self.assertEqual(query["date_inicio"], ["2026-06-01"])
        self.assertEqual(query["date_fim"], ["2026-06-30"])
        self.assertEqual(query["category"], [str(self.category.pk)])

    def test_category_rows_link_for_missing_category_filters_missing_category(self):
        self.make_payment(amount=Decimal("100.00"), category=None)
        self.client.force_login(self.user)

        response = self.client.get(reverse("internal_dashboard"), {"mes": "6", "ano": "2026"})
        category_row = next(item for item in response.context["category_rows"] if item["label"] == "No category")
        query = parse_qs(urlsplit(category_row["url"]).query)

        self.assertEqual(query["category"], ["sem"])

    def test_work_budget_row_links_to_work_filtered_payments_and_budget_import(self):
        work = Work.objects.create(name="Tacima", normalized_name="tacima")
        self.make_payment(amount=Decimal("100.00"), work=work, cost_center=self.work_center)
        self.client.force_login(self.user)

        response = self.client.get(reverse("internal_dashboard"), {"mes": "6", "ano": "2026"})
        row = next(item for item in response.context["work_budget_rows"] if item["item"].work_id == work.pk)
        query = parse_qs(urlsplit(row["payments_url"]).query)

        self.assertEqual(urlsplit(row["payments_url"]).path, reverse("internal_pending_payments"))
        self.assertEqual(query["status"], ["all"])
        self.assertEqual(query["date_inicio"], ["2026-06-01"])
        self.assertEqual(query["date_fim"], ["2026-06-30"])
        self.assertEqual(query["work"], [str(work.pk)])
        self.assertEqual(urlsplit(row["budget_import_url"]).path, reverse("internal_work_budget_import", args=[work.pk]))

    def test_realized_status_filter_on_payments_matches_dashboard_links(self):
        approved = self.make_payment(description="Approved no dashboard", status=Payment.Status.APPROVED)
        self.make_payment(description="Pending fora do dashboard", status=Payment.Status.PENDING_CONFIRMATION)
        self.client.force_login(self.user)

        response = self.client.get(
            reverse("internal_pending_payments"),
            {"status": "realizados", "date_inicio": "2026-06-01", "date_fim": "2026-06-30"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, approved.counterparty.name)
        self.assertEqual([payment.pk for payment in response.context["payments"]], [approved.pk])

    def test_payment_filters_accept_dashboard_work_category_and_ofx_query(self):
        work = Work.objects.create(name="Tacima", normalized_name="tacima")
        matched = self.make_payment(
            description="Filtro combinado",
            amount=Decimal("100.00"),
            work=work,
            cost_center=self.work_center,
            category=self.category,
            status=Payment.Status.APPROVED,
        )
        reconciled = self.make_payment(
            description="Com OFX",
            amount=Decimal("80.00"),
            work=work,
            cost_center=self.work_center,
            category=self.category,
            status=Payment.Status.APPROVED,
        )
        self.make_confirmed_reconciliation(reconciled, fitid="FILTER-CONFIRMED", amount=Decimal("-80.00"))
        self.make_payment(description="Outra category", amount=Decimal("70.00"), category=None)
        self.client.force_login(self.user)

        response = self.client.get(
            reverse("internal_pending_payments"),
            {
                "status": "realizados",
                "date_inicio": "2026-06-01",
                "date_fim": "2026-06-30",
                "work": str(work.pk),
                "category": str(self.category.pk),
                "ofx": "sem",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual([payment.pk for payment in response.context["payments"]], [matched.pk])

    def test_payment_filters_accept_missing_category_work_and_cost_center(self):
        matched = self.make_payment(
            description="Sem classificacao",
            amount=Decimal("100.00"),
            category=None,
            work=None,
            cost_center=None,
        )
        self.make_payment(description="Com category", amount=Decimal("80.00"), category=self.category)
        self.client.force_login(self.user)

        response = self.client.get(
            reverse("internal_pending_payments"),
            {
                "status": "realizados",
                "date_inicio": "2026-06-01",
                "date_fim": "2026-06-30",
                "category": "sem",
                "work": "sem",
                "cost_center": "sem",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual([payment.pk for payment in response.context["payments"]], [matched.pk])

    def test_draft_period_filter_from_dashboard_link_lists_matching_drafts(self):
        june_draft = TelegramDraft.objects.create(
            telegram_user_id=327694327,
            sender_name="Jair",
            payment_date=date(2026, 6, 10),
        )
        TelegramDraft.objects.create(
            telegram_user_id=327694327,
            sender_name="Jair",
            payment_date=date(2026, 5, 10),
        )
        self.client.force_login(self.user)

        response = self.client.get(
            reverse("internal_telegram_drafts"),
            {
                "status": TelegramDraft.Status.ACTIVE,
                "date_inicio": "2026-06-01",
                "date_fim": "2026-06-30",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual([draft.pk for draft in response.context["drafts"]], [june_draft.pk])

    def test_financial_center_names_with_special_chars_are_escaped(self):
        work = Work.objects.create(
            name="Tacima <Especial & Project>",
            normalized_name="tacima especial project",
        )
        self.make_payment(amount=Decimal("100.00"), work=work, cost_center=self.work_center)
        self.client.force_login(self.user)

        response = self.client.get(reverse("internal_dashboard"), {"mes": "6", "ano": "2026"})

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Tacima <Especial & Project>")
        self.assertContains(response, "Tacima &lt;Especial &amp; Project&gt;")

    def test_many_financial_center_groups_are_collapsed_into_others(self):
        for index in range(10):
            work = Work.objects.create(name=f"Project Muito Longa {index}", normalized_name=f"project muito longa {index}")
            self.make_payment(amount=Decimal(f"{100 + index}.00"), work=work, cost_center=self.work_center)
        self.client.force_login(self.user)

        response = self.client.get(reverse("internal_dashboard"), {"mes": "6", "ano": "2026"})
        legend = response.context["financial_center_legend"]

        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(len(legend), 8)
        self.assertTrue(legend[-1]["is_other"])
        self.assertIn("Other", legend[-1]["label"])

    def test_work_without_budget_shows_import_link(self):
        work = Work.objects.create(name="Tacima", normalized_name="tacima")
        self.make_payment(amount=Decimal("100.00"), work=work, cost_center=self.work_center)
        self.client.force_login(self.user)

        response = self.client.get(reverse("internal_dashboard"), {"mes": "6", "ano": "2026"})

        self.assertContains(response, "No budget")
        self.assertContains(response, reverse("internal_work_budget_import", args=[work.pk]))
        self.assertContains(response, "Import budget")

    def test_payment_without_work_stays_out_of_work_table_but_enters_company_chart_group(self):
        self.make_payment(amount=Decimal("100.00"), work=None, cost_center=self.cost_center)
        self.client.force_login(self.user)

        response = self.client.get(reverse("internal_dashboard"), {"mes": "6", "ano": "2026"})
        labels = [item["label"] for item in response.context["financial_center_legend"]]

        self.assertEqual(response.context["summary"].work_budget_summaries, [])
        self.assertIn("Company", labels)
