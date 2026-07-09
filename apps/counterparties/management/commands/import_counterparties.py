import json

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.counterparties.importers import import_counterparty_workbooks


class Command(BaseCommand):
    help = "Import normalized vendors and workers into the counterparty database."

    def add_arguments(self, parser):
        parser.add_argument(
            "--suppliers",
            default=str(settings.SUPPLIERS_NORMALIZED_FILE),
            help="Path to the normalized vendor spreadsheet.",
        )
        parser.add_argument(
            "--workers",
            default=str(settings.WORKERS_NORMALIZED_FILE),
            help="Path to the normalized worker spreadsheet.",
        )
        parser.add_argument("--dry-run", action="store_true", help="Process without writing to the database.")
        parser.add_argument("--json", action="store_true", help="Print the report as JSON.")

    def handle(self, *args, **options):
        report = import_counterparty_workbooks(
            supplier_path=options["suppliers"],
            worker_path=options["workers"],
            dry_run=options["dry_run"],
        )
        data = report.as_dict()
        if options["json"]:
            self.stdout.write(json.dumps(data, ensure_ascii=False, indent=2))
            return

        self.stdout.write(self.style.SUCCESS("Counterparty import completed."))
        self.stdout.write(f"Files: {', '.join(data['files'])}")
        self.stdout.write(f"Rows read: {data['rows_read']}")
        self.stdout.write(f"Rows skipped: {data['rows_skipped']}")
        self.stdout.write(f"Created: {data['created']}")
        self.stdout.write(f"Updated: {data['updated']}")
        self.stdout.write(f"Unchanged: {data['unchanged']}")
        self.stdout.write(f"Documents created: {data['documents_created']}")
        self.stdout.write(f"Aliases created: {data['aliases_created']}")
        self.stdout.write(f"Conflicts: {len(data['conflicts'])}")
        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("Dry run: no changes were saved."))
