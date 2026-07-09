# Produção na VPS

Este guia parte do princípio de que o domínio já aponta para o IP da VPS e que Docker com Compose já está instalado.

Substitua `sistema.example.com` pelo domínio real antes de subir.

## Arquivos de produção

- `docker-compose.prod.yml`: serviços de produção.
- `deploy/nginx/conf.d/inplant.http.conf`: Nginx em HTTP para primeiro boot e emissão do certificado.
- `deploy/nginx/conf.d/inplant.https.conf.example`: Nginx HTTPS para usar depois do Certbot.
- `.env`: deve ser criado direto na VPS e nunca versionado.

## Checklist do `.env` na VPS

Crie o arquivo no servidor:

```bash
nano .env
```

Modelo mínimo:

```dotenv
DJANGO_SETTINGS_MODULE=config.settings.production
DJANGO_SECRET_KEY=gere-uma-chave-longa-e-unica
DJANGO_DEBUG=False
DJANGO_ALLOWED_HOSTS=sistema.example.com
DJANGO_CSRF_TRUSTED_ORIGINS=https://sistema.example.com
DJANGO_ADMIN_URL=caminho-privado-admin/
DJANGO_TIME_ZONE=America/Fortaleza

POSTGRES_DB=inplant
POSTGRES_USER=inplant
POSTGRES_PASSWORD=gere-uma-senha-forte
POSTGRES_HOST=db
POSTGRES_PORT=5432

REDIS_URL=redis://redis:6379/0
CELERY_BROKER_URL=redis://redis:6379/0
CELERY_RESULT_BACKEND=redis://redis:6379/1

TELEGRAM_BOT_TOKEN=token-do-bot
TELEGRAM_ALLOWED_USER_IDS=123456789,987654321

OPENAI_API_KEY=chave-openai
OPENAI_MODEL=gpt-4.1-mini

DEFAULT_PAYER=Empresa
DEFAULT_BANK_ACCOUNT=Conta Principal

AXES_ENABLED=True
AXES_FAILURE_LIMIT=5
AXES_COOLOFF_TIME=1
```

Checklist:

- `DJANGO_SECRET_KEY` deve ser longa, aleatória e diferente de qualquer exemplo.
- `DJANGO_ALLOWED_HOSTS` deve conter apenas o domínio real.
- `DJANGO_CSRF_TRUSTED_ORIGINS` deve usar `https://`.
- `DJANGO_ADMIN_URL` deve ser um caminho privado, não `admin/` nem `controle-interno/`.
- `POSTGRES_PASSWORD` deve ser forte.
- `TELEGRAM_ALLOWED_USER_IDS` deve conter apenas você e seu sócio.
- O `.env` deve ser criado na VPS, não enviado ao GitHub.

## Primeiro deploy

Edite `deploy/nginx/conf.d/inplant.http.conf` e troque `sistema.example.com` pelo domínio real.

Suba os serviços em HTTP:

```bash
docker compose -f docker-compose.prod.yml up -d --build db redis web worker beat nginx
```

Rode migrações e arquivos estáticos:

```bash
docker compose -f docker-compose.prod.yml exec web python manage.py migrate
docker compose -f docker-compose.prod.yml exec web python manage.py collectstatic --noinput
```

Crie o superusuário:

```bash
docker compose -f docker-compose.prod.yml exec web python manage.py createsuperuser
```

Crie os usuários web autorizados para você e seu sócio pelo Admin ou shell:

```bash
docker compose -f docker-compose.prod.yml exec web python manage.py shell
```

Exemplo no shell:

```python
from django.contrib.auth import get_user_model
User = get_user_model()
User.objects.create_user("jair", password="senha-forte")
User.objects.create_user("socio", password="outra-senha-forte")
```

Crie os Telegram IDs autorizados:

```python
from apps.accounts.models import AuthorizedTelegramUser
AuthorizedTelegramUser.objects.create(telegram_user_id=123456789, name="Jair", username="jair", is_active=True)
AuthorizedTelegramUser.objects.create(telegram_user_id=987654321, name="Sócio", username="socio", is_active=True)
```

Mantenha também `TELEGRAM_ALLOWED_USER_IDS` no `.env` com os mesmos IDs como segunda camada de configuração.

## HTTPS com Certbot

Com o Nginx HTTP rodando, emita o certificado:

