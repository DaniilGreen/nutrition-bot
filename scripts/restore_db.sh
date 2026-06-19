#!/usr/bin/env sh
# Восстановление БД burnfat из дампа backup_db.sh.
# Использование: scripts/restore_db.sh backups/burnfat_YYYYMMDD_HHMMSS.sql.gz
set -eu

F=${1:?укажи файл дампа: make restore f=backups/burnfat_YYYYMMDD_HHMMSS.sql.gz}
USER=${POSTGRES_USER:-burnfat}
DB=${POSTGRES_DB:-burnfat}

gunzip -c "$F" | docker compose exec -T db psql -U "$USER" -d "$DB"
echo "✅ Восстановлено из $F"
