# Inplant Engenharia - Payment Assistant

Internal finance application for recording expenses from Telegram receipts, reviewing payment entries, reconciling them against bank statements, and generating accounting import spreadsheets.

## Clone The Project

Start by cloning the repository and entering the project directory:

```bash
git clone git@github-inplant:codemapstartup/inplantengenharia_finance.git
cd inplantengenharia_finance
```

## Stack

- Django
- PostgreSQL
- Celery
- Redis
- Docker Compose
- Telegram Bot API
- OpenAI API

## First Local Run

1. Create `.env` from `.env.example`.
2. Adjust the local values in `.env`.
3. Start the containers:

```bash
docker compose up --build
```

4. In another terminal, run migrations:

```bash
docker compose exec web python manage.py migrate
```

5. Create an administrator user:

```bash
docker compose exec web python manage.py createsuperuser
```

6. Open the internal dashboard:

```text
http://localhost:8000/interno/dashboard/
```

## Security

- Do not commit `.env`.
- Create the real `.env` directly on the VPS.
- Restrict web access to internal users.
- Restrict the Telegram bot by `telegram_user_id`.
- Keep PostgreSQL and Redis without public ports.

More details are available in [docs/security.md](docs/security.md).

## Documentation

The main documentation index is [docs/README.md](docs/README.md).

The original Portuguese documentation was preserved in [docs/pt-BR/](docs/pt-BR/).

Before adding new files to Git, read [docs/repository_structure.md](docs/repository_structure.md). Real payment date, OFX files, receipts, master date, backups, and dumps must stay out of the repository.
