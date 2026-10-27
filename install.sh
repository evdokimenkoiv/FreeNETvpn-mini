#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

INSTALL_DIR=/opt/freenetvpn-mini
XRAY_VERSION=26.3.27
MIN_FREE_MB=1024
phase=bootstrap

failure() {
  code=$?
  trap - ERR
  printf '\nОшибка установки на этапе %s (код %s).\n' "$phase" "$code" >&2
  printf 'Исправьте сообщение выше и повторите команду. Ключи и клиентские конфигурации сохраняются.\n' >&2
  exit "$code"
}
trap failure ERR

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'EOF'
FreeNETvpn-mini: преднастроенные WireGuard и VLESS REALITY.
Использование: sudo bash install.sh [--no-firewall]
Поддерживается Ubuntu 22.04/24.04/26.04 LTS x86_64 с systemd и публичным IPv4.
Переменная SERVER_HOST задаёт публичный IPv4 или DNS-имя; без неё адрес определяется автоматически.
EOF
  exit 0
fi

[[ ${EUID} -eq 0 ]] || { echo "Запустите: sudo bash install.sh"; exit 1; }
firewall=1
for argument in "$@"; do
  case "$argument" in
    --no-firewall) firewall=0 ;;
    *) echo "Использование: sudo bash install.sh [--no-firewall]"; exit 1 ;;
  esac
done

source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ ! -f "${source_dir}/tools/configure.py" ]]; then
  if [[ -f "${INSTALL_DIR}/install.sh" ]]; then
    exec bash "${INSTALL_DIR}/install.sh" "$@"
  fi
  [[ ! -e "$INSTALL_DIR" ]] || { echo "Каталог $INSTALL_DIR уже существует, но установка не найдена"; exit 1; }
  ref="${FREENET_MINI_REF:-main}"
  [[ "$ref" =~ ^[a-zA-Z0-9._/-]+$ ]] || { echo "Недопустимый FREENET_MINI_REF"; exit 1; }
  apt-get update
  apt-get install -y --no-install-recommends ca-certificates curl
  stage="$(mktemp -d "${INSTALL_DIR}.download.XXXXXX")"
  archive="$(mktemp)"
  trap 'rm -f -- "$archive"; rm -rf -- "$stage"' EXIT
  curl --fail --location --retry 3 --connect-timeout 15 \
    "https://api.github.com/repos/evdokimenkoiv/FreeNETvpn-mini/tarball/${ref}" -o "$archive"
  tar --extract --gzip --file "$archive" --strip-components=1 --directory "$stage" --no-same-owner
  [[ -f "$stage/tools/configure.py" && -f "$stage/install.sh" ]] || { echo "Неполный архив проекта"; exit 1; }
  mv -T --no-clobber -- "$stage" "$INSTALL_DIR"
  rm -f -- "$archive"
  trap - EXIT
  exec bash "${INSTALL_DIR}/install.sh" "$@"
fi

cd "$source_dir"
[[ "$source_dir" == "$INSTALL_DIR" ]] || { echo "Установка поддерживается только из $INSTALL_DIR"; exit 1; }

# shellcheck disable=SC1091
source /etc/os-release
[[ "$ID" == ubuntu && ( "$VERSION_ID" == 22.04 || "$VERSION_ID" == 24.04 || "$VERSION_ID" == 26.04 ) ]] || {
  echo "Поддерживаются Ubuntu 22.04, 24.04 и 26.04 LTS"; exit 1;
}
[[ "$(uname -m)" == x86_64 ]] || { echo "Этот выпуск поддерживает только x86_64"; exit 1; }
[[ -d /run/systemd/system ]] || { echo "Требуется systemd"; exit 1; }
free_mb="$(df -Pk / | awk 'NR==2 {print int($4/1024)}')"
(( free_mb >= MIN_FREE_MB )) || {
  echo "Нужно не менее ${MIN_FREE_MB} МиБ свободного места на /, сейчас доступно ${free_mb} МиБ"; exit 1;
}

phase=packages
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  python3 openssl ca-certificates curl unzip wireguard-tools iptables iproute2 ufw
apt-get clean

phase=configuration
default_interface="$(ip -4 route show default | awk 'NR==1 {print $5}')"
[[ -n "$default_interface" ]] || { echo "Не найден default IPv4 route"; exit 1; }
export WAN_INTERFACE="$default_interface"
python3 tools/configure.py
panel_password="$(python3 panel/server.py --init-auth)"
# Read only installer-generated scalar values, never source the file.
SERVER_HOST="$(awk -F= '$1=="SERVER_HOST" {print $2}' .env)"
WG_PORT="$(awk -F= '$1=="WG_PORT" {print $2}' .env)"
VLESS_PORT="$(awk -F= '$1=="VLESS_PORT" {print $2}' .env)"

