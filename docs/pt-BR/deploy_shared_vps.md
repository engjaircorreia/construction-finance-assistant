# Deploy em VPS com outro app em produção

Use este guia quando a VPS já tiver outro app ocupando Nginx, HTTPS, portas `80` e `443`.

Neste cenário, não suba o `nginx` deste projeto. O app financeiro roda internamente em `127.0.0.1:8010`, e o Nginx já existente na VPS encaminha o domínio/subdomínio para ele.

## 1. Preparar DNS

Crie um subdomínio para o sistema, por exemplo:

```text
financeiro.seudominio.com.br
```

O DNS deve apontar para o IP da VPS.

## 2. Baixar o projeto

Na VPS:

```bash
cd /opt
git clone git@github.com:engjaircorreia/construction-finance-assistant.git
cd construction-finance-assistant
```

Se preferir outro diretório, ajuste os comandos seguintes.

## 3. Criar `.env` direto na VPS

Não copie `.env` local para o GitHub.

```bash
nano .env
```

Modelo:

```dotenv
COMPOSE_PROJECT_NAME=construction_finance_assistant
APP_HOST_PORT=8010

DJANGO_SETTINGS_MODULE=config.settings.production
DJANGO_SECRET_KEY=gere-uma-chave-longa-e-unica
DJANGO_DEBUG=False
DJANGO_ALLOWED_HOSTS=financeiro.seudominio.com.br
DJANGO_CSRF_TRUSTED_ORIGINS=https://financeiro.seudominio.com.br
DJANGO_ADMIN_URL=caminho-privado-admin/
DJANGO_TIME_ZONE=America/Fortaleza

POSTGRES_DB=construction_finance
POSTGRES_USER=construction_finance
POSTGRES_PASSWORD=gere-uma-senha-forte
POSTGRES_HOST=db
POSTGRES_PORT=5432

REDIS_URL=redis://redis:6379/0
CELERY_BROKER_URL=redis://redis:6379/0
CELERY_RESULT_BACKEND=redis://redis:6379/1

TELEGRAM_BOT_TOKEN=token-do-bot
TELEGRAM_ALLOWED_USER_IDS=id_do_jair,id_do_socio

OPENAI_API_KEY=chave-openai
OPENAI_MODEL=gpt-4.1-mini

DEFAULT_PAYER=Empresa
DEFAULT_BANK_ACCOUNT=Conta Principal

AXES_ENABLED=True
AXES_FAILURE_LIMIT=5
AXES_COOLOFF_TIME=1
```

Use valores reais e fortes.

## 4. Subir containers sem Nginx próprio

```bash
docker compose -f docker-compose.shared-vps.yml up -d --build
```

Rodar migrações e estáticos:

```bash
docker compose -f docker-compose.shared-vps.yml exec web python manage.py migrate
docker compose -f docker-compose.shared-vps.yml exec web python manage.py collectstatic --noinput
```

Criar usuário admin:

```bash
docker compose -f docker-compose.shared-vps.yml exec web python manage.py createsuperuser
```

Testar localmente dentro da VPS:

```bash
curl -I http://127.0.0.1:8010/health/
```

Deve retornar `200 OK`.

Ver logs do bot do Telegram:

```bash
docker compose -f docker-compose.shared-vps.yml logs -f bot
```

O serviço `bot` não expõe porta pública; ele usa polling com a API do Telegram e aceita apenas os `telegram_user_id` autorizados no banco ou em `TELEGRAM_ALLOWED_USER_IDS`.

## 5. Configurar Nginx existente

No Nginx que já roda na VPS, adicione um novo server block para o subdomínio:

```nginx
server {
    listen 80;
    server_name financeiro.seudominio.com.br;

    client_max_body_size 20m;

    location / {
        proxy_pass http://127.0.0.1:8010;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_redirect off;
    }
}
```

Teste e recarregue:

```bash
sudo nginx -t
sudo systemctl reload nginx
```

## 6. HTTPS

Se o Certbot já é usado na VPS:

```bash
sudo certbot --nginx -d financeiro.seudominio.com.br
```

Depois confirme:

```bash
curl -I https://financeiro.seudominio.com.br/health/
```

## 7. Criar usuários autorizados

No Admin, crie:

- usuário web do Jair;
- usuário web do sócio;
- `AuthorizedTelegramUser` para o Jair;
- `AuthorizedTelegramUser` para o sócio.

Ou pelo shell:

```bash
docker compose -f docker-compose.shared-vps.yml exec web python manage.py shell
```

```python
from apps.accounts.models import AuthorizedTelegramUser
AuthorizedTelegramUser.objects.create(telegram_user_id=123456789, name="Jair", is_active=True)
AuthorizedTelegramUser.objects.create(telegram_user_id=987654321, name="Sócio", is_active=True)
```

## 8. Atualizações futuras

```bash
cd /opt/construction-finance-assistant
git pull
docker compose -f docker-compose.shared-vps.yml up -d --build
docker compose -f docker-compose.shared-vps.yml exec web python manage.py migrate
docker compose -f docker-compose.shared-vps.yml exec web python manage.py collectstatic --noinput
```

## 9. Backups

Crie backups fora do repositório:

```bash
mkdir -p /backups/construction_finance_assistant
```

Backup manual recomendado:

```bash
deploy/backup.sh
```

O script salva banco e arquivos persistentes, cria copias diarias e mensais, e aplica retencao simples.

Detalhes de restore e cron ficam em `docs/backup_restore.md`.

## Cuidados importantes

- Não use `docker-compose.prod.yml` se ele tentar subir outro Nginx nas portas `80` e `443`.
- Use `COMPOSE_PROJECT_NAME=construction_finance_assistant` para os containers/volumes não se misturarem com outro app.
- Banco e Redis ficam sem porta pública.
- Só a porta `8010` local fica acessível na própria VPS.
- O domínio público deve passar pelo Nginx existente.
