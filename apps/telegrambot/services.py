from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import hashlib
import logging
import re

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils.text import get_valid_filename
from django.utils import timezone

from apps.accounts.models import AuthorizedTelegramUser
from apps.banking.ofx_import import import_uploaded_ofx_file
from apps.banking.payment_suggestions import suggest_payments_from_ofx
from apps.banking.reconciliation import reconcile_ofx_transactions
from apps.banking.reports import build_ofx_import_summary
from apps.core.log_safety import sanitize_log_payload
from apps.counterparties.importers import digits_only, normalize_text
from apps.counterparties.models import BudgetItem, Category, CostCenter, Counterparty, Origin, Work
from apps.documents.models import UploadedFile
from apps.payments.ai_extraction import (
    AIExtractionError,
    AIPaymentExtraction,
    OpenAIPaymentExtractor,
    apply_ai_extraction_to_payment,
    find_by_name,
    find_counterparty,
    should_ignore_counterparty_from_text,
)
from apps.payments.confirmation import resolve_budget_item
from apps.payments.defaults import apply_cost_center_default
from apps.payments.corrections import (
    PaymentCorrectionError,
    apply_text_correction_to_payment,
    is_projected_street_number,
    meaningful_tokens,
)
from apps.payments.corrections import parse_payment_correction
from apps.payments.extraction import apply_extraction_to_payment, extract_from_uploaded_file
from apps.payments.models import Payment
from apps.payments.confirmation import format_payment_suggestion
from apps.payments.counterparty_resolution import (
    AmbiguousCounterpartyError,
    CounterpartyCandidate,
    candidate_from_payment,
    ensure_alias,
    ensure_document,
    find_category,
    find_existing_counterparty,
    person_type_for_document,
    prepare_counterparty_review,
)

from .models import TelegramDraft


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TelegramSender:
    telegram_user_id: int
    name: str = ""
    username: str = ""


@dataclass(frozen=True)
class TelegramAttachment:
    file_id: str
    filename: str
    content_type: str
    content: bytes
    kind: str
    size_bytes: int | None = None


@dataclass(frozen=True)
class TelegramIntakeResult:
    authorized: bool
    reply_text: str
    uploaded_file: UploadedFile | None = None
    payment: Payment | None = None
    draft: TelegramDraft | None = None


