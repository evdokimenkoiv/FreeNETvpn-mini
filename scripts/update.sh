#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

INSTALL_DIR=/opt/freenetvpn-mini
[[ ${EUID} -eq 0 ]] || { echo "Запустите с sudo"; exit 1; }
[[ -f "$INSTALL_DIR/install.sh" && -f "$INSTALL_DIR/tools/configure.py" ]] || {
  echo "Установка не найдена в $INSTALL_DIR. Используйте install.sh для первой установки."; exit 1;
}

work="$(mktemp -d)"
trap 'rm -rf -- "$work"' EXIT
mkdir "$work/source"
curl --fail --location --retry 3 --connect-timeout 15 \
  https://api.github.com/repos/evdokimenkoiv/FreeNETvpn-mini/tarball/main \
  -o "$work/project.tar.gz"
tar --extract --gzip --file "$work/project.tar.gz" --strip-components=1 \
  --directory "$work/source" --no-same-owner
[[ -f "$work/source/install.sh" && -f "$work/source/panel/server.py" ]] || {
  echo "Загружен неполный архив проекта"; exit 1;
}

# The repository contains no data/, runtime/, clients/ or .env, so existing
# credentials, generated clients and local settings remain in place.
cp -a "$work/source/." "$INSTALL_DIR/"
bash "$INSTALL_DIR/install.sh" "$@"
