#!/usr/bin/env bash
set -Eeuo pipefail

# Temporary mitigation for CVE-2026-98432. Ordinary TCP options are not
# affected: only IPv4 packets with additional IPv4 header options are dropped.
TABLE=freenetvpn_security
NFT="${NFT:-/usr/sbin/nft}"

case "${1:-start}" in
  start)
    if "$NFT" list table inet "$TABLE" >/dev/null 2>&1; then
      "$NFT" delete table inet "$TABLE"
    fi
    "$NFT" -f - <<'EOF'
table inet freenetvpn_security {
  chain input {
    type filter hook input priority -310; policy accept;
    ip protocol tcp ip hdrlength > 5 counter drop comment "CVE-2026-98432"
  }
}
EOF
    ;;
  stop)
    if "$NFT" list table inet "$TABLE" >/dev/null 2>&1; then
      "$NFT" delete table inet "$TABLE"
    fi
    ;;
  *)
    echo "Использование: $0 {start|stop}" >&2
    exit 2
    ;;
esac
