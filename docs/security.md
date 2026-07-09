# System Security

This is an internal system. Only authorized company users should access the web interface and Telegram bot.

## What The Project Already Provides

Django configuration:

- `DEBUG` controlled by environment variable.
- `SECRET_KEY` controlled by environment variable.
- `ALLOWED_HOSTS` controlled by environment variable.
- `CSRF_TRUSTED_ORIGINS` controlled by environment variable.
- Admin URL configurable through `DJANGO_ADMIN_URL`.
- Secure cookies in production.
- Active CSRF protection.
- `django-axes` prepared to limit login attempts.
- Basic security headers:
  - `SECURE_CONTENT_TYPE_NOSNIFF=True`.
  - `SECURE_REFERRER_POLICY=same-origin`.
  - `X_FRAME_OPTIONS=DENY`.

Files and secrets:

- `.env` is ignored by Git.
- `.env.example` can be committed because it has no real secrets.
- Runtime folders and generated files are ignored:
  - `storage/`.
  - `uploads/`.
  - `exports/`.
  - `receipts/`.
  - `ofx/`.
  - `spreadsheets_geradas/`.
  - `generated/`.
  - `private/`.

Docker Compose:

- PostgreSQL uses `expose` and does not publish a public port.
- Redis uses `expose` and does not publish a public port.
- The web service is the only service exposed locally during development.

## What To Configure Only On The VPS

Create the real `.env` directly on the VPS.

Never upload these to GitHub:

- `DJANGO_SECRET_KEY`.
- `POSTGRES_PASSWORD`.
- `TELEGRAM_BOT_TOKEN`.
- `OPENAI_API_KEY`.
- Any token, password, or private key.

Important production variables:

```env
DJANGO_SETTINGS_MODULE=config.settings.production
DJANGO_SECRET_KEY=long-random-production-value
DJANGO_DEBUG=False
DJANGO_ALLOWED_HOSTS=your-domain.com
DJANGO_CSRF_TRUSTED_ORIGINS=https://your-domain.com
DJANGO_ADMIN_URL=private-admin-path/
POSTGRES_PASSWORD=strong-production-password
TELEGRAM_BOT_TOKEN=real-bot-token
TELEGRAM_ALLOWED_USER_IDS=owner_id,partner_id
OPENAI_API_KEY=real-openai-key
```

VPS infrastructure:

- Use HTTPS with Nginx and Certbot/Let's Encrypt.
- Do not expose PostgreSQL publicly.
- Do not expose Redis publicly.
- Open only required firewall ports:
  - `80` and `443` for HTTP/HTTPS.
  - `22` for SSH, preferably restricted and key-based.
- Disable SSH password login when possible.
- Install Fail2ban.
- Configure database and file backups.

## Telegram Rules

- The bot should process only authorized `telegram_user_id` values.
- In the MVP, only the owner and partner are authorized.
- Messages from other people should be ignored or rejected.
- The bot should operate in private conversations.
- Group messages should be ignored.

## Pre-Deploy Checklist

- Real `.env` exists only on the VPS.
- `DEBUG=False`.
- `ALLOWED_HOSTS` does not contain `*`, `localhost`, or `127.0.0.1`.
- `CSRF_TRUSTED_ORIGINS` points to the real HTTPS domain.
- `DJANGO_ADMIN_URL` was changed to a private route.
- HTTPS works.
- PostgreSQL and Redis have no public ports.
- Django users were created manually.
- Authorized Telegram users were registered.
- Logs do not show tokens, passwords, or full receipt content.
