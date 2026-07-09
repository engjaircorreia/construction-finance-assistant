import json

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.counterparties.importers import import_budget_workbooks


class Command(BaseCommand):
    help = "Import budget items from the real project budget spreadsheets."

    def add_arguments(self, parser):
        parser.add_argument(
            "--path",
            action="append",
            dest="paths",
            help="Path to a budget spreadsheet. Can be used more than once.",
        )
        parser.add_argument("--dry-run", action="store_true", help="Process without writing to the database.")
        parser.add_argument("--json", action="store_true", help="Print the report as JSON.")

    def handle(self, *args, **options):
        paths = options["paths"] or [str(path) for path in settings.BUDGET_WORKBOOKS]
        report = import_budget_workbooks(paths=paths, dry_run=options["dry_run"])
        data = report.as_dict()
        if options["json"]:
            self.stdout.write(json.dumps(data, ensure_ascii=False, indent=2))
            return

        self.stdout.write(self.style.SUCCESS("Budget item import completed."))
        self.stdout.write(f"Files: {', '.join(data['files'])}")
        self.stdout.write(f"Rows read: {data['rows_read']}")
        self.stdout.write(f"Rows skipped: {data['rows_skipped']}")
        self.stdout.write(f"Projects created: {data['works_created']}")
        self.stdout.write(f"Items created: {data['created']}")
        self.stdout.write(f"Items updated: {data['updated']}")
        self.stdout.write(f"Unchanged: {data['unchanged']}")
        self.stdout.write(f"Conflicts: {len(data['conflicts'])}")
        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("Dry run: no changes were saved."))
