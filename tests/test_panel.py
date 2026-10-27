import base64
from http.client import HTTPConnection
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from panel import server as panel  # noqa: E402


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.password = panel.ensure_auth(self.root)
        (self.root / ".env").write_text("SERVER_HOST=198.51.100.10\nWG_PORT=51820\nVLESS_PORT=443\n")
        (self.root / "clients" / "wireguard").mkdir(parents=True)
        (self.root / "clients" / "vless").mkdir(parents=True)
        (self.root / "clients" / "wireguard" / "client1.conf").write_text("test-wg-config\n")
        (self.root / "clients" / "vless" / "client1.txt").write_text("vless://test\n")
        (self.root / "clients" / "vless" / "client1.json").write_text('{"test": true}\n')
        self.httpd = panel.make_server(self.root, port=0)
        self.port = self.httpd.server_port
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        token = base64.b64encode(f"admin:{self.password}".encode()).decode()
        self.headers = {"Authorization": f"Basic {token}"}

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(method, path, body=body, headers={**self.headers, **(headers or {})})
        response = connection.getresponse()
        content = response.read()
        result = response.status, dict(response.getheaders()), content
        connection.close()
        return result

    def test_authentication_and_status(self):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request("GET", "/")
        response = connection.getresponse()
        response.read()
        self.assertEqual(response.status, 401)
        connection.close()

        status, headers, content = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        data = json.loads(content)
        self.assertEqual(data["serverHost"], "198.51.100.10")
        self.assertEqual(data["vlessUri"], "vless://test")
        self.assertTrue(data["clients"]["wireguard"])

    def test_download_and_static_page(self):
        status, _, content = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b'<meta name="viewport"', content)
        status, headers, content = self.request("GET", "/download/wireguard")
        self.assertEqual(status, 200)
        self.assertEqual(content, b"test-wg-config\n")
        self.assertIn("attachment", headers["Content-Disposition"])

    def test_restart_and_cross_site_protection(self):
        with patch("panel.server.restart_service", return_value=(True, "active")) as restart:
            status, _, content = self.request(
                "POST", "/api/restart", body=json.dumps({"service": "vless"}),
                headers={"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.port}"},
            )
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(content)["ok"])
            restart.assert_called_once_with("vless")

        with patch("panel.server.restart_service") as restart:
            status, _, _ = self.request(
                "POST", "/api/restart", body=json.dumps({"service": "vless"}),
                headers={"Content-Type": "application/json", "Origin": "https://attacker.invalid"},
            )
            self.assertEqual(status, 403)
            restart.assert_not_called()

    def test_password_file_contains_no_plaintext(self):
        record = json.loads((self.root / "data" / "panel.json").read_text())
        self.assertNotIn(self.password, json.dumps(record))
        self.assertTrue(panel.verify_password("admin", self.password, record))
        self.assertFalse(panel.verify_password("admin", "wrong", record))

    def test_password_reset_applies_without_panel_restart(self):
        new_password = panel.ensure_auth(self.root, reset=True)
        status, _, _ = self.request("GET", "/")
        self.assertEqual(status, 401)
        token = base64.b64encode(f"admin:{new_password}".encode()).decode()
        status, _, _ = self.request("GET", "/", headers={"Authorization": f"Basic {token}"})
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
