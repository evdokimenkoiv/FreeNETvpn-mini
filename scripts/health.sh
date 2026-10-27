#!/usr/bin/env bash
set -Eeuo pipefail
[[ ${EUID} -eq 0 ]] || { echo "Запустите с sudo"; exit 1; }
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
systemctl is-active wg-quick@wg0 freenetvpn-xray freenetvpn-panel
wg show wg0
/usr/local/bin/xray run -test -config /etc/xray/config.json
panel_http="$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' http://127.0.0.1:8080/)"
[[ "$panel_http" == 401 ]] || { echo "Панель вернула HTTP $panel_http вместо 401"; exit 1; }
printf 'Панель на 127.0.0.1:8080 отвечает и требует авторизацию.\n'
printf '\nПорты из настроек:\n'
awk -F= '$1=="SERVER_HOST" || $1=="WG_PORT" || $1=="VLESS_PORT"' "$root/.env"
printf '\nСвободное место:\n'
df -h /
