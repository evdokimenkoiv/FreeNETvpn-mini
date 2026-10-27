#!/usr/bin/env python3
"""Generate persistent WireGuard and VLESS REALITY configurations."""

import argparse
import base64
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import urllib.request
from urllib.parse import quote, urlencode
import uuid

DEFAULTS = {
    "WG_PORT": "51820",
    "VLESS_PORT": "443",
    "WG_ADDRESS": "10.8.0.1/24",
    "WG_CLIENT_ADDRESS": "10.8.0.2/32",
    "WG_DNS": "1.1.1.1, 1.0.0.1",
    "REALITY_TARGET": "www.microsoft.com:443",
    "REALITY_SNI": "www.microsoft.com",
}
HOST_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}$")
INTERFACE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,15}$")


def atomic_write(path, content, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def read_env(path):
    values = {}
    if not path.exists():
        return values
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ValueError(f"Invalid .env line: {raw_line!r}")
        values[key] = value
    return values


def detect_public_ipv4():
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "FreeNETvpn-mini/1"})
            with urllib.request.urlopen(request, timeout=10) as response:
                value = response.read(128).decode().strip()
            address = ipaddress.ip_address(value)
            if isinstance(address, ipaddress.IPv4Address):
                return str(address)
        except (OSError, ValueError):
            continue
    raise RuntimeError("Cannot detect public IPv4. Set SERVER_HOST to a public IPv4 address or DNS name.")


def validate_host(value):
    value = value.strip().rstrip(".")
    if not HOST_RE.fullmatch(value) or ".." in value:
        raise ValueError("SERVER_HOST must be an IPv4 address or DNS name without a scheme or port")
    try:
        address = ipaddress.ip_address(value)
        if not isinstance(address, ipaddress.IPv4Address):
            raise ValueError("Only IPv4 endpoints are supported")
    except ValueError:
        if value.startswith("-") or value.endswith("-") or "." not in value:
            raise ValueError("SERVER_HOST must be a public IPv4 address or a valid DNS name")
    return value


def validate_port(value, name):
    port = int(value)
    if not 1 <= port <= 65535:
        raise ValueError(f"{name} must be between 1 and 65535")
    return port


def x25519_keypair():
    private_der = subprocess.run(
        ["openssl", "genpkey", "-algorithm", "X25519", "-outform", "DER"],
        check=True, capture_output=True,
    ).stdout
    public_der = subprocess.run(
        ["openssl", "pkey", "-inform", "DER", "-pubout", "-outform", "DER"],
        input=private_der, check=True, capture_output=True,
    ).stdout
    return {
        "private": base64.b64encode(private_der[-32:]).decode(),
        "public": base64.b64encode(public_der[-32:]).decode(),
    }


def reality_key(value):
    """Return the base64.RawURLEncoding form required by Xray REALITY."""
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError) as error:
        raise ValueError("Invalid REALITY X25519 key encoding") from error
    if len(raw) != 32:
        raise ValueError("REALITY X25519 keys must contain exactly 32 bytes")
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def initial_state():
    return {
        "version": 1,
        "wireguard": {**x25519_keypair(), "client": x25519_keypair()},
        "vless": {
            **x25519_keypair(),
            "uuid": str(uuid.uuid4()),
            "short_id": secrets.token_hex(8),
        },
    }


