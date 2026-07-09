from django.core.management.base import BaseCommand

from apps.exports.services import export_approved_payments


class Command(BaseCommand):
    help = "Export approved payments to the import spreadsheet template."

    def handle(self, *args, **options):
        batch = export_approved_payments()
        self.stdout.write(
            self.style.SUCCESS(
                "Export generated: "
                f"batch={batch.pk}; records={batch.records_count}; "
                f"accounting={batch.accounting_file.name}; import={batch.import_file.name or batch.file.name}"
            )
        )
