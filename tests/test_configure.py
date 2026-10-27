import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from urllib.parse import parse_qs, urlparse

PROJECT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT / "tools" / "configure.py"


class ConfigureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def configure(self, host="198.51.100.10", extra=None):
        command = ["python3", str(SCRIPT), "--root", str(self.root), "--host", host,
                   "--wan-interface", "eth0"]
        subprocess.run(command, check=True, capture_output=True, text=True, env={**os.environ, **(extra or {})})

    def test_generates_both_clients_and_persistent_secrets(self):
        self.configure()
        state_path = self.root / "data" / "state.json"
        first = json.loads(state_path.read_text())
        wg = (self.root / "clients" / "wireguard" / "client1.conf").read_text()
        self.assertIn("Endpoint = 198.51.100.10:51820", wg)
        self.assertIn("AllowedIPs = 0.0.0.0/0", wg)
        self.assertIn(first["wireguard"]["client"]["private"], wg)

        uri = (self.root / "clients" / "vless" / "client1.txt").read_text().strip()
        parsed = urlparse(uri)
        query = parse_qs(parsed.query)
        self.assertEqual(parsed.scheme, "vless")
        self.assertEqual(parsed.hostname, "198.51.100.10")
        self.assertEqual(parsed.port, 443)
        self.assertEqual(query["security"], ["reality"])
        self.assertEqual(query["flow"], ["xtls-rprx-vision"])
        self.assertEqual(query["pbk"], [first["vless"]["public"]])
        self.assertEqual(stat.S_IMODE(state_path.stat().st_mode), 0o600)

        self.configure(host="vpn.example.com")
        second = json.loads(state_path.read_text())
        self.assertEqual(first, second)
        self.assertIn("Endpoint = vpn.example.com:51820",
                      (self.root / "clients" / "wireguard" / "client1.conf").read_text())

    def test_server_configuration_matches_client(self):
        self.configure()
        state = json.loads((self.root / "data" / "state.json").read_text())
        server = json.loads((self.root / "runtime" / "xray.json").read_text())
        client = json.loads((self.root / "clients" / "vless" / "client1.json").read_text())
        inbound = server["inbounds"][0]
        self.assertEqual(inbound["settings"]["clients"][0]["id"], state["vless"]["uuid"])
        self.assertEqual(inbound["streamSettings"]["realitySettings"]["privateKey"], state["vless"]["private"])
        outbound = client["outbounds"][0]
        self.assertEqual(outbound["streamSettings"]["realitySettings"]["publicKey"], state["vless"]["public"])
        self.assertEqual(outbound["settings"]["vnext"][0]["users"][0]["id"], state["vless"]["uuid"])

    def test_rejects_invalid_port(self):
        result = subprocess.run(
            ["python3", str(SCRIPT), "--root", str(self.root), "--host", "198.51.100.10",
             "--wan-interface", "eth0"],
            capture_output=True, text=True, env={**os.environ, "WG_PORT": "70000"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("WG_PORT", result.stderr)


if __name__ == "__main__":
    unittest.main()
