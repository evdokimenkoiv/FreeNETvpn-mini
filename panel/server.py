#!/usr/bin/env python3
"""Loopback-only administration panel for FreeNETvpn-mini."""

import argparse
import base64
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qs, quote, urlsplit
import uuid

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from tools import configure  # noqa: E402

STATIC_ROOT = Path(__file__).resolve().parent / "static"
AUTH_ITERATIONS = 600_000
SERVICES = {"wireguard": "wg-quick@wg0", "vless": "freenetvpn-xray"}
MAX_CLIENTS = 100
STATE_LOCK = threading.Lock()


def atomic_write(path, content, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def password_record(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, AUTH_ITERATIONS)
    return {
        "version": 1,
        "username": "admin",
        "algorithm": "pbkdf2-sha256",
        "iterations": AUTH_ITERATIONS,
        "salt": base64.b64encode(salt).decode(),
        "hash": base64.b64encode(digest).decode(),
    }


def ensure_auth(root, reset=False):
    path = root / "data" / "panel.json"
    if path.exists() and not reset:
        return None
    password = secrets.token_urlsafe(18)
    atomic_write(path, json.dumps(password_record(password), indent=2) + "\n")
    return password


def verify_password(username, password, record):
    if not hmac.compare_digest(username.encode(), record["username"].encode()):
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), base64.b64decode(record["salt"]), int(record["iterations"]),
    )
    return hmac.compare_digest(digest, base64.b64decode(record["hash"]))


def command(*args, timeout=6):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        return subprocess.CompletedProcess(args, 1, "", str(error))


def service_status(unit):
    result = command("systemctl", "is-active", unit)
    return result.stdout.strip() or "unknown"


def restart_service(service):
    result = command("systemctl", "restart", SERVICES[service], timeout=25)
    if result.returncode != 0:
        return False, (result.stderr or result.stdout or "systemctl failed").strip()[-500:]
    return True, service_status(SERVICES[service])


def read_env(root):
    values = {}
    path = root / ".env"
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        key, separator, value = line.partition("=")
        if separator and key.isupper():
            values[key] = value
    return values


def memory_status():
    values = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        if key in {"MemTotal", "MemAvailable"}:
            values[key] = int(value.strip().split()[0]) * 1024
    return {"total": values.get("MemTotal", 0), "available": values.get("MemAvailable", 0)}


def wireguard_status():
    result = {"handshake": None, "rx": 0, "tx": 0}
    transfer = command("wg", "show", "wg0", "transfer")
    if transfer.returncode == 0:
        for line in transfer.stdout.splitlines():
            fields = line.split()
            if len(fields) >= 3:
                result["rx"] += int(fields[1])
                result["tx"] += int(fields[2])
    handshake = command("wg", "show", "wg0", "latest-handshakes")
    timestamps = []
    if handshake.returncode == 0:
        for line in handshake.stdout.splitlines():
            fields = line.split()
            if len(fields) >= 2 and int(fields[1]) > 0:
                timestamps.append(int(fields[1]))
    if timestamps:
        result["handshake"] = max(timestamps)
    return result


def client_lists(root):
    state_path = root / "data" / "state.json"
    result = {"wireguard": [], "vless": []}
    if state_path.exists():
        state = json.loads(state_path.read_text())
        if state.get("version") == 2:
            for client in state["wireguard"]["clients"]:
                name = client["name"]
                result["wireguard"].append({
                    "name": name,
                    "address": client["address"],
                    "download": f"/download/wireguard?name={quote(name)}",
                })
            for client in state["vless"]["clients"]:
                name = client["name"]
                uri_path = root / "clients" / "vless" / f"{name}.txt"
                result["vless"].append({
                    "name": name,
                    "uri": uri_path.read_text().strip() if uri_path.exists() else "",
                    "download": f"/download/vless?name={quote(name)}",
                    "json": f"/download/vless-json?name={quote(name)}",
                })
            return result
    # Read-only compatibility while an old installation is being migrated.
    for protocol, filename in (("wireguard", "client1.conf"), ("vless", "client1.txt")):
        path = root / "clients" / protocol / filename
        if path.exists():
            item = {"name": "client1", "download": f"/download/{protocol}"}
            if protocol == "wireguard":
                item["address"] = ""
            else:
                item.update({
                    "uri": path.read_text().strip(),
                    "json": "/download/vless-json",
                })
            result[protocol].append(item)
    return result


