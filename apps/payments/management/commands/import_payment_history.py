import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.payments.importers import import_payment_history


class Command(BaseCommand):
    help = "Import payment history from XLSX files to populate master data and historical payments."

    def add_arguments(self, parser):
        parser.add_argument(
            "--path",
            default=str(settings.PAYMENTS_HISTORY_DIR),
            help="XLSX file or directory with historical spreadsheets.",
        )
        parser.add_argument("--dry-run", action="store_true", help="Process without writing to the database.")
        parser.add_argument(
            "--no-payments",
            action="store_true",
            help="Import only records, categories, and classification rules.",
        )
        parser.add_argument(
            "--report-file",
            help="Optional path to save the Markdown import report.",
        )
        parser.add_argument("--json", action="store_true", help="Print the report as JSON.")

    def handle(self, *args, **options):
        report = import_payment_history(
            path=options["path"],
            dry_run=options["dry_run"],
            create_payments=not options["no_payments"],
        )
        data = report.as_dict()
        if options["report_file"]:
            report_path = Path(options["report_file"])
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(format_report(data), encoding="utf-8")
        if options["json"]:
            self.stdout.write(json.dumps(data, ensure_ascii=False, indent=2))
            return

        self.stdout.write(self.style.SUCCESS("Payment history import completed."))
        self.stdout.write(f"Files: {', '.join(data['files'])}")
        self.stdout.write(f"Rows read: {data['rows_read']}")
        self.stdout.write(f"Rows skipped: {data['rows_skipped']}")
        self.stdout.write(f"Unpaid/non-expense rows skipped: {data['unpaid_or_non_expense_skipped']}")
        self.stdout.write(f"Payments created: {data['payments_created']}")
        self.stdout.write(f"Existing payments: {data['payments_unchanged']}")
        self.stdout.write(f"Counterparties created: {data['counterparties_created']}")
        self.stdout.write(f"Counterparties updated: {data['counterparties_updated']}")
        self.stdout.write(f"Categories created: {data['categories_created']}")
        self.stdout.write(f"Chart of accounts created: {data['chart_accounts_created']}")
        self.stdout.write(f"Cost centers created: {data['cost_centers_created']}")
        self.stdout.write(f"Projects created: {data['works_created']}")
        self.stdout.write(f"Classification rules updated: {data['classification_rules_updated']}")
        self.stdout.write(f"Conflicts: {len(data['conflicts'])}")
        if options["report_file"]:
            self.stdout.write(f"Report: {options['report_file']}")
        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("Dry run: no changes were saved."))


def format_report(data):
    lines = [
        "# Payment History Import Report",
        "",
        "## Summary",
        "",
        f"- Files: {', '.join(data['files'])}",
        f"- Rows read: {data['rows_read']}",
        f"- Rows skipped: {data['rows_skipped']}",
        f"- Unpaid/non-expense rows skipped: {data['unpaid_or_non_expense_skipped']}",
        f"- Payments created: {data['payments_created']}",
        f"- Existing payments: {data['payments_unchanged']}",
        f"- Counterparties created: {data['counterparties_created']}",
        f"- Counterparties updated: {data['counterparties_updated']}",
        f"- Categories created: {data['categories_created']}",
        f"- Chart of accounts created: {data['chart_accounts_created']}",
        f"- Cost centers created: {data['cost_centers_created']}",
        f"- Projects created: {data['works_created']}",
        f"- Documents created: {data['documents_created']}",
        f"- Aliases created: {data['aliases_created']}",
        f"- Classification rules updated: {data['classification_rules_updated']}",
        f"- Conflicts: {len(data['conflicts'])}",
        "",
    ]
    if data["conflicts"]:
        lines.extend(
            [
                "## Conflicts",
                "",
                "| File | Row | Counterparty | Reason | Detail |",
                "|---|---:|---|---|---|",
            ]
        )
        for conflict in data["conflicts"]:
            lines.append(
                f"| {conflict['source_file']} | {conflict['row_number']} | "
                f"{conflict['counterparty_name']} | {conflict['reason']} | {conflict['detail']} |"
            )
        lines.append("")
    return "\n".join(lines)
