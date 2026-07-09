from __future__ import annotations

from dataclasses import dataclass

from .ofx_import import OfxImportReport
from .payment_suggestions import PaymentSuggestionReport
from .reconciliation import ReconciliationReport


@dataclass(frozen=True)
class OfxImportSummary:
    import_report: OfxImportReport
    reconciliation_report: ReconciliationReport
    suggestion_report: PaymentSuggestionReport | None = None

    @property
    def period_display(self) -> str:
        start = self.import_report.ofx_file.start_date
        end = self.import_report.ofx_file.end_date
        if start and end:
            return f"{start:%d/%m/%Y} to {end:%d/%m/%Y}"
        if start:
            return f"from {start:%d/%m/%Y}"
        if end:
            return f"until {end:%d/%m/%Y}"
        return "-"

    @property
    def bank_account_display(self) -> str:
        bank_id = self.import_report.ofx_file.bank_id or "-"
        account_id = self.import_report.ofx_file.account_id or "-"
        if bank_id == "-" and account_id == "-":
            return "-"
        return f"{bank_id} / {account_id}"

    @property
    def lines(self) -> list[str]:
        lines = [
            f"Period: {self.period_display}",
            f"Bank/account: {self.bank_account_display}",
            f"Transactions read: {self.import_report.transactions_read}",
            f"New transactions: {self.import_report.transactions_created}",
            f"Existing transactions: {self.import_report.transactions_existing}",
            f"Updated transactions: {self.import_report.transactions_updated}",
            f"Imported expenses: {self.import_report.debit_transactions}",
            f"Ignored credits/income: {self.reconciliation_report.ignored_credits}",
            f"Automatically reconciled expenses: {self.reconciliation_report.reconciled}",
            f"Possibly duplicated expenses: {self.reconciliation_report.possible_duplicates}",
            f"Expenses without payment: {self.reconciliation_report.missing_payments}",
            f"Divergences: {self.reconciliation_report.divergent}",
        ]
        if self.suggestion_report is not None:
            lines.extend(
                [
                    f"Suggested payments: {self.suggestion_report.payments_created}",
                    f"Reused suggestions: {self.suggestion_report.payments_reused}",
                    f"Pending registration: {self.suggestion_report.pending_registration}",
                    f"Pending confirmation: {self.suggestion_report.pending_confirmation}",
                    f"Suggestion conflicts: {len(self.suggestion_report.conflicts)}",
                ]
            )
        return lines

    def as_text(self) -> str:
        return "\n".join(self.lines)

    def as_sentence(self) -> str:
        return "; ".join(self.lines) + "."


def build_ofx_import_summary(
    import_report: OfxImportReport,
    reconciliation_report: ReconciliationReport,
    suggestion_report: PaymentSuggestionReport | None = None,
) -> OfxImportSummary:
    return OfxImportSummary(
        import_report=import_report,
        reconciliation_report=reconciliation_report,
        suggestion_report=suggestion_report,
    )