def render(root, config, state):
    runtime = root / "runtime"
    clients = root / "clients"
    wg = state["wireguard"]
    vless = state["vless"]
    interface = config["WAN_INTERFACE"]
    dns = config["WG_DNS"]
    wg_interface = ipaddress.ip_interface(config["WG_ADDRESS"])
    wg_client = ipaddress.ip_interface(config["WG_CLIENT_ADDRESS"])
    if not isinstance(wg_interface, ipaddress.IPv4Interface) or not isinstance(wg_client, ipaddress.IPv4Interface):
        raise ValueError("WireGuard addresses must be IPv4 interfaces")
    if wg_client.ip not in wg_interface.network or wg_client.ip == wg_interface.ip:
        raise ValueError("WG_CLIENT_ADDRESS must be a different address inside WG_ADDRESS network")
    wg_network = str(wg_interface.network)

    wg_server = f"""[Interface]
Address = {config['WG_ADDRESS']}
ListenPort = {config['WG_PORT']}
PrivateKey = {wg['private']}
SaveConfig = false
PostUp = iptables -I FORWARD 1 -i %i -j ACCEPT; iptables -I FORWARD 1 -o %i -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT; iptables -t nat -A POSTROUTING -s {wg_network} -o {interface} -j MASQUERADE
PostDown = iptables -D FORWARD -i %i -j ACCEPT; iptables -D FORWARD -o %i -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT; iptables -t nat -D POSTROUTING -s {wg_network} -o {interface} -j MASQUERADE

[Peer]
PublicKey = {wg['client']['public']}
AllowedIPs = {config['WG_CLIENT_ADDRESS']}
"""
    atomic_write(runtime / "wg0.conf", wg_server)

    xray_server = {
        "log": {"loglevel": "warning"},
        "inbounds": [{
            "listen": "0.0.0.0",
            "port": int(config["VLESS_PORT"]),
            "protocol": "vless",
            "settings": {
                "clients": [{
                    "id": vless["uuid"],
                    "flow": "xtls-rprx-vision",
                    "email": "client1",
                }],
                "decryption": "none",
            },
            "streamSettings": {
                "network": "tcp",
                "security": "reality",
                "realitySettings": {
                    "show": False,
                    "dest": config["REALITY_TARGET"],
                    "xver": 0,
                    "serverNames": [config["REALITY_SNI"]],
                    "privateKey": vless["private"],
                    "shortIds": [vless["short_id"]],
                },
            },
        }],
        "outbounds": [{"protocol": "freedom", "settings": {"domainStrategy": "UseIP"}}],
    }
    # The installer copies this file to /etc/xray with root:xray 0640 permissions.
    atomic_write(runtime / "xray.json", json.dumps(xray_server, indent=2) + "\n", 0o640)

    wg_client = f"""[Interface]
PrivateKey = {wg['client']['private']}
Address = {config['WG_CLIENT_ADDRESS']}
DNS = {dns}
MTU = 1420

[Peer]
PublicKey = {wg['public']}
Endpoint = {config['SERVER_HOST']}:{config['WG_PORT']}
AllowedIPs = 0.0.0.0/0
PersistentKeepalive = 25
"""
    atomic_write(clients / "wireguard" / "client1.conf", wg_client)

    stream = {
        "network": "tcp",
        "security": "reality",
        "realitySettings": {
            "serverName": config["REALITY_SNI"],
            "fingerprint": "chrome",
            "publicKey": vless["public"],
            "shortId": vless["short_id"],
            "spiderX": "/",
        },
    }
    xray_client = {
        "log": {"loglevel": "warning"},
        "inbounds": [{
            "listen": "127.0.0.1",
            "port": 1080,
            "protocol": "socks",
            "settings": {"auth": "noauth", "udp": True},
        }],
        "outbounds": [{
            "protocol": "vless",
            "settings": {"vnext": [{
                "address": config["SERVER_HOST"],
                "port": int(config["VLESS_PORT"]),
                "users": [{
                    "id": vless["uuid"],
                    "encryption": "none",
                    "flow": "xtls-rprx-vision",
                }],
            }]},
            "streamSettings": stream,
        }],
    }
    atomic_write(clients / "vless" / "client1.json", json.dumps(xray_client, indent=2) + "\n")
    query = urlencode({
        "encryption": "none",
        "flow": "xtls-rprx-vision",
        "security": "reality",
        "sni": config["REALITY_SNI"],
        "fp": "chrome",
        "pbk": vless["public"],
        "sid": vless["short_id"],
        "type": "tcp",
        "spx": "/",
    })
    uri = f"vless://{vless['uuid']}@{config['SERVER_HOST']}:{config['VLESS_PORT']}?{query}#FreeNET-mini"
    atomic_write(clients / "vless" / "client1.txt", uri + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--host", help="Public IPv4 address or DNS name")
    parser.add_argument("--wan-interface", help="Interface used for WireGuard NAT")
    args = parser.parse_args()
    root = args.root.resolve()
    saved = read_env(root / ".env")
    config = dict(DEFAULTS)
    config.update({key: value for key, value in saved.items() if key in DEFAULTS or key == "SERVER_HOST"})
    for key in (*DEFAULTS, "SERVER_HOST"):
        if os.environ.get(key):
            config[key] = os.environ[key]
    if args.host:
        config["SERVER_HOST"] = args.host
    if not config.get("SERVER_HOST"):
        config["SERVER_HOST"] = detect_public_ipv4()
    config["SERVER_HOST"] = validate_host(config["SERVER_HOST"])
    config["WG_PORT"] = str(validate_port(config["WG_PORT"], "WG_PORT"))
    config["VLESS_PORT"] = str(validate_port(config["VLESS_PORT"], "VLESS_PORT"))
    interface = args.wan_interface or os.environ.get("WAN_INTERFACE")
    if not interface:
        raise ValueError("WAN_INTERFACE is required")
    if not INTERFACE_RE.fullmatch(interface):
        raise ValueError("Invalid WAN_INTERFACE")
    config["WAN_INTERFACE"] = interface
    for address in config["WG_DNS"].split(","):
        value = address.strip()
        if not isinstance(ipaddress.ip_address(value), ipaddress.IPv4Address):
            raise ValueError("WG_DNS accepts IPv4 addresses only")
    if not HOST_RE.fullmatch(config["REALITY_SNI"]):
        raise ValueError("Invalid REALITY_SNI")
    target_host, separator, target_port = config["REALITY_TARGET"].rpartition(":")
    if not separator or not HOST_RE.fullmatch(target_host):
        raise ValueError("REALITY_TARGET must have host:port format")
    validate_port(target_port, "REALITY_TARGET port")

    state_path = root / "data" / "state.json"
    if state_path.exists():
        state = json.loads(state_path.read_text())
        if state.get("version") != 1:
            raise ValueError("Unsupported state version")
    else:
        state = initial_state()
    # Migrate early installations from padded standard base64 without changing keys.
    state["vless"]["private"] = reality_key(state["vless"]["private"])
    state["vless"]["public"] = reality_key(state["vless"]["public"])
    atomic_write(state_path, json.dumps(state, indent=2) + "\n")
    render(root, config, state)
    env_text = "# Generated by FreeNETvpn-mini. Secrets are stored separately in data/state.json.\n"
    env_text += "".join(f"{key}={config[key]}\n" for key in ("SERVER_HOST", *DEFAULTS))
    atomic_write(root / ".env", env_text)
    print(f"Configured WireGuard and VLESS for {config['SERVER_HOST']}")


if __name__ == "__main__":
    main()
