# Shared VPS Deployment

Use this guide when the VPS already hosts another app that owns Nginx, HTTPS, ports `80`, and `443`.

In this scenario, do not start this project's Nginx. The finance app runs internally on `127.0.0.1:8010`, and the existing VPS Nginx proxies a domain or subdomain to it.

## 1. Create A Subdomain

Example:

```text
finance.example.com
```

Point the DNS record to the VPS IP.

## 2. Clone The Project

Recommended path:

```bash
mkdir -p ~/apps
cd ~/apps
git clone git@github.com:engjaircorreia/construction-finance-assistant.git
cd construction-finance-assistant
```

If you prefer another directory, adjust the commands below.

## 3. Create `.env` On The VPS

Do not copy local `.env` to GitHub.

Create a real `.env` directly on the VPS:

```env
COMPOSE_PROJECT_NAME=construction_finance_assistant
DJANGO_SETTINGS_MODULE=config.settings.production
DJANGO_SECRET_KEY=replace-with-a-long-random-secret
DJANGO_DEBUG=False
DJANGO_ALLOWED_HOSTS=finance.example.com
DJANGO_CSRF_TRUSTED_ORIGINS=https://finance.example.com
DJANGO_ADMIN_URL=private-admin-path/
POSTGRES_DB=construction_finance
POSTGRES_USER=construction_finance
POSTGRES_PASSWORD=replace-with-a-strong-password
POSTGRES_HOST=db
POSTGRES_PORT=5432
REDIS_URL=redis://redis:6379/0
CELERY_BROKER_URL=redis://redis:6379/0
CELERY_RESULT_BACKEND=redis://redis:6379/1
TELEGRAM_BOT_TOKEN=real-token
TELEGRAM_ALLOWED_USER_IDS=owner_id,partner_id
OPENAI_API_KEY=real-key
DEFAULT_BANK_ACCOUNT=Main Account
DEFAULT_PAYER=Company
```

## 4. Start Containers Without Project Nginx

```bash
docker compose -f docker-compose.shared-vps.yml up -d --build
```

Run migrations and static collection:

```bash
docker compose -f docker-compose.shared-vps.yml exec web python manage.py migrate
docker compose -f docker-compose.shared-vps.yml exec web python manage.py collectstatic --noinput
```

Create an admin user:

```bash
docker compose -f docker-compose.shared-vps.yml exec web python manage.py createsuperuser
```

The `bot` service exposes no public port. It uses Telegram polling and accepts only authorized `telegram_user_id` records from the database or `TELEGRAM_ALLOWED_USER_IDS`.

## 5. Configure Existing Nginx

Add a server block to the VPS Nginx:

```nginx
server {
    listen 80;
    server_name finance.example.com;

    location / {
        proxy_pass http://127.0.0.1:8010;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Validate and reload:

```bash
nginx -t
systemctl reload nginx
```

## 6. HTTPS

If Certbot is already used on the VPS:

```bash
certbot --nginx -d finance.example.com
```

Confirm that the final Nginx block proxies HTTPS traffic to `127.0.0.1:8010` and forwards `X-Forwarded-Proto`.

## 7. Create Authorized Users

Create:

- web user for the owner;
- web user for the partner;
- `AuthorizedTelegramUser` for the owner;
- `AuthorizedTelegramUser` for the partner.

Shell example:

```bash
docker compose -f docker-compose.shared-vps.yml exec web python manage.py shell
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

## 8. Future Updates

```bash
cd ~/apps/construction-finance-assistant
git pull
docker compose -f docker-compose.shared-vps.yml build
docker compose -f docker-compose.shared-vps.yml up -d
docker compose -f docker-compose.shared-vps.yml exec web python manage.py migrate
docker compose -f docker-compose.shared-vps.yml exec web python manage.py collectstatic --noinput
```

## 9. Backups

Create backups outside the repository:

```bash
mkdir -p /backups/construction_finance_assistant
```

Run:

```bash
COMPOSE_FILE=docker-compose.shared-vps.yml BACKUP_ROOT=/backups/construction_finance_assistant deploy/backup.sh
```

## Important Notes

- Do not use `docker-compose.prod.yml` if it tries to start another Nginx on ports `80` and `443`.
- Use `COMPOSE_PROJECT_NAME=construction_finance_assistant` so containers and volumes do not mix with another app.
- Database and Redis have no public ports.
- Only local port `8010` is exposed on the VPS.
- The public domain should go through the existing Nginx.
