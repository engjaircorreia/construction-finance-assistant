# Backup e restore

Este documento descreve a rotina de backup do sistema financeiro.

Use este fluxo na VPS. Nao salve backups dentro do repositorio e nao envie
backups para o GitHub.

## O que entra no backup

- Banco PostgreSQL.
- Volume de arquivos persistentes em `/app/storage`, incluindo:
  - uploads;
  - comprovantes;
  - OFX;
  - planilhas geradas;
  - arquivos privados usados pela aplicacao.

## Local dos backups na VPS

Padrao:

```bash
/backups/inplant_finance
```

Estrutura criada pelo script:

```text
/backups/inplant_finance/
  daily/
    db/
    storage/
  monthly/
    db/
    storage/
```

## Backup manual

Na VPS:

```bash
cd ~/apps/inplantengenharia_finance
deploy/backup.sh
```

Se precisar usar outro compose ou outro destino:

```bash
cd ~/apps/inplantengenharia_finance
COMPOSE_FILE=docker-compose.shared-vps.yml \
BACKUP_ROOT=/backups/inplant_finance \
deploy/backup.sh
```

O script:

- exige `.env` local na VPS;
- usa o servico `db` para gerar `pg_dump`;
- usa o servico `web` para compactar `/app/storage`;
- nao imprime senha do banco;
- nao imprime token do Telegram;
- nao imprime chave da OpenAI;
- cria nomes com data/hora;
- mantem backups diarios por 14 dias;
- cria uma copia mensal por mes;
- mantem backups mensais por 180 dias.

## Agendamento com cron

Exemplo para rodar todo dia as 02:15:

```cron
15 2 * * * cd /home/deploy/apps/inplantengenharia_finance && /home/deploy/apps/inplantengenharia_finance/deploy/backup.sh >> /var/log/inplant_finance_backup.log 2>&1
```

Antes de colocar no cron, rode manualmente e confira se os arquivos foram
criados em `/backups/inplant_finance`.

## Como listar backups

```bash
find /backups/inplant_finance -type f -maxdepth 4 -printf '%TY-%Tm-%Td %TH:%TM %p\n' | sort
```

## Copiar backup para maquina local

Exemplo:

```bash
rsync -av deploy@srv1772642:/backups/inplant_finance/ ./backups/inplant_finance/
```

Nao coloque a pasta local de backups dentro do repositorio.

## Restore em ambiente controlado

Nao execute restore diretamente na producao sem antes testar em ambiente
separado.

O fluxo recomendado e:

1. Copiar os arquivos de backup para uma maquina local ou staging.
2. Subir uma copia da aplicacao.
3. Restaurar banco.
4. Restaurar storage.
5. Rodar checks e abrir o sistema.
6. Somente depois decidir se precisa restaurar producao.

## Simulacao local de restore

Use uma copia separada do projeto ou, no minimo, um `COMPOSE_PROJECT_NAME`
diferente para nao misturar volumes com o seu ambiente local do dia a dia.

Exemplo em uma pasta de teste:

```bash
cd ~/tmp
git clone git@github.com:codemapstartup/inplantengenharia_finance.git inplant_restore_test
cd inplant_restore_test
cp .env.example .env
```

Edite o `.env` de teste com valores locais e sem segredos reais. Use um nome
de projeto separado:

```dotenv
COMPOSE_PROJECT_NAME=inplant_restore_test
DJANGO_SETTINGS_MODULE=config.settings.development
DJANGO_DEBUG=True
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1
DJANGO_CSRF_TRUSTED_ORIGINS=http://localhost:8000
POSTGRES_DB=inplant
POSTGRES_USER=inplant
POSTGRES_PASSWORD=senha-local-de-teste
POSTGRES_HOST=db
POSTGRES_PORT=5432
TELEGRAM_BOT_TOKEN=
OPENAI_API_KEY=
```

Suba os containers e prepare o banco vazio:

```bash
docker compose up -d --build db redis web
docker compose exec web python manage.py migrate
```

Copie os backups para uma pasta fora do repositorio, por exemplo:

```bash
mkdir -p ~/backups/inplant_restore_test
rsync -av deploy@srv1772642:/backups/inplant_finance/ ~/backups/inplant_restore_test/
```

Escolha um par de arquivos com o mesmo timestamp, por exemplo:

```text
~/backups/inplant_restore_test/daily/db/postgres_YYYYMMDD_HHMMSS.sql.gz
~/backups/inplant_restore_test/daily/storage/storage_YYYYMMDD_HHMMSS.tar.gz
```

Restaure primeiro o banco, depois os arquivos:

```bash
gunzip -c ~/backups/inplant_restore_test/daily/db/postgres_YYYYMMDD_HHMMSS.sql.gz \
  | docker compose exec -T db psql -U inplant -d inplant

docker compose exec -T web sh -c 'rm -rf /app/storage/*'
cat ~/backups/inplant_restore_test/daily/storage/storage_YYYYMMDD_HHMMSS.tar.gz \
  | docker compose exec -T web tar xzf - -C /app
```

Confira a restauracao:

```bash
docker compose exec web python manage.py check
docker compose exec web python manage.py shell -c "from apps.payments.models import Payment; from apps.documents.models import UploadedFile; print('pagamentos=', Payment.objects.count()); print('arquivos=', UploadedFile.objects.count())"
```

Abra `http://localhost:8000/` e valide login, lancamentos, rascunhos,
planilhas geradas e OFX importados.

Ao terminar a simulacao:

```bash
docker compose down -v
```

## Restaurar banco em ambiente local/staging

Exemplo usando um backup `.sql.gz`:

```bash
cd ~/apps/inplantengenharia_finance
gunzip -c /caminho/seguro/postgres_YYYYMMDD_HHMMSS.sql.gz \
  | docker compose exec -T db psql -U inplant -d inplant
```

Se o banco local nao estiver vazio, recrie o banco no ambiente de teste antes
de restaurar. Nao faca isso na producao sem uma janela de manutencao e outro
backup recente validado.

## Restaurar arquivos em ambiente local/staging

Exemplo usando um backup `storage_*.tar.gz`:

```bash
cd ~/apps/inplantengenharia_finance
docker compose exec -T web sh -c 'rm -rf /app/storage/*'
cat /caminho/seguro/storage_YYYYMMDD_HHMMSS.tar.gz \
  | docker compose exec -T web tar xzf - -C /app
```

Depois confira:

```bash
docker compose exec web python manage.py check
docker compose exec web python manage.py shell -c "from apps.documents.models import UploadedFile; print(UploadedFile.objects.count())"
```

## Checklist apos restore

- Aplicacao sobe sem erro.
- Login web funciona.
- Rascunhos aparecem.
- Pagamentos aparecem.
- Arquivos anexados abrem internamente quando aplicavel.
- Planilhas geradas continuam registradas.
- OFX importados continuam registrados.
- `python manage.py check` passa.

## Cuidados

- Backup sem restore testado ainda e apenas uma promessa.
- Nunca coloque backup em pasta publica do servidor.
- Nunca compacte `.env` junto com os backups da aplicacao.
- Nunca cole tokens, senhas ou chaves em chamados, commits ou logs.