def status(root):
    env = read_env(root)
    disk = shutil.disk_usage("/")
    xray = command("/usr/local/bin/xray", "version")
    version = xray.stdout.splitlines()[0] if xray.returncode == 0 and xray.stdout else "unknown"
    clients = client_lists(root)
    return {
        "serverHost": env.get("SERVER_HOST", "unknown"),
        "ports": {"wireguard": env.get("WG_PORT", "51820"), "vless": env.get("VLESS_PORT", "443")},
        "services": {name: service_status(unit) for name, unit in SERVICES.items()},
        "wireguard": wireguard_status(),
        "xrayVersion": version,
        "disk": {"total": disk.total, "used": disk.used, "free": disk.free},
        "memory": memory_status(),
        "clients": {"wireguard": bool(clients["wireguard"]), "vless": bool(clients["vless"])},
        "clientLists": clients,
        "vlessUri": clients["vless"][0]["uri"] if clients["vless"] else "",
        "timestamp": int(time.time()),
    }


def validate_client_name(value):
    if not configure.CLIENT_NAME_RE.fullmatch(value):
        raise ValueError("Имя: строчные a-z, цифры, '-' и '_', до 32 символов")
    return value


def next_client_name(clients):
    used = {client["name"] for client in clients}
    number = 1
    while f"client{number}" in used:
        number += 1
    return f"client{number}"


def next_wg_address(config, clients):
    network = ipaddress.ip_interface(config["WG_ADDRESS"]).network
    server_ip = ipaddress.ip_interface(config["WG_ADDRESS"]).ip
    used = {ipaddress.ip_interface(client["address"]).ip for client in clients}
    candidate = network.network_address + 2
    while candidate < network.broadcast_address:
        if candidate != server_ip and candidate not in used:
            return f"{candidate}/32"
        candidate += 1
    raise ValueError("В подсети WG_ADDRESS закончились адреса")


def install_runtime(root, etc_root, protocol):
    if protocol == "wireguard":
        source = root / "runtime" / "wg0.conf"
        destination = etc_root / "wireguard" / "wg0.conf"
        mode = 0o600
    else:
        source = root / "runtime" / "xray.json"
        destination = etc_root / "xray" / "config.json"
        mode = 0o640
    atomic_write(destination, source.read_text(), mode)
    if protocol == "vless" and etc_root == Path("/etc"):
        shutil.chown(destination, user="root", group="xray")


def mutate_client(root, etc_root, protocol, action, requested_name=""):
    if protocol not in SERVICES or action not in {"create", "regenerate", "delete"}:
        raise ValueError("Неизвестная операция")
    with STATE_LOCK:
        config = configure.load_config(root)
        state = configure.load_state(root, config)
        clients = state[protocol]["clients"]
        if action == "create":
            name = validate_client_name(requested_name) if requested_name else next_client_name(clients)
            if any(client["name"] == name for client in clients):
                raise ValueError("Клиент с таким именем уже существует")
            if len(clients) >= MAX_CLIENTS:
                raise ValueError(f"Можно создать не более {MAX_CLIENTS} клиентов на протокол")
            if protocol == "wireguard":
                client = {
                    "name": name,
                    **configure.x25519_keypair(),
                    "address": next_wg_address(config, clients),
                }
            else:
                client = {"name": name, "uuid": str(uuid.uuid4()), "short_id": secrets.token_hex(8)}
            clients.append(client)
        else:
            name = validate_client_name(requested_name)
            matches = [client for client in clients if client["name"] == name]
            if not matches:
                raise ValueError("Клиент не найден")
            client = matches[0]
            if action == "delete":
                if len(clients) == 1:
                    raise ValueError("Нельзя удалить последнего клиента протокола")
                clients.remove(client)
            elif protocol == "wireguard":
                client.update(configure.x25519_keypair())
            else:
                client.update({"uuid": str(uuid.uuid4()), "short_id": secrets.token_hex(8)})
        configure.save_configuration(root, config, state)
        install_runtime(root, etc_root, protocol)
    restart_ok, restart_status = restart_service(protocol)
    return {
        "ok": True,
        "applied": True,
        "action": action,
        "protocol": protocol,
        "name": name,
        "restart": {"ok": restart_ok, "status": restart_status},
        "clients": client_lists(root),
    }


class PanelServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, root, etc_root):
        self.root = root
        self.etc_root = etc_root
        super().__init__(address, handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "FreeNETvpn-mini"
    sys_version = ""

    def log_message(self, fmt, *args):
        print(f"{self.client_address[0]} {self.command} {urlsplit(self.path).path}", flush=True)

    def authorized(self):
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:], validate=True).decode()
            username, password = decoded.split(":", 1)
        except (ValueError, UnicodeDecodeError):
            return False
        record = json.loads((self.server.root / "data" / "panel.json").read_text())
        return verify_password(username, password, record)

    def require_auth(self):
        if self.authorized():
            return True
        self.send_bytes(401, b"Authentication required\n", "text/plain; charset=utf-8",
                        {"WWW-Authenticate": 'Basic realm="FreeNETvpn-mini", charset="UTF-8"'})
        return False

    def send_bytes(self, code, content, content_type, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                         "img-src 'self' data:; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(content)

    def send_json(self, code, value):
        self.send_bytes(code, json.dumps(value).encode(), "application/json; charset=utf-8")

    def send_file(self, path, content_type, download_name=None):
        if not path.is_file():
            self.send_json(404, {"error": "Client configuration not found"})
            return
        content = path.read_bytes()
        extra = {}
        if download_name:
            extra["Content-Disposition"] = f'attachment; filename="{download_name}"'
        self.send_bytes(200, content, content_type, extra)

    def same_origin(self):
        allowed_origins = {
            f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}",
        }
        origin = self.headers.get("Origin")
        fetch_site = self.headers.get("Sec-Fetch-Site", "same-origin")
        return (not origin or origin in allowed_origins) and fetch_site in {"same-origin", "none"}

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if not 1 <= length <= 4096:
            raise ValueError("Invalid request size")
        return json.loads(self.rfile.read(length))

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self.require_auth():
            return
        parsed = urlsplit(self.path)
        path = parsed.path
        root = self.server.root
        if path == "/":
            self.send_file(STATIC_ROOT / "index.html", "text/html; charset=utf-8")
        elif path == "/app.js":
            self.send_file(STATIC_ROOT / "app.js", "text/javascript; charset=utf-8")
        elif path == "/style.css":
            self.send_file(STATIC_ROOT / "style.css", "text/css; charset=utf-8")
        elif path == "/favicon.ico":
            self.send_bytes(204, b"", "image/x-icon")
        elif path == "/api/status":
            self.send_json(200, status(root))
        elif path == "/api/clients":
            self.send_json(200, client_lists(root))
        elif path in {"/download/wireguard", "/download/vless", "/download/vless-json"}:
            try:
                name = validate_client_name(parse_qs(parsed.query).get("name", ["client1"])[0])
            except ValueError as error:
                self.send_json(400, {"error": str(error)})
                return
            if path == "/download/wireguard":
                self.send_file(root / "clients" / "wireguard" / f"{name}.conf",
                               "application/x-wireguard-profile", f"{name}.conf")
            elif path == "/download/vless":
                self.send_file(root / "clients" / "vless" / f"{name}.txt",
                               "text/plain; charset=utf-8", f"{name}-vless.txt")
            else:
                self.send_file(root / "clients" / "vless" / f"{name}.json",
                               "application/json", f"{name}.json")
        else:
            self.send_json(404, {"error": "Not found"})

    def do_POST(self):
        if not self.require_auth():
            return
        path = urlsplit(self.path).path
        if path not in {"/api/restart", "/api/clients"}:
            self.send_json(404, {"error": "Not found"})
            return
        if not self.same_origin():
            self.send_json(403, {"error": "Cross-site request rejected"})
            return
        try:
            payload = self.read_json()
            if path == "/api/restart":
                service = payload["service"]
                if service not in SERVICES:
                    raise ValueError("Unknown service")
                ok, message = restart_service(service)
                self.send_json(200 if ok else 500, {"ok": ok, "service": service, "status": message})
                return
            result = mutate_client(
                self.server.root,
                self.server.etc_root,
                payload["protocol"],
                payload["action"],
                payload.get("name", ""),
            )
            self.send_json(200, result)
        except (ValueError, KeyError, json.JSONDecodeError) as error:
            self.send_json(400, {"error": str(error) or "Invalid request"})
        except Exception as error:
            self.send_json(500, {"error": f"Не удалось применить изменение: {str(error)[-500:]}"})


def make_server(root, host="127.0.0.1", port=8080, etc_root=Path("/etc")):
    return PanelServer((host, port), Handler, root, Path(etc_root))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--init-auth", action="store_true")
    parser.add_argument("--reset-password", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    if args.init_auth or args.reset_password:
        password = ensure_auth(root, reset=args.reset_password)
        if password:
            print(password)
        return
    if args.host != "127.0.0.1":
        raise SystemExit("The panel must listen on 127.0.0.1 only")
    server = make_server(root, args.host, args.port)
    print(f"FreeNETvpn-mini panel: http://127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
