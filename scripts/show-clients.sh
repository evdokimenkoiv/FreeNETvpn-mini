#!/usr/bin/env bash
set -Eeuo pipefail
[[ ${EUID} -eq 0 ]] || { echo "Запустите с sudo: файлы содержат приватные ключи"; exit 1; }
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
shopt -s nullglob

wg_files=("$root"/clients/wireguard/*.conf)
for file in "${wg_files[@]}"; do
  printf '\n=== WireGuard: %s ===\n\n' "$(basename "$file")"
  cat "$file"
done

vless_files=("$root"/clients/vless/*.txt)
for file in "${vless_files[@]}"; do
  printf '\n=== VLESS REALITY: %s ===\n\n' "$(basename "$file")"
  cat "$file"
done

if (( ${#wg_files[@]} == 0 && ${#vless_files[@]} == 0 )); then
  printf 'Клиентские конфигурации не найдены. Запустите install.sh или создайте клиента в панели.\n'
  exit 1
fi
printf '\nJSON-профили VLESS находятся в %s/clients/vless/*.json\n\n' "$root"