phase=xray
if [[ ! -x /usr/local/bin/xray || ! -f data/xray-version || "$(cat data/xray-version)" != "$XRAY_VERSION" ]]; then
  stage="$(mktemp -d)"
  trap 'rm -rf -- "$stage"' EXIT
  base="https://github.com/XTLS/Xray-core/releases/download/v${XRAY_VERSION}"
  curl --fail --location --retry 3 --connect-timeout 15 "$base/Xray-linux-64.zip" -o "$stage/xray.zip"
  curl --fail --location --retry 3 --connect-timeout 15 "$base/Xray-linux-64.zip.dgst" -o "$stage/xray.dgst"
  expected="$(grep -iE '^(SHA256|SHA2-256)' "$stage/xray.dgst" | grep -Eo '[0-9a-fA-F]{64}' | head -n1)"
  [[ "$expected" =~ ^[0-9a-fA-F]{64}$ ]] || { echo "Не найден SHA-256 в официальном .dgst"; exit 1; }
  actual="$(sha256sum "$stage/xray.zip" | awk '{print $1}')"
  [[ "${actual,,}" == "${expected,,}" ]] || { echo "SHA-256 Xray не совпал"; exit 1; }
  unzip -p "$stage/xray.zip" xray >"$stage/xray"
  install -m0755 "$stage/xray" /usr/local/bin/xray
  printf '%s\n' "$XRAY_VERSION" >data/xray-version
  chmod 0600 data/xray-version
  rm -rf -- "$stage"
  trap - EXIT
fi
/usr/local/bin/xray version | head -n1

phase=services
modprobe wireguard
printf 'wireguard\n' >/etc/modules-load.d/freenetvpn-mini.conf
printf 'net.ipv4.ip_forward=1\n' >/etc/sysctl.d/99-freenetvpn-mini.conf
sysctl --system >/dev/null
id -u xray >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin xray
install -d -m0755 /etc/xray /etc/wireguard
install -o root -g xray -m0640 runtime/xray.json /etc/xray/config.json
install -m0600 runtime/wg0.conf /etc/wireguard/wg0.conf
install -m0644 systemd/freenetvpn-xray.service /etc/systemd/system/freenetvpn-xray.service
install -m0644 systemd/freenetvpn-panel.service /etc/systemd/system/freenetvpn-panel.service
/usr/local/bin/xray run -test -config /etc/xray/config.json
systemctl daemon-reload
systemctl enable wg-quick@wg0 freenetvpn-xray freenetvpn-panel
systemctl restart wg-quick@wg0
systemctl restart freenetvpn-xray
systemctl restart freenetvpn-panel

phase=firewall
if [[ $firewall -eq 1 ]]; then
  ssh_ports="$(sshd -T 2>/dev/null | awk '$1=="port" {print $2}' | sort -u || true)"
  [[ -n "$ssh_ports" ]] || ssh_ports=22
  while read -r port; do
    [[ "$port" =~ ^[0-9]+$ ]] && ufw allow "${port}/tcp" comment SSH >/dev/null
  done <<<"$ssh_ports"
  ufw allow "${WG_PORT}/udp" comment WireGuard >/dev/null
  ufw allow "${VLESS_PORT}/tcp" comment VLESS >/dev/null
  if ! ufw status | grep -q '^Status: active'; then
    ufw --force enable
  fi
fi

phase=health
systemctl is-active --quiet wg-quick@wg0
systemctl is-active --quiet freenetvpn-xray
systemctl is-active --quiet freenetvpn-panel
wg show wg0 >/dev/null
/usr/local/bin/xray run -test -config /etc/xray/config.json >/dev/null
panel_http="$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' http://127.0.0.1:8080/)"
[[ "$panel_http" == 401 ]] || { echo "Панель на loopback вернула HTTP $panel_http вместо 401"; exit 1; }

cat <<EOF

Готово. WireGuard: UDP ${WG_PORT}; VLESS REALITY: TCP ${VLESS_PORT}.
WireGuard-клиент: ${INSTALL_DIR}/clients/wireguard/client1.conf
VLESS-ссылка:     ${INSTALL_DIR}/clients/vless/client1.txt
Показать оба:     sudo bash ${INSTALL_DIR}/scripts/show-clients.sh
Проверить статус: sudo bash ${INSTALL_DIR}/scripts/health.sh

Панель управления (только через SSH-туннель):
  ssh -L 8080:127.0.0.1:8080 <ssh-user>@${SERVER_HOST}
  Адрес: http://127.0.0.1:8080
  Логин: admin
$(if [[ -n "$panel_password" ]]; then printf '  Новый пароль: %s\n  Сохраните его: повторно автоматически не выводится.\n' "$panel_password"; else printf '  Пароль: используйте созданный ранее.\n  Сброс: sudo python3 %s/panel/server.py --reset-password\n' "$INSTALL_DIR"; fi)

VLESS URL:
$(cat clients/vless/client1.txt)
EOF
