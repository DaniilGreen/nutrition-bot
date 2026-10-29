#!/usr/bin/env bash
# Forced command CI-ключа на сервере (см. scripts/setup-deploy-user.sh): ключ из GitHub Actions
# не получает shell, а может попросить ровно одно из двух — `deploy` или `verify`.
#
# Всё тело — в main(): `git reset --hard` переписывает и этот файл, а bash читает скрипт
# по ходу выполнения; без обёртки дочитал бы уже новую версию с середины.
set -euo pipefail

main() {
  cd /opt/burnfat
  local compose=(docker compose -f docker-compose.yml -f docker-compose.prod.yml)

  case "${SSH_ORIGINAL_COMMAND:-}" in
    deploy)
      git fetch --quiet origin main
      git reset --hard --quiet origin/main   # деплой = точная копия main
      "${compose[@]}" up -d --build
      docker image prune -f >/dev/null       # 10 ГБ диска: старые слои иначе забьют его
      # Кэш сборки `image prune` не трогает, а он растёт ~120 МБ за деплой:
      # шесть деплоев за 05.09.2026 дали 739 МБ и загнали диск в 96%.
      # Не чистим догола — на нём держится тридцатисекундный деплой; держим потолок.
      docker builder prune -f --keep-storage=512MB >/dev/null 2>&1 || true
      ;;
    verify)
      # Ждём выхода бота на polling до 90 с, а не ровно 20: 21.10.2026 бот поднялся за 43 с
      # (миграции + пересоздание контейнеров), и проверка покраснела на живом деплое.
      "${compose[@]}" ps
      for _ in $(seq 1 18); do
        "${compose[@]}" logs --since 3m bot | grep -q "Run polling" && return 0
        sleep 5
      done
      echo "бот не вышел на polling за 90 с" >&2
      return 1
      ;;
    *)
      echo "разрешено только: deploy | verify" >&2
      return 1
      ;;
  esac
}

main
exit
