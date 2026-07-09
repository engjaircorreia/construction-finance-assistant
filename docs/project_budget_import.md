# Project Budget Import

New projects can be registered through Telegram or the web interface before a budget spreadsheet exists. In that case, the system shows `Project without imported budget`, but it does not block the payment entry or export.

Until a reliable budget exists for the project, keep `Budget item index` empty. The system must not invent a budget index.

## Web Import

When the project is already registered, use:

```text
/interno/projects/<project-id>/orcamento/importar/
```

The flow accepts `.xlsx` files, links the spreadsheet to the selected project, imports or updates items by `project + index`, and shows a report with created, updated, skipped, and conflicted items.

If the spreadsheet declares a different project name from the selected project, the sheet is treated as a conflict to avoid importing a budget into the wrong project.

## Command Import

The Django command remains available:

```bash
python manage.py import_budget_items --path "files/Budget Sintético - Sertãozinho.xlsx"
```

On the VPS with Docker:

```bash
docker compose -f docker-compose.shared-vps.yml exec web python manage.py import_budget_items --path "/app/files/Budget Sintético - Sertãozinho.xlsx"
```

Pass multiple spreadsheets by repeating `--path`.

## Review

After import, check Django Admin to confirm the project has active budget items. When opening drafts, payments, or monthly close, the warning disappears for projects with active `BudgetItem` records.

## Possible Next Step

If needed, the web screen can later add a preview before saving and an explicit option to deactivate old items that are not present in the new spreadsheet.
