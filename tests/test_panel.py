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
from tools import configure  # noqa: E402


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "opt"
        self.etc = Path(self.temporary.name) / "etc"
        config = configure.load_config(self.root, host="198.51.100.10", wan_interface="eth0")
        configure.save_configuration(self.root, config, configure.load_state(self.root, config))
        self.password = panel.ensure_auth(self.root)
        self.httpd = panel.make_server(self.root, port=0, etc_root=self.etc)
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
        connection = HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request(method, path, body=body, headers={**self.headers, **(headers or {})})
        response = connection.getresponse()
        content = response.read()
        result = response.status, dict(response.getheaders()), content
        connection.close()
        return result

    def post(self, path, payload, origin=None):
        return self.request(
            "POST", path, body=json.dumps(payload),
            headers={
                "Content-Type": "application/json",
                "Origin": origin or f"http://127.0.0.1:{self.port}",
            },
        )

    def state(self):
        return json.loads((self.root / "data" / "state.json").read_text())

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
        self.assertTrue(data["vlessUri"].startswith("vless://"))
        self.assertEqual(len(data["clientLists"]["wireguard"]), 1)
        self.assertEqual(data["clientLists"]["wireguard"][0]["address"], "10.8.0.2/32")

    def test_download_and_static_page(self):
        status, _, content = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b'id="create-client-form"', content)
        status, headers, content = self.request("GET", "/download/wireguard?name=client1")
        self.assertEqual(status, 200)
        self.assertIn(b"[Interface]", content)
        self.assertIn("attachment", headers["Content-Disposition"])
        status, _, _ = self.request("GET", "/download/wireguard?name=../state")
        self.assertEqual(status, 400)

    def test_restart_and_cross_site_protection(self):
        with patch("panel.server.restart_service", return_value=(True, "active")) as restart:
            status, _, content = self.post("/api/restart", {"service": "vless"})
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(content)["ok"])
            restart.assert_called_once_with("vless")

        with patch("panel.server.restart_service") as restart:
            status, _, _ = self.post(
                "/api/clients", {"protocol": "vless", "action": "create", "name": "phone"},
                origin="https://attacker.invalid",
            )
            self.assertEqual(status, 403)
            restart.assert_not_called()
            self.assertEqual(len(self.state()["vless"]["clients"]), 1)

    def test_create_regenerate_download_and_delete_clients(self):
        with patch("panel.server.restart_service", return_value=(True, "active")) as restart:
            status, _, content = self.post(
                "/api/clients", {"protocol": "wireguard", "action": "create", "name": "phone"},
            )
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(content)["name"], "phone")
            self.assertEqual(self.state()["wireguard"]["clients"][1]["address"], "10.8.0.3/32")
            status, _, content = self.request("GET", "/download/wireguard?name=phone")
            self.assertEqual(status, 200)
            self.assertIn(b"Address = 10.8.0.3/32", content)

            self.post("/api/clients", {"protocol": "vless", "action": "create", "name": "phone"})
            old_uuid = self.state()["vless"]["clients"][1]["uuid"]
            status, _, _ = self.post(
                "/api/clients", {"protocol": "vless", "action": "regenerate", "name": "phone"},
            )
            self.assertEqual(status, 200)
            self.assertNotEqual(self.state()["vless"]["clients"][1]["uuid"], old_uuid)
            self.assertEqual(len(json.loads((self.etc / "xray/config.json").read_text())["inbounds"][0]["settings"]["clients"]), 2)

            status, _, _ = self.post(
                "/api/clients", {"protocol": "vless", "action": "delete", "name": "phone"},
            )
            self.assertEqual(status, 200)
            self.assertFalse((self.root / "clients" / "vless" / "phone.txt").exists())
            self.assertFalse((self.root / "clients" / "vless" / "phone.json").exists())
            status, _, _ = self.request("GET", "/download/vless?name=phone")
            self.assertEqual(status, 404)

            status, _, content = self.post(
                "/api/clients", {"protocol": "vless", "action": "delete", "name": "client1"},
            )
            self.assertEqual(status, 400)
            self.assertIn("последнего", json.loads(content)["error"])
            self.assertGreaterEqual(restart.call_count, 4)

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
