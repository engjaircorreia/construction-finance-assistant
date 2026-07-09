#!/usr/bin/env sh
set -eu

if [ "${POSTGRES_HOST:-}" != "" ]; then
  echo "Waiting for PostgreSQL at ${POSTGRES_HOST}:${POSTGRES_PORT:-5432}..."
  until nc -z "${POSTGRES_HOST}" "${POSTGRES_PORT:-5432}"; do
    sleep 1
  done
fi

exec "$@"

