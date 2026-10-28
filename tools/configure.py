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
CLIENT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


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


def detect_wan_interface():
    result = subprocess.run(["ip", "-4", "route", "show", "default"], check=True, capture_output=True, text=True)
    for line in result.stdout.splitlines():
        fields = line.split()
        if "dev" in fields:
            return fields[fields.index("dev") + 1]
    raise ValueError("Cannot detect the default IPv4 interface")


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


def initial_state(client_address=DEFAULTS["WG_CLIENT_ADDRESS"]):
    return {
        "version": 2,
        "wireguard": {
            **x25519_keypair(),
            "clients": [{"name": "client1", **x25519_keypair(), "address": client_address}],
        },
        "vless": {
            **x25519_keypair(),
            "clients": [{"name": "client1", "uuid": str(uuid.uuid4()), "short_id": secrets.token_hex(8)}],
        },
    }


def load_config(root, host=None, wan_interface=None):
    root = Path(root).resolve()
    saved = read_env(root / ".env")
    config = dict(DEFAULTS)
    config.update({key: value for key, value in saved.items() if key in DEFAULTS or key in {"SERVER_HOST", "WAN_INTERFACE"}})
    for key in (*DEFAULTS, "SERVER_HOST"):
        if os.environ.get(key):
            config[key] = os.environ[key]
    if host:
        config["SERVER_HOST"] = host
    if not config.get("SERVER_HOST"):
        config["SERVER_HOST"] = detect_public_ipv4()
    config["SERVER_HOST"] = validate_host(config["SERVER_HOST"])
    config["WG_PORT"] = str(validate_port(config["WG_PORT"], "WG_PORT"))
    config["VLESS_PORT"] = str(validate_port(config["VLESS_PORT"], "VLESS_PORT"))
    interface = wan_interface or os.environ.get("WAN_INTERFACE") or saved.get("WAN_INTERFACE") or detect_wan_interface()
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
    return config


def load_state(root, config):
    state_path = Path(root) / "data" / "state.json"
    if state_path.exists():
        state = json.loads(state_path.read_text())
    else:
        state = initial_state(config["WG_CLIENT_ADDRESS"])
    if state.get("version") == 1:
        # Preserve all credentials from the original single-client schema.
        state = {
            "version": 2,
            "wireguard": {
                "private": state["wireguard"]["private"],
                "public": state["wireguard"]["public"],
                "clients": [{
                    "name": "client1",
                    "private": state["wireguard"]["client"]["private"],
                    "public": state["wireguard"]["client"]["public"],
                    "address": config["WG_CLIENT_ADDRESS"],
                }],
            },
            "vless": {
                "private": state["vless"]["private"],
                "public": state["vless"]["public"],
                "clients": [{
                    "name": "client1",
                    "uuid": state["vless"]["uuid"],
                    "short_id": state["vless"]["short_id"],
                }],
            },
        }
    if state.get("version") != 2:
        raise ValueError("Unsupported state version")
    state["vless"]["private"] = reality_key(state["vless"]["private"])
    state["vless"]["public"] = reality_key(state["vless"]["public"])
    for protocol in ("wireguard", "vless"):
        clients = state[protocol].get("clients")
        if not isinstance(clients, list):
            raise ValueError(f"Invalid {protocol} client list")
        names = set()
        for client in clients:
            if not CLIENT_NAME_RE.fullmatch(client.get("name", "")) or client["name"] in names:
                raise ValueError("Client names must be unique lowercase identifiers")
            names.add(client["name"])
    return state