class TelegramIntakeService:
    unauthorized_reply = "Unauthorized access."

    def process_text(self, sender: TelegramSender, message_id: int, text: str) -> TelegramIntakeResult:
        if not self.is_authorized(sender):
            return TelegramIntakeResult(authorized=False, reply_text=self.unauthorized_reply)
        logger.info(
            "Telegram text received: %s",
            sanitize_log_payload(
                {
                    "telegram_user_id": sender.telegram_user_id,
                    "message_id": message_id,
                    "text_length": len(text or ""),
                }
            ),
        )

        correction_payment = self.find_payment_waiting_text_correction(sender.telegram_user_id)
        if correction_payment:
            try:
                result = apply_text_correction_to_payment(
                    correction_payment.pk,
                    text,
                    telegram_user_id=sender.telegram_user_id,
                )
            except PaymentCorrectionError as exc:
                return TelegramIntakeResult(
                    authorized=True,
                    reply_text=f"Could not apply the correction: {exc}",
                    payment=correction_payment,
                )
            return TelegramIntakeResult(
                authorized=True,
                reply_text=f"{result.message}\n\n{self.build_confirmation_message(result.payment)}",
                payment=result.payment,
            )

        content = text.encode("utf-8")
        uploaded_file = self.create_uploaded_file(
            sender=sender,
            message_id=message_id,
            original_filename=f"telegram-text-{message_id}.txt",
            content_type="text/plain",
            kind=UploadedFile.Kind.TEXT,
            content=content,
            extracted_text=text,
        )
        draft = self.add_uploaded_file_to_draft(sender, uploaded_file)
        return TelegramIntakeResult(
            authorized=True,
            reply_text=self.build_draft_message(draft),
            uploaded_file=uploaded_file,
            draft=draft,
        )

    def process_attachment(
        self,
        sender: TelegramSender,
        message_id: int,
        attachment: TelegramAttachment,
    ) -> TelegramIntakeResult:
        if not self.is_authorized(sender):
            return TelegramIntakeResult(authorized=False, reply_text=self.unauthorized_reply)

        uploaded_file = self.create_uploaded_file(
            sender=sender,
            message_id=message_id,
            original_filename=attachment.filename,
            content_type=attachment.content_type,
            kind=attachment.kind,
            content=attachment.content,
            telegram_file_id=attachment.file_id,
            size_bytes=attachment.size_bytes,
        )
        logger.info(
            "Telegram attachment received: %s",
            sanitize_log_payload(
                {
                    "telegram_user_id": sender.telegram_user_id,
                    "message_id": message_id,
                    "uploaded_file_id": uploaded_file.pk,
                    "kind": attachment.kind,
                    "content_type": attachment.content_type,
                    "size_bytes": attachment.size_bytes,
                    "filename": attachment.filename,
                }
            ),
        )
        if attachment.kind == UploadedFile.Kind.OFX:
            try:
                report = import_uploaded_ofx_file(uploaded_file)
                reconciliation_report = reconcile_ofx_transactions(report.ofx_file.transactions.all())
                suggestion_report = suggest_payments_from_ofx(report.ofx_file)
                summary = build_ofx_import_summary(report, reconciliation_report, suggestion_report)
            except Exception as exc:
                logger.warning(
                    "Telegram OFX processing failed: %s",
                    sanitize_log_payload(
                        {
                            "telegram_user_id": sender.telegram_user_id,
                            "message_id": message_id,
                            "uploaded_file_id": uploaded_file.pk,
                            "error_class": exc.__class__.__name__,
                        }
                    ),
                )
                raise
            return TelegramIntakeResult(
                authorized=True,
                reply_text=build_telegram_ofx_summary(summary, suggestion_report),
                uploaded_file=uploaded_file,
            )
        draft = self.add_uploaded_file_to_draft(sender, uploaded_file)
        return TelegramIntakeResult(
            authorized=True,
            reply_text=self.build_draft_message(draft),
            uploaded_file=uploaded_file,
            draft=draft,
        )

    def is_authorized(self, sender: TelegramSender) -> bool:
        AuthorizedTelegramUser.objects.filter(
            telegram_user_id=sender.telegram_user_id,
            is_active=True,
        ).update(last_seen_at=timezone.now())
        if AuthorizedTelegramUser.objects.filter(
            telegram_user_id=sender.telegram_user_id,
            is_active=True,
        ).exists():
            return True
        return str(sender.telegram_user_id) in settings.TELEGRAM_ALLOWED_USER_IDS

    def create_uploaded_file(
        self,
        sender: TelegramSender,
        message_id: int,
        original_filename: str,
        content_type: str,
        kind: str,
        content: bytes,
        telegram_file_id: str = "",
        size_bytes: int | None = None,
        extracted_text: str = "",
    ) -> UploadedFile:
        digest = hashlib.sha256(content).hexdigest()
        safe_filename = get_valid_filename(original_filename)
        uploaded_file = UploadedFile(
            original_filename=safe_filename,
            content_type=content_type,
            size_bytes=size_bytes if size_bytes is not None else len(content),
            sha256=digest,
            source=UploadedFile.Source.TELEGRAM,
            kind=kind,
            status=UploadedFile.Status.RECEIVED,
            telegram_file_id=telegram_file_id,
            telegram_message_id=str(message_id),
            telegram_user_id=sender.telegram_user_id,
            extracted_text=extracted_text,
            notes=self.sender_note(sender),
        )
        uploaded_file.file.save(safe_filename, ContentFile(content), save=False)
        uploaded_file.save()
        return uploaded_file

    def create_initial_payment(self, uploaded_file: UploadedFile, description: str = "") -> Payment:
        payment = Payment.objects.create(
            description=description,
            source="telegram",
            status=Payment.Status.RECEIVED,
            uploaded_file=uploaded_file,
            needs_review=True,
            review_reason="Waiting for extraction and user confirmation.",
            raw_payload={
                "telegram_message_id": uploaded_file.telegram_message_id,
                "telegram_user_id": uploaded_file.telegram_user_id,
                "uploaded_file_id": uploaded_file.pk,
            },
        )
        extraction = extract_from_uploaded_file(uploaded_file)
        payment = apply_extraction_to_payment(payment, extraction)
        if self.should_use_ai(extraction):
            try:
                ai_extraction = OpenAIPaymentExtractor().extract(payment)
                payment = apply_ai_extraction_to_payment(payment, ai_extraction)
            except AIExtractionError as exc:
                payload = payment.raw_payload or {}
                payload["ai_extraction_error"] = exc.__class__.__name__
                payment.raw_payload = payload
                payment.review_reason = f"{payment.review_reason} IA no executada: {exc.__class__.__name__}."
                payment.save(update_fields=["raw_payload", "review_reason", "updated_at"])
        try:
            payment = prepare_counterparty_review(payment)
        except AmbiguousCounterpartyError as exc:
            payment.status = Payment.Status.PENDING_REGISTRATION
            payment.needs_review = True
            payment.review_reason = f"Ambiguous counterparty: {exc}"
            payment.save(update_fields=["status", "needs_review", "review_reason", "updated_at"])
        return payment

    def add_uploaded_file_to_draft(self, sender: TelegramSender, uploaded_file: UploadedFile) -> TelegramDraft:
        with transaction.atomic():
            draft = self.get_or_create_active_draft(sender)
            draft.uploaded_files.add(uploaded_file)
            if uploaded_file.kind == UploadedFile.Kind.TEXT and uploaded_file.extracted_text:
                draft.text_content = append_text(draft.text_content, uploaded_file.extracted_text)
            extraction = extract_from_uploaded_file(uploaded_file)
            if (
                uploaded_file.kind == UploadedFile.Kind.TEXT
                and uploaded_file.extracted_text
                and uploaded_file.extracted_text not in draft.text_content
            ):
                draft.text_content = append_text(draft.text_content, uploaded_file.extracted_text)
            self.apply_extraction_to_draft(draft, extraction)
            if uploaded_file.kind == UploadedFile.Kind.TEXT:
                self.apply_text_updates_to_draft(draft, uploaded_file.extracted_text or extraction.raw_text)
            self.reconcile_draft_budget_index(draft)
            payload = draft.raw_payload or {}
            extractions = payload.setdefault("draft_extractions", [])
            extractions.append(
                {
                    "uploaded_file_id": uploaded_file.pk,
                    "telegram_message_id": uploaded_file.telegram_message_id,
                    "extraction": extraction.as_dict(),
                }
            )
            draft.raw_payload = payload
            draft.save()
            draft_id = draft.pk
        draft = TelegramDraft.objects.get(pk=draft_id)
        if self.should_rearrange_draft_with_ai(draft, uploaded_file):
            draft = self.rearrange_draft_with_ai(draft, uploaded_file)
        return draft

    def reconcile_draft_budget_index(self, draft: TelegramDraft) -> None:
        if not draft.work:
            return
        inferred_index = infer_budget_item_index(draft.work, draft.text_content, current_index=draft.work_item_index)
        if inferred_index:
            draft.work_item_index = inferred_index

    def get_or_create_active_draft(self, sender: TelegramSender) -> TelegramDraft:
        draft = TelegramDraft.objects.filter(
            telegram_user_id=sender.telegram_user_id,
            status=TelegramDraft.Status.ACTIVE,
        ).order_by("-updated_at").first()
        if draft:
            return draft
        return TelegramDraft.objects.create(
            telegram_user_id=sender.telegram_user_id,
            sender_name=sender.name,
            sender_username=sender.username,
        )

    def apply_extraction_to_draft(self, draft: TelegramDraft, extraction) -> None:
        if extraction.amount is not None:
            draft.amount = extraction.amount
        if extraction.payment_date:
            draft.payment_date = extraction.payment_date
        if extraction.counterparty:
            draft.counterparty = extraction.counterparty
            draft.category = draft.category or extraction.counterparty.default_category
            clear_draft_counterparty_candidate(draft)
        elif extraction.counterparty_candidate_name or extraction.counterparty_candidate_document:
            store_draft_counterparty_candidate(
                draft,
                CounterpartyCandidate(
                    name=extraction.counterparty_candidate_name,
                    document=extraction.counterparty_candidate_document,
                    alias=extraction.counterparty_candidate_name,
                    source="telegram",
                ),
                source_kind=extraction.source_kind,
            )
        if extraction.payment_method:
            draft.payment_method = extraction.payment_method
        if extraction.description and not draft.description:
            draft.description = extraction.description[:255]
        draft.needs_ai = draft.needs_ai or extraction.needs_ai
        draft.confidence = max(draft.confidence, extraction.confidence)
        if extraction.source_kind != UploadedFile.Kind.TEXT and extraction.raw_text:
            self.apply_work_candidate_to_draft(draft, extraction.raw_text)
        apply_cost_center_default(draft)

    def should_rearrange_draft_with_ai(self, draft: TelegramDraft, uploaded_file: UploadedFile) -> bool:
        if not settings.OPENAI_API_KEY or not settings.OPENAI_DRAFT_REARRANGE_ENABLED:
            return False
        if uploaded_file.kind in {UploadedFile.Kind.PDF, UploadedFile.Kind.IMAGE}:
            return True
        if uploaded_file.kind == UploadedFile.Kind.TEXT:
            return draft.uploaded_files.exclude(kind=UploadedFile.Kind.TEXT).exists()
        return False

    def rearrange_draft_with_ai(self, draft: TelegramDraft, uploaded_file: UploadedFile) -> TelegramDraft:
        ai_uploaded_file = uploaded_file
        if uploaded_file.kind == UploadedFile.Kind.TEXT:
            ai_uploaded_file = draft.uploaded_files.exclude(kind=UploadedFile.Kind.TEXT).order_by("-created_at").first()
            ai_uploaded_file = ai_uploaded_file or uploaded_file
        payment_stub = build_payment_stub_from_draft(draft, ai_uploaded_file)
        try:
            ai_extraction = OpenAIPaymentExtractor().extract(payment_stub)
        except AIExtractionError as exc:
            payload = draft.raw_payload or {}
            payload["draft_ai_rearrangement_error"] = exc.__class__.__name__
            draft.raw_payload = payload
            draft.save(update_fields=["raw_payload", "updated_at"])
            return draft

        apply_ai_rearrangement_to_draft(draft, payment_stub, ai_extraction)
        self.reconcile_draft_budget_index(draft)
        draft.save()
        return draft

    def apply_text_updates_to_draft(self, draft: TelegramDraft, text: str) -> None:
        if not text:
            return
        payment_stub = Payment(work=draft.work)
        updates = parse_payment_correction(text, payment_stub)
        if "amount" in updates:
            draft.amount = updates["amount"]
        if "payment_date" in updates:
            draft.payment_date = updates["payment_date"]
        if "payment_method" in updates:
            draft.payment_method = updates["payment_method"]
        if "description" in updates:
            draft.description = updates["description"][:255]
        if "category" in updates:
            draft.category = updates["category"]
        if "cost_center" in updates:
            draft.cost_center = updates["cost_center"]
        if "work" in updates:
            draft.work = updates["work"]
            clear_draft_work_candidate(draft)
        if "work_item_index" in updates and not is_projected_street_number(
            normalize_text(text),
            updates["work_item_index"],
        ):
            draft.work_item_index = updates["work_item_index"]
        elif draft.work:
            inferred_index = infer_budget_item_index(draft.work, text, current_index=draft.work_item_index)
            if inferred_index:
                draft.work_item_index = inferred_index
        if "counterparty" in updates:
            draft.counterparty = updates["counterparty"]
            draft.category = draft.category or updates["counterparty"].default_category
            clear_draft_counterparty_candidate(draft)
        elif "counterparty_candidate" in updates:
            store_draft_counterparty_candidate(
                draft,
                updates["counterparty_candidate"],
                source_kind=UploadedFile.Kind.TEXT,
            )
        if not draft.description and draft.text_content:
            draft.description = draft.text_content[:255]
        self.apply_work_candidate_to_draft(draft, text)
        apply_cost_center_default(draft)

    def apply_work_candidate_to_draft(self, draft: TelegramDraft, text: str) -> None:
        candidate_name = extract_work_candidate_name(text)
        if not candidate_name:
            return
        matches = find_work_matches(candidate_name)
        if len(matches) == 1:
            draft.work = matches[0]
            clear_draft_work_candidate(draft)
            return
        if len(matches) > 1:
            payload = draft.raw_payload or {}
            payload["work_candidate_ambiguous"] = {
                "name": candidate_name,
                "matches": [work.name for work in matches],
                "source": "telegram",
            }
            draft.raw_payload = payload
            return
        if draft.work_id:
            return
        store_draft_work_candidate(draft, candidate_name)

    def finalize_draft(
        self,
        draft_id: int,
        sender: TelegramSender,
        require_authorization: bool = True,
    ) -> TelegramIntakeResult:
        if require_authorization and not self.is_authorized(sender):
            return TelegramIntakeResult(authorized=False, reply_text=self.unauthorized_reply)
        with transaction.atomic():
            draft = TelegramDraft.objects.select_for_update().get(
                pk=draft_id,
                telegram_user_id=sender.telegram_user_id,
            )
            if draft.status == TelegramDraft.Status.CANCELED:
                return TelegramIntakeResult(authorized=True, reply_text="This draft has already been canceled.")
            if draft.status == TelegramDraft.Status.FINALIZED and draft.finalized_payment_id:
                return TelegramIntakeResult(
                    authorized=True,
                    reply_text=self.build_confirmation_message(draft.finalized_payment),
                    payment=draft.finalized_payment,
                )
            if draft_has_blocking_pendencies(draft):
                return TelegramIntakeResult(
                    authorized=True,
                    reply_text=(
                        "Resolva as pending items antes de finalizar.\n\n"
                        f"{self.build_draft_message(draft)}"
                    ),
                    draft=draft,
                )
            self.reconcile_draft_budget_index(draft)
            apply_cost_center_default(draft)
            uploaded_file = draft.uploaded_files.exclude(kind=UploadedFile.Kind.TEXT).order_by("-created_at").first()
            uploaded_file = uploaded_file or draft.uploaded_files.order_by("-created_at").first()
            draft_payload = draft.raw_payload or {}
            payment_payload = {
                "telegram_draft_id": draft.pk,
                "telegram_user_id": draft.telegram_user_id,
                "draft_text_content": draft.text_content,
                "draft_uploaded_file_ids": list(draft.uploaded_files.values_list("pk", flat=True)),
                "draft_payload": draft_payload,
            }
            if not draft.counterparty_id and draft_payload.get("counterparty_candidate"):
                payment_payload["counterparty_candidate"] = draft_payload["counterparty_candidate"]

            payment = Payment.objects.create(
                competence_date=draft.payment_date,
                due_date=draft.payment_date,
                payment_date=draft.payment_date,
                amount=draft.amount or 0,
                counterparty=draft.counterparty,
                description=(draft.description or draft.text_content)[:255],
                category=draft.category,
                cost_center=draft.cost_center,
                work=draft.work,
                work_item_index=draft.work_item_index,
                payment_method=draft.payment_method,
                source="telegram",
                status=Payment.Status.RECEIVED,
                uploaded_file=uploaded_file,
                confidence=draft.confidence,
                needs_review=True,
                review_reason="Draft finalized by Telegram. Waiting for confirmation.",
                raw_payload=payment_payload,
            )
            draft.status = TelegramDraft.Status.FINALIZED
            draft.finalized_payment = payment
            draft.save(update_fields=["status", "finalized_payment", "updated_at"])

        if settings.OPENAI_API_KEY and (draft.needs_ai or payment.confidence < 0.60):
            try:
                ai_extraction = OpenAIPaymentExtractor().extract(payment)
                payment = apply_ai_extraction_to_payment(payment, ai_extraction)
            except AIExtractionError as exc:
                payload = payment.raw_payload or {}
                payload["ai_extraction_error"] = exc.__class__.__name__
                payment.raw_payload = payload
                payment.review_reason = f"{payment.review_reason} IA no executada: {exc.__class__.__name__}."
                payment.save(update_fields=["raw_payload", "review_reason", "updated_at"])

        try:
            payment = prepare_counterparty_review(payment)
        except AmbiguousCounterpartyError as exc:
            payment.status = Payment.Status.PENDING_REGISTRATION
            payment.needs_review = True
            payment.review_reason = f"Ambiguous counterparty: {exc}"
            payment.save(update_fields=["status", "needs_review", "review_reason", "updated_at"])
        if payment.counterparty_id and payment.status == Payment.Status.RECEIVED:
            payment.status = Payment.Status.PENDING_CONFIRMATION
            payment.review_reason = "Draft finalized. Waiting for payment confirmation."
            payment.save(update_fields=["status", "review_reason", "updated_at"])
        return TelegramIntakeResult(
            authorized=True,
            reply_text=self.build_confirmation_message(payment),
            payment=payment,
        )

    def register_counterparty_from_draft(
        self,
        draft_id: int,
        kind: str,
        sender: TelegramSender,
    ) -> TelegramIntakeResult:
        if not self.is_authorized(sender):
            return TelegramIntakeResult(authorized=False, reply_text=self.unauthorized_reply)
        if kind not in {Counterparty.Kind.SUPPLIER, Counterparty.Kind.WORKER}:
            return TelegramIntakeResult(authorized=True, reply_text="Invalid registration type.")

        with transaction.atomic():
            draft = TelegramDraft.objects.select_for_update().get(
                pk=draft_id,
                telegram_user_id=sender.telegram_user_id,
            )
            if draft.status != TelegramDraft.Status.ACTIVE:
                return TelegramIntakeResult(
                    authorized=True,
                    reply_text="This draft is not active for vendor/worker registration.",
                    draft=draft,
                )
            if draft.counterparty_id:
                return TelegramIntakeResult(
                    authorized=True,
                    reply_text=f"This draft is already linked to {draft.counterparty.name}.",
                    draft=draft,
                )

            candidate = draft_counterparty_candidate(draft)
            candidate = CounterpartyCandidate(
                name=candidate.name,
                document=digits_only(candidate.document),
                alias=candidate.alias or candidate.name,
                category_name=candidate.category_name,
                source=candidate.source or Origin.TELEGRAM,
            )
            if not candidate.name and not candidate.document:
                return TelegramIntakeResult(
                    authorized=True,
                    reply_text="This draft has no pending suggested registration.",
                    draft=draft,
                )

            try:
                counterparty = find_existing_counterparty(candidate)
            except AmbiguousCounterpartyError as exc:
                return TelegramIntakeResult(
                    authorized=True,
                    reply_text=(
                        "I found more than one possible record for this name. "
                        f"Send a correction before registering. Detail: {exc}"
                    ),
                    draft=draft,
                )

            created = False
            if counterparty is None:
                counterparty = Counterparty.objects.create(
                    name=candidate.name or candidate.document,
                    normalized_name=candidate.normalized_name or candidate.document,
                    kind=kind,
                    person_type=person_type_for_document(candidate.document),
                    primary_document=candidate.document,
                    default_category=find_category(candidate.category_name),
                    source=Origin.TELEGRAM,
                    confidence=Decimal("1.00") if candidate.document else Decimal("0.70"),
                    notes="Created from a draft received through Telegram.",
                )
                ensure_document(counterparty, candidate.document)
                ensure_alias(counterparty, candidate.alias, Origin.TELEGRAM)
                ensure_alias(counterparty, candidate.name, Origin.TELEGRAM)
                created = True

            draft.counterparty = counterparty
            draft.category = draft.category or counterparty.default_category
            clear_draft_counterparty_candidate(draft)
            draft.save(update_fields=["counterparty", "category", "raw_payload", "updated_at"])

        kind_label = "Vendor" if kind == Counterparty.Kind.SUPPLIER else "Worker"
        action_text = "registered" if created else "existing record reused"
        return TelegramIntakeResult(
            authorized=True,
            reply_text=(
                f"{kind_label} {action_text}: {counterparty.name}.\n\n"
                f"{self.build_draft_message(draft)}"
            ),
            draft=draft,
        )

    def leave_work_candidate_as_company(self, draft_id: int, sender: TelegramSender) -> TelegramIntakeResult:
        if not self.is_authorized(sender):
            return TelegramIntakeResult(authorized=False, reply_text=self.unauthorized_reply)
        with transaction.atomic():
            draft = TelegramDraft.objects.select_for_update().get(
                pk=draft_id,
                telegram_user_id=sender.telegram_user_id,
            )
            if draft.status != TelegramDraft.Status.ACTIVE:
                return TelegramIntakeResult(
                    authorized=True,
                    reply_text="This draft is not active for changing cost center.",
                    draft=draft,
                )
            candidate = draft_work_candidate(draft)
            if not candidate.name:
                return TelegramIntakeResult(
                    authorized=True,
                    reply_text="This draft has no pending suggested project.",
                    draft=draft,
                )
            company_cost_center, _ = CostCenter.objects.get_or_create(
                normalized_name="empresa",
                defaults={"name": "Company"},
            )
            draft.work = None
            draft.work_item_index = ""
            draft.cost_center = company_cost_center
            clear_draft_work_candidate(draft)
            draft.save(update_fields=["work", "work_item_index", "cost_center", "raw_payload", "updated_at"])
        return TelegramIntakeResult(
            authorized=True,
            reply_text=(
                "Suggested project removed. Cost center kept as Company.\n\n"
                f"{self.build_draft_message(draft)}"
            ),
            draft=draft,
        )

    def register_work_from_draft(self, draft_id: int, sender: TelegramSender) -> TelegramIntakeResult:
        if not self.is_authorized(sender):
            return TelegramIntakeResult(authorized=False, reply_text=self.unauthorized_reply)
        with transaction.atomic():
            draft = TelegramDraft.objects.select_for_update().get(
                pk=draft_id,
                telegram_user_id=sender.telegram_user_id,
            )
            if draft.status != TelegramDraft.Status.ACTIVE:
                return TelegramIntakeResult(
                    authorized=True,
                    reply_text="This draft is not active for project registration.",
                    draft=draft,
                )
            candidate = draft_work_candidate(draft)
            if not candidate.name:
                return TelegramIntakeResult(
                    authorized=True,
                    reply_text="This draft has no pending suggested project.",
                    draft=draft,
                )

            matches = find_work_matches(candidate.name)
            if matches:
                work = matches[0]
                created = False
            else:
                normalized_name = normalize_text(candidate.name)
                work, created = Work.objects.get_or_create(
                    normalized_name=normalized_name,
                    defaults={
                        "name": candidate.name,
                        "status": Work.Status.ACTIVE,
                        "is_active": True,
                    },
                )
            if not work.is_active:
                work.is_active = True
                work.save(update_fields=["is_active", "updated_at"])

            work_cost_center, _ = CostCenter.objects.get_or_create(
                normalized_name="project",
                defaults={"name": "Project"},
            )
            draft.work = work
            draft.cost_center = work_cost_center
            draft.work_item_index = ""
            clear_draft_work_candidate(draft)
            draft.save(update_fields=["work", "cost_center", "work_item_index", "raw_payload", "updated_at"])

        action_text = "Project registered" if created else "Existing project reused"
        lines = [
            f"{action_text}: {work.name}.",
        ]
        if not BudgetItem.objects.filter(work=work, is_active=True).exists():
            lines.append("Warning: this project has no imported budget items yet.")
        lines.extend(["", self.build_draft_message(draft)])
        return TelegramIntakeResult(
            authorized=True,
            reply_text="\n".join(lines),
            draft=draft,
        )

    def cancel_draft(
        self,
        draft_id: int,
        sender: TelegramSender,
        require_authorization: bool = True,
    ) -> TelegramIntakeResult:
        if require_authorization and not self.is_authorized(sender):
            return TelegramIntakeResult(authorized=False, reply_text=self.unauthorized_reply)
        with transaction.atomic():
            draft = TelegramDraft.objects.select_for_update().get(
                pk=draft_id,
                telegram_user_id=sender.telegram_user_id,
            )
            if draft.status != TelegramDraft.Status.ACTIVE:
                return TelegramIntakeResult(
                    authorized=True,
                    reply_text="This draft is not active for cancellation.",
                    draft=draft,
                )
            draft.status = TelegramDraft.Status.CANCELED
            draft.save(update_fields=["status", "updated_at"])
        return TelegramIntakeResult(authorized=True, reply_text=f"Draft #{draft_id} canceled.")

    def start_new_from_draft(self, draft_id: int, sender: TelegramSender) -> TelegramIntakeResult:
        if not self.is_authorized(sender):
            return TelegramIntakeResult(authorized=False, reply_text=self.unauthorized_reply)
        with transaction.atomic():
            draft = TelegramDraft.objects.select_for_update().get(
                pk=draft_id,
                telegram_user_id=sender.telegram_user_id,
            )
            if draft.status != TelegramDraft.Status.ACTIVE:
                return TelegramIntakeResult(
                    authorized=True,
                    reply_text="This draft is not active for starting a new payment.",
                    draft=draft,
                )
            draft.status = TelegramDraft.Status.CANCELED
            draft.save(update_fields=["status", "updated_at"])
        return TelegramIntakeResult(
            authorized=True,
            reply_text="Previous draft closed. Send the new payment data.",
        )

    def start_new_draft(self, sender: TelegramSender) -> TelegramIntakeResult:
        if not self.is_authorized(sender):
            return TelegramIntakeResult(authorized=False, reply_text=self.unauthorized_reply)
        TelegramDraft.objects.filter(
            telegram_user_id=sender.telegram_user_id,
            status=TelegramDraft.Status.ACTIVE,
        ).update(status=TelegramDraft.Status.CANCELED)
        return TelegramIntakeResult(
            authorized=True,
            reply_text="Previous draft closed. Send the new payment data.",
        )

    def find_payment_waiting_text_correction(self, telegram_user_id: int) -> Payment | None:
        payments = Payment.objects.filter(
            source="telegram",
            status=Payment.Status.CORRECTING,
        ).order_by("-updated_at")
        for payment in payments:
            if (payment.raw_payload or {}).get("telegram_user_id") == telegram_user_id:
                return payment
        return None

    def should_use_ai(self, extraction) -> bool:
        if not settings.OPENAI_API_KEY:
            return False
        return bool(extraction.needs_ai or extraction.confidence < 0.60)

    def sender_note(self, sender: TelegramSender) -> str:
        parts = [f"Telegram ID: {sender.telegram_user_id}"]
        if sender.name:
            parts.append(f"Name: {sender.name}")
        if sender.username:
            parts.append(f"Username: @{sender.username}")
        return "; ".join(parts)

    def build_confirmation_message(self, payment: Payment) -> str:
        if not payment.counterparty_id:
            candidate = candidate_from_payment(payment)
            return (
                "I received the payment, but need to confirm the record before approval.\n\n"
                f"{format_payment_suggestion(payment)}\n\n"
                f"Suggested name: {candidate.name or '-'}\n"
                f"Suggested CPF/CNPJ: {candidate.document or '-'}\n\n"
                "Choose whether it is a vendor or worker, or request a correction."
            )
        return (
            "I received it and prepared a suggestion pending confirmation.\n\n"
            f"{format_payment_suggestion(payment)}\n\n"
            "Choose an action:"
        )

    def build_draft_message(self, draft: TelegramDraft) -> str:
        budget_item = None
        if draft.work_id and draft.work_item_index:
            budget_item = BudgetItem.objects.filter(work=draft.work, index=draft.work_item_index, is_active=True).first()
        candidate = draft_counterparty_candidate(draft)
        work_candidate = draft_work_candidate(draft)
        pendencies = draft_blocking_pendency_lines(draft)
        counterparty_name = draft.counterparty.name if draft.counterparty else candidate.name
        document = draft.counterparty.primary_document if draft.counterparty else candidate.document
        file_count = draft.uploaded_files.count()
        lines = [
            f"Draft #{draft.pk} updated.",
            "",
            "Data extracted so far:",
            f"Date: {draft.payment_date.strftime('%d/%m/%Y') if draft.payment_date else '-'}",
            f"Amount: R$ {draft.amount:.2f}" if draft.amount is not None else "Amount: -",
            f"Vendor/Worker: {counterparty_name or '-'}",
            f"CPF/CNPJ: {document or '-'}",
            f"Description: {draft.description or draft.text_content[:200] or '-'}",
            f"Category: {draft.category.name if draft.category else '-'}",
            f"Payment method: {draft.payment_method or '-'}",
            f"Cost center: {draft.cost_center.name if draft.cost_center else '-'}",
            f"Project: {draft.work.name if draft.work else '-'}",
            f"Likely index: {draft.work_item_index or '-'}",
            f"Service/Item: {budget_item.description if budget_item else '-'}",
            f"Files/messages in draft: {file_count}",
        ]
        if work_candidate.name and not draft.work_id:
            insert_at = lines.index(f"Likely index: {draft.work_item_index or '-'}")
            lines.insert(insert_at, f"Suggested project not registered: {work_candidate.name}")
        if not draft.counterparty_id and (candidate.name or candidate.document):
            insert_at = lines.index(f"Category: {draft.category.name if draft.category else '-'}")
            lines[insert_at:insert_at] = [
                "Suggested registration:",
                f"Name: {candidate.name or '-'}",
                f"CPF/CNPJ: {candidate.document or '-'}",
                f"Source: {draft_counterparty_candidate_origin(draft) or '-'}",
            ]
        if pendencies:
            lines.extend(["", "Pending items:"])
            lines.extend(f"- {pendency}" for pendency in pendencies)
            lines.append("")
            lines.append("Resolve pending items or correct by message.")
        else:
            lines.extend(["", "No blocking pending items. Finalize to create the suggestion."])
        return "\n".join(lines)


