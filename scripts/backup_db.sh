#!/usr/bin/env sh
# Дамп БД burnfat в ./backups/ — «вечная» запиннутая плотность (INV-2) и снапшоты
# истории (INV-3) перестают жить в одном незабэкапленном volume (REVIEW.md §G, слепая зона 4).
# ponytail: pg_dump через работающий db-контейнер; для 2-5 юзеров cron раз в сутки достаточно.
set -eu

OUT_DIR=${BACKUP_DIR:-backups}
USER=${POSTGRES_USER:-burnfat}
DB=${POSTGRES_DB:-burnfat}
mkdir -p "$OUT_DIR"
OUT="$OUT_DIR/burnfat_$(date +%Y%m%d_%H%M%S).sql.gz"

docker compose exec -T db pg_dump -U "$USER" "$DB" | gzip >"$OUT"
echo "✅ Бэкап: $OUT ($(du -h "$OUT" | cut -f1))"