```bash
docker compose -f docker-compose.prod.yml run --rm certbot certonly \
  --webroot \
  --webroot-path /var/www/certbot \
  -d sistema.example.com \
  --email seu-email@example.com \
  --agree-tos \
  --no-eff-email
```

Depois:

```bash
cp deploy/nginx/conf.d/inplant.https.conf.example deploy/nginx/conf.d/inplant.https.conf
```

Edite `inplant.https.conf` e troque `sistema.example.com` pelo domínio real. Remova ou renomeie o arquivo HTTP inicial para evitar conflito:

```bash
mv deploy/nginx/conf.d/inplant.http.conf deploy/nginx/conf.d/inplant.http.conf.disabled
docker compose -f docker-compose.prod.yml restart nginx
```

Teste:

```bash
curl -I https://sistema.example.com/health/
```

Renovação manual:

```bash
docker compose -f docker-compose.prod.yml run --rm certbot renew --webroot --webroot-path /var/www/certbot
docker compose -f docker-compose.prod.yml restart nginx
```

Coloque a renovação no cron da VPS:

```cron
15 3 * * * cd /caminho/do/projeto && docker compose -f docker-compose.prod.yml run --rm certbot renew --webroot --webroot-path /var/www/certbot && docker compose -f docker-compose.prod.yml restart nginx
```

## Rotina de atualização

```bash
git pull
docker compose -f docker-compose.prod.yml build web worker beat
docker compose -f docker-compose.prod.yml up -d
docker compose -f docker-compose.prod.yml exec web python manage.py migrate
docker compose -f docker-compose.prod.yml exec web python manage.py collectstatic --noinput
docker compose -f docker-compose.prod.yml ps
```

## Backups

Crie uma pasta fora do repositório ou use `/backups/inplant`:

```bash
mkdir -p /backups/inplant
```

Para o deploy compartilhado atual, prefira o script:

```bash
BACKUP_ROOT=/backups/inplant_finance COMPOSE_FILE=docker-compose.shared-vps.yml deploy/backup.sh
```

Para este `docker-compose.prod.yml`, use:

```bash
BACKUP_ROOT=/backups/inplant COMPOSE_FILE=docker-compose.prod.yml deploy/backup.sh
```

Veja tambem `docs/backup_restore.md` para restore e cron.

Backup do banco:

```bash
docker compose -f docker-compose.prod.yml exec -T db pg_dump \
  -U "$POSTGRES_USER" \
  -d "$POSTGRES_DB" \
  > /backups/inplant/db_$(date +%Y%m%d_%H%M%S).sql
```

Se as variáveis do shell da VPS não estiverem carregadas, use os nomes configurados no `.env`:

```bash
docker compose -f docker-compose.prod.yml exec -T db pg_dump -U inplant -d inplant \
  > /backups/inplant/db_$(date +%Y%m%d_%H%M%S).sql
```

Backup dos arquivos gerados, uploads, comprovantes e planilhas:

```bash
docker run --rm \
  -v inplantengenharia_app_storage:/app/storage:ro \
  -v /backups/inplant:/backup \
  alpine tar czf /backup/storage_$(date +%Y%m%d_%H%M%S).tar.gz -C /app storage
```

Backup dos certificados:

```bash
docker run --rm \
  -v inplantengenharia_certbot_conf:/etc/letsencrypt:ro \
  -v /backups/inplant:/backup \
  alpine tar czf /backup/letsencrypt_$(date +%Y%m%d_%H%M%S).tar.gz -C /etc letsencrypt
```

Teste de restauração deve ser feito em uma VPS ou ambiente separado antes de confiar no backup.

## Comandos úteis

Logs:

```bash
docker compose -f docker-compose.prod.yml logs -f web
docker compose -f docker-compose.prod.yml logs -f worker
docker compose -f docker-compose.prod.yml logs -f nginx
```

Importar cadastros e histórico, se necessário:

```bash
docker compose -f docker-compose.prod.yml exec web python manage.py import_counterparties
docker compose -f docker-compose.prod.yml exec web python manage.py import_payment_history
```

Exportar planilha final:

```bash
docker compose -f docker-compose.prod.yml exec web python manage.py export_approved_payments
```

Conciliar OFX:

```bash
docker compose -f docker-compose.prod.yml exec web python manage.py reconcile_ofx_transactions
```