def append_text(current: str, new_text: str) -> str:
    current = str(current or "").strip()
    new_text = str(new_text or "").strip()
    if not new_text:
        return current
    if not current:
        return new_text
    return f"{current}\n{new_text}"


def store_draft_counterparty_candidate(
    draft: TelegramDraft,
    candidate: CounterpartyCandidate,
    source_kind: str = "",
) -> None:
    payload = draft.raw_payload or {}
    payload["counterparty_candidate"] = {
        "name": candidate.name,
        "document": digits_only(candidate.document),
        "alias": candidate.alias,
        "category_name": candidate.category_name,
        "source": candidate.source,
    }
    if source_kind:
        payload["counterparty_candidate"]["source_kind"] = source_kind
    draft.raw_payload = payload


def clear_draft_counterparty_candidate(draft: TelegramDraft) -> None:
    payload = draft.raw_payload or {}
    if "counterparty_candidate" in payload:
        payload.pop("counterparty_candidate", None)
        draft.raw_payload = payload


def draft_counterparty_candidate(draft: TelegramDraft) -> CounterpartyCandidate:
    payload = draft.raw_payload or {}
    candidate = payload.get("counterparty_candidate") or {}
    return CounterpartyCandidate(
        name=candidate.get("name", ""),
        document=candidate.get("document", ""),
        alias=candidate.get("alias", ""),
        category_name=candidate.get("category_name", ""),
        source=candidate.get("source", ""),
    )


