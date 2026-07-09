# Segurança do sistema

Este sistema é interno. Somente o proprietário e o sócio devem acessar o navegador e usar o bot do Telegram.

## O que já fica no projeto

Configuração Django:

- `DEBUG` por variável de ambiente.
- `SECRET_KEY` por variável de ambiente.
- `ALLOWED_HOSTS` por variável de ambiente.
- `CSRF_TRUSTED_ORIGINS` por variável de ambiente.
- Admin em URL configurável por `DJANGO_ADMIN_URL`.
- Cookies seguros em produção.
- Proteção CSRF ativa.
- `django-axes` preparado para limitar tentativas de login.
- Headers básicos de segurança:
  - `SECURE_CONTENT_TYPE_NOSNIFF=True`.
  - `SECURE_REFERRER_POLICY=same-origin`.
  - `X_FRAME_OPTIONS=DENY`.

Arquivos e segredos:

- `.env` está no `.gitignore`.
- `.env.example` pode ir para o GitHub porque não tem segredos reais.
- Pastas de runtime e arquivos gerados ficam ignorados:
  - `storage/`.
  - `uploads/`.
  - `exports/`.
  - `comprovantes/`.
  - `ofx/`.
  - `planilhas_geradas/`.
  - `generated/`.
  - `private/`.

Docker Compose:

- PostgreSQL usa `expose`, não publica porta para fora.
- Redis usa `expose`, não publica porta para fora.
- O serviço web é o único exposto localmente no desenvolvimento.

## O que configurar somente na VPS

Criar o `.env` real diretamente na VPS.

Nunca subir para o GitHub:

- `DJANGO_SECRET_KEY`.
- `POSTGRES_PASSWORD`.
- `TELEGRAM_BOT_TOKEN`.
- `OPENAI_API_KEY`.
- qualquer token, senha ou chave privada.

Variáveis importantes em produção:

```env
DJANGO_SETTINGS_MODULE=config.settings.production
DJANGO_SECRET_KEY=valor-real-longo-e-aleatorio
DJANGO_DEBUG=False
DJANGO_ALLOWED_HOSTS=seudominio.com.br
DJANGO_CSRF_TRUSTED_ORIGINS=https://seudominio.com.br
DJANGO_ADMIN_URL=caminho-privado-admin/
POSTGRES_PASSWORD=senha-real-forte
TELEGRAM_BOT_TOKEN=token-real-do-bot
TELEGRAM_ALLOWED_USER_IDS=id_do_proprietario,id_do_socio
OPENAI_API_KEY=chave-real-openai
```

Infraestrutura da VPS:

- Usar HTTPS com Nginx e Certbot/Let's Encrypt.
- Não expor PostgreSQL publicamente.
- Não expor Redis publicamente.
- Liberar no firewall apenas portas necessárias:
  - `80` e `443` para HTTP/HTTPS.
  - `22` para SSH, de preferência restrito e com chave.
- Desativar login SSH por senha quando possível.
- Instalar Fail2ban.
- Configurar backups do banco e arquivos.

## Regras do Telegram

- O bot só deve processar mensagens de `telegram_user_id` autorizado.
- No MVP, apenas proprietário e sócio ficam autorizados.
- Mensagens de outras pessoas devem ser ignoradas ou recusadas.
- O bot deve operar em conversa privada.
- Mensagens vindas de grupos devem ser ignoradas.

## Checklist antes do deploy

- `.env` real existe somente na VPS.
- `DEBUG=False`.
- `ALLOWED_HOSTS` não contém `*`, `localhost` ou `127.0.0.1`.
- `CSRF_TRUSTED_ORIGINS` aponta para HTTPS do domínio real.
- `DJANGO_ADMIN_URL` foi trocada para uma rota privada.
- HTTPS está funcionando.
- PostgreSQL e Redis não têm porta pública.
- Usuários Django foram criados manualmente.
- `telegram_user_id` do proprietário e do sócio foram cadastrados.
- Logs não mostram tokens, senhas ou comprovantes completos.

