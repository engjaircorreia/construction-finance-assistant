from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import logging
import re

from django.db import transaction

from apps.core.log_safety import sanitize_log_payload
from apps.counterparties.importers import digits_only, normalize_text
from apps.counterparties.matching import find_best_counterparty_by_name
from apps.counterparties.models import CounterpartyDocument
from apps.documents.models import UploadedFile

from .models import OfxFile, OfxTransaction


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OfxImportReport:
    ofx_file: OfxFile
    transactions_read: int = 0
    transactions_skipped: int = 0
    transactions_created: int = 0
    transactions_updated: int = 0
    transactions_unchanged: int = 0
    debit_transactions: int = 0
    credit_transactions: int = 0
    fallback_fitids: int = 0

    @property
    def transactions_existing(self) -> int:
        return self.transactions_updated + self.transactions_unchanged


@transaction.atomic
def import_uploaded_ofx_file(uploaded_file: UploadedFile) -> OfxImportReport:
    uploaded_file.file.open("rb")
    try:
        content = uploaded_file.file.read().decode("latin-1", errors="ignore")
    finally:
        uploaded_file.file.close()
    return import_ofx_content(content, uploaded_file=uploaded_file, original_filename=uploaded_file.original_filename)


def import_ofx_content(
    content: str,
    *,
    uploaded_file: UploadedFile | None = None,
    original_filename: str = "",
) -> OfxImportReport:
    defaults = {
        "original_filename": original_filename or "extrato.ofx",
        "bank_id": tag_value(content, "BANKID"),
        "account_id": tag_value(content, "ACCTID"),
        "start_date": parse_ofx_date(tag_value(content, "DTSTART")),
        "end_date": parse_ofx_date(tag_value(content, "DTEND")),
        "status": OfxFile.Status.IMPORTED,
    }
    if uploaded_file:
        ofx_file, _ = OfxFile.objects.get_or_create(uploaded_file=uploaded_file, defaults=defaults)
    else:
        ofx_file = OfxFile.objects.create(**defaults)
    created_count = 0
    updated_count = 0
    unchanged_count = 0
    read_count = 0
    skipped_count = 0
    debit_count = 0
    credit_count = 0
    fallback_fitid_count = 0
    for block in re.findall(r"<STMTTRN>(.*?)(?=<STMTTRN>|</BANKTRANLIST>|</STMTRS>)", content, flags=re.I | re.S):
        parsed = parse_transaction_block(block)
        if not parsed:
            skipped_count += 1
            continue
        read_count += 1
        if parsed["amount"] < 0:
            debit_count += 1
        else:
            credit_count += 1
        if (parsed.get("raw_payload") or {}).get("fitid_source") == "fallback_block_hash":
            fallback_fitid_count += 1
        transaction, created = OfxTransaction.objects.get_or_create(
            ofx_file=ofx_file,
            fitid=parsed["fitid"],
            defaults={key: value for key, value in parsed.items() if key != "fitid"},
        )
        if created:
            created_count += 1
            continue
        changed = False
        for field, value in parsed.items():
            if field in {"fitid", "status"}:
                continue
            if getattr(transaction, field) != value:
                setattr(transaction, field, value)
                changed = True
        if changed:
            transaction.save()
            updated_count += 1
        else:
            unchanged_count += 1
    ofx_file.status = OfxFile.Status.PROCESSED
    ofx_file.save(update_fields=["status", "updated_at"])
    if uploaded_file:
        uploaded_file.status = UploadedFile.Status.PROCESSED
        uploaded_file.save(update_fields=["status", "updated_at"])
    logger.info(
        "OFX import completed: %s",
        sanitize_log_payload(
            {
                "ofx_file_id": ofx_file.pk,
                "uploaded_file_id": uploaded_file.pk if uploaded_file else None,
                "original_filename": ofx_file.original_filename,
                "transactions_read": read_count,
                "transactions_skipped": skipped_count,
                "transactions_created": created_count,
                "transactions_updated": updated_count,
                "transactions_unchanged": unchanged_count,
                "debit_transactions": debit_count,
                "credit_transactions": credit_count,
                "fallback_fitids": fallback_fitid_count,
            }
        ),
    )
    return OfxImportReport(
        ofx_file=ofx_file,
        transactions_read=read_count,
        transactions_skipped=skipped_count,
        transactions_created=created_count,
        transactions_updated=updated_count,
        transactions_unchanged=unchanged_count,
        debit_transactions=debit_count,
        credit_transactions=credit_count,
        fallback_fitids=fallback_fitid_count,
    )


def parse_transaction_block(block: str) -> dict | None:
    posted_at = parse_ofx_date(tag_value(block, "DTPOSTED"))
    amount = parse_amount(tag_value(block, "TRNAMT"))
    if posted_at is None or amount is None:
        return None
    memo = tag_value(block, "MEMO")
    document, name = extract_document_and_name(memo)
    counterparty = find_counterparty(document, name)
    raw_fitid = tag_value(block, "FITID")
    block_hash = fallback_fitid(block)
    # Quando o banco nao envia FITID, usamos hash do bloco OFX para manter idempotencia dentro do file.
    fitid = raw_fitid or block_hash
    fitid_source = "ofx" if raw_fitid else "fallback_block_hash"
    return {
        "fitid": fitid,
        "transaction_type": tag_value(block, "TRNTYPE"),
        "posted_at": posted_at,
        "amount": amount,
        "memo": memo,
        "normalized_memo": normalize_text(memo),
        "document_extracted": document,
        "name_extracted": name,
        "counterparty": counterparty,
        "status": OfxTransaction.Status.PENDING,
        "raw_payload": {
            "ofx_block_hash": block_hash,
            "fitid_source": fitid_source,
        },
    }


def tag_value(text: str, tag: str) -> str:
    match = re.search(rf"<{tag}>\s*([^<\r\n]*)", text, flags=re.I)
    return " ".join(match.group(1).split()) if match else ""


def parse_ofx_date(value: str):
    if not value:
        return None
    try:
        return datetime.strptime(value[:8], "%Y%m%d").date()
    except ValueError:
        return None


def parse_amount(value: str) -> Decimal | None:
    if not value:
        return None
    try:
        return Decimal(value.replace(",", ".")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None


def extract_document_and_name(memo: str) -> tuple[str, str]:
    match = re.search(r"(?<!\d)(\d{11}|\d{14})(?!\d)\s+(.+)$", memo or "")
    if not match:
        return "", ""
    document = digits_only(match.group(1))
    name = re.split(r"\s{2,}|\b(?:AG|CONTA|DOC|TED|PIX)\b", match.group(2).strip())[0].strip(" -")
    return document, name


def find_counterparty(document: str, name: str):
    if document:
        document_record = (
            CounterpartyDocument.objects.select_related("counterparty")
            .filter(number=document, counterparty__is_active=True)
            .first()
        )
        if document_record:
            return document_record.counterparty
    if name:
        return find_best_counterparty_by_name(name)
    return None


def fallback_fitid(block: str) -> str:
    return hashlib.sha256(block.encode("utf-8", errors="ignore")).hexdigest()[:32]