def render(root, config, state):
    root = Path(root)
    clients_root = root / "clients"
    wg = state["wireguard"]
    vless = state["vless"]
    interface = config["WAN_INTERFACE"]
    dns = config["WG_DNS"]
    wg_interface = ipaddress.ip_interface(config["WG_ADDRESS"])
    if not isinstance(wg_interface, ipaddress.IPv4Interface):
        raise ValueError("WireGuard address must be an IPv4 interface")
    wg_network = str(wg_interface.network)

    peers = []
    for client in wg["clients"]:
        client_interface = ipaddress.ip_interface(client["address"])
        if client_interface.ip not in wg_interface.network or client_interface.ip == wg_interface.ip:
            raise ValueError("WireGuard client addresses must be inside WG_ADDRESS network")
        peers.append(f"""[Peer]
PublicKey = {client['public']}
AllowedIPs = {client_interface.ip}/32
""")

    wg_server = f"""[Interface]
Address = {config['WG_ADDRESS']}
ListenPort = {config['WG_PORT']}
PrivateKey = {wg['private']}
SaveConfig = false
PostUp = iptables -I FORWARD 1 -i %i -j ACCEPT; iptables -I FORWARD 1 -o %i -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT; iptables -t nat -A POSTROUTING -s {wg_network} -o {interface} -j MASQUERADE
PostDown = iptables -D FORWARD -i %i -j ACCEPT; iptables -D FORWARD -o %i -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT; iptables -t nat -D POSTROUTING -s {wg_network} -o {interface} -j MASQUERADE

{chr(10).join(peers)}"""
    atomic_write(root / "runtime" / "wg0.conf", wg_server)

    xray_clients = [{
        "id": client["uuid"],
        "flow": "xtls-rprx-vision",
        "email": client["name"],
    } for client in vless["clients"]]
    xray_server = {
        "log": {"loglevel": "warning"},
        "inbounds": [{
            "listen": "0.0.0.0",
            "port": int(config["VLESS_PORT"]),
            "protocol": "vless",
            "settings": {"clients": xray_clients, "decryption": "none"},
            "streamSettings": {
                "network": "tcp",
                "security": "reality",
                "realitySettings": {
                    "show": False,
                    "dest": config["REALITY_TARGET"],
                    "xver": 0,
                    "serverNames": [config["REALITY_SNI"]],
                    "privateKey": vless["private"],
                    "shortIds": [client["short_id"] for client in vless["clients"]],
                },
            },
        }],
        "outbounds": [{"protocol": "freedom", "settings": {"domainStrategy": "UseIP"}}],
    }
    atomic_write(root / "runtime" / "xray.json", json.dumps(xray_server, indent=2) + "\n", 0o640)

    wg_directory = clients_root / "wireguard"
    expected_wg = set()
    for client in wg["clients"]:
        name = client["name"]
        expected_wg.add(f"{name}.conf")
        content = f"""[Interface]
PrivateKey = {client['private']}
Address = {client['address']}
DNS = {dns}
MTU = 1420

[Peer]
PublicKey = {wg['public']}
Endpoint = {config['SERVER_HOST']}:{config['WG_PORT']}
AllowedIPs = 0.0.0.0/0
PersistentKeepalive = 25
"""
        atomic_write(wg_directory / f"{name}.conf", content)
    if wg_directory.exists():
        for path in wg_directory.glob("*.conf"):
            if path.name not in expected_wg:
                path.unlink()

    vless_directory = clients_root / "vless"
    expected_vless = set()
    for client in vless["clients"]:
        name = client["name"]
        expected_vless.update({f"{name}.txt", f"{name}.json"})
        stream = {
            "network": "tcp",
            "security": "reality",
            "realitySettings": {
                "serverName": config["REALITY_SNI"],
                "fingerprint": "chrome",
                "publicKey": vless["public"],
                "shortId": client["short_id"],
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
                        "id": client["uuid"],
                        "encryption": "none",
                        "flow": "xtls-rprx-vision",
                    }],
                }]},
                "streamSettings": stream,
            }],
        }
        atomic_write(vless_directory / f"{name}.json", json.dumps(xray_client, indent=2) + "\n")
        query = urlencode({
            "encryption": "none",
            "flow": "xtls-rprx-vision",
            "security": "reality",
            "sni": config["REALITY_SNI"],
            "fp": "chrome",
            "pbk": vless["public"],
            "sid": client["short_id"],
            "type": "tcp",
            "spx": "/",
        })
        uri = f"vless://{client['uuid']}@{config['SERVER_HOST']}:{config['VLESS_PORT']}?{query}#FreeNET-mini-{quote(name)}"
        atomic_write(vless_directory / f"{name}.txt", uri + "\n")
    if vless_directory.exists():
        for path in vless_directory.iterdir():
            if path.is_file() and path.suffix in {".txt", ".json"} and path.name not in expected_vless:
                path.unlink()


def save_configuration(root, config, state):
    root = Path(root)
    atomic_write(root / "data" / "state.json", json.dumps(state, indent=2) + "\n")
    render(root, config, state)
    env_text = "# Generated by FreeNETvpn-mini. Secrets are stored separately in data/state.json.\n"
    env_text += "".join(f"{key}={config[key]}\n" for key in ("SERVER_HOST", *DEFAULTS, "WAN_INTERFACE"))
    atomic_write(root / ".env", env_text)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--host", help="Public IPv4 address or DNS name")
    parser.add_argument("--wan-interface", help="Interface used for WireGuard NAT")
    args = parser.parse_args()
    root = args.root.resolve()
    config = load_config(root, host=args.host, wan_interface=args.wan_interface)
    state = load_state(root, config)
    save_configuration(root, config, state)
    print(f"Configured WireGuard and VLESS for {config['SERVER_HOST']}")


if __name__ == "__main__":
    main()
