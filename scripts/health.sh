#!/usr/bin/env bash
set -Eeuo pipefail
[[ ${EUID} -eq 0 ]] || { echo "Запустите с sudo"; exit 1; }
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
systemctl is-active wg-quick@wg0 freenetvpn-xray
wg show wg0
/usr/local/bin/xray run -test -config /etc/xray/config.json
printf '\nПорты из настроек:\n'
awk -F= '$1=="SERVER_HOST" || $1=="WG_PORT" || $1=="VLESS_PORT"' "$root/.env"
printf '\nСвободное место:\n'
df -h /
