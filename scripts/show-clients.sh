#!/usr/bin/env bash
set -Eeuo pipefail
[[ ${EUID} -eq 0 ]] || { echo "Запустите с sudo: файлы содержат приватные ключи"; exit 1; }
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
printf '\n=== WireGuard: импортируйте client1.conf в приложение WireGuard ===\n\n'
cat "$root/clients/wireguard/client1.conf"
printf '\n=== VLESS REALITY: импортируйте эту ссылку в совместимый клиент ===\n\n'
cat "$root/clients/vless/client1.txt"
printf '\nПолный JSON для локального Xray (SOCKS5 127.0.0.1:1080): %s\n\n' "$root/clients/vless/client1.json"
