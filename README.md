# FreeNETvpn-mini

Упрощённая версия [FreeNETvpn](https://github.com/evdokimenkoiv/FreeNETvpn): только **WireGuard** и **VLESS REALITY**, полностью автоматическая настройка и установка одной командой.

В mini-версии нет Docker, Caddy, веб-панели, IKEv2, L2TP, Outline, AmneziaWG, MTProto и дополнительных прокси. WireGuard и Xray работают непосредственно как systemd-сервисы. При установке создаются один WireGuard-пир `client1` и один VLESS-пользователь; домен и TLS-сертификат не нужны.

## Требования и минимальные ресурсы

- Чистая **Ubuntu 22.04/24.04/26.04 LTS x86_64**, systemd, root/sudo и публичный IPv4.
- На провайдерском firewall открыть **UDP 51820** и **TCP 443** (либо значения `WG_PORT`/`VLESS_PORT`).
- SSH-порт сохраняется в правилах UFW. UDP/TCP-порты не должны перехватываться HTTP-прокси.

| Ресурс | Минимум | Рекомендуется |
|---|---:|---:|
| Свободное место на `/` перед установкой | **1 ГиБ** — проверяется установщиком | **2 ГиБ и больше** |
| Общий размер диска VPS | **8 ГиБ** для минимальной Ubuntu | **10–20 ГиБ** |
| RAM | **512 МиБ** | **1 ГиБ** |
| CPU | **1 vCPU** | 1 vCPU |

Сам mini-проект ориентировочно добавляет **150–350 МиБ** к базовой системе: Xray, WireGuard tools, зависимости и APT-метаданные. Точный результат зависит от уже установленных пакетов и состояния APT. Временные архивы установщик удаляет. Основной расход диска обычно даёт сама Ubuntu, её обновления и журналы, а не VPN.

Для VPS с 512 МиБ RAM желательно заранее включить swap на 512 МиБ–1 ГиБ, чтобы APT не завершался по OOM. После установки сервисы потребляют заметно меньше памяти, чем полный проект: нет Docker daemon, containerd, базы веб-панели и дополнительных контейнеров.

## Установка одной командой

```bash
sudo bash -c 'set -e; apt-get update -qq; apt-get install -y --no-install-recommends ca-certificates curl; f=$(mktemp); trap "rm -f -- \"$f\"" EXIT; curl -fsSL https://raw.githubusercontent.com/evdokimenkoiv/FreeNETvpn-mini/main/install.sh -o "$f"; bash "$f"'
```

Установщик:

1. определяет публичный IPv4 (можно задать `SERVER_HOST` вручную);
2. устанавливает только необходимые пакеты и закреплённый Xray `26.3.27`;
3. проверяет ZIP Xray по SHA-256 из официального `.dgst` релиза;
4. генерирует постоянные ключи WireGuard и REALITY, UUID и short ID;
5. настраивает IP forwarding, NAT, UFW и systemd;
6. проверяет конфигурацию Xray и состояние обоих сервисов;
7. выводит готовую VLESS-ссылку и путь к WireGuard-профилю.

Если автоматическое определение адреса не подходит, перед той же командой задайте DNS-имя или IPv4:

```bash
sudo SERVER_HOST=vpn.example.com bash -c 'set -e; apt-get update -qq; apt-get install -y --no-install-recommends ca-certificates curl; f=$(mktemp); trap "rm -f -- \"$f\"" EXIT; curl -fsSL https://raw.githubusercontent.com/evdokimenkoiv/FreeNETvpn-mini/main/install.sh -o "$f"; bash "$f"'
```

Проект устанавливается в `/opt/freenetvpn-mini`. Повторный запуск не пересоздаёт ключи и клиентов. Если UFW управляется отдельно, в скачанном проекте используйте `sudo bash install.sh --no-firewall`.

## Подключение

Показать готовые профили:

```bash
sudo bash /opt/freenetvpn-mini/scripts/show-clients.sh
```

- **WireGuard:** скачайте `/opt/freenetvpn-mini/clients/wireguard/client1.conf` и импортируйте в официальное приложение WireGuard. Настроен полный IPv4-туннель.
- **VLESS:** импортируйте ссылку из `/opt/freenetvpn-mini/clients/vless/client1.txt` в клиент с поддержкой **VLESS TCP + REALITY + Vision**. Подойдут актуальные клиенты на Xray-core.
- **VLESS JSON:** `client1.json` — полный локальный конфиг Xray с SOCKS5 на `127.0.0.1:1080`.

Файлы содержат приватные ключи. Передавайте их только по защищённому каналу и не публикуйте. Права на секреты и клиентские профили ограничены root.

## Проверка и обслуживание

```bash
sudo bash /opt/freenetvpn-mini/scripts/health.sh
systemctl status wg-quick@wg0 freenetvpn-xray
journalctl -u freenetvpn-xray -n 50 --no-pager
```

Основные настройки находятся в `/opt/freenetvpn-mini/.env`, секреты — в `data/state.json`. После изменения `.env` выполните `sudo bash /opt/freenetvpn-mini/install.sh`; клиентские файлы будут перегенерированы с теми же ключами. Для смены адреса сервера достаточно обновить `SERVER_HOST`; для смены ключей следует сознательно восстановить/пересоздать установку, а не удалять отдельные поля состояния.

Для резервной копии сохраните вне сервера каталоги `/opt/freenetvpn-mini/data`, `/opt/freenetvpn-mini/clients` и файл `.env`. Резервная копия содержит все VPN-секреты.

Поддерживается IPv4; IPv6-туннель и веб-интерфейс намеренно не входят в mini. Автоматических обновлений Xray нет: закреплённая версия меняется вместе с кодом репозитория.

## Лицензия

MIT, как у исходного проекта. См. [LICENSE](LICENSE).
