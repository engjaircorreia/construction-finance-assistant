#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.shared-vps.yml}"
BACKUP_ROOT="${BACKUP_ROOT:-/backups/inplant_finance}"
DAILY_RETENTION_DAYS="${DAILY_RETENTION_DAYS:-14}"
MONTHLY_RETENTION_DAYS="${MONTHLY_RETENTION_DAYS:-180}"
RUN_RETENTION="${RUN_RETENTION:-1}"

usage() {
  cat <<'EOF'
Uso:
  deploy/backup.sh [--no-retention]

Variaveis opcionais:
  PROJECT_DIR              diretorio do projeto
  COMPOSE_FILE             arquivo compose relativo ao projeto
  BACKUP_ROOT              diretorio externo para backups
  DAILY_RETENTION_DAYS     dias para manter backups diarios
  MONTHLY_RETENTION_DAYS   dias para manter backups mensais
  RUN_RETENTION            1 para aplicar retencao, 0 para nao aplicar

Exemplo:
  BACKUP_ROOT=/backups/inplant_finance deploy/backup.sh
EOF
}

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >&2
}

fail() {
  printf 'Erro: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "Comando obrigatorio nao encontrado: $1"
}

require_file() {
  [ -f "$1" ] || fail "Arquivo obrigatorio nao encontrado: $1"
}

parse_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --no-retention)
        RUN_RETENTION=0
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        fail "Argumento invalido: $1"
        ;;
    esac
    shift
  done
}

compose() {
  docker compose -f "$PROJECT_DIR/$COMPOSE_FILE" "$@"
}

ensure_backup_root_is_external() {
  local project_real backup_real
  project_real="$(cd "$PROJECT_DIR" && pwd -P)"
  mkdir -p "$BACKUP_ROOT"
  backup_real="$(cd "$BACKUP_ROOT" && pwd -P)"
  case "$backup_real" in
    "$project_real"|"$project_real"/*)
      fail "BACKUP_ROOT deve ficar fora do repositorio: $backup_real"
      ;;
  esac
}

check_services() {
  compose ps -q db >/dev/null || fail "Servico db nao encontrado no compose."
  compose ps -q web >/dev/null || fail "Servico web nao encontrado no compose."
  [ -n "$(compose ps -q db)" ] || fail "Servico db nao esta criado."
  [ -n "$(compose ps -q web)" ] || fail "Servico web nao esta criado."
}

prepare_directories() {
  mkdir -p \
    "$BACKUP_ROOT/daily/db" \
    "$BACKUP_ROOT/daily/storage" \
    "$BACKUP_ROOT/monthly/db" \
    "$BACKUP_ROOT/monthly/storage"
}

backup_database() {
  local timestamp target tmp
  timestamp="$1"
  target="$BACKUP_ROOT/daily/db/postgres_${timestamp}.sql.gz"
  tmp="${target}.tmp"

  log "Iniciando backup do PostgreSQL."
  compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --no-acl' \
    | gzip -9 > "$tmp"
  mv "$tmp" "$target"
  chmod 600 "$target"
  log "Backup do PostgreSQL criado: $target"
  printf '%s\n' "$target"
}

backup_storage() {
  local timestamp target tmp
  timestamp="$1"
  target="$BACKUP_ROOT/daily/storage/storage_${timestamp}.tar.gz"
  tmp="${target}.tmp"

  log "Iniciando backup dos arquivos persistentes."
  compose exec -T web tar --ignore-failed-read -czf - -C /app storage > "$tmp"
  mv "$tmp" "$target"
  chmod 600 "$target"
  log "Backup dos arquivos criado: $target"
  printf '%s\n' "$target"
}

create_monthly_copy_if_needed() {
  local source kind month_key target_dir target
  source="$1"
  kind="$2"
  month_key="$(date '+%Y%m')"
  target_dir="$BACKUP_ROOT/monthly/$kind"

  if find "$target_dir" -maxdepth 1 -type f -name "*_${month_key}_*" | grep -q .; then
    return
  fi

  target="$target_dir/$(basename "$source")"
  cp -p "$source" "$target"
  log "Copia mensal criada: $target"
}

apply_retention() {
  [ "$RUN_RETENTION" = "1" ] || return

  log "Aplicando retencao de backups."
  find "$BACKUP_ROOT/daily/db" -type f -name 'postgres_*.sql.gz' -mtime +"$DAILY_RETENTION_DAYS" -delete
  find "$BACKUP_ROOT/daily/storage" -type f -name 'storage_*.tar.gz' -mtime +"$DAILY_RETENTION_DAYS" -delete
  find "$BACKUP_ROOT/monthly/db" -type f -name 'postgres_*.sql.gz' -mtime +"$MONTHLY_RETENTION_DAYS" -delete
  find "$BACKUP_ROOT/monthly/storage" -type f -name 'storage_*.tar.gz' -mtime +"$MONTHLY_RETENTION_DAYS" -delete
}

main() {
  parse_args "$@"
  require_command docker
  require_file "$PROJECT_DIR/$COMPOSE_FILE"
  require_file "$PROJECT_DIR/.env"
  ensure_backup_root_is_external
  prepare_directories
  check_services

  local timestamp db_backup storage_backup
  timestamp="$(date '+%Y%m%d_%H%M%S')"
  db_backup="$(backup_database "$timestamp" | tail -n 1)"
  storage_backup="$(backup_storage "$timestamp" | tail -n 1)"
  create_monthly_copy_if_needed "$db_backup" "db"
  create_monthly_copy_if_needed "$storage_backup" "storage"
  apply_retention

  log "Backup concluido."
}

main "$@"
