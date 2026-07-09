from django.core.management.base import BaseCommand

from apps.banking.models import OfxTransaction
from apps.banking.payment_suggestions import suggest_payments_from_ofx
from apps.banking.reconciliation import reconcile_ofx_transactions


class Command(BaseCommand):
    help = "Reconcile OFX transactions with posted payments and create pending suggestions when payment is missing."

    def handle(self, *args, **options):
        report = reconcile_ofx_transactions()
        suggestion_report = suggest_payments_from_ofx(
            OfxTransaction.objects.filter(pk__in=report.processed_transaction_ids)
        )
        self.stdout.write(
            self.style.SUCCESS(
                "OFX reconciliation completed: "
                f"reconciled={report.reconciled}; "
                f"possible_duplicates={report.possible_duplicates}; "
                f"missing_payments={report.missing_payments}; "
                f"divergent={report.divergent}; "
                f"ignored={report.ignored_credits}; "
                f"links_created={report.reconciliations_created}; "
                f"links_updated={report.reconciliations_updated}; "
                f"suggested_payments={suggestion_report.payments_created}; "
                f"pending_registration={suggestion_report.pending_registration}; "
                f"pending_confirmation={suggestion_report.pending_confirmation}; "
                f"conflicts={len(suggestion_report.conflicts)}"
            )
        )
