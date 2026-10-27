#!/usr/bin/env python3
"""Loopback-only administration panel for FreeNETvpn-mini."""

import argparse
import base64
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import time
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = Path(__file__).resolve().parent / "static"
AUTH_ITERATIONS = 600_000
SERVICES = {"wireguard": "wg-quick@wg0", "vless": "freenetvpn-xray"}


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
    if transfer.returncode == 0 and transfer.stdout.split():
        fields = transfer.stdout.split()
        if len(fields) >= 3:
            result["rx"], result["tx"] = int(fields[1]), int(fields[2])
    handshake = command("wg", "show", "wg0", "latest-handshakes")
    if handshake.returncode == 0 and handshake.stdout.split():
        fields = handshake.stdout.split()
        if len(fields) >= 2:
            timestamp = int(fields[1])
            result["handshake"] = timestamp or None
    return result


def status(root):
    env = read_env(root)
    disk = shutil.disk_usage("/")
    xray = command("/usr/local/bin/xray", "version")
    version = xray.stdout.splitlines()[0] if xray.returncode == 0 and xray.stdout else "unknown"
    return {
        "serverHost": env.get("SERVER_HOST", "unknown"),
        "ports": {"wireguard": env.get("WG_PORT", "51820"), "vless": env.get("VLESS_PORT", "443")},
        "services": {name: service_status(unit) for name, unit in SERVICES.items()},
        "wireguard": wireguard_status(),
        "xrayVersion": version,
        "disk": {"total": disk.total, "used": disk.used, "free": disk.free},
        "memory": memory_status(),
        "clients": {
            "wireguard": (root / "clients" / "wireguard" / "client1.conf").exists(),
            "vless": (root / "clients" / "vless" / "client1.txt").exists(),
        },
        "vlessUri": (root / "clients" / "vless" / "client1.txt").read_text().strip()
        if (root / "clients" / "vless" / "client1.txt").exists() else "",
        "timestamp": int(time.time()),
    }


class PanelServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, root):
        self.root = root
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

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self.require_auth():
            return
        path = urlsplit(self.path).path
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
        elif path == "/download/wireguard":
            self.send_file(root / "clients" / "wireguard" / "client1.conf",
                           "application/x-wireguard-profile", "client1.conf")
        elif path == "/download/vless":
            self.send_file(root / "clients" / "vless" / "client1.txt", "text/plain; charset=utf-8", "vless.txt")
        elif path == "/download/vless-json":
            self.send_file(root / "clients" / "vless" / "client1.json", "application/json", "vless.json")
        else:
            self.send_json(404, {"error": "Not found"})

    def do_POST(self):
        if not self.require_auth():
            return
        path = urlsplit(self.path).path
        if path != "/api/restart":
            self.send_json(404, {"error": "Not found"})
            return
        allowed_origins = {
            f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}",
        }
        origin = self.headers.get("Origin")
        fetch_site = self.headers.get("Sec-Fetch-Site", "same-origin")
        if (origin and origin not in allowed_origins) or fetch_site not in {"same-origin", "none"}:
            self.send_json(403, {"error": "Cross-site request rejected"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= 4096:
                raise ValueError("Invalid request size")
            payload = json.loads(self.rfile.read(length))
            service = payload["service"]
            if service not in SERVICES:
                raise ValueError("Unknown service")
        except (ValueError, KeyError, json.JSONDecodeError):
            self.send_json(400, {"error": "Expected a valid service"})
            return
        ok, message = restart_service(service)
        self.send_json(200 if ok else 500, {"ok": ok, "service": service, "status": message})


def make_server(root, host="127.0.0.1", port=8080):
    return PanelServer((host, port), Handler, root)


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
