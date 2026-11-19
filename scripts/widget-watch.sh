#!/bin/sh
# Ждём, когда Telegram вернёт HTML-виджеты (D-92). Раз в день — тихий тестовый виджет владельцу:
# отказ NOT_ALLOWED — молчим; прошёл — удаляем его, пишем «виджеты вернули» и больше не проверяем.
# ponytail: cron + curl, как /opt/healthcheck.sh. Виджеты вернули — скрипт и cron можно удалить.
#
# Живёт на сервере как /opt/widget-watch.sh, запускается из /etc/cron.d/widget-watch.
# Эталон — здесь; деплой его НЕ раскладывает, после правки:
#   scp scripts/widget-watch.sh root@89.124.97.90:/opt/widget-watch.sh
set -u
DONE=/var/lib/widget-watch.done
[ -e "$DONE" ] && exit 0
TOKEN=$(sed -n s/^BOT_TOKEN=//p /opt/burnfat/.env | tr -d "\r\"'")
CHAT=$(sed -n s/^OWNER_TELEGRAM_ID=//p /opt/finance_bot/.env | tr -d "\r\"'")
[ -n "$TOKEN" ] && [ -n "$CHAT" ] || exit 0
API="https://api.telegram.org/bot$TOKEN"

r=$(curl -s -m 20 -X POST "$API/sendWidget" -d chat_id="$CHAT" -d disable_notification=true \
    -d allow_queries=true --data-urlencode 'html=<p>t</p>')
case "$r" in
  *'"ok":true'*)
    mid=$(printf '%s' "$r" | sed -n 's/.*"message_id":\([0-9]*\).*/\1/p')
    [ -n "$mid" ] && curl -s -m 15 -o /dev/null -X POST "$API/deleteMessage" \
      -d chat_id="$CHAT" -d message_id="$mid"
    curl -s -m 15 -o /dev/null -X POST "$API/sendMessage" -d chat_id="$CHAT" --data-urlencode \
      'text=🧩 Telegram вернул HTML-виджеты: тестовый прошёл. Бот уже шлёт их сам; проверь, что рисуются.'
    touch "$DONE"
    echo "$(date -u +%FT%TZ) виджеты вернули"
    ;;
  *WIDGET_NOT_ALLOWED*) echo "$(date -u +%FT%TZ) всё ещё выключены" ;;
  *) echo "$(date -u +%FT%TZ) неожиданный ответ: $(printf '%s' "$r" | head -c 200)" ;;
esac
