#!/usr/bin/env bash
# Разовая настройка сервера: CI деплоит под пользователем `deploy`, а не root.
#   sudo bash scripts/setup-deploy-user.sh "<публичный ключ CI>"
# Запускать из /opt/burnfat, уже обновлённого до версии, где есть scripts/deploy.sh.
#
# CI-ключ привязан к forced command scripts/deploy.sh: без shell, pty и проброса портов,
# только `deploy` / `verify`. Сам `deploy` в группе docker, а это на хосте всё ещё
# root-эквивалент — граница здесь в ключе, который умеет одно действие, а не в правах docker.
# ponytail: rootless docker закрыл бы и это, но ради двух ботов на одном VPS — лишний слой.
set -euo pipefail

pubkey=${1:?usage: $0 "<ssh public key>"}
app=/opt/burnfat
user=deploy

test -f "$app/scripts/deploy.sh" || { echo "в $app нет scripts/deploy.sh — сперва обнови код" >&2; exit 1; }

id "$user" >/dev/null 2>&1 || useradd --create-home --shell /bin/bash "$user"
usermod -aG docker "$user"

# Данные БД — в named volume, не в $app, так что chown базу не задевает.
chown -R "$user:$user" "$app"
chmod 600 "$app/.env"
chmod +x "$app/scripts/deploy.sh"

install -d -m 700 -o "$user" -g "$user" "/home/$user/.ssh"
opts="command=\"$app/scripts/deploy.sh\",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding"
printf '%s %s\n' "$opts" "$pubkey" >"/home/$user/.ssh/authorized_keys"
chown "$user:$user" "/home/$user/.ssh/authorized_keys"
chmod 600 "/home/$user/.ssh/authorized_keys"

echo "готово; проверка с машины с CI-ключом: ssh $user@<host> verify"