def draft_counterparty_candidate_origin(draft: TelegramDraft) -> str:
    payload = draft.raw_payload or {}
    candidate = payload.get("counterparty_candidate") or {}
    source = candidate.get("source", "")
    source_kind = candidate.get("source_kind", "")
    if source == Origin.AI:
        return "IA"
    source_labels = {
        UploadedFile.Kind.PDF: "PDF",
        UploadedFile.Kind.TEXT: "texto",
        UploadedFile.Kind.IMAGE: "imagem",
    }
    return source_labels.get(source_kind, "texto" if source == Origin.TELEGRAM else source)


def draft_has_pending_counterparty_candidate(draft: TelegramDraft) -> bool:
    candidate = draft_counterparty_candidate(draft)
    return bool((candidate.name or candidate.document) and not draft.counterparty_id)


def draft_has_pending_work_candidate(draft: TelegramDraft) -> bool:
    candidate = draft_work_candidate(draft)
    return bool(candidate.name and not draft.work_id)


def draft_has_blocking_pendencies(draft: TelegramDraft) -> bool:
    return draft_has_pending_counterparty_candidate(draft) or draft_has_pending_work_candidate(draft)


def draft_blocking_pendency_lines(draft: TelegramDraft) -> list[str]:
    lines = []
    if draft_has_pending_counterparty_candidate(draft):
        candidate = draft_counterparty_candidate(draft)
        name = candidate.name or "-"
        document = candidate.document or "-"
        origin = draft_counterparty_candidate_origin(draft) or "-"
        lines.append(f"Suggested registration without confirmation: {name} | CPF/CNPJ: {document} | Source: {origin}")
    if draft_has_pending_work_candidate(draft):
        candidate = draft_work_candidate(draft)
        lines.append(f"Suggested project not registered: {candidate.name}")
    return lines


