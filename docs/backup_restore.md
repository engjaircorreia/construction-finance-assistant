# Backup And Restore

This document describes the backup routine for the finance system.

Use this workflow on the VPS. Do not store backups inside the repository and do not upload backups to GitHub.

## What Is Backed Up

- PostgreSQL database.
- Persistent file volume at `/app/storage`, including uploads, receipts, OFX files, generated spreadsheets, and private application files.

## Backup Location On The VPS

Default location:

```bash
/backups/construction_finance_assistant
```

The script creates this structure:

```text
/backups/construction_finance_assistant/
  daily/
    db/
    storage/
  monthly/
    db/
    storage/
```

## Manual Backup

On the VPS:

```bash
cd ~/apps/construction-finance-assistant
deploy/backup.sh
```

To use another compose file or destination:

```bash
cd ~/apps/construction-finance-assistant
COMPOSE_FILE=docker-compose.shared-vps.yml BACKUP_ROOT=/backups/construction_finance_assistant deploy/backup.sh
```

The script:

- requires a local `.env` on the VPS;
- uses the `db` service to generate `pg_dump`;
- uses the `web` service to compress `/app/storage`;
- does not print database passwords, Telegram tokens, or OpenAI keys;
- creates timestamped file names;
- keeps daily backups for 14 days;
- creates one monthly copy per month;
- keeps monthly backups for 180 days.

## Cron Schedule

Example: run every day at 02:15:

```cron
15 2 * * * cd /home/deploy/apps/construction-finance-assistant && /home/deploy/apps/construction-finance-assistant/deploy/backup.sh >> /var/log/construction_finance_assistant_backup.log 2>&1
```

Before adding the cron job, run the script manually and confirm that files were created in `/backups/construction_finance_assistant`.

## List Backups

```bash
find /backups/construction_finance_assistant -maxdepth 4 -type f -printf '%TY-%Tm-%Td %TH:%TM %p
' | sort
```

## Copy Backups Locally

Example:

```bash
rsync -av deploy@srv1772642:/backups/construction_finance_assistant/ ./backups/construction_finance_assistant/
```

Do not place the local backup folder inside the repository.

## Restore In A Controlled Environment

Do not restore directly into production before testing in a separate environment.

Recommended workflow:

1. Copy backup files to a local or staging machine.
2. Start a copy of the application.
3. Restore the database.
4. Restore storage.
5. Run checks and open the system.
6. Only then decide whether production needs restoration.

## Local Restore Simulation

Use a separate project copy or, at minimum, a different `COMPOSE_PROJECT_NAME` so volumes do not mix with daily local development.

```bash
cd ~/tmp
git clone git@github.com:engjaircorreia/construction-finance-assistant.git construction_finance_restore_test
cd construction_finance_restore_test
cp .env.example .env
```

Use local values and no real secrets:

```dotenv
COMPOSE_PROJECT_NAME=construction_finance_restore_test
DJANGO_SETTINGS_MODULE=config.settings.development
DJANGO_DEBUG=True
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1
DJANGO_CSRF_TRUSTED_ORIGINS=http://localhost:8000
POSTGRES_DB=construction_finance
POSTGRES_USER=construction_finance
POSTGRES_PASSWORD=local-test-password
POSTGRES_HOST=db
POSTGRES_PORT=5432
TELEGRAM_BOT_TOKEN=
OPENAI_API_KEY=
```

Start containers and prepare the empty database:

```bash
docker compose up -d --build db redis web
docker compose exec web python manage.py migrate
```

Copy backups outside the repository:

```bash
mkdir -p ~/backups/construction_finance_restore_test
rsync -av deploy@srv1772642:/backups/construction_finance_assistant/ ~/backups/construction_finance_restore_test/
```

Pick matching timestamped files:

```text
~/backups/construction_finance_restore_test/daily/db/postgres_YYYYMMDD_HHMMSS.sql.gz
~/backups/construction_finance_restore_test/daily/storage/storage_YYYYMMDD_HHMMSS.tar.gz
```

Restore database first, then files:

```bash
gunzip -c ~/backups/construction_finance_restore_test/daily/db/postgres_YYYYMMDD_HHMMSS.sql.gz   | docker compose exec -T db psql -U construction_finance -d construction_finance

docker compose exec -T web sh -c 'rm -rf /app/storage/*'
cat ~/backups/construction_finance_restore_test/daily/storage/storage_YYYYMMDD_HHMMSS.tar.gz   | docker compose exec -T web tar xzf - -C /app
```

Check the restore:

```bash
docker compose exec web python manage.py check
docker compose exec web python manage.py shell -c "from apps.payments.models import Payment; from apps.documents.models import UploadedFile; print('payments=', Payment.objects.count()); print('files=', UploadedFile.objects.count())"
```

Open `http://localhost:8000/` and validate login, payments, drafts, generated spreadsheets, and imported OFX files.

When done:

```bash
docker compose down -v
```

## Restore Database In Local/Staging

```bash
cd ~/apps/construction-finance-assistant
gunzip -c /safe/path/postgres_YYYYMMDD_HHMMSS.sql.gz   | docker compose exec -T db psql -U construction_finance -d construction_finance
```

If the local database is not empty, recreate it in the test environment before restoring. Do not do this in production without a maintenance window and another recently validated backup.

## Restore Files In Local/Staging

```bash
cd ~/apps/construction-finance-assistant
docker compose exec -T web sh -c 'rm -rf /app/storage/*'
cat /safe/path/storage_YYYYMMDD_HHMMSS.tar.gz   | docker compose exec -T web tar xzf - -C /app
```

Then check:

```bash
docker compose exec web python manage.py check
docker compose exec web python manage.py shell -c "from apps.documents.models import UploadedFile; print(UploadedFile.objects.count())"
```

## Post-Restore Checklist

- Application starts without errors.
- Web login works.
- Drafts appear.
- Payments appear.
- Attached files open internally when applicable.
- Generated spreadsheets remain registered.
- Imported OFX files remain registered.
- `python manage.py check` passes.

## Cautions

- A backup without a tested restore is only a promise.
- Never place backups in a public server folder.
- Never compress `.env` together with application backups.
- Never paste tokens, passwords, or keys into tickets, commits, or logs.
