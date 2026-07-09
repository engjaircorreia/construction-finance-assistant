# Production On The VPS

This guide assumes the domain already points to the VPS IP and Docker with Compose is installed.

Replace `sistema.example.com` with the real domain before starting.

## Production Files

- `docker-compose.prod.yml`: production services.
- `deploy/nginx/conf.d/app.http.conf`: initial HTTP Nginx config for first boot and certificate issuance.
- `deploy/nginx/conf.d/app.https.conf.example`: HTTPS example.
- `deploy/backup.sh`: database and storage backup script.

## Production Environment

Create the real `.env` directly on the VPS. Do not copy a local `.env` to GitHub.

Minimum model:

```env
DJANGO_SETTINGS_MODULE=config.settings.production
DJANGO_SECRET_KEY=replace-with-a-long-random-secret
DJANGO_DEBUG=False
DJANGO_ALLOWED_HOSTS=sistema.example.com
DJANGO_CSRF_TRUSTED_ORIGINS=https://sistema.example.com
DJANGO_ADMIN_URL=private-admin-path/
DJANGO_TIME_ZONE=America/Fortaleza
POSTGRES_DB=construction_finance
POSTGRES_USER=construction_finance
POSTGRES_PASSWORD=replace-with-a-strong-password
POSTGRES_HOST=db
POSTGRES_PORT=5432
REDIS_URL=redis://redis:6379/0
CELERY_BROKER_URL=redis://redis:6379/0
CELERY_RESULT_BACKEND=redis://redis:6379/1
TELEGRAM_BOT_TOKEN=replace-with-real-token
TELEGRAM_ALLOWED_USER_IDS=owner_id,partner_id
OPENAI_API_KEY=replace-with-real-key
DEFAULT_BANK_ACCOUNT=Main Account
DEFAULT_PAYER=Company
```

Security rules:

- `DJANGO_SECRET_KEY` must be long, random, and different from examples.
- `DJANGO_ALLOWED_HOSTS` must contain only the real domain.
- `DJANGO_CSRF_TRUSTED_ORIGINS` must use HTTPS and match allowed hosts.
- `DJANGO_ADMIN_URL` must be private, not `admin/` or `controle-interno/`.
- `TELEGRAM_ALLOWED_USER_IDS` must contain only authorized users.
- `.env` must be created on the VPS, not committed.

## First Boot In HTTP

Edit `deploy/nginx/conf.d/app.http.conf` and replace `sistema.example.com` with the real domain.

Start services:

```bash
docker compose -f docker-compose.prod.yml up -d --build
```

Run migrations and collect static files:

```bash
docker compose -f docker-compose.prod.yml exec web python manage.py migrate
docker compose -f docker-compose.prod.yml exec web python manage.py collectstatic --noinput
```

Create the superuser:

```bash
docker compose -f docker-compose.prod.yml exec web python manage.py createsuperuser
```

Create authorized web and Telegram users in Admin or shell:

```bash
docker compose -f docker-compose.prod.yml exec web python manage.py shell
```

```python
from django.contrib.auth import get_user_model
from apps.accounts.models import AuthorizedTelegramUser

User = get_user_model()
owner = User.objects.create_user(username="owner", password="change-me")
partner = User.objects.create_user(username="partner", password="change-me")
AuthorizedTelegramUser.objects.create(user=owner, telegram_user_id=123456789, name="Owner", is_active=True)
AuthorizedTelegramUser.objects.create(user=partner, telegram_user_id=987654321, name="Partner", is_active=True)
```

Keep `TELEGRAM_ALLOWED_USER_IDS` in `.env` as a second configuration layer.

## Enable HTTPS

Use Certbot or the VPS certificate workflow. After issuing the certificate, configure HTTPS using the example file:

```bash
cp deploy/nginx/conf.d/app.https.conf.example deploy/nginx/conf.d/app.https.conf
```

Edit the domain and certificate paths, then reload Nginx/container as appropriate.

Manual renewal example:

```bash
certbot renew --dry-run
```

Add renewal to cron according to the VPS setup.

## Update Routine

```bash
cd ~/apps/construction-finance-assistant
git pull
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml up -d
docker compose -f docker-compose.prod.yml exec web python manage.py migrate
docker compose -f docker-compose.prod.yml exec web python manage.py collectstatic --noinput
```

## Backups

Create backups outside the repository, for example `/backups/construction_finance` or `/backups/construction_finance_assistant`.

Use:

```bash
deploy/backup.sh
```

Test restore in a separate VPS or staging environment before trusting the backup.

## Useful Commands

Logs:

```bash
docker compose -f docker-compose.prod.yml logs -f web
docker compose -f docker-compose.prod.yml logs -f bot
docker compose -f docker-compose.prod.yml logs -f worker
```

Import master date or history when needed:

```bash
docker compose -f docker-compose.prod.yml exec web python manage.py import_counterparties --path /app/files/counterparties.xlsx
docker compose -f docker-compose.prod.yml exec web python manage.py import_payment_history --path /app/files/history.xlsx
```

Export final spreadsheet:

```bash
docker compose -f docker-compose.prod.yml exec web python manage.py export_approved_payments
```

Reconcile OFX:

```bash
docker compose -f docker-compose.prod.yml exec web python manage.py reconcile_ofx_transactions
```
