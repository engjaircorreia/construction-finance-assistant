from io import BytesIO
from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from apps.banking.models import OfxFile, OfxTransaction, Reconciliation
from apps.counterparties.models import BudgetImportBatch, BudgetItem, Category, CostCenter, CounterpartyDocument, Origin, Work
from apps.counterparties.models import Counterparty
from apps.documents.models import UploadedFile
from apps.exports.models import ExportBatch
from apps.exports.services import payment_missing_required_fields
from apps.payments.models import Payment, PaymentConfirmation
from apps.telegrambot.models import TelegramDraft
from openpyxl import Workbook


class InternalPanelTests(TestCase):
    def setUp(self):
        self.tempdir = TemporaryDirectory()
        self.override = override_settings(MEDIA_ROOT=Path(self.tempdir.name))
        self.override.enable()
        User = get_user_model()
        self.user = User.objects.create_user(username="jair", password="senha-forte")
        self.counterparty = Counterparty.objects.create(
            name="ACME Materiais",
            normalized_name="acme materiais",
            primary_document="12345678000199",
        )
        self.payment = Payment.objects.create(
            payment_date=date(2026, 6, 24),
            amount=Decimal("250.00"),
            counterparty=self.counterparty,
            status=Payment.Status.RECEIVED,
            source="telegram",
            needs_review=True,
        )
        self.ofx_file = OfxFile.objects.create(original_filename="extrato.ofx")
        self.transaction = OfxTransaction.objects.create(
            ofx_file=self.ofx_file,
            fitid="FIT-1",
            posted_at=date(2026, 6, 24),
            amount=Decimal("-250.00"),
            memo="PIX ENVIADO ACME MATERIAIS",
            status=OfxTransaction.Status.PENDING,
        )

    def tearDown(self):
        self.override.disable()
        self.tempdir.cleanup()

    def test_anonymous_user_is_redirected_to_login(self):
        draft = TelegramDraft.objects.create(telegram_user_id=123, sender_name="Jair")
        work = Work.objects.create(name="Tacima", normalized_name="tacima")
        urls = [
            reverse("internal_dashboard"),
            reverse("internal_monthly_closing"),
            reverse("internal_operational_diagnostics"),
            reverse("internal_counterparty_create"),
            reverse("internal_supplier_quick_create"),
            reverse("internal_worker_quick_create"),
            reverse("internal_work_cost_center_quick_create"),
            reverse("internal_work_budget_import", args=[work.pk]),
            reverse("internal_pending_payments"),
            reverse("internal_payment_bulk_action"),
            reverse("internal_telegram_drafts"),
            reverse("internal_telegram_draft_detail", args=[draft.pk]),
            reverse("internal_telegram_draft_update", args=[draft.pk]),
            reverse("internal_payment_create"),
            reverse("internal_payment_update", args=[self.payment.pk]),
            reverse("internal_payment_delete", args=[self.payment.pk]),
            reverse("internal_payment_detail", args=[self.payment.pk]),
            reverse("internal_unreconciled_ofx"),
            reverse("internal_ofx_clear_period"),
            reverse("internal_ofx_payment_bulk_edit"),
            reverse("internal_ofx_action", args=[self.transaction.pk, "ignore"]),
            reverse("internal_export_batches"),
        ]

        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/accounts/login/", response["Location"])

    def test_authenticated_user_can_access_internal_pages(self):
        self.client.force_login(self.user)
        batch = self.generated_batch()
        work = Work.objects.create(name="Tacima", normalized_name="tacima")
        urls = [
            reverse("internal_dashboard"),
            reverse("internal_monthly_closing"),
            reverse("internal_operational_diagnostics"),
            reverse("internal_counterparty_create"),
            reverse("internal_supplier_quick_create"),
            reverse("internal_worker_quick_create"),
            reverse("internal_work_cost_center_quick_create"),
            reverse("internal_work_budget_import", args=[work.pk]),
            reverse("internal_pending_payments"),
            reverse("internal_telegram_drafts"),
            reverse("internal_payment_create"),
            reverse("internal_payment_update", args=[self.payment.pk]),
            reverse("internal_payment_delete", args=[self.payment.pk]),
            reverse("internal_payment_detail", args=[self.payment.pk]),
            reverse("internal_unreconciled_ofx"),
            reverse("internal_export_batches"),
            reverse("internal_export_download", args=[batch.pk]),
            reverse("internal_export_download_kind", args=[batch.pk, "importacao"]),
            reverse("internal_export_download_kind", args=[batch.pk, "exportacao"]),
        ]

        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)

    def test_authenticated_user_sees_logout_button(self):
        self.client.force_login(self.user)

        response = self.client.get(
            reverse("internal_pending_payments"),
            {"date_inicio": "2026-06-01", "date_fim": "2026-06-30"},
        )

        self.assertContains(response, "Sign Out")
        self.assertContains(response, f'action="{reverse("logout")}"')
        self.assertContains(response, 'method="post"')

    def test_logout_post_ends_session_and_redirects_to_login(self):
        self.client.force_login(self.user)

        response = self.client.post(reverse("logout"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/accounts/login/")
        internal_response = self.client.get(reverse("internal_pending_payments"))
        self.assertEqual(internal_response.status_code, 302)
        self.assertIn("/accounts/login/", internal_response["Location"])

    def test_main_internal_pages_respond_200_for_authenticated_user(self):
        self.client.force_login(self.user)
        urls = [
            reverse("internal_dashboard"),
            reverse("internal_monthly_closing"),
            reverse("internal_pending_payments"),
            reverse("internal_telegram_drafts"),
            reverse("internal_unreconciled_ofx"),
            reverse("internal_export_batches"),
            reverse("internal_operational_diagnostics"),
        ]

        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)

    def test_operational_diagnostics_displays_main_counters(self):
        self.client.force_login(self.user)
        TelegramDraft.objects.create(telegram_user_id=123, sender_name="Jair")
        self.payment.status = Payment.Status.PENDING_REGISTRATION
        self.payment.save(update_fields=["status", "updated_at"])
        self.transaction.status = OfxTransaction.Status.DIVERGENT
        self.transaction.save(update_fields=["status", "updated_at"])
        batch = self.generated_batch(generated_by=self.user)

        response = self.client.get(reverse("internal_operational_diagnostics"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Operational diagnostics")
        self.assertContains(response, "Active drafts")
        self.assertContains(response, "Pending payments")
        self.assertContains(response, "Pending registration")
        self.assertContains(response, "Divergent OFX")
        self.assertContains(response, f"Batch #{batch.pk}")
        self.assertEqual(response.context["diagnostics"]["counters"]["active_drafts"], 1)
        self.assertEqual(response.context["diagnostics"]["counters"]["pending_registration"], 1)
        self.assertEqual(response.context["diagnostics"]["counters"]["ofx_divergent"], 1)

    @override_settings(
        TELEGRAM_BOT_TOKEN="telegram-secret-token-for-test",
        OPENAI_API_KEY="openai-secret-key-for-test",
        SECRET_KEY="django-secret-key-for-test",
    )
    def test_operational_diagnostics_does_not_expose_sensitive_values(self):
        self.client.force_login(self.user)
        ExportBatch.objects.create(
            status=ExportBatch.Status.ERROR,
            error_message="Failure with telegram-secret-token-for-test and openai-secret-key-for-test.",
        )

        response = self.client.get(reverse("internal_operational_diagnostics"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "telegram-secret-token-for-test")
        self.assertNotContains(response, "openai-secret-key-for-test")
        self.assertNotContains(response, "django-secret-key-for-test")
        self.assertContains(response, "[removed]")

    def test_operational_diagnostics_survives_redis_and_worker_failures(self):
        self.client.force_login(self.user)

        with patch("apps.core.views.check_redis_status", side_effect=RuntimeError("redis indisponivel")), patch(
            "apps.core.views.check_celery_status",
            side_effect=RuntimeError("celery indisponivel"),
        ):
            response = self.client.get(reverse("internal_operational_diagnostics"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Redis")
        self.assertContains(response, "Celery/worker")
        self.assertContains(response, "not checked")

    def test_telegram_drafts_page_lists_recent_drafts(self):
        self.client.force_login(self.user)
        draft = TelegramDraft.objects.create(
            telegram_user_id=123,
            sender_name="Jair",
            sender_username="JairCorreia",
            amount=Decimal("100.00"),
            raw_payload={"work_candidate": {"name": "Tacima", "source": "telegram"}},
        )

        response = self.client.get(reverse("internal_telegram_drafts"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f"#{draft.pk}")
        self.assertContains(response, "Jair")
        self.assertContains(response, "Tacima")
        self.assertContains(response, reverse("internal_telegram_draft_detail", args=[draft.pk]))

    def test_telegram_drafts_default_filter_shows_only_active(self):
        self.client.force_login(self.user)
        active = TelegramDraft.objects.create(telegram_user_id=123, sender_name="Remetente Um")
        TelegramDraft.objects.create(
            telegram_user_id=456,
            sender_name="Remetente Dois",
            status=TelegramDraft.Status.FINALIZED,
        )

        response = self.client.get(reverse("internal_telegram_drafts"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Remetente Um")
        self.assertContains(response, f"#{active.pk}")
        self.assertNotContains(response, "Remetente Dois")

    def test_telegram_drafts_status_filter_works(self):
        self.client.force_login(self.user)
        TelegramDraft.objects.create(telegram_user_id=123, sender_name="Remetente Um")
        finalized = TelegramDraft.objects.create(
            telegram_user_id=456,
            sender_name="Remetente Dois",
            status=TelegramDraft.Status.FINALIZED,
        )

        response = self.client.get(reverse("internal_telegram_drafts"), {"status": TelegramDraft.Status.FINALIZED})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Remetente Dois")
        self.assertContains(response, f"#{finalized.pk}")
        self.assertNotContains(response, "Remetente Um")

    def test_telegram_draft_open_link_goes_to_detail(self):
        self.client.force_login(self.user)
        draft = TelegramDraft.objects.create(telegram_user_id=123, sender_name="Jair")

        list_response = self.client.get(reverse("internal_telegram_drafts"))
        detail_response = self.client.get(reverse("internal_telegram_draft_detail", args=[draft.pk]))

        self.assertContains(list_response, reverse("internal_telegram_draft_detail", args=[draft.pk]))
        self.assertEqual(detail_response.status_code, 200)
        self.assertContains(detail_response, f"Draft #{draft.pk}")
        self.assertContains(detail_response, "Payment preview")
        self.assertContains(detail_response, "Payment suggestion")

    def test_telegram_draft_valid_post_updates_draft_without_creating_payment(self):
        self.client.force_login(self.user)
        category = Category.objects.create(name="Materiais", normalized_name="materiais")
        cost_center = CostCenter.objects.create(name="Project", normalized_name="project")
        work = Work.objects.create(name="Sertãozinho", normalized_name="sertaozinho")
        draft = TelegramDraft.objects.create(telegram_user_id=123, sender_name="Jair")

        response = self.client.post(
            reverse("internal_telegram_draft_update", args=[draft.pk]),
            {
                "payment_date": "2026-06-25",
                "amount": "300.50",
                "counterparty": str(self.counterparty.pk),
                "description": "Compra de material",
                "category": str(category.pk),
                "payment_method": "PIX",
                "cost_center": str(cost_center.pk),
                "work": str(work.pk),
                "work_item_index": "1.2",
            },
        )
        draft.refresh_from_db()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("internal_telegram_draft_detail", args=[draft.pk]))
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(draft.payment_date, date(2026, 6, 25))
        self.assertEqual(draft.amount, Decimal("300.50"))
        self.assertEqual(draft.counterparty, self.counterparty)
        self.assertEqual(draft.category, category)
        self.assertEqual(draft.payment_method, "PIX")
        self.assertEqual(draft.cost_center, cost_center)
        self.assertEqual(draft.work, work)
        self.assertEqual(draft.work_item_index, "1.2")

    def test_telegram_draft_finalize_and_cancel_get_do_not_change_status(self):
        self.client.force_login(self.user)
        draft = TelegramDraft.objects.create(telegram_user_id=123, sender_name="Jair")

        finalize_response = self.client.get(reverse("internal_telegram_draft_finalize", args=[draft.pk]))
        draft.refresh_from_db()
        self.assertEqual(finalize_response.status_code, 405)
        self.assertEqual(draft.status, TelegramDraft.Status.ACTIVE)
        self.assertEqual(Payment.objects.count(), 1)

        cancel_response = self.client.get(reverse("internal_telegram_draft_cancel", args=[draft.pk]))
        draft.refresh_from_db()
        self.assertEqual(cancel_response.status_code, 405)
        self.assertEqual(draft.status, TelegramDraft.Status.ACTIVE)

    def test_telegram_draft_finalize_post_creates_payment_and_finalizes_draft(self):
        self.client.force_login(self.user)
        draft = TelegramDraft.objects.create(
            telegram_user_id=123,
            sender_name="Jair",
            payment_date=date(2026, 6, 25),
            amount=Decimal("450.00"),
            counterparty=self.counterparty,
            description="Compra de material",
        )

        response = self.client.post(reverse("internal_telegram_draft_finalize", args=[draft.pk]))
        draft.refresh_from_db()
        payment = Payment.objects.exclude(pk=self.payment.pk).get()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("internal_payment_detail", args=[payment.pk]))
        self.assertEqual(draft.status, TelegramDraft.Status.FINALIZED)
        self.assertEqual(draft.finalized_payment, payment)
        self.assertEqual(payment.counterparty, self.counterparty)
        self.assertEqual(payment.amount, Decimal("450.00"))

    def test_telegram_draft_finalize_with_work_without_budget_is_allowed(self):
        self.client.force_login(self.user)
        work = Work.objects.create(name="Tacima", normalized_name="tacima")
        cost_center = CostCenter.objects.create(name="Project", normalized_name="project")
        draft = TelegramDraft.objects.create(
            telegram_user_id=123,
            sender_name="Jair",
            payment_date=date(2026, 6, 25),
            amount=Decimal("450.00"),
            counterparty=self.counterparty,
            description="Labor sem imported budget",
            cost_center=cost_center,
            work=work,
            work_item_index="",
        )

        response = self.client.post(reverse("internal_telegram_draft_finalize", args=[draft.pk]))
        draft.refresh_from_db()
        payment = Payment.objects.exclude(pk=self.payment.pk).get()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(draft.status, TelegramDraft.Status.FINALIZED)
        self.assertEqual(payment.work, work)
        self.assertEqual(payment.cost_center, cost_center)
        self.assertEqual(payment.work_item_index, "")

    def test_telegram_draft_cancel_post_marks_draft_as_canceled(self):
        self.client.force_login(self.user)
        draft = TelegramDraft.objects.create(telegram_user_id=123, sender_name="Jair")

        response = self.client.post(reverse("internal_telegram_draft_cancel", args=[draft.pk]))
        draft.refresh_from_db()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("internal_telegram_drafts"))
        self.assertEqual(draft.status, TelegramDraft.Status.CANCELED)
        self.assertEqual(Payment.objects.count(), 1)

    def test_finalized_telegram_draft_cannot_be_edited(self):
        self.client.force_login(self.user)
        draft = TelegramDraft.objects.create(
            telegram_user_id=123,
            sender_name="Jair",
            status=TelegramDraft.Status.FINALIZED,
        )

        response = self.client.get(reverse("internal_telegram_draft_update", args=[draft.pk]))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("internal_telegram_draft_detail", args=[draft.pk]))

    def test_login_without_next_redirects_to_dashboard(self):
        response = self.client.post(
            reverse("login"),
            {
                "username": "jair",
                "password": "senha-forte",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("internal_dashboard"))

    def test_counterparty_create_saves_supplier_and_returns_to_payment_form(self):
        self.client.force_login(self.user)
        category = Category.objects.create(name="Materiais", normalized_name="materiais")
        cost_center = CostCenter.objects.create(name="Company", normalized_name="empresa")

        response = self.client.post(
            reverse("internal_counterparty_create"),
            {
                "next": reverse("internal_payment_create"),
                "name": "Novo Vendor Ltda",
                "kind": Counterparty.Kind.SUPPLIER,
                "person_type": Counterparty.PersonType.UNKNOWN,
                "primary_document": "12.345.678/0001-90",
                "default_category": str(category.pk),
                "default_cost_center": str(cost_center.pk),
            },
        )
        counterparty = Counterparty.objects.get(name="Novo Vendor Ltda")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"{reverse('internal_payment_create')}?counterparty={counterparty.pk}")
        self.assertEqual(counterparty.normalized_name, "novo vendor ltda")
        self.assertEqual(counterparty.primary_document, "12345678000190")
        self.assertEqual(counterparty.person_type, Counterparty.PersonType.COMPANY)
        self.assertEqual(counterparty.default_category, category)
        self.assertEqual(counterparty.default_cost_center, cost_center)
        self.assertTrue(
            CounterpartyDocument.objects.filter(
                counterparty=counterparty,
                number="12345678000190",
                is_primary=True,
            ).exists()
        )

    def test_counterparty_create_blocks_duplicate_document(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("internal_counterparty_create"),
            {
                "next": reverse("internal_payment_create"),
                "name": "Documento Repetido",
                "kind": Counterparty.Kind.SUPPLIER,
                "person_type": Counterparty.PersonType.UNKNOWN,
                "primary_document": self.counterparty.primary_document,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "A vendor/worker is already registered with this CPF/CNPJ.")
        self.assertFalse(Counterparty.objects.filter(name="Documento Repetido").exists())

    def test_supplier_quick_create_uses_minimal_fields_and_returns_to_payment_form(self):
        self.client.force_login(self.user)
        category = Category.objects.create(name="Materiais", normalized_name="materiais")

        response = self.client.post(
            reverse("internal_supplier_quick_create"),
            {
                "next": reverse("internal_payment_create"),
                "name": "Vendor Rápido",
                "primary_document": "11.222.333/0001-44",
                "default_category": str(category.pk),
            },
        )
        counterparty = Counterparty.objects.get(name="Vendor Rápido")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"{reverse('internal_payment_create')}?counterparty={counterparty.pk}")
        self.assertEqual(counterparty.kind, Counterparty.Kind.SUPPLIER)
        self.assertEqual(counterparty.primary_document, "11222333000144")
        self.assertEqual(counterparty.default_category, category)

    def test_worker_quick_create_marks_counterparty_as_worker(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("internal_worker_quick_create"),
            {
                "next": reverse("internal_payment_create"),
                "name": "Worker Rápido",
                "primary_document": "123.456.789-01",
            },
        )
        counterparty = Counterparty.objects.get(name="Worker Rápido")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(counterparty.kind, Counterparty.Kind.WORKER)
        self.assertEqual(counterparty.person_type, Counterparty.PersonType.INDIVIDUAL)
        self.assertEqual(counterparty.primary_document, "12345678901")

    def test_work_cost_center_quick_create_returns_selected_work_and_cost_center(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("internal_work_cost_center_quick_create"),
            {
                "next": reverse("internal_payment_create"),
                "work_name": "Project Nova",
                "cost_center_name": "Project",
                "city": "Sertãozinho",
                "state": "sp",
            },
        )
        work = Work.objects.get(name="Project Nova")
        cost_center = CostCenter.objects.get(name="Project")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"],
            f"{reverse('internal_payment_create')}?cost_center={cost_center.pk}&work={work.pk}",
        )
        self.assertEqual(work.normalized_name, "project nova")
        self.assertEqual(work.city, "Sertãozinho")
        self.assertEqual(work.state, "SP")

    def test_work_cost_center_quick_create_reuses_existing_cost_center_and_blocks_duplicate_work(self):
        self.client.force_login(self.user)
        CostCenter.objects.create(name="Project", normalized_name="project")
        Work.objects.create(name="Project Existente", normalized_name="project existente")

        response = self.client.post(
            reverse("internal_work_cost_center_quick_create"),
            {
                "next": reverse("internal_payment_create"),
                "work_name": "Project Existente",
                "cost_center_name": "Project",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "A project with this name already exists.")
        self.assertEqual(CostCenter.objects.filter(normalized_name="project").count(), 1)
        self.assertEqual(Work.objects.filter(normalized_name="project existente").count(), 1)

    def test_budget_import_rejects_non_xlsx_file(self):
        self.client.force_login(self.user)
        work = Work.objects.create(name="Tacima", normalized_name="tacima")
        upload = SimpleUploadedFile("orcamento.txt", b"conteudo", content_type="text/plain")

        response = self.client.post(reverse("internal_work_budget_import", args=[work.pk]), {"budget_file": upload})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Upload a valid .xlsx file.")
        self.assertEqual(UploadedFile.objects.filter(kind=UploadedFile.Kind.SPREADSHEET).count(), 0)
        self.assertEqual(BudgetItem.objects.count(), 0)

    def test_budget_import_valid_xlsx_creates_budget_items_and_report(self):
        self.client.force_login(self.user)
        work = Work.objects.create(name="Tacima", normalized_name="tacima")

        response = self.client.post(
            reverse("internal_work_budget_import", args=[work.pk]),
            {"budget_file": self.budget_upload(work_name="Tacima")},
        )
        batch = BudgetImportBatch.objects.get(work=work)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Import report")
        self.assertContains(response, "Items created")
        self.assertEqual(BudgetItem.objects.filter(work=work).count(), 3)
        self.assertEqual(batch.items_created, 3)
        self.assertEqual(batch.items_updated, 0)
        self.assertEqual(batch.rows_skipped, 0)
        self.assertEqual(batch.uploaded_by, self.user)
        self.assertEqual(batch.uploaded_file.kind, UploadedFile.Kind.SPREADSHEET)
        self.assertEqual(batch.uploaded_file.status, UploadedFile.Status.PROCESSED)

    def test_budget_import_reupload_does_not_duplicate_items(self):
        self.client.force_login(self.user)
        work = Work.objects.create(name="Tacima", normalized_name="tacima")
        url = reverse("internal_work_budget_import", args=[work.pk])

        self.client.post(url, {"budget_file": self.budget_upload(work_name="Tacima")})
        response = self.client.post(url, {"budget_file": self.budget_upload(work_name="Tacima")})
        latest_batch = BudgetImportBatch.objects.filter(work=work).latest("pk")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(BudgetItem.objects.filter(work=work).count(), 3)
        self.assertGreaterEqual(UploadedFile.objects.filter(kind=UploadedFile.Kind.SPREADSHEET).count(), 1)
        self.assertEqual(BudgetImportBatch.objects.filter(work=work).count(), 2)
        self.assertEqual(latest_batch.items_created, 0)
        self.assertEqual(latest_batch.items_updated + latest_batch.items_unchanged, 3)

    def test_budget_import_shows_conflicts_in_report(self):
        self.client.force_login(self.user)
        work = Work.objects.create(name="Tacima", normalized_name="tacima")

        response = self.client.post(
            reverse("internal_work_budget_import", args=[work.pk]),
            {"budget_file": self.budget_upload(work_name="Outra Project")},
        )
        batch = BudgetImportBatch.objects.get(work=work)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(BudgetItem.objects.filter(work=work).count(), 0)
        self.assertEqual(len(batch.conflicts), 1)
        self.assertContains(response, "Conflicts")
        self.assertContains(response, "budget_work_mismatch")
        self.assertContains(response, "The spreadsheet declares a different project")

    def test_budget_import_removes_work_without_budget_warning(self):
        self.client.force_login(self.user)
        work = Work.objects.create(name="Tacima", normalized_name="tacima")
        payment = self.make_export_ready_payment(status=Payment.Status.APPROVED, work=work)

        before_response = self.client.get(reverse("internal_payment_detail", args=[payment.pk]))
        self.client.post(
            reverse("internal_work_budget_import", args=[work.pk]),
            {"budget_file": self.budget_upload(work_name="Tacima")},
        )
        after_response = self.client.get(reverse("internal_payment_detail", args=[payment.pk]))

        self.assertContains(before_response, "Project without imported budget")
        self.assertNotContains(after_response, "Project without imported budget")

    def test_draft_quick_counterparty_form_opens_with_candidate_initial(self):
        self.client.force_login(self.user)
        draft = TelegramDraft.objects.create(
            telegram_user_id=123,
            sender_name="Jair",
            raw_payload={
                "counterparty_candidate": {
                    "name": "Ivaldo Martins",
                    "document": "12345678901",
                    "source": "pdf",
                }
            },
        )

        response = self.client.get(
            reverse("internal_supplier_quick_create"),
            {
                "draft": str(draft.pk),
                "next": reverse("internal_telegram_draft_detail", args=[draft.pk]),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'value="Ivaldo Martins"')
        self.assertContains(response, 'value="12345678901"')
        self.assertContains(response, f'name="draft" value="{draft.pk}"')

    def test_draft_quick_supplier_create_links_counterparty_to_draft(self):
        self.client.force_login(self.user)
        draft = TelegramDraft.objects.create(
            telegram_user_id=123,
            sender_name="Jair",
            raw_payload={
                "counterparty_candidate": {
                    "name": "Vendor do Draft",
                    "document": "11222333000144",
                    "source": "pdf",
                }
            },
        )
        detail_url = reverse("internal_telegram_draft_detail", args=[draft.pk])

        response = self.client.post(
            reverse("internal_supplier_quick_create"),
            {
                "draft": str(draft.pk),
                "next": detail_url,
                "name": "Vendor do Draft",
                "primary_document": "11.222.333/0001-44",
            },
        )
        draft.refresh_from_db()
        counterparty = Counterparty.objects.get(name="Vendor do Draft")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], detail_url)
        self.assertEqual(counterparty.kind, Counterparty.Kind.SUPPLIER)
        self.assertEqual(counterparty.primary_document, "11222333000144")
        self.assertEqual(draft.counterparty, counterparty)
        self.assertNotIn("counterparty_candidate", draft.raw_payload)

    def test_draft_quick_worker_create_links_counterparty_to_draft(self):
        self.client.force_login(self.user)
        draft = TelegramDraft.objects.create(
            telegram_user_id=123,
            sender_name="Jair",
            raw_payload={
                "counterparty_candidate": {
                    "name": "Worker do Draft",
                    "document": "",
                    "source": "texto",
                }
            },
        )
        detail_url = reverse("internal_telegram_draft_detail", args=[draft.pk])

        response = self.client.post(
            reverse("internal_worker_quick_create"),
            {
                "draft": str(draft.pk),
                "next": detail_url,
                "name": "Worker do Draft",
                "primary_document": "",
            },
        )
        draft.refresh_from_db()
        counterparty = Counterparty.objects.get(name="Worker do Draft")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], detail_url)
        self.assertEqual(counterparty.kind, Counterparty.Kind.WORKER)
        self.assertEqual(draft.counterparty, counterparty)
        self.assertNotIn("counterparty_candidate", draft.raw_payload)

    def test_draft_quick_work_create_links_work_and_projects_cost_center_to_draft(self):
        self.client.force_login(self.user)
        draft = TelegramDraft.objects.create(
            telegram_user_id=123,
            sender_name="Jair",
            raw_payload={"work_candidate": {"name": "Tacima", "source": "telegram"}},
        )
        detail_url = reverse("internal_telegram_draft_detail", args=[draft.pk])

        response = self.client.post(
            reverse("internal_work_cost_center_quick_create"),
            {
                "draft": str(draft.pk),
                "next": detail_url,
                "work_name": "Tacima",
                "cost_center_name": "Company",
            },
        )
        draft.refresh_from_db()
        work = Work.objects.get(normalized_name="tacima")
        cost_center = CostCenter.objects.get(normalized_name="project")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], detail_url)
        self.assertEqual(draft.work, work)
        self.assertEqual(draft.cost_center, cost_center)
        self.assertNotIn("work_candidate", draft.raw_payload)

    def test_draft_quick_counterparty_existing_document_is_reused_without_duplicate(self):
        self.client.force_login(self.user)
        existing = Counterparty.objects.create(
            name="Vendor Existente",
            normalized_name="vendor existente",
            primary_document="11222333000144",
        )
        draft = TelegramDraft.objects.create(
            telegram_user_id=123,
            sender_name="Jair",
            raw_payload={
                "counterparty_candidate": {
                    "name": "Vendor Existente",
                    "document": "11222333000144",
                    "source": "pdf",
                }
            },
        )
        detail_url = reverse("internal_telegram_draft_detail", args=[draft.pk])
        count_before = Counterparty.objects.count()

        response = self.client.post(
            reverse("internal_supplier_quick_create"),
            {
                "draft": str(draft.pk),
                "next": detail_url,
                "name": "Vendor Existente",
                "primary_document": "11.222.333/0001-44",
            },
        )
        draft.refresh_from_db()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], detail_url)
        self.assertEqual(Counterparty.objects.count(), count_before)
        self.assertEqual(draft.counterparty, existing)
        self.assertNotIn("counterparty_candidate", draft.raw_payload)

    def test_draft_quick_counterparty_ambiguous_name_returns_error_without_creating_duplicate(self):
        self.client.force_login(self.user)
        Counterparty.objects.create(name="Name Ambiguo", normalized_name="name ambiguo")
        Counterparty.objects.create(
            name="Name Ambiguo Alternactive",
            normalized_name="name ambiguo",
            kind=Counterparty.Kind.WORKER,
        )
        draft = TelegramDraft.objects.create(
            telegram_user_id=123,
            sender_name="Jair",
            raw_payload={"counterparty_candidate": {"name": "Name Ambiguo", "document": "", "source": "texto"}},
        )
        count_before = Counterparty.objects.count()

        response = self.client.post(
            reverse("internal_supplier_quick_create"),
            {
                "draft": str(draft.pk),
                "next": reverse("internal_telegram_draft_detail", args=[draft.pk]),
                "name": "Name Ambiguo",
                "primary_document": "",
            },
        )
        draft.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ambiguous record")
        self.assertEqual(Counterparty.objects.count(), count_before)
        self.assertIsNone(draft.counterparty)
        self.assertIn("counterparty_candidate", draft.raw_payload)

    def test_payment_create_can_preselect_new_counterparty(self):
        self.client.force_login(self.user)

        cost_center = CostCenter.objects.create(name="Project", normalized_name="project")
        work = Work.objects.create(name="Sertãozinho", normalized_name="sertaozinho")

        response = self.client.get(
            reverse("internal_payment_create"),
            {
                "counterparty": str(self.counterparty.pk),
                "cost_center": str(cost_center.pk),
                "work": str(work.pk),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"].initial["counterparty"], self.counterparty.pk)
        self.assertEqual(response.context["form"].initial["cost_center"], cost_center.pk)
        self.assertEqual(response.context["form"].initial["work"], work.pk)
        self.assertContains(response, "Novo vendor")
        self.assertContains(response, "Novo worker")
        self.assertContains(response, "New project/cost center")

    def test_manual_payment_create_saves_pending_confirmation(self):
        self.client.force_login(self.user)
        category = Category.objects.create(name="Materiais", normalized_name="materiais")
        cost_center = CostCenter.objects.create(name="Company", normalized_name="empresa")

        response = self.client.post(
            reverse("internal_payment_create"),
            {
                "payment_date": "2026-06-24",
                "amount": "180.50",
                "counterparty": str(self.counterparty.pk),
                "description": "Compra manual de material",
                "category": str(category.pk),
                "payment_method": "PIX",
                "cost_center": str(cost_center.pk),
            },
        )
        payment = Payment.objects.exclude(pk=self.payment.pk).get()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("internal_payment_detail", args=[payment.pk]))
        self.assertEqual(payment.source, "manual")
        self.assertEqual(payment.status, Payment.Status.PENDING_CONFIRMATION)
        self.assertEqual(payment.created_by, self.user)
        self.assertEqual(payment.competence_date, date(2026, 6, 24))
        self.assertEqual(payment.due_date, date(2026, 6, 24))
        self.assertEqual(payment.amount, Decimal("180.50"))
        self.assertEqual(payment.category, category)
        self.assertEqual(payment.cost_center, cost_center)
        self.assertTrue(payment.raw_payload["manual_web_entry"])

    def test_manual_payment_create_applies_company_cost_center_default(self):
        self.client.force_login(self.user)
        category = Category.objects.create(name="Impostos", normalized_name="impostos")
        company = CostCenter.objects.create(name="Company", normalized_name="empresa")

        self.client.post(
            reverse("internal_payment_create"),
            {
                "payment_date": "2026-06-24",
                "amount": "90.00",
                "counterparty": str(self.counterparty.pk),
                "description": "Payment de imposto",
                "category": str(category.pk),
                "payment_method": "PIX",
            },
        )

        payment = Payment.objects.exclude(pk=self.payment.pk).get()
        self.assertEqual(payment.cost_center, company)
        self.assertIsNone(payment.work)

    def test_payment_update_preserves_origin_and_returns_to_confirmation(self):
        self.client.force_login(self.user)
        self.payment.status = Payment.Status.CORRECTING
        self.payment.save(update_fields=["status", "updated_at"])
        category = Category.objects.create(name="Services", normalized_name="servicos")
        cost_center = CostCenter.objects.create(name="Project", normalized_name="project")

        response = self.client.post(
            reverse("internal_payment_update", args=[self.payment.pk]),
            {
                "payment_date": "2026-06-25",
                "amount": "300.00",
                "counterparty": str(self.counterparty.pk),
                "description": "Description corrigida",
                "category": str(category.pk),
                "payment_method": "TED",
                "cost_center": str(cost_center.pk),
            },
        )

        self.payment.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.payment.source, "telegram")
        self.assertEqual(self.payment.status, Payment.Status.PENDING_CONFIRMATION)
        self.assertEqual(self.payment.amount, Decimal("300.00"))
        self.assertEqual(self.payment.description, "Description corrigida")
        self.assertEqual(self.payment.category, category)
        self.assertEqual(self.payment.cost_center, cost_center)
        self.assertTrue(self.payment.raw_payload["manual_web_update"])

    def test_payment_update_form_uses_saved_values_instead_of_filter_query(self):
        self.client.force_login(self.user)
        saved_category = Category.objects.create(name="Materiais", normalized_name="materiais")
        saved_cost_center = CostCenter.objects.create(name="Company", normalized_name="empresa")
        saved_work = Work.objects.create(name="Sertãozinho", normalized_name="sertaozinho")
        other_counterparty = Counterparty.objects.create(name="Outro vendor", normalized_name="outro vendor")
        other_category = Category.objects.create(name="Outras despesas", normalized_name="outras despesas")
        other_cost_center = CostCenter.objects.create(name="Project", normalized_name="project")
        other_work = Work.objects.create(name="Tacima", normalized_name="tacima")
        self.payment.status = Payment.Status.CORRECTING
        self.payment.category = saved_category
        self.payment.cost_center = saved_cost_center
        self.payment.work = saved_work
        self.payment.competence_date = date(2026, 6, 30)
        self.payment.due_date = date(2026, 6, 30)
        self.payment.payment_date = date(2026, 6, 30)
        self.payment.description = "Description salva"
        self.payment.save(
            update_fields=[
                "status",
                "category",
                "cost_center",
                "work",
                "competence_date",
                "due_date",
                "payment_date",
                "description",
                "updated_at",
            ]
        )

        response = self.client.get(
            reverse("internal_payment_update", args=[self.payment.pk]),
            {
                "counterparty": str(other_counterparty.pk),
                "category": str(other_category.pk),
                "cost_center": str(other_cost_center.pk),
                "work": str(other_work.pk),
            },
        )
        form = response.context["form"]

        self.assertEqual(response.status_code, 200)
        self.assertEqual(str(form["counterparty"].value()), str(self.counterparty.pk))
        self.assertEqual(str(form["category"].value()), str(saved_category.pk))
        self.assertEqual(str(form["cost_center"].value()), str(saved_cost_center.pk))
        self.assertEqual(str(form["work"].value()), str(saved_work.pk))
        self.assertContains(response, "Description salva")
        self.assertContains(response, 'name="competence_date" value="2026-06-30"')
        self.assertContains(response, 'name="due_date" value="2026-06-30"')
        self.assertContains(response, 'name="payment_date" value="2026-06-30"')

    def test_payment_update_redirects_to_next_filtered_list(self):
        self.client.force_login(self.user)
        self.payment.status = Payment.Status.CORRECTING
        self.payment.save(update_fields=["status", "updated_at"])
        category = Category.objects.create(name="Services", normalized_name="servicos")
        cost_center = CostCenter.objects.create(name="Company", normalized_name="empresa")
        next_url = (
            f"{reverse('internal_pending_payments')}"
            "?date_inicio=2026-06-01&date_fim=2026-06-30&status=correcting"
        )

        response = self.client.post(
            reverse("internal_payment_update", args=[self.payment.pk]),
            {
                "payment_date": "2026-06-25",
                "amount": "300.00",
                "counterparty": str(self.counterparty.pk),
                "description": "Description corrigida",
                "category": str(category.pk),
                "payment_method": "PIX",
                "cost_center": str(cost_center.pk),
                "next": next_url,
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], next_url)

    def test_approved_payment_update_is_blocked_until_correction(self):
        self.client.force_login(self.user)
        self.payment.status = Payment.Status.APPROVED
        self.payment.save(update_fields=["status", "updated_at"])

        response = self.client.get(reverse("internal_payment_update", args=[self.payment.pk]))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("internal_payment_detail", args=[self.payment.pk]))

    def test_payment_delete_requires_post_and_removes_unlocked_payment(self):
        self.client.force_login(self.user)

        get_response = self.client.get(reverse("internal_payment_delete", args=[self.payment.pk]))
        post_response = self.client.post(reverse("internal_payment_delete", args=[self.payment.pk]))

        self.assertEqual(get_response.status_code, 200)
        self.assertEqual(post_response.status_code, 302)
        self.assertFalse(Payment.objects.filter(pk=self.payment.pk).exists())

    def test_payment_delete_redirects_to_next_filtered_list(self):
        self.client.force_login(self.user)
        next_url = (
            f"{reverse('internal_pending_payments')}"
            "?date_inicio=2026-06-01&date_fim=2026-06-30&status=all"
        )

        response = self.client.post(reverse("internal_payment_delete", args=[self.payment.pk]), {"next": next_url})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], next_url)
        self.assertFalse(Payment.objects.filter(pk=self.payment.pk).exists())

    def test_payment_delete_requires_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)

        response = client.post(reverse("internal_payment_delete", args=[self.payment.pk]))

        self.assertEqual(response.status_code, 403)
        self.assertTrue(Payment.objects.filter(pk=self.payment.pk).exists())

    def test_payment_delete_blocks_generated_exported_payment(self):
        self.client.force_login(self.user)
        batch = self.generated_batch()
        batch.payments.add(self.payment)

        response = self.client.post(reverse("internal_payment_delete", args=[self.payment.pk]))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(Payment.objects.filter(pk=self.payment.pk).exists())

    def test_payment_detail_shows_main_date_and_related_context(self):
        self.client.force_login(self.user)
        category = Category.objects.create(name="Materiais", normalized_name="materiais")
        cost_center = CostCenter.objects.create(name="Company", normalized_name="empresa")
        uploaded_file = UploadedFile.objects.create(
            original_filename="receipt.pdf",
            content_type="application/pdf",
            kind=UploadedFile.Kind.PDF,
            source=UploadedFile.Source.TELEGRAM,
            extracted_text="Extracted receipt text",
        )
        self.payment.category = category
        self.payment.cost_center = cost_center
        self.payment.description = "Compra de materiais"
        self.payment.payment_method = "PIX"
        self.payment.uploaded_file = uploaded_file
        self.payment.raw_payload = {"source": "teste", "amount_extraido": "250.00"}
        self.payment.save()
        PaymentConfirmation.objects.create(
            payment=self.payment,
            user=self.user,
            action=Payment.ConfirmationAction.CORRECT,
            message="Revisão inicial",
        )

        response = self.client.get(reverse("internal_payment_detail", args=[self.payment.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Main data")
        self.assertContains(response, "ACME Materiais")
        self.assertContains(response, "Compra de materiais")
        self.assertContains(response, "Materiais")
        self.assertContains(response, "Company")
        self.assertContains(response, "receipt.pdf")
        self.assertContains(response, "Extracted receipt text")
        self.assertContains(response, "amount_extraido")
        self.assertContains(response, "250.00")
        self.assertContains(response, "Confirmation history")
        self.assertContains(response, "Revisão inicial")

    def test_work_without_budget_warning_appears_on_draft_and_payment_detail(self):
        self.client.force_login(self.user)
        work = Work.objects.create(name="Tacima", normalized_name="tacima")
        draft = TelegramDraft.objects.create(
            telegram_user_id=123,
            sender_name="Jair",
            work=work,
            work_item_index="",
        )
        payment = self.make_export_ready_payment(status=Payment.Status.APPROVED, work=work)
        payment.work_item_index = ""
        payment.save(update_fields=["work_item_index", "updated_at"])

        draft_response = self.client.get(reverse("internal_telegram_draft_detail", args=[draft.pk]))
        payment_response = self.client.get(reverse("internal_payment_detail", args=[payment.pk]))

        self.assertContains(draft_response, "Project without imported budget")
        self.assertContains(draft_response, "import_budget_items")
        self.assertContains(payment_response, "Project without imported budget")
        self.assertContains(payment_response, "Project without budget")
        self.assertContains(payment_response, "import_budget_items")

    def test_payment_detail_shows_ofx_reconciliation(self):
        self.client.force_login(self.user)
        self.transaction.status = OfxTransaction.Status.RECONCILED
        self.transaction.document_extracted = "12345678000199"
        self.transaction.save()
        Reconciliation.objects.create(
            payment=self.payment,
            transaction=self.transaction,
            status=Reconciliation.Status.CONFIRMED,
            confidence=Decimal("1.00"),
            notes="Amount e date conferem.",
        )

        response = self.client.get(reverse("internal_payment_detail", args=[self.payment.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "OFX reconciled")
        self.assertContains(response, "Confirmed")
        self.assertContains(response, "PIX ENVIADO ACME MATERIAIS")
        self.assertContains(response, "12345678000199")

    def test_payment_actions_require_post(self):
        self.client.force_login(self.user)
        url = reverse("internal_payment_action", args=[self.payment.pk, "approve"])

        response = self.client.get(url)

        self.assertEqual(response.status_code, 405)

    def test_payment_actions_require_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        url = reverse("internal_payment_action", args=[self.payment.pk, "approve"])

        response = client.post(url)

        self.assertEqual(response.status_code, 403)

    def test_payment_actions_can_approve_correct_and_cancel_with_normal_client(self):
        self.client.force_login(self.user)

        approve_response = self.client.post(reverse("internal_payment_action", args=[self.payment.pk, "approve"]))
        self.payment.refresh_from_db()
        self.assertEqual(approve_response.status_code, 302)
        self.assertEqual(self.payment.status, Payment.Status.APPROVED)

        correct_response = self.client.post(reverse("internal_payment_action", args=[self.payment.pk, "correct"]))
        self.payment.refresh_from_db()
        self.assertEqual(correct_response.status_code, 302)
        self.assertEqual(self.payment.status, Payment.Status.CORRECTING)

        cancel_response = self.client.post(reverse("internal_payment_action", args=[self.payment.pk, "cancel"]))
        self.payment.refresh_from_db()
        self.assertEqual(cancel_response.status_code, 302)
        self.assertEqual(self.payment.status, Payment.Status.CANCELED)

    def test_payment_action_redirects_to_next_filtered_list(self):
        self.client.force_login(self.user)
        next_url = (
            f"{reverse('internal_pending_payments')}"
            "?date_inicio=2026-06-01&date_fim=2026-06-30&status=all"
        )

        response = self.client.post(
            reverse("internal_payment_action", args=[self.payment.pk, "approve"]),
            {"next": next_url},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], next_url)

    def test_bulk_payment_action_requires_post(self):
        self.client.force_login(self.user)
        url = reverse("internal_payment_bulk_action")

        response = self.client.get(url)

        self.assertEqual(response.status_code, 405)

    def test_bulk_payment_action_requires_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        url = reverse("internal_payment_bulk_action")

        response = client.post(url, {"action": "approve", "payment_ids": [str(self.payment.pk)]})

        self.assertEqual(response.status_code, 403)

    def test_bulk_payment_action_approves_selected_pending_confirmation_payments(self):
        self.client.force_login(self.user)
        payment_a = self.make_export_ready_payment(
            status=Payment.Status.PENDING_CONFIRMATION,
            amount=Decimal("120.00"),
        )
        payment_b = self.make_export_ready_payment(
            status=Payment.Status.PENDING_CONFIRMATION,
            amount=Decimal("180.00"),
        )

        response = self.client.post(
            reverse("internal_payment_bulk_action"),
            {
                "action": "approve",
                "payment_ids": [str(payment_a.pk), str(payment_b.pk)],
                "next": reverse("internal_pending_payments"),
            },
        )

        payment_a.refresh_from_db()
        payment_b.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("internal_pending_payments"))
        self.assertEqual(payment_a.status, Payment.Status.APPROVED)
        self.assertEqual(payment_b.status, Payment.Status.APPROVED)
        self.assertEqual(
            PaymentConfirmation.objects.filter(
                payment_id__in=[payment_a.pk, payment_b.pk],
                action=Payment.ConfirmationAction.APPROVE,
                user=self.user,
            ).count(),
            2,
        )

    def test_bulk_payment_action_skips_non_pending_confirmation_payments(self):
        self.client.force_login(self.user)
        pending = self.make_export_ready_payment(
            status=Payment.Status.PENDING_CONFIRMATION,
            amount=Decimal("120.00"),
        )
        approved = self.make_export_ready_payment(
            status=Payment.Status.APPROVED,
            amount=Decimal("180.00"),
        )
        canceled = self.make_export_ready_payment(
            status=Payment.Status.CANCELED,
            amount=Decimal("220.00"),
        )

        response = self.client.post(
            reverse("internal_payment_bulk_action"),
            {
                "action": "approve",
                "payment_ids": [str(pending.pk), str(approved.pk), str(canceled.pk)],
            },
        )

        pending.refresh_from_db()
        approved.refresh_from_db()
        canceled.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(pending.status, Payment.Status.APPROVED)
        self.assertEqual(approved.status, Payment.Status.APPROVED)
        self.assertEqual(canceled.status, Payment.Status.CANCELED)
        self.assertEqual(
            PaymentConfirmation.objects.filter(
                payment_id__in=[pending.pk, approved.pk, canceled.pk],
                action=Payment.ConfirmationAction.APPROVE,
            ).count(),
            1,
        )

    def test_payments_list_shows_bulk_approval_controls(self):
        self.client.force_login(self.user)
        payment = self.make_export_ready_payment(
            status=Payment.Status.PENDING_CONFIRMATION,
        )

        response = self.client.get(
            reverse("internal_pending_payments"),
            {"date_inicio": "2026-06-01", "date_fim": "2026-06-30"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["bulk_approvable_count"], 1)
        self.assertContains(response, "Approve selected")
        self.assertContains(response, f'name="payment_ids" value="{payment.pk}"')
        self.assertContains(response, "R$ 100,00")

    def test_payments_list_defaults_to_active_statuses(self):
        self.client.force_login(self.user)
        approved_counterparty = Counterparty.objects.create(
            name="Vendor Approved Visível",
            normalized_name="vendor approved visivel",
        )
        historical_counterparty = Counterparty.objects.create(
            name="Vendor Histórico Visível",
            normalized_name="vendor historico visivel",
        )
        approved = Payment.objects.create(
            payment_date=date(2026, 6, 24),
            amount=Decimal("125.00"),
            counterparty=approved_counterparty,
            status=Payment.Status.APPROVED,
            source=Origin.MANUAL,
        )
        historical = Payment.objects.create(
            payment_date=date(2026, 6, 24),
            amount=Decimal("220.00"),
            counterparty=historical_counterparty,
            status=Payment.Status.POSTED,
            source=Origin.HISTORICAL,
        )
        canceled = Payment.objects.create(
            payment_date=date(2026, 6, 24),
            amount=Decimal("75.00"),
            counterparty=Counterparty.objects.create(
                name="Vendor Canceled Oculto",
                normalized_name="vendor canceled oculto",
            ),
            status=Payment.Status.CANCELED,
            source=Origin.MANUAL,
        )

        response = self.client.get(
            reverse("internal_pending_payments"),
            {"date_inicio": "2026-06-01", "date_fim": "2026-06-30"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_status"], "all")
        self.assertIn(self.payment, response.context["payments"])
        self.assertIn(approved, response.context["payments"])
        self.assertIn(historical, response.context["payments"])
        self.assertNotIn(canceled, response.context["payments"])
        self.assertContains(response, "Vendor Approved Visível")
        self.assertContains(response, "Vendor Histórico Visível")
        self.assertNotContains(response, "R$ 75,00")

    def test_payments_list_hides_canceled_payments_even_with_explicit_filter(self):
        self.client.force_login(self.user)
        canceled = Payment.objects.create(
            payment_date=date(2026, 6, 24),
            amount=Decimal("75.00"),
            counterparty=Counterparty.objects.create(
                name="Vendor Canceled Visível",
                normalized_name="vendor canceled visivel",
            ),
            status=Payment.Status.CANCELED,
            source=Origin.MANUAL,
        )

        response = self.client.get(reverse("internal_pending_payments"), {"status": Payment.Status.CANCELED})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_status"], "all")
        self.assertNotIn(canceled, response.context["payments"])
        self.assertNotContains(response, "Vendor Canceled Visível")

    def test_payments_list_defaults_to_current_month_period(self):
        self.client.force_login(self.user)
        july_payment = self.make_payment_for_list(
            payment_date=date(2026, 7, 1),
            status=Payment.Status.APPROVED,
        )

        with patch("apps.core.views.timezone.localdate", return_value=date(2026, 6, 24)):
            response = self.client.get(reverse("internal_pending_payments"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["date_start"], date(2026, 6, 1))
        self.assertEqual(response.context["date_end"], date(2026, 6, 30))
        self.assertIn(self.payment, response.context["payments"])
        self.assertNotIn(july_payment, response.context["payments"])

    def test_payments_list_filters_by_period_status_category_counterparty_cost_center_and_work(self):
        self.client.force_login(self.user)
        category = Category.objects.create(name="Services", normalized_name="servicos")
        other_category = Category.objects.create(name="Materiais", normalized_name="materiais")
        cost_center = CostCenter.objects.create(name="Company", normalized_name="empresa")
        other_cost_center = CostCenter.objects.create(name="Project", normalized_name="project")
        work = Work.objects.create(name="Sertãozinho", normalized_name="sertaozinho")
        other_work = Work.objects.create(name="Jaurez Távora", normalized_name="jaurez tavora")
        counterparty = Counterparty.objects.create(name="Filtro Certo", normalized_name="filtro certo")
        other_counterparty = Counterparty.objects.create(name="Filtro Errado", normalized_name="filtro errado")
        target = self.make_payment_for_list(
            counterparty=counterparty,
            category=category,
            cost_center=cost_center,
            work=work,
            payment_date=date(2026, 6, 15),
            status=Payment.Status.PENDING_CONFIRMATION,
        )
        self.make_payment_for_list(
            counterparty=other_counterparty,
            category=category,
            cost_center=cost_center,
            work=work,
            payment_date=date(2026, 6, 15),
            status=Payment.Status.PENDING_CONFIRMATION,
        )
        self.make_payment_for_list(
            counterparty=counterparty,
            category=other_category,
            cost_center=cost_center,
            work=work,
            payment_date=date(2026, 6, 15),
            status=Payment.Status.PENDING_CONFIRMATION,
        )
        self.make_payment_for_list(
            counterparty=counterparty,
            category=category,
            cost_center=other_cost_center,
            work=work,
            payment_date=date(2026, 6, 15),
            status=Payment.Status.PENDING_CONFIRMATION,
        )
        self.make_payment_for_list(
            counterparty=counterparty,
            category=category,
            cost_center=cost_center,
            work=other_work,
            payment_date=date(2026, 6, 15),
            status=Payment.Status.PENDING_CONFIRMATION,
        )
        self.make_payment_for_list(
            counterparty=counterparty,
            category=category,
            cost_center=cost_center,
            work=work,
            payment_date=date(2026, 7, 1),
            status=Payment.Status.PENDING_CONFIRMATION,
        )
        self.make_payment_for_list(
            counterparty=counterparty,
            category=category,
            cost_center=cost_center,
            work=work,
            payment_date=date(2026, 6, 15),
            status=Payment.Status.APPROVED,
        )

        response = self.client.get(
            reverse("internal_pending_payments"),
            {
                "date_inicio": "2026-06-01",
                "date_fim": "2026-06-30",
                "status": Payment.Status.PENDING_CONFIRMATION,
                "counterparty": str(counterparty.pk),
                "category": str(category.pk),
                "cost_center": str(cost_center.pk),
                "work": str(work.pk),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context["payments"]), [target])
        self.assertContains(response, "Filtro Certo")

    def test_payments_list_action_links_preserve_active_filters(self):
        self.client.force_login(self.user)
        response = self.client.get(
            reverse("internal_pending_payments"),
            {
                "date_inicio": "2026-06-01",
                "date_fim": "2026-06-30",
                "status": "all",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["current_url"],
            f"{reverse('internal_pending_payments')}?date_inicio=2026-06-01&date_fim=2026-06-30&status=all",
        )
        self.assertContains(response, "next=/interno/payments/%3Fdate_inicio%3D2026-06-01")
        self.assertContains(response, 'name="next" value="/interno/payments/?date_inicio=2026-06-01')

    def test_payments_list_filters_by_document_and_reconciled_ofx(self):
        self.client.force_login(self.user)
        documented = Counterparty.objects.create(
            name="Vendor Documentado",
            normalized_name="vendor documentado",
            primary_document="99887766000155",
        )
        undocumented = Counterparty.objects.create(name="Vendor Sem Documento", normalized_name="vendor sem documento")
        with_ofx = self.make_payment_for_list(counterparty=documented, status=Payment.Status.RECONCILED)
        without_ofx = self.make_payment_for_list(counterparty=undocumented, status=Payment.Status.APPROVED)
        Reconciliation.objects.create(
            payment=with_ofx,
            transaction=self.transaction,
            status=Reconciliation.Status.CONFIRMED,
        )

        with_response = self.client.get(
            reverse("internal_pending_payments"),
            {
                "status": "all",
                "documento": "com",
                "ofx": "com",
                "date_inicio": "2026-06-01",
                "date_fim": "2026-06-30",
            },
        )
        without_response = self.client.get(
            reverse("internal_pending_payments"),
            {
                "status": "all",
                "documento": "sem",
                "ofx": "sem",
                "date_inicio": "2026-06-01",
                "date_fim": "2026-06-30",
            },
        )

        self.assertContains(with_response, "Vendor Documentado")
        self.assertContains(without_response, "Vendor Sem Documento")
        self.assertIn(with_ofx, with_response.context["payments"])
        self.assertNotIn(without_ofx, with_response.context["payments"])
        self.assertIn(without_ofx, without_response.context["payments"])
        self.assertNotIn(with_ofx, without_response.context["payments"])

    def test_payments_list_shows_indicators_for_incomplete_payments(self):
        self.client.force_login(self.user)
        undocumented = Counterparty.objects.create(name="Sem Documento", normalized_name="sem documento")
        work = Work.objects.create(name="Tacima", normalized_name="tacima")
        Payment.objects.create(
            amount=Decimal("75.00"),
            status=Payment.Status.PENDING_REGISTRATION,
            source="telegram",
        )
        Payment.objects.create(
            payment_date=date(2026, 6, 24),
            amount=Decimal("80.00"),
            counterparty=undocumented,
            status=Payment.Status.APPROVED,
            source="telegram",
        )
        Payment.objects.create(
            payment_date=date(2026, 6, 24),
            amount=Decimal("90.00"),
            counterparty=self.counterparty,
            work=work,
            status=Payment.Status.RECEIVED,
            source="telegram",
        )

        undated_response = self.client.get(
            reverse("internal_pending_payments"),
            {"status": "all", "date_status": "sem_date"},
        )
        dated_response = self.client.get(
            reverse("internal_pending_payments"),
            {"status": "all", "date_inicio": "2026-06-01", "date_fim": "2026-06-30"},
        )

        self.assertEqual(undated_response.status_code, 200)
        self.assertContains(undated_response, "Without date")
        self.assertContains(undated_response, "No category")
        self.assertContains(undated_response, "No cost center")
        self.assertContains(undated_response, "No counterparty")
        self.assertContains(undated_response, "Pending registration")
        self.assertContains(undated_response, "OFX pending")
        self.assertEqual(dated_response.status_code, 200)
        self.assertContains(dated_response, "Without CPF/CNPJ")
        self.assertContains(dated_response, "Project without budget")

    def test_download_only_allows_generated_spreadsheets(self):
        self.client.force_login(self.user)
        generated = self.generated_batch()
        pending = ExportBatch.objects.create(status=ExportBatch.Status.PENDING, records_count=1)
        pending.file.save("pending.xlsx", ContentFile(b"conteudo"), save=True)

        generated_response = self.client.get(reverse("internal_export_download", args=[generated.pk]))
        pending_response = self.client.get(reverse("internal_export_download", args=[pending.pk]))

        self.assertEqual(generated_response.status_code, 200)
        self.assertEqual(pending_response.status_code, 404)

    def test_generate_spreadsheets_action_requires_post_and_creates_batch(self):
        self.client.force_login(self.user)
        self.payment.status = Payment.Status.APPROVED
        self.payment.category = Category.objects.create(name="Materiais", normalized_name="materiais")
        self.payment.cost_center = CostCenter.objects.create(name="Company", normalized_name="empresa")
        self.payment.competence_date = date(2026, 6, 24)
        self.payment.due_date = date(2026, 6, 24)
        self.payment.save()

        response = self.client.post(reverse("internal_export_batches"))
        batch = ExportBatch.objects.get()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(batch.records_count, 1)
        self.assertTrue(batch.accounting_file.name.endswith(".xlsx"))
        self.assertTrue(batch.import_file.name.endswith(".xlsx"))

    def test_export_batches_page_lists_generated_batches(self):
        self.client.force_login(self.user)
        self.generated_batch(
            records_count=3,
            generated_by=self.user,
            period_start=date(2026, 6, 1),
            period_end=date(2026, 6, 30),
        )

        response = self.client.get(reverse("internal_export_batches"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "01/06/2026 to 30/06/2026")
        self.assertContains(response, "3")
        self.assertContains(response, "Generated")
        self.assertContains(response, "jair")

    def test_export_batches_page_shows_download_buttons_when_files_exist(self):
        self.client.force_login(self.user)
        self.generated_batch()

        response = self.client.get(reverse("internal_export_batches"))

        self.assertContains(response, "Export contador")
        self.assertContains(response, "Import sistema")

    def test_export_batches_page_shows_error_batches(self):
        self.client.force_login(self.user)
        ExportBatch.objects.create(
            status=ExportBatch.Status.ERROR,
            records_count=0,
            generated_by=self.user,
            period_start=date(2026, 6, 1),
            period_end=date(2026, 6, 30),
            error_message="Modelo de spreadsheet inválido.",
        )

        response = self.client.get(reverse("internal_export_batches"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Error")
        self.assertContains(response, "Modelo de spreadsheet inválido.")

    def test_download_missing_export_file_returns_404(self):
        self.client.force_login(self.user)
        batch = ExportBatch.objects.create(status=ExportBatch.Status.GENERATED, records_count=1)

        response = self.client.get(reverse("internal_export_download_kind", args=[batch.pk, "exportacao"]))

        self.assertEqual(response.status_code, 404)

    def test_ofx_period_filter_limits_transactions_by_month_and_year(self):
        self.client.force_login(self.user)
        self.transaction.memo = "OFX JUNHO"
        self.transaction.save(update_fields=["memo"])
        self.make_ofx_transaction(
            fitid="FIT-JULY",
            status=OfxTransaction.Status.PENDING,
            posted_at=date(2026, 7, 5),
            memo="OFX JULHO",
        )

        response = self.client.get(
            reverse("internal_unreconciled_ofx"),
            {"mes": "6", "ano": "2026", "status": "all"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "OFX JUNHO")
        self.assertNotContains(response, "OFX JULHO")

    def test_ofx_status_filter_limits_transactions_by_status(self):
        self.client.force_login(self.user)
        self.transaction.memo = "OFX PENDENTE"
        self.transaction.save(update_fields=["memo"])
        self.make_ofx_transaction(
            fitid="FIT-DIV",
            status=OfxTransaction.Status.DIVERGENT,
            memo="OFX DIVERGENTE",
        )

        response = self.client.get(
            reverse("internal_unreconciled_ofx"),
            {"mes": "6", "ano": "2026", "status": OfxTransaction.Status.DIVERGENT},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "OFX DIVERGENTE")
        self.assertNotContains(response, "OFX PENDENTE")

    def test_ofx_reconciled_transaction_shows_related_payment(self):
        self.client.force_login(self.user)
        payment = self.make_export_ready_payment(status=Payment.Status.RECONCILED)
        transaction = self.make_ofx_transaction(
            fitid="FIT-REC",
            status=OfxTransaction.Status.RECONCILED,
            memo="OFX CONCILIADO",
            counterparty=self.counterparty,
        )
        Reconciliation.objects.create(
            payment=payment,
            transaction=transaction,
            status=Reconciliation.Status.CONFIRMED,
        )

        response = self.client.get(
            reverse("internal_unreconciled_ofx"),
            {"mes": "6", "ano": "2026", "status": OfxTransaction.Status.RECONCILED},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "OFX CONCILIADO")
        self.assertContains(response, f"#{payment.pk}")
        self.assertContains(response, reverse("internal_payment_detail", args=[payment.pk]))

    def test_ofx_transaction_with_suggested_payment_shows_review_links(self):
        self.client.force_login(self.user)
        category = Category.objects.create(name="Materiais", normalized_name="materiais")
        cost_center = CostCenter.objects.create(name="Company", normalized_name="empresa")
        payment = Payment.objects.create(
            payment_date=date(2026, 6, 24),
            amount=Decimal("250.00"),
            counterparty=self.counterparty,
            category=category,
            cost_center=cost_center,
            status=Payment.Status.PENDING_CONFIRMATION,
            source=Origin.OFX,
            raw_payload={"ofx_transaction_id": self.transaction.pk},
        )

        response = self.client.get(
            reverse("internal_unreconciled_ofx"),
            {"mes": "6", "ano": "2026", "status": "all", "payment": "com_sugestao"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f"#{payment.pk}")
        self.assertContains(response, reverse("internal_payment_detail", args=[payment.pk]))
        self.assertNotContains(response, reverse("internal_payment_update", args=[payment.pk]))
        self.assertContains(response, "Review")
        self.assertContains(response, "Pending")
        self.assertContains(response, "actions-menu")
        self.assertContains(response, "ACME Materiais")
        self.assertContains(response, "Materiais")
        self.assertContains(response, "Company")

    def test_ofx_review_renders_one_row_per_ofx_transaction_and_keeps_alternatives_in_details(self):
        self.client.force_login(self.user)
        category = Category.objects.create(name="Materiais", normalized_name="materiais")
        cost_center = CostCenter.objects.create(name="Project", normalized_name="project")
        work = Work.objects.create(name="Sertãozinho", normalized_name="sertaozinho")
        first = Payment.objects.create(
            payment_date=date(2026, 6, 24),
            amount=Decimal("700.00"),
            counterparty=self.counterparty,
            category=category,
            cost_center=cost_center,
            work=work,
            status=Payment.Status.POSTED,
            source=Origin.OFX,
            raw_payload={"ofx_transaction_id": self.transaction.pk},
        )
        second = Payment.objects.create(
            payment_date=date(2026, 6, 24),
            amount=Decimal("700.00"),
            counterparty=self.counterparty,
            category=category,
            cost_center=cost_center,
            work=work,
            status=Payment.Status.POSTED,
            source=Origin.OFX,
            raw_payload={"ofx_transaction_id": self.transaction.pk},
        )
        Reconciliation.objects.create(
            payment=first,
            transaction=self.transaction,
            status=Reconciliation.Status.SUGGESTED,
        )
        Reconciliation.objects.create(
            payment=second,
            transaction=self.transaction,
            status=Reconciliation.Status.SUGGESTED,
        )

        response = self.client.get(
            reverse("internal_unreconciled_ofx"),
            {"mes": "6", "ano": "2026", "status": "all", "payment": "com_sugestao"},
        )

        html = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "OFX details")
        self.assertContains(response, "Other suggestions")
        self.assertEqual(html.count('class="ofx-review-row"'), 1)
        self.assertIn(f'data-payment-id="{first.pk}"', html)
        self.assertIn(f'data-alternative-payment-id="{second.pk}"', html)

    def test_ofx_pending_registration_shows_quick_counterparty_buttons(self):
        self.client.force_login(self.user)
        self.transaction.name_extracted = "Vendor Novo Ltda"
        self.transaction.document_extracted = "11222333000144"
        self.transaction.save(update_fields=["name_extracted", "document_extracted"])
        payment = Payment.objects.create(
            payment_date=date(2026, 6, 24),
            amount=Decimal("250.00"),
            status=Payment.Status.PENDING_REGISTRATION,
            source=Origin.OFX,
            raw_payload={"ofx_transaction_id": self.transaction.pk},
        )

        response = self.client.get(
            reverse("internal_unreconciled_ofx"),
            {"mes": "6", "ano": "2026", "status": "all", "payment": Payment.Status.PENDING_REGISTRATION},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Vendor")
        self.assertContains(response, "Worker")
        self.assertContains(response, reverse("internal_supplier_quick_create"))
        self.assertContains(response, "Vendor+Novo+Ltda")
        self.assertContains(response, f"payment={payment.pk}")

    def test_ofx_quick_supplier_create_links_pending_registration_payment(self):
        self.client.force_login(self.user)
        category = Category.objects.create(name="Materiais", normalized_name="materiais")
        cost_center = CostCenter.objects.create(name="Company", normalized_name="empresa")
        self.transaction.name_extracted = "Vendor Novo Ltda"
        self.transaction.document_extracted = "11222333000144"
        self.transaction.save(update_fields=["name_extracted", "document_extracted"])
        payment = Payment.objects.create(
            competence_date=date(2026, 6, 24),
            due_date=date(2026, 6, 24),
            payment_date=date(2026, 6, 24),
            amount=Decimal("250.00"),
            cost_center=cost_center,
            payment_method="PIX",
            status=Payment.Status.PENDING_REGISTRATION,
            source=Origin.OFX,
            needs_review=True,
            raw_payload={
                "ofx_transaction_id": self.transaction.pk,
                "counterparty_candidate": {
                    "name": "Vendor Novo Ltda",
                    "document": "11222333000144",
                    "source": Origin.OFX,
                },
            },
        )

        response = self.client.post(
            reverse("internal_supplier_quick_create"),
            {
                "next": reverse("internal_unreconciled_ofx"),
                "payment": payment.pk,
                "name": "Vendor Novo Ltda",
                "primary_document": "11.222.333/0001-44",
                "default_category": category.pk,
            },
            follow=True,
        )

        payment.refresh_from_db()
        counterparty = Counterparty.objects.get(primary_document="11222333000144")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(counterparty.kind, Counterparty.Kind.SUPPLIER)
        self.assertEqual(payment.counterparty, counterparty)
        self.assertEqual(payment.status, Payment.Status.PENDING_CONFIRMATION)
        self.assertEqual(payment.category, category)
        self.assertEqual(payment.cost_center, cost_center)
        self.assertTrue(payment.needs_review)
        self.assertIsNone(payment.confirmed_at)
        self.assertIn("resolved_counterparty_candidate", payment.raw_payload)
        self.assertContains(response, f"Vendor {counterparty.name} linked to payment #{payment.pk}.")

    def test_ofx_new_supplier_flow_can_be_completed_approved_and_exportable(self):
        self.client.force_login(self.user)
        category = Category.objects.create(name="Materiais", normalized_name="materiais")
        cost_center = CostCenter.objects.create(name="Company", normalized_name="empresa")
        self.transaction.name_extracted = "Vendor Exportavel Ltda"
        self.transaction.document_extracted = "22333444000155"
        self.transaction.save(update_fields=["name_extracted", "document_extracted"])
        payment = Payment.objects.create(
            competence_date=date(2026, 6, 24),
            due_date=date(2026, 6, 24),
            payment_date=date(2026, 6, 24),
            amount=Decimal("380.00"),
            cost_center=cost_center,
            payment_method="PIX",
            status=Payment.Status.PENDING_REGISTRATION,
            source=Origin.OFX,
            needs_review=True,
            raw_payload={"ofx_transaction_id": self.transaction.pk},
        )

        self.client.post(
            reverse("internal_supplier_quick_create"),
            {
                "next": reverse("internal_unreconciled_ofx"),
                "payment": payment.pk,
                "name": "Vendor Exportavel Ltda",
                "primary_document": "22.333.444/0001-55",
                "default_category": category.pk,
            },
        )
        self.client.post(
            reverse("internal_ofx_payment_bulk_edit"),
            {
                "payment_ids": [payment.pk],
                "payer": "Main Account",
                "bank_account": "Sicredi",
                "next": reverse("internal_unreconciled_ofx"),
            },
        )

        approve_response = self.client.post(reverse("internal_payment_action", args=[payment.pk, "approve"]))

        payment.refresh_from_db()
        self.assertEqual(approve_response.status_code, 302)
        self.assertEqual(payment.status, Payment.Status.APPROVED)
        self.assertEqual(payment.counterparty.primary_document, "22333444000155")
        self.assertEqual(payment.category, category)
        self.assertEqual(payment.cost_center, cost_center)
        self.assertEqual(payment.payer, "Main Account")
        self.assertEqual(payment.bank_account, "Sicredi")
        self.assertEqual(payment_missing_required_fields(payment), [])
        self.assertEqual(PaymentConfirmation.objects.filter(payment=payment).count(), 1)

    def test_ofx_ignored_credit_only_appears_when_filter_allows(self):
        self.client.force_login(self.user)
        self.transaction.memo = "OFX DEBITO PENDENTE"
        self.transaction.save(update_fields=["memo"])
        self.make_ofx_transaction(
            fitid="FIT-CREDIT",
            status=OfxTransaction.Status.IGNORED,
            amount=Decimal("500.00"),
            memo="OFX CREDITO IGNORADO",
        )

        default_response = self.client.get(
            reverse("internal_unreconciled_ofx"),
            {"mes": "6", "ano": "2026"},
        )
        ignored_response = self.client.get(
            reverse("internal_unreconciled_ofx"),
            {"mes": "6", "ano": "2026", "status": OfxTransaction.Status.IGNORED},
        )

        self.assertNotContains(default_response, "OFX CREDITO IGNORADO")
        self.assertContains(ignored_response, "OFX CREDITO IGNORADO")
        self.assertNotContains(ignored_response, "OFX DEBITO PENDENTE")

    def test_ofx_review_filters_by_payment_counterparty_cost_center_and_work(self):
        self.client.force_login(self.user)
        category = Category.objects.create(name="Materiais", normalized_name="materiais")
        work_center = CostCenter.objects.create(name="Project", normalized_name="project")
        work = Work.objects.create(name="Tacima", normalized_name="tacima")
        matching_payment = Payment.objects.create(
            payment_date=date(2026, 6, 24),
            amount=Decimal("250.00"),
            counterparty=self.counterparty,
            category=category,
            cost_center=work_center,
            work=work,
            status=Payment.Status.PENDING_CONFIRMATION,
            source=Origin.OFX,
            raw_payload={"ofx_transaction_id": self.transaction.pk},
        )
        other_transaction = self.make_ofx_transaction(
            fitid="FIT-OTHER-FILTER",
            status=OfxTransaction.Status.MISSING_PAYMENT,
            memo="OUTRA TRANSACAO",
        )
        Payment.objects.create(
            payment_date=date(2026, 6, 24),
            amount=Decimal("90.00"),
            status=Payment.Status.PENDING_REGISTRATION,
            source=Origin.OFX,
            raw_payload={"ofx_transaction_id": other_transaction.pk},
        )

        response = self.client.get(
            reverse("internal_unreconciled_ofx"),
            {
                "mes": "6",
                "ano": "2026",
                "status": "all",
                "payment": Payment.Status.PENDING_CONFIRMATION,
                "counterparty": self.counterparty.pk,
                "cost_center": work_center.pk,
                "work": work.pk,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f"#{matching_payment.pk}")
        self.assertContains(response, "PIX ENVIADO ACME MATERIAIS")
        self.assertNotContains(response, "OUTRA TRANSACAO")

    def test_ofx_review_truncates_long_memo_and_renders_brazilian_currency(self):
        self.client.force_login(self.user)
        long_name = "Vendor " + ("Muito Longo " * 12)
        long_memo = "MEMO " + ("muito detalhado " * 30)
        self.transaction.name_extracted = long_name
        self.transaction.memo = long_memo
        self.transaction.amount = Decimal("-1234.56")
        self.transaction.save(update_fields=["name_extracted", "memo", "amount"])

        response = self.client.get(
            reverse("internal_unreconciled_ofx"),
            {"mes": "6", "ano": "2026", "status": "all"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "R$ -1.234,56")
        self.assertContains(response, "MEMO muito detalhado")
        self.assertContains(response, "…")
        self.assertNotContains(response, long_memo)

    def test_ofx_review_can_confirm_suggested_reconciliation(self):
        self.client.force_login(self.user)
        payment = self.make_export_ready_payment(status=Payment.Status.APPROVED, amount=Decimal("250.00"))
        transaction = self.make_ofx_transaction(
            fitid="FIT-SUGGESTED-CONFIRM",
            status=OfxTransaction.Status.POSSIBLE_DUPLICATE,
            memo="OFX COM CONCILIACAO SUGERIDA",
            counterparty=self.counterparty,
        )
        reconciliation = Reconciliation.objects.create(
            payment=payment,
            transaction=transaction,
            status=Reconciliation.Status.SUGGESTED,
        )

        response = self.client.post(
            reverse("internal_ofx_action", args=[transaction.pk, "confirm_reconciliation"]),
            {"reconciliation_id": reconciliation.pk, "next": reverse("internal_unreconciled_ofx")},
            follow=True,
        )

        payment.refresh_from_db()
        transaction.refresh_from_db()
        reconciliation.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(reconciliation.status, Reconciliation.Status.CONFIRMED)
        self.assertEqual(transaction.status, OfxTransaction.Status.RECONCILED)
        self.assertEqual(payment.status, Payment.Status.RECONCILED)

    def test_ofx_bulk_edit_get_does_not_change_date(self):
        self.client.force_login(self.user)
        category = Category.objects.create(name="Materiais", normalized_name="materiais")
        payment = Payment.objects.create(
            payment_date=date(2026, 6, 24),
            amount=Decimal("250.00"),
            counterparty=self.counterparty,
            status=Payment.Status.PENDING_CONFIRMATION,
            source=Origin.OFX,
            raw_payload={"ofx_transaction_id": self.transaction.pk},
        )

        response = self.client.get(reverse("internal_ofx_payment_bulk_edit"), {"category": category.pk})

        payment.refresh_from_db()
        self.assertEqual(response.status_code, 405)
        self.assertIsNone(payment.category)

    def test_ofx_bulk_edit_updates_selected_suggestions_with_csrf(self):
        category = Category.objects.create(name="Materiais", normalized_name="materiais")
        cost_center = CostCenter.objects.create(name="Company", normalized_name="empresa")
        selected = Payment.objects.create(
            payment_date=date(2026, 6, 24),
            amount=Decimal("250.00"),
            counterparty=self.counterparty,
            status=Payment.Status.PENDING_CONFIRMATION,
            source=Origin.OFX,
            raw_payload={"ofx_transaction_id": self.transaction.pk},
        )
        unselected = Payment.objects.create(
            payment_date=date(2026, 6, 24),
            amount=Decimal("90.00"),
            counterparty=self.counterparty,
            status=Payment.Status.PENDING_CONFIRMATION,
            source=Origin.OFX,
            raw_payload={"ofx_transaction_id": self.transaction.pk},
        )
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)
        csrf_client.get(reverse("internal_unreconciled_ofx"), {"mes": "6", "ano": "2026", "status": "all"})
        csrf_token = csrf_client.cookies["csrftoken"].value

        response = csrf_client.post(
            reverse("internal_ofx_payment_bulk_edit"),
            {
                "payment_ids": [selected.pk],
                "category": category.pk,
                "cost_center": cost_center.pk,
                "payment_method": "PIX",
                "payer": "Main Account",
                "bank_account": "Sicredi",
                "payment_status": Payment.Status.CORRECTING,
                "next": reverse("internal_unreconciled_ofx"),
            },
            HTTP_X_CSRFTOKEN=csrf_token,
            follow=True,
        )

        selected.refresh_from_db()
        unselected.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(selected.category, category)
        self.assertEqual(selected.cost_center, cost_center)
        self.assertEqual(selected.payment_method, "PIX")
        self.assertEqual(selected.payer, "Main Account")
        self.assertEqual(selected.bank_account, "Sicredi")
        self.assertEqual(selected.status, Payment.Status.CORRECTING)
        self.assertEqual(selected.raw_payload["bulk_edits"][-1]["type"], "ofx_review_bulk_edit")
        self.assertIsNone(unselected.category)
        self.assertEqual(unselected.status, Payment.Status.PENDING_CONFIRMATION)

    def test_ofx_bulk_edit_ignores_blocked_payments(self):
        self.client.force_login(self.user)
        category = Category.objects.create(name="Materiais", normalized_name="materiais")
        editable = Payment.objects.create(
            payment_date=date(2026, 6, 24),
            amount=Decimal("250.00"),
            counterparty=self.counterparty,
            status=Payment.Status.PENDING_CONFIRMATION,
            source=Origin.OFX,
            raw_payload={"ofx_transaction_id": self.transaction.pk},
        )
        canceled = Payment.objects.create(
            payment_date=date(2026, 6, 24),
            amount=Decimal("100.00"),
            counterparty=self.counterparty,
            status=Payment.Status.CANCELED,
            source=Origin.OFX,
            raw_payload={"ofx_transaction_id": self.transaction.pk},
        )
        reconciled = Payment.objects.create(
            payment_date=date(2026, 6, 24),
            amount=Decimal("110.00"),
            counterparty=self.counterparty,
            status=Payment.Status.RECONCILED,
            source=Origin.OFX,
            raw_payload={"ofx_transaction_id": self.transaction.pk},
        )
        exported = Payment.objects.create(
            payment_date=date(2026, 6, 24),
            amount=Decimal("120.00"),
            counterparty=self.counterparty,
            status=Payment.Status.PENDING_CONFIRMATION,
            source=Origin.OFX,
            raw_payload={"ofx_transaction_id": self.transaction.pk},
        )
        batch = ExportBatch.objects.create(status=ExportBatch.Status.GENERATED)
        batch.payments.add(exported)

        response = self.client.post(
            reverse("internal_ofx_payment_bulk_edit"),
            {
                "payment_ids": [editable.pk, canceled.pk, reconciled.pk, exported.pk],
                "category": category.pk,
                "next": reverse("internal_unreconciled_ofx"),
            },
            follow=True,
        )

        editable.refresh_from_db()
        canceled.refresh_from_db()
        reconciled.refresh_from_db()
        exported.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(editable.category, category)
        self.assertIsNone(canceled.category)
        self.assertIsNone(reconciled.category)
        self.assertIsNone(exported.category)

    def test_ofx_bulk_edit_work_sets_default_work_cost_center(self):
        self.client.force_login(self.user)
        work_center = CostCenter.objects.create(name="Project", normalized_name="project")
        work = Work.objects.create(name="Tacima", normalized_name="tacima")
        payment = Payment.objects.create(
            payment_date=date(2026, 6, 24),
            amount=Decimal("250.00"),
            counterparty=self.counterparty,
            status=Payment.Status.PENDING_CONFIRMATION,
            source=Origin.OFX,
            raw_payload={"ofx_transaction_id": self.transaction.pk},
        )

        response = self.client.post(
            reverse("internal_ofx_payment_bulk_edit"),
            {
                "payment_ids": [payment.pk],
                "work": work.pk,
                "next": reverse("internal_unreconciled_ofx"),
            },
            follow=True,
        )

        payment.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payment.work, work)
        self.assertEqual(payment.cost_center, work_center)

    def test_ofx_bulk_edit_does_not_approve_payments(self):
        self.client.force_login(self.user)
        payment = Payment.objects.create(
            payment_date=date(2026, 6, 24),
            amount=Decimal("250.00"),
            status=Payment.Status.PENDING_REGISTRATION,
            source=Origin.OFX,
            raw_payload={"ofx_transaction_id": self.transaction.pk},
        )

        response = self.client.post(
            reverse("internal_ofx_payment_bulk_edit"),
            {
                "payment_ids": [payment.pk],
                "payment_method": "PIX",
                "payment_status": Payment.Status.PENDING_CONFIRMATION,
                "next": reverse("internal_unreconciled_ofx"),
            },
            follow=True,
        )

        payment.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payment.payment_method, "PIX")
        self.assertEqual(payment.status, Payment.Status.PENDING_REGISTRATION)
        self.assertNotEqual(payment.status, Payment.Status.APPROVED)

    def test_ofx_bulk_approval_approves_pending_confirmation_payment(self):
        self.client.force_login(self.user)
        payment = self.make_export_ready_payment(status=Payment.Status.PENDING_CONFIRMATION, amount=Decimal("250.00"))
        payment.source = Origin.OFX
        payment.raw_payload = {"ofx_transaction_id": self.transaction.pk}
        payment.save(update_fields=["source", "raw_payload"])

        response = self.client.post(
            reverse("internal_payment_bulk_action"),
            {
                "action": "approve",
                "payment_ids": [payment.pk],
                "next": reverse("internal_unreconciled_ofx"),
            },
            follow=True,
        )

        payment.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payment.status, Payment.Status.APPROVED)
        self.assertEqual(PaymentConfirmation.objects.filter(payment=payment).count(), 1)
        self.assertContains(response, "1 payment(s) approved in bulk.")

    def test_ofx_bulk_approval_skips_pending_registration_and_missing_fields(self):
        self.client.force_login(self.user)
        ready = self.make_export_ready_payment(status=Payment.Status.PENDING_CONFIRMATION, amount=Decimal("250.00"))
        ready.source = Origin.OFX
        ready.raw_payload = {"ofx_transaction_id": self.transaction.pk}
        ready.save(update_fields=["source", "raw_payload"])
        pending_registration = Payment.objects.create(
            payment_date=date(2026, 6, 24),
            amount=Decimal("100.00"),
            status=Payment.Status.PENDING_REGISTRATION,
            source=Origin.OFX,
            raw_payload={"ofx_transaction_id": self.transaction.pk},
        )
        missing_fields = Payment.objects.create(
            payment_date=date(2026, 6, 24),
            amount=Decimal("110.00"),
            counterparty=self.counterparty,
            status=Payment.Status.PENDING_CONFIRMATION,
            source=Origin.OFX,
            raw_payload={"ofx_transaction_id": self.transaction.pk},
        )

        response = self.client.post(
            reverse("internal_payment_bulk_action"),
            {
                "action": "approve",
                "payment_ids": [ready.pk, pending_registration.pk, missing_fields.pk],
                "next": reverse("internal_unreconciled_ofx"),
            },
            follow=True,
        )

        ready.refresh_from_db()
        pending_registration.refresh_from_db()
        missing_fields.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ready.status, Payment.Status.APPROVED)
        self.assertEqual(pending_registration.status, Payment.Status.PENDING_REGISTRATION)
        self.assertEqual(missing_fields.status, Payment.Status.PENDING_CONFIRMATION)
        self.assertEqual(PaymentConfirmation.objects.filter(payment=ready).count(), 1)
        self.assertEqual(PaymentConfirmation.objects.filter(payment__in=[pending_registration, missing_fields]).count(), 0)
        self.assertContains(response, "pending registration")
        self.assertContains(response, "missing required fields")

    def test_ofx_bulk_approval_skips_possible_duplicate(self):
        self.client.force_login(self.user)
        duplicate_transaction = self.make_ofx_transaction(
            fitid="FIT-DUP-BULK-APPROVAL",
            status=OfxTransaction.Status.POSSIBLE_DUPLICATE,
            memo="OFX POSSIVEL DUPLICADO",
        )
        payment = self.make_export_ready_payment(status=Payment.Status.PENDING_CONFIRMATION, amount=Decimal("250.00"))
        payment.source = Origin.OFX
        payment.raw_payload = {"ofx_transaction_id": duplicate_transaction.pk}
        payment.save(update_fields=["source", "raw_payload"])

        response = self.client.post(
            reverse("internal_payment_bulk_action"),
            {
                "action": "approve",
                "payment_ids": [payment.pk],
                "next": reverse("internal_unreconciled_ofx"),
            },
            follow=True,
        )

        payment.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payment.status, Payment.Status.PENDING_CONFIRMATION)
        self.assertFalse(PaymentConfirmation.objects.filter(payment=payment).exists())
        self.assertContains(response, "possible OFX duplicate")

    def test_ofx_review_shows_bulk_approval_controls(self):
        self.client.force_login(self.user)
        payment = self.make_export_ready_payment(status=Payment.Status.PENDING_CONFIRMATION, amount=Decimal("250.00"))
        payment.source = Origin.OFX
        payment.raw_payload = {"ofx_transaction_id": self.transaction.pk}
        payment.save(update_fields=["source", "raw_payload"])

        response = self.client.get(
            reverse("internal_unreconciled_ofx"),
            {"mes": "6", "ano": "2026", "status": "all", "payment": "com_sugestao"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Bulk approval")
        self.assertContains(response, "Approve selected")
        self.assertContains(response, "1 approvable OFX-suggested payment(s)")

    def test_ofx_upload_requires_login(self):
        response = self.client.post(
            reverse("internal_unreconciled_ofx"),
            {"ofx_file": self.ofx_upload()},
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])

    def test_ofx_upload_rejects_non_ofx_file(self):
        self.client.force_login(self.user)
        upload = SimpleUploadedFile("extrato.txt", b"conteudo", content_type="text/plain")

        response = self.client.post(reverse("internal_unreconciled_ofx"), {"ofx_file": upload}, follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Upload a valid .ofx file.")
        self.assertEqual(UploadedFile.objects.count(), 0)
        self.assertEqual(OfxFile.objects.count(), 1)

    def test_ofx_upload_creates_ofx_file_and_transaction(self):
        self.client.force_login(self.user)
        payment_count = Payment.objects.count()

        response = self.client.post(
            reverse("internal_unreconciled_ofx"),
            {"ofx_file": self.ofx_upload(fitid="FIT-UPLOAD", memo="PAGAMENTO PIX ACME MATERIAIS")},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(UploadedFile.objects.filter(kind=UploadedFile.Kind.OFX).count(), 1)
        self.assertEqual(OfxFile.objects.filter(uploaded_file__kind=UploadedFile.Kind.OFX).count(), 1)
        self.assertTrue(OfxTransaction.objects.filter(fitid="FIT-UPLOAD").exists())
        self.assertEqual(Payment.objects.count(), payment_count + 1)
        payment = Payment.objects.latest("pk")
        self.assertEqual(payment.source, Origin.OFX)
        self.assertEqual(payment.status, Payment.Status.PENDING_REGISTRATION)
        self.assertNotEqual(payment.status, Payment.Status.APPROVED)
        self.assertContains(response, "OFX imported and reconciled.")
        self.assertContains(response, "Transactions read: 1")
        self.assertContains(response, "Imported expenses: 1")
        self.assertContains(response, "Bank/account: 748 / 12345")
        self.assertContains(response, "Suggested payments: 1")
        self.assertContains(response, "Pending registration: 1")

    def test_ofx_reupload_does_not_duplicate_transactions(self):
        self.client.force_login(self.user)
        content = minimal_ofx(fitid="FIT-REUPLOAD")

        self.client.post(reverse("internal_unreconciled_ofx"), {"ofx_file": self.ofx_upload(content=content)})
        response = self.client.post(
            reverse("internal_unreconciled_ofx"),
            {"ofx_file": self.ofx_upload(content=content)},
            follow=True,
        )

        self.assertEqual(UploadedFile.objects.filter(kind=UploadedFile.Kind.OFX).count(), 1)
        self.assertEqual(OfxFile.objects.filter(uploaded_file__kind=UploadedFile.Kind.OFX).count(), 1)
        self.assertEqual(OfxTransaction.objects.filter(fitid="FIT-REUPLOAD").count(), 1)
        self.assertEqual(Payment.objects.filter(source=Origin.OFX).count(), 1)
        self.assertContains(response, "Existing transactions: 1")
        self.assertContains(response, "Reused suggestions: 1")

    def test_ofx_upload_runs_initial_reconciliation(self):
        self.client.force_login(self.user)
        payment = self.make_export_ready_payment(status=Payment.Status.APPROVED, amount=Decimal("250.00"))
        content = minimal_ofx(
            fitid="FIT-MATCH",
            amount="-250.00",
            memo="PAGAMENTO PIX ACME MATERIAIS",
        )

        self.client.post(
            reverse("internal_unreconciled_ofx"),
            {"ofx_file": self.ofx_upload(content=content)},
        )
        self.client.post(
            reverse("internal_unreconciled_ofx"),
            {"ofx_file": self.ofx_upload(content=content)},
        )

        transaction = OfxTransaction.objects.get(fitid="FIT-MATCH")
        payment.refresh_from_db()
        self.assertEqual(transaction.status, OfxTransaction.Status.RECONCILED)
        self.assertEqual(payment.status, Payment.Status.RECONCILED)
        self.assertEqual(OfxTransaction.objects.filter(fitid="FIT-MATCH").count(), 1)
        self.assertEqual(Reconciliation.objects.filter(payment=payment, transaction=transaction).count(), 1)

    def test_ofx_upload_creates_only_reviewable_payment_suggestion(self):
        self.client.force_login(self.user)
        before = Payment.objects.count()

        self.client.post(
            reverse("internal_unreconciled_ofx"),
            {"ofx_file": self.ofx_upload(fitid="FIT-NO-PAYMENT", memo="PAGAMENTO PIX FORNECEDOR NOVO")},
        )

        self.assertEqual(Payment.objects.count(), before + 1)
        payment = Payment.objects.latest("pk")
        self.assertEqual(payment.source, Origin.OFX)
        self.assertEqual(payment.status, Payment.Status.PENDING_REGISTRATION)
        self.assertTrue(payment.needs_review)
        self.assertIsNone(payment.confirmed_at)

    def test_ofx_clear_period_removes_transactions_files_reconciliations_and_suggestions(self):
        self.client.force_login(self.user)
        uploaded_file = UploadedFile.objects.create(
            original_filename="extrato.ofx",
            kind=UploadedFile.Kind.OFX,
            source=UploadedFile.Source.MANUAL,
            status=UploadedFile.Status.PROCESSED,
        )
        self.ofx_file.uploaded_file = uploaded_file
        self.ofx_file.save(update_fields=["uploaded_file", "updated_at"])
        payment = Payment.objects.create(
            payment_date=date(2026, 6, 24),
            amount=Decimal("250.00"),
            counterparty=self.counterparty,
            status=Payment.Status.PENDING_CONFIRMATION,
            source=Origin.OFX,
            raw_payload={"ofx_transaction_id": self.transaction.pk},
        )
        Reconciliation.objects.create(
            payment=payment,
            transaction=self.transaction,
            status=Reconciliation.Status.SUGGESTED,
        )

        response = self.client.post(
            reverse("internal_ofx_clear_period"),
            {"mes": "6", "ano": "2026", "next": reverse("internal_unreconciled_ofx")},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(OfxTransaction.objects.filter(pk=self.transaction.pk).exists())
        self.assertFalse(OfxFile.objects.filter(pk=self.ofx_file.pk).exists())
        uploaded_file.refresh_from_db()
        self.assertEqual(uploaded_file.status, UploadedFile.Status.IGNORED)
        self.assertFalse(Payment.objects.filter(pk=payment.pk).exists())
        self.assertEqual(Reconciliation.objects.count(), 0)
        self.assertContains(response, "OFX do período zerado")

    def test_ofx_clear_period_restores_manual_reconciled_payment_to_approved(self):
        self.client.force_login(self.user)
        payment = self.make_export_ready_payment(status=Payment.Status.RECONCILED, amount=Decimal("250.00"))
        Reconciliation.objects.create(
            payment=payment,
            transaction=self.transaction,
            status=Reconciliation.Status.CONFIRMED,
        )

        response = self.client.post(
            reverse("internal_ofx_clear_period"),
            {"mes": "6", "ano": "2026", "next": reverse("internal_unreconciled_ofx")},
            follow=True,
        )

        payment.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payment.status, Payment.Status.APPROVED)
        self.assertFalse(OfxTransaction.objects.filter(pk=self.transaction.pk).exists())
        self.assertEqual(Reconciliation.objects.count(), 0)
        self.assertContains(response, "1 reconciled payment(s) returned to approved.")

    def test_ofx_clear_period_aborts_when_ofx_suggestion_was_exported(self):
        self.client.force_login(self.user)
        payment = Payment.objects.create(
            payment_date=date(2026, 6, 24),
            amount=Decimal("250.00"),
            counterparty=self.counterparty,
            status=Payment.Status.APPROVED,
            source=Origin.OFX,
            raw_payload={"ofx_transaction_id": self.transaction.pk},
        )
        batch = ExportBatch.objects.create(status=ExportBatch.Status.GENERATED)
        batch.payments.add(payment)

        response = self.client.post(
            reverse("internal_ofx_clear_period"),
            {"mes": "6", "ano": "2026", "next": reverse("internal_unreconciled_ofx")},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(OfxTransaction.objects.filter(pk=self.transaction.pk).exists())
        self.assertTrue(Payment.objects.filter(pk=payment.pk).exists())
        self.assertContains(response, "I did not clear the entire OFX import")

    def test_monthly_closing_defaults_to_current_month_and_year(self):
        self.client.force_login(self.user)

        with patch("apps.core.views.timezone.localdate", return_value=date(2026, 6, 24)):
            response = self.client.get(reverse("internal_monthly_closing"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_month"], 6)
        self.assertEqual(response.context["selected_year"], 2026)
        self.assertContains(response, "01/06/2026")
        self.assertContains(response, "30/06/2026")

    def test_monthly_closing_month_and_year_filter_changes_period(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("internal_monthly_closing"), {"mes": "5", "ano": "2026"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["summary"].period_start, date(2026, 5, 1))
        self.assertEqual(response.context["summary"].period_end, date(2026, 5, 31))
        self.assertContains(response, "01/05/2026")
        self.assertContains(response, "31/05/2026")

    def test_monthly_closing_summary_displays_expected_totals(self):
        self.client.force_login(self.user)
        category = Category.objects.create(name="Materiais", normalized_name="materiais")
        cost_center = CostCenter.objects.create(name="Company", normalized_name="empresa")
        self.payment.status = Payment.Status.APPROVED
        self.payment.amount = Decimal("100.00")
        self.payment.category = category
        self.payment.cost_center = cost_center
        self.payment.competence_date = date(2026, 6, 24)
        self.payment.due_date = date(2026, 6, 24)
        self.payment.save()
        Payment.objects.create(
            payment_date=date(2026, 6, 20),
            amount=Decimal("50.00"),
            category=category,
            cost_center=cost_center,
            status=Payment.Status.RECONCILED,
            source="telegram",
        )
        Payment.objects.create(
            payment_date=date(2026, 6, 22),
            amount=Decimal("30.00"),
            status=Payment.Status.PENDING_CONFIRMATION,
            source="telegram",
        )
        self.transaction.status = OfxTransaction.Status.DIVERGENT
        self.transaction.save()

        response = self.client.get(reverse("internal_monthly_closing"), {"mes": "6", "ano": "2026"})

        summary = response.context["summary"]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(summary.total_payments, 3)
        self.assertEqual(summary.approved_count, 1)
        self.assertEqual(summary.reconciled_count, 1)
        self.assertEqual(summary.pending_confirmation_count, 1)
        self.assertEqual(summary.approved_amount, Decimal("100.00"))
        self.assertEqual(summary.reconciled_amount, Decimal("50.00"))
        self.assertEqual(summary.ofx_divergent_count, 1)
        self.assertContains(response, "R$ 100")
        self.assertContains(response, "R$ 50")
        self.assertContains(response, "1 payment(s) pending approval")
        self.assertContains(response, "1 divergent OFX transaction(s)")

    def test_monthly_closing_checklist_shows_pending_items_by_type(self):
        self.client.force_login(self.user)
        TelegramDraft.objects.create(
            telegram_user_id=123,
            sender_name="Jair",
            payment_date=date(2026, 6, 24),
        )
        self.payment.status = Payment.Status.PENDING_REGISTRATION
        self.payment.save(update_fields=["status", "updated_at"])
        Payment.objects.create(
            payment_date=date(2026, 6, 20),
            amount=Decimal("30.00"),
            status=Payment.Status.PENDING_CONFIRMATION,
            source="telegram",
        )
        Payment.objects.create(
            payment_date=date(2026, 6, 21),
            amount=Decimal("40.00"),
            status=Payment.Status.CORRECTING,
            source="telegram",
        )
        Payment.objects.create(
            payment_date=date(2026, 6, 22),
            amount=Decimal("50.00"),
            status=Payment.Status.APPROVED,
            source="telegram",
        )
        self.transaction.status = OfxTransaction.Status.DIVERGENT
        self.transaction.save(update_fields=["status", "updated_at"])
        self.make_ofx_transaction("FIT-DUP-CHECK", OfxTransaction.Status.POSSIBLE_DUPLICATE)

        response = self.client.get(reverse("internal_monthly_closing"), {"mes": "6", "ano": "2026"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Operational checklist")
        self.assertContains(response, "Active drafts in period")
        self.assertContains(response, "Payments pending registration")
        self.assertContains(response, "Payments pending approval")
        self.assertContains(response, "Payments under correction")
        self.assertContains(response, "Divergent OFX")
        self.assertContains(response, "OFX possible duplicate")
        self.assertContains(response, "Missing required fields")
        self.assertContains(response, "blocked")
        self.assertFalse(response.context["can_generate_spreadsheets"])

    def test_monthly_closing_checklist_releases_generation_without_blockers(self):
        self.client.force_login(self.user)
        self.transaction.status = OfxTransaction.Status.RECONCILED
        self.transaction.save(update_fields=["status", "updated_at"])
        self.make_export_ready_payment(status=Payment.Status.APPROVED)

        response = self.client.get(reverse("internal_monthly_closing"), {"mes": "6", "ano": "2026"})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["can_generate_spreadsheets"])
        self.assertContains(response, "Generate spreadsheets")
        self.assertNotContains(response, "Resolva as pending items bloqueantes")

    def test_monthly_closing_checklist_shows_resolution_links(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("internal_monthly_closing"), {"mes": "6", "ano": "2026"})

        self.assertContains(response, reverse("internal_telegram_drafts"))
        self.assertContains(response, reverse("internal_pending_payments"))
        self.assertContains(response, reverse("internal_unreconciled_ofx"))
        self.assertContains(response, reverse("internal_export_batches"))
        self.assertContains(response, "date_inicio=2026-06-01")
        self.assertContains(response, "date_fim=2026-06-30")
        self.assertContains(response, "mes=6&amp;ano=2026")

    def test_monthly_closing_post_generates_export_batch(self):
        self.client.force_login(self.user)
        self.transaction.status = OfxTransaction.Status.RECONCILED
        self.transaction.save(update_fields=["status", "updated_at"])
        self.make_export_ready_payment(status=Payment.Status.APPROVED)

        response = self.client.post(reverse("internal_monthly_closing"), {"mes": "6", "ano": "2026"})
        batch = ExportBatch.objects.get(status=ExportBatch.Status.GENERATED)

        self.assertEqual(response.status_code, 302)
        self.assertIn("mes=6&ano=2026", response["Location"])
        self.assertEqual(batch.period_start, date(2026, 6, 1))
        self.assertEqual(batch.period_end, date(2026, 6, 30))
        self.assertEqual(batch.records_count, 1)
        self.assertEqual(batch.payments.count(), 1)

    def test_monthly_closing_creates_two_downloadable_files(self):
        self.client.force_login(self.user)
        self.transaction.status = OfxTransaction.Status.RECONCILED
        self.transaction.save(update_fields=["status", "updated_at"])
        self.make_export_ready_payment(status=Payment.Status.APPROVED)

        self.client.post(reverse("internal_monthly_closing"), {"mes": "6", "ano": "2026"})
        batch = ExportBatch.objects.get(status=ExportBatch.Status.GENERATED)

        self.assertTrue(batch.accounting_file.name.endswith(".xlsx"))
        self.assertTrue(batch.import_file.name.endswith(".xlsx"))
        accounting_response = self.client.get(reverse("internal_export_download_kind", args=[batch.pk, "exportacao"]))
        import_response = self.client.get(reverse("internal_export_download_kind", args=[batch.pk, "importacao"]))
        self.assertEqual(accounting_response.status_code, 200)
        self.assertEqual(import_response.status_code, 200)

    def test_monthly_closing_export_only_uses_approved_or_reconciled_payments(self):
        self.client.force_login(self.user)
        self.transaction.status = OfxTransaction.Status.RECONCILED
        self.transaction.save(update_fields=["status", "updated_at"])
        approved = self.make_export_ready_payment(status=Payment.Status.APPROVED)
        reconciled = self.make_export_ready_payment(status=Payment.Status.RECONCILED)
        self.make_export_ready_payment(status=Payment.Status.RECEIVED)
        self.make_export_ready_payment(status=Payment.Status.POSTED)
        self.make_export_ready_payment(status=Payment.Status.CANCELED)

        self.client.post(reverse("internal_monthly_closing"), {"mes": "6", "ano": "2026"})
        batch = ExportBatch.objects.get(status=ExportBatch.Status.GENERATED)

        self.assertEqual(batch.records_count, 2)
        self.assertEqual(set(batch.payments.all()), {approved, reconciled})

    def test_monthly_closing_missing_required_fields_blocks_generation(self):
        self.client.force_login(self.user)
        self.transaction.status = OfxTransaction.Status.RECONCILED
        self.transaction.save(update_fields=["status", "updated_at"])
        Payment.objects.create(
            payment_date=date(2026, 6, 24),
            amount=Decimal("100.00"),
            status=Payment.Status.APPROVED,
            source="telegram",
        )

        response = self.client.post(
            reverse("internal_monthly_closing"),
            {"mes": "6", "ano": "2026"},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(ExportBatch.objects.filter(status=ExportBatch.Status.GENERATED).exists())
        self.assertContains(response, "Close blocked")
        self.assertContains(response, "Category")
        self.assertContains(response, "Cost center")

    def test_monthly_closing_shows_work_without_budget_warning_without_blocking(self):
        self.client.force_login(self.user)
        self.transaction.status = OfxTransaction.Status.RECONCILED
        self.transaction.save(update_fields=["status", "updated_at"])
        work = Work.objects.create(name="Tacima", normalized_name="tacima")
        payment = self.make_export_ready_payment(status=Payment.Status.APPROVED, work=work)
        payment.work_item_index = ""
        payment.save(update_fields=["work_item_index", "updated_at"])

        response = self.client.get(reverse("internal_monthly_closing"), {"mes": "6", "ano": "2026"})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["can_generate_spreadsheets"])
        self.assertContains(response, "Projects without imported budget")
        self.assertContains(response, "remain allowed and exportable")
        self.assertContains(response, "does not invent a budget item index")
        self.assertEqual(len(response.context["summary"].payments_with_work_without_budget), 1)
        self.assertEqual(response.context["summary"].payments_with_work_without_budget[0].payment_id, payment.pk)

    def test_monthly_closing_blocks_ofx_expense_without_payment(self):
        self.client.force_login(self.user)
        self.payment.status = Payment.Status.CANCELED
        self.payment.save(update_fields=["status", "updated_at"])
        self.transaction.status = OfxTransaction.Status.MISSING_PAYMENT
        self.transaction.save(update_fields=["status", "updated_at"])
        self.make_export_ready_payment(status=Payment.Status.APPROVED)

        response = self.client.post(
            reverse("internal_monthly_closing"),
            {"mes": "6", "ano": "2026"},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(ExportBatch.objects.filter(status=ExportBatch.Status.GENERATED).exists())
        self.assertFalse(response.context["can_generate_spreadsheets"])
        self.assertContains(response, "OFX expense(s) without payment")
        self.assertContains(response, "OFX expenses without payment")

    def test_monthly_closing_does_not_block_only_because_ofx_is_missing_or_credit_is_ignored(self):
        self.client.force_login(self.user)
        self.payment.status = Payment.Status.CANCELED
        self.payment.save(update_fields=["status", "updated_at"])
        self.transaction.amount = Decimal("250.00")
        self.transaction.status = OfxTransaction.Status.IGNORED
        self.transaction.save(update_fields=["amount", "status", "updated_at"])
        self.make_export_ready_payment(status=Payment.Status.APPROVED)

        response = self.client.get(reverse("internal_monthly_closing"), {"mes": "6", "ano": "2026"})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["can_generate_spreadsheets"])
        self.assertContains(response, "Ignored OFX credits")
        self.assertContains(response, "Approved without reconciliation")
        self.assertNotContains(response, "OFX do período ainda no importado")

    def test_monthly_closing_missing_ofx_is_warning_not_blocker(self):
        self.client.force_login(self.user)
        self.payment.status = Payment.Status.CANCELED
        self.payment.save(update_fields=["status", "updated_at"])
        self.transaction.delete()
        self.ofx_file.delete()
        self.make_export_ready_payment(status=Payment.Status.APPROVED)

        response = self.client.get(reverse("internal_monthly_closing"), {"mes": "6", "ano": "2026"})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["can_generate_spreadsheets"])
        self.assertContains(response, "Import the month&#x27;s OFX to validate the close")
        self.assertContains(response, "attention")
        self.assertNotContains(response, "OFX do período ainda no importado")

    def generated_batch(
        self,
        status=ExportBatch.Status.GENERATED,
        records_count=1,
        generated_by=None,
        period_start=None,
        period_end=None,
        with_files=True,
    ):
        batch = ExportBatch.objects.create(
            status=status,
            records_count=records_count,
            generated_by=generated_by,
            period_start=period_start,
            period_end=period_end,
        )
        if with_files:
            batch.file.save("spreadsheet.xlsx", ContentFile(b"conteudo"), save=True)
            batch.import_file.save("spreadsheet_importacao.xlsx", ContentFile(b"conteudo"), save=True)
            batch.accounting_file.save("spreadsheet_exportacao.xlsx", ContentFile(b"conteudo"), save=True)
        return batch

    def make_export_ready_payment(self, status=Payment.Status.APPROVED, amount=Decimal("100.00"), work=None):
        category, _ = Category.objects.get_or_create(name="Materiais", defaults={"normalized_name": "materiais"})
        cost_center, _ = CostCenter.objects.get_or_create(name="Company", defaults={"normalized_name": "empresa"})
        return Payment.objects.create(
            competence_date=date(2026, 6, 24),
            due_date=date(2026, 6, 24),
            payment_date=date(2026, 6, 24),
            amount=amount,
            counterparty=self.counterparty,
            category=category,
            cost_center=cost_center,
            work=work,
            description="Compra de materiais",
            payment_method="PIX",
            status=status,
            source="telegram",
            needs_review=False,
        )

    def make_payment_for_list(
        self,
        counterparty=None,
        category=None,
        cost_center=None,
        work=None,
        payment_date=date(2026, 6, 24),
        status=Payment.Status.RECEIVED,
        amount=Decimal("100.00"),
    ):
        return Payment.objects.create(
            payment_date=payment_date,
            amount=amount,
            counterparty=counterparty,
            category=category,
            cost_center=cost_center,
            work=work,
            status=status,
            source="telegram",
        )

    def make_ofx_transaction(
        self,
        fitid,
        status,
        posted_at=date(2026, 6, 24),
        amount=Decimal("-100.00"),
        memo="OFX TESTE",
        counterparty=None,
    ):
        return OfxTransaction.objects.create(
            ofx_file=self.ofx_file,
            fitid=fitid,
            posted_at=posted_at,
            amount=amount,
            memo=memo,
            status=status,
            counterparty=counterparty,
        )

    def ofx_upload(self, filename="extrato.ofx", content=None, fitid="FIT-1", amount="-250.00", memo="PAGAMENTO PIX"):
        payload = content or minimal_ofx(fitid=fitid, amount=amount, memo=memo)
        return SimpleUploadedFile(filename, payload.encode("latin-1"), content_type="application/x-ofx")

    def budget_upload(self, filename="orcamento.xlsx", work_name="Tacima", description="CALÇADA"):
        output = BytesIO()
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Budget Sintético"
        sheet.append(["OBRA", work_name])
        sheet.append([])
        sheet.append(
            [
                "ÍNDICE",
                "ETAPA/ITEM",
                "CÓDIGO",
                "BASE",
                "TIPO",
                "DESCRIÇÃO",
                "UNID.",
                "QTDE.",
                "CUSTO UNITÁRIO",
                "CUSTO TOTAL",
            ]
        )
        sheet.append(["1", "Step", "", "", "", "RUA PRINCIPAL", "", 0, 0, 1000])
        sheet.append(["1.4", "Subetapa", "", "", "", description, "", 0, 0, 500])
        sheet.append(["1.4.1", "Item", "#1.4.1", "Sem base", "Composição", "ALVENARIA", "m²", 10, 95.72, 957.2])
        workbook.save(output)
        return SimpleUploadedFile(
            filename,
            output.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )


def minimal_ofx(fitid="FIT-1", amount="-250.00", memo="PAGAMENTO PIX"):
    return f"""
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
<FITID>{fitid}
<MEMO>{memo}
</STMTTRN>
</BANKTRANLIST>
</STMTRS>
</STMTTRNRS>
</BANKMSGSRSV1>
</OFX>
"""