@dataclass(frozen=True)
class WorkCandidate:
    name: str = ""
    source: str = ""


def store_draft_work_candidate(draft: TelegramDraft, name: str, source: str = "telegram") -> None:
    payload = draft.raw_payload or {}
    payload["work_candidate"] = {
        "name": name,
        "source": source,
    }
    payload.pop("work_candidate_ambiguous", None)
    draft.raw_payload = payload


def clear_draft_work_candidate(draft: TelegramDraft) -> None:
    payload = draft.raw_payload or {}
    changed = False
    for key in ("work_candidate", "work_candidate_ambiguous"):
        if key in payload:
            payload.pop(key, None)
            changed = True
    if changed:
        draft.raw_payload = payload


def draft_work_candidate(draft: TelegramDraft) -> WorkCandidate:
    payload = draft.raw_payload or {}
    candidate = payload.get("work_candidate") or {}
    return WorkCandidate(name=candidate.get("name", ""), source=candidate.get("source", ""))


def extract_work_candidate_name(text: str) -> str:
    text = " ".join(str(text or "").split())
    if not text:
        return ""

    patterns = (
        r"\bproject\s+(?:de\s+|da\s+|do\s+|em\s+)?(?P<name>[^,;\n]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        candidate = cleanup_work_candidate_name(match.group("name"))
        if is_meaningful_work_candidate(candidate):
            return candidate
    suffix_candidate = extract_work_candidate_before_label(text)
    if suffix_candidate and is_meaningful_work_candidate(suffix_candidate):
        return suffix_candidate
    return ""


def extract_work_candidate_before_label(text: str) -> str:
    match = re.search(r"\bproject\b", text, flags=re.IGNORECASE)
    if not match:
        return ""
    before = text[: match.start()].strip(" .,:;-")
    if not before:
        return ""
    tokens = before.split()
    if not tokens:
        return ""
    stop_tokens = {"para", "por", "com", "sem", "de", "da", "do", "das", "dos", "em", "na", "no"}
    candidate_tokens = []
    for token in reversed(tokens):
        normalized = normalize_text(token.strip(" .,:;-"))
        if normalized in stop_tokens:
            break
        candidate_tokens.append(token)
        if len(candidate_tokens) >= 4:
            break
    if not candidate_tokens:
        return ""
    return cleanup_work_candidate_name(" ".join(reversed(candidate_tokens)))


def cleanup_work_candidate_name(value: str) -> str:
    value = re.sub(
        r"\b(?:amount|date|vendor|worker|beneficiario|beneficiário|destinatario|destinatário|"
        r"recebedor|category|cost center|forma|payment|item|subitem|indice|índice|cpf|cnpj|via)\b.*$",
        "",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(r"^(?:de|da|do|em)\s+", "", value, flags=re.IGNORECASE)
    return " ".join(value.strip(" .,:;-").split())


def is_meaningful_work_candidate(candidate: str) -> bool:
    normalized = normalize_text(candidate)
    if not normalized:
        return False
    generic_names = {
        "calcada",
        "calçada",
        "material",
        "materiais",
        "servico",
        "servicos",
        "serviço",
        "serviços",
        "imposto",
        "taxa",
        "despesa",
        "despesas",
        "payment",
        "construcao",
        "construção",
    }
    return normalized not in generic_names


def find_work_matches(candidate_name: str) -> list[Work]:
    normalized_candidate = normalize_text(candidate_name)
    if not normalized_candidate:
        return []

    exact_matches = []
    partial_matches = []
    for work in Work.objects.filter(is_active=True):
        names = [normalize_text(work.name)]
        if work.aliases:
            names.extend(normalize_text(alias) for alias in re.split(r"[,;\n]+", work.aliases) if alias.strip())
        if normalized_candidate in names:
            exact_matches.append(work)
        elif any(normalized_candidate in name or name in normalized_candidate for name in names if name):
            partial_matches.append(work)
    return exact_matches or partial_matches


def build_payment_stub_from_draft(draft: TelegramDraft, uploaded_file: UploadedFile) -> Payment:
    return Payment(
        competence_date=draft.payment_date,
        due_date=draft.payment_date,
        payment_date=draft.payment_date,
        amount=draft.amount or 0,
        counterparty=draft.counterparty,
        description=(draft.description or draft.text_content)[:255],
        category=draft.category,
        cost_center=draft.cost_center,
        work=draft.work,
        work_item_index=draft.work_item_index,
        payment_method=draft.payment_method,
        source="telegram",
        status=Payment.Status.RECEIVED,
        uploaded_file=uploaded_file,
        confidence=draft.confidence,
        needs_review=True,
        raw_payload={
            "draft_text_content": draft.text_content,
            "draft_payload": draft.raw_payload or {},
        },
    )


def apply_ai_rearrangement_to_draft(
    draft: TelegramDraft,
    payment_stub: Payment,
    extraction: AIPaymentExtraction,
) -> None:
    if extraction.amount is not None and extraction.amount >= 0:
        draft.amount = extraction.amount
    if extraction.payment_date:
        draft.payment_date = extraction.payment_date

    counterparty = find_counterparty(extraction)
    if counterparty and should_ignore_counterparty_from_text(payment_stub, counterparty.name):
        counterparty = None
    if counterparty:
        draft.counterparty = counterparty
        draft.category = draft.category or counterparty.default_category
        clear_draft_counterparty_candidate(draft)
    elif extraction.counterparty_name or extraction.counterparty_document:
        if not should_ignore_counterparty_from_text(payment_stub, extraction.counterparty_name):
            store_draft_counterparty_candidate(
                draft,
                CounterpartyCandidate(
                    name=extraction.counterparty_name,
                    document=extraction.counterparty_document,
                    alias=extraction.counterparty_name,
                    category_name=extraction.category_name,
                    source="ia",
                ),
            )

    category = find_by_name(Category, extraction.category_name)
    if category:
        draft.category = category
    cost_center = find_by_name(CostCenter, extraction.cost_center_name)
    if cost_center:
        draft.cost_center = cost_center
    work = find_by_name(Work, extraction.work_name)
    if work:
        draft.work = work
    if extraction.payment_method:
        draft.payment_method = extraction.payment_method
    if extraction.description:
        draft.description = extraction.description[:255]
    if extraction.work_item_index and not is_projected_street_number(
        normalize_text(draft.text_content),
        extraction.work_item_index,
    ):
        draft.work_item_index = extraction.work_item_index

    apply_cost_center_default(draft)
    draft.confidence = max(draft.confidence, extraction.confidence)
    payload = draft.raw_payload or {}
    payload["draft_ai_rearrangement"] = extraction.as_dict()
    payload.pop("draft_ai_rearrangement_error", None)
    draft.raw_payload = payload


def infer_budget_item_index(work, text: str, current_index: str = "") -> str:
    normalized_text = normalize_text(text)
    stage = find_stage_for_text(work, normalized_text)
    if not stage:
        return ""

    text_tokens = set(meaningful_tokens(normalized_text))
    candidates = []
    for item in BudgetItem.objects.filter(work=work, is_active=True):
        if item.index == stage.index:
            continue
        item_stage = find_stage_for_item(work, item.index)
        if item_stage and item_stage.index != stage.index:
            continue
        item_tokens = set(meaningful_tokens(item.description))
        overlap = text_tokens & item_tokens
        if overlap:
            score = len(overlap)
            score += item.index.count(".")
            if item.item_type == BudgetItem.ItemType.SUBSTAGE:
                score += 2
            candidates.append((score, item.index))
    if not candidates and current_index:
        updated_index = replace_stage_in_index(current_index, stage.index)
        if BudgetItem.objects.filter(work=work, index=updated_index, is_active=True).exists():
            return updated_index
    if not candidates:
        return ""
    candidates.sort(reverse=True)
    return candidates[0][1]


def find_stage_for_text(work, normalized_text: str) -> BudgetItem | None:
    street_number = extract_street_number(normalized_text)
    stages = BudgetItem.objects.filter(work=work, item_type=BudgetItem.ItemType.STAGE, is_active=True)
    if street_number:
        matches = [stage for stage in stages if street_number in normalize_text(stage.description)]
        if len(matches) == 1:
            return matches[0]
        qualifier = extract_stage_qualifier(normalized_text)
        if qualifier:
            qualified_matches = [stage for stage in matches if stage_has_qualifier(stage, qualifier)]
            if len(qualified_matches) == 1:
                return qualified_matches[0]
        return None

    text_tokens = set(meaningful_budget_location_tokens(normalized_text))
    if not text_tokens:
        return None
    scored = []
    for stage in stages:
        stage_tokens = set(meaningful_budget_location_tokens(stage.normalized_description))
        if not stage_tokens:
            continue
        overlap = text_tokens & stage_tokens
        if len(overlap) >= min(2, len(stage_tokens)):
            scored.append((len(overlap), stage))
    if not scored:
        return None
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1]


def extract_stage_qualifier(normalized_text: str) -> str:
    import re

    if re.search(r"\btii\b|\bt2\b|\btype\s*2\b", normalized_text):
        return "tii"
    if re.search(r"\bti\b|\bt1\b|\btype\s*1\b", normalized_text):
        return "ti"
    return ""


def stage_has_qualifier(stage: BudgetItem, qualifier: str) -> bool:
    import re

    text = normalize_text(stage.description)
    if qualifier == "tii":
        return bool(re.search(r"\btii\b", text))
    if qualifier == "ti":
        return bool(re.search(r"\bti\b", text)) and not bool(re.search(r"\btii\b", text))
    return False


def extract_street_number(normalized_text: str) -> str:
    import re

    match = re.search(r"rua\s+projetada\s+0*(\d+)", normalized_text)
    if not match:
        return ""
    return f"projetada {int(match.group(1)):02d}"


def replace_stage_in_index(index: str, stage_index: str) -> str:
    if "." not in index:
        return stage_index
    return f"{stage_index}.{index.split('.', 1)[1]}"


def find_stage_for_item(work, index: str) -> BudgetItem | None:
    stage_index = index.split(".", 1)[0]
    return BudgetItem.objects.filter(work=work, index=stage_index, is_active=True).first()


def meaningful_budget_location_tokens(value: str) -> list[str]:
    ignored = {"rua", "ti", "tii", "trecho", "etapa"}
    normalized = normalize_text(value).replace("administractive", "administrativo")
    return [token for token in normalized.split() if len(token) > 2 and token not in ignored]


def build_telegram_ofx_summary(summary, suggestion_report) -> str:
    lines = [
        "OFX received and imported.",
        "",
        f"Transactions read: {summary.import_report.transactions_read}",
        f"New: {summary.import_report.transactions_created}",
        f"Existing: {summary.import_report.transactions_existing}",
        f"Ignored credits: {summary.reconciliation_report.ignored_credits}",
        f"Reconciled: {summary.reconciliation_report.reconciled}",
        f"Possible duplicates: {summary.reconciliation_report.possible_duplicates}",
        f"Divergent: {summary.reconciliation_report.divergent}",
        f"Suggested payments: {suggestion_report.payments_created}",
        f"Pending registration/confirmation: {suggestion_report.pending_registration}/{suggestion_report.pending_confirmation}",
    ]
    pending_review_count = suggestion_report.pending_registration + suggestion_report.pending_confirmation
    if pending_review_count:
        lines.extend(["", "Review pending payments in the web interface."])
    if pending_review_count >= 5:
        lines.append("There are several items; web review will be faster.")
    if suggestion_report.conflicts:
        lines.append(f"Suggestion conflicts: {len(suggestion_report.conflicts)}")
    return "\n".join(lines)
