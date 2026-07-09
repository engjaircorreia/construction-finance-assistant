# Repository Structure

This document defines where each type of file should live and what must not be committed.

## Versioned Folders

| Folder | Purpose |
| --- | --- |
| `apps/` | Django apps and business rules. |
| `config/` | Django, Celery, URLs, WSGI, and ASGI configuration. |
| `deploy/` | Deployment and backup support scripts/configuration. |
| `docs/` | Technical documentation, operational guides, and prompt history. |
| `docs/pt-BR/` | Original Portuguese documentation copy. |
| `files/` | Versioned empty spreadsheet templates only. Real date must be ignored. |
| `static/` | Versioned static files, if any. |
| `storage/` | Only `.gitkeep`; real/generated content must be ignored. |
| `templates/` | HTML templates for the internal system. |

## Local Or VPS Folders

| Folder | Purpose | Git |
| --- | --- | --- |
| `date/` | Local area for real files used in manual imports. | Ignored except `.gitkeep`. |
| `storage/` | Generated files, uploads, exported spreadsheets, and temporary app files. | Ignored except `.gitkeep`. |
| `backups/` or `/backups/construction_finance_assistant` | Database and file backups. | Ignored. |
| `media/`, `uploads/`, `receipts/`, `ofx/` | Files uploaded by users/bot. | Ignored. |

## What Can Be Committed

- Source code.
- Migrations.
- HTML templates.
- Deployment scripts without secrets.
- Documentation.
- `.env.example`.
- Empty import/export spreadsheet templates:
  - `files/modelo_exportacao.xlsx`
  - `files/modelo_importacao.xlsx`
  - `files/planilhas_modelo_importacao/Planilha_Modelo_Pagamentos.xlsx`

## What Must Not Be Committed

- Real `.env`.
- Telegram token.
- OpenAI key.
- SQL dumps.
- Backups.
- Real OFX files.
- PDFs/receipts.
- Receipt images.
- Real payment spreadsheets.
- Real vendor/worker master date.
- Real project budget spreadsheets, unless explicitly approved.
- Spreadsheets exported for the accountant.
- Runtime-generated files in `storage/`.

## Convention For New Documents

- Operational documents stay in `docs/`.
- Architecture decisions may go in `docs/architecture_decisions/`.
- Long prompts and temporary test checklists should not be committed unless they are still useful for operating the system.

## Before Committing

Run:

```bash
git status --short
git diff --check
```

Before adding files, check for real date:

```bash
git status --short --ignored
```

If a real file appears as `??`, update `.gitignore` before committing.
