"""Isolated filesystem, fake Caddy executable and localhost fake admin API."""

import contextlib
import http.server
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock


CLI = Path(__file__).resolve().parents[1] / "preview"
loader = importlib.machinery.SourceFileLoader("preview_cli", str(CLI))
spec = importlib.util.spec_from_loader(loader.name, loader)
preview = importlib.util.module_from_spec(spec)
loader.exec_module(preview)

FAKE_CADDY = '''#!/usr/bin/env python3
import json, pathlib, re, sys
source = sys.stdin.read()
if "INVALID" in source:
    print("sensitive fixture diagnostic", file=sys.stderr)
    sys.exit(1)
paths = re.findall(r'^import "([^"\\n]+/gen-[^"\\n]+/Caddyfile)"$', source, re.M)
if len(paths) != 1:
    sys.exit(2)
generation = pathlib.Path(paths[0]).parent
state = json.loads((generation / "state.json").read_text())
assert "reverse_proxy 127.0.0.1:" in "".join(p.read_text() for p in (generation / "routes").glob("*.caddy")) or not state["routes"]
print(json.dumps({"fixture": state, "existing_services": "preserved"}))
'''


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.loads.append(data)
        self.server.paths.append(self.path)
        if self.server.failures:
            self.server.failures -= 1
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"sensitive fixture diagnostic")
        else:
            self.server.live = data
            self.send_response(200)
            self.end_headers()

    def log_message(self, *args):
        pass


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "registry"
        self.root.mkdir(mode=0o750)
        self.config = self.base / "Caddyfile"
        self.config.write_text('existing.example {\n respond "unchanged"\n}\nimport "'
                               + str(self.root / "current/Caddyfile") + '"\n')
        self.fake = self.base / "fake-caddy"
        self.fake.write_text(FAKE_CADDY)
        self.fake.chmod(0o755)
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.loads = []
        self.server.paths = []
        self.server.live = None
        self.server.failures = 0
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.options = ["--directory", str(self.root), "--caddyfile", str(self.config),
                        "--caddy", str(self.fake), "--admin",
                        f"http://127.0.0.1:{self.server.server_port}"]
        self.run_cli("init")
        self.original = self.config.read_bytes()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temporary.cleanup()

    def run_cli(self, *args, code=0):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = preview.main(self.options + list(args))
        self.assertEqual(result, code, err.getvalue())
        self.assertNotIn("sensitive fixture diagnostic", err.getvalue())
        return out.getvalue(), err.getvalue()

    def routes(self):
        return json.loads((self.root / "current/state.json").read_text())["routes"]

    def test_lifecycle_and_existing_config(self):
        output, warning = self.run_cli("register", "checkout-test", "--port", "5173", "--ttl", "2h")
        self.assertEqual(output.strip(), "https://checkout-test.dev.hp.affablegecko.net")
        self.assertIn("reusing this port", warning)
        self.assertEqual(self.server.paths, ["/load"])
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertEqual(self.server.live["existing_services"], "preserved")
        self.assertEqual(self.routes()["checkout-test"]["port"], 5173)
        self.assertEqual(json.loads(self.run_cli("list", "--json")[0])[0]["name"], "checkout-test")
        self.run_cli("unregister", "checkout-test")
        self.assertEqual(self.routes(), {})
        self.assertEqual(len(list(self.root.glob("gen-*"))), 1)
        self.assertFalse((self.root / "pending.json").exists())

    def test_collisions_and_renewal(self):
        self.run_cli("register", "one", "--port", "5173", "--ttl", "1s")
        expiry = self.routes()["one"]["expires"]
        self.run_cli("register", "one", "--port", "5173", "--ttl", "1d")
        self.assertGreater(self.routes()["one"]["expires"], expiry)
        self.run_cli("register", "one", "--port", "5174", code=1)
        self.run_cli("register", "two", "--port", "5173", code=1)
        self.assertEqual(len(self.server.loads), 2)

    def test_prune_and_expired_still_listed(self):
        with mock.patch.object(preview.time, "time", return_value=time.time() - 20):
            self.run_cli("register", "old", "--port", "5173", "--ttl", "1s")
        self.run_cli("register", "fresh", "--port", "5174")
        self.assertIn("EXPIRED", self.run_cli("list")[0])
        self.run_cli("prune")
        self.assertEqual(set(self.routes()), {"fresh"})
        loads = len(self.server.loads)
        self.run_cli("prune")
        self.assertEqual(len(self.server.loads), loads)

    def test_adaptation_failure_never_publishes_or_reloads(self):
        before = (self.root / "current").readlink()
        self.config.write_text(self.config.read_text() + "\nINVALID\n")
        self.run_cli("register", "one", "--port", "5173", code=1)
        self.assertEqual((self.root / "current").readlink(), before)
        self.assertEqual(self.server.loads, [])
        self.assertEqual(len(list(self.root.glob("gen-*"))), 1)

    def test_missing_main_import_refused(self):
        self.config.write_text('existing.example { respond "unchanged" }\n')
        self.run_cli("register", "one", "--port", "5173", code=1)
        self.assertEqual(self.server.loads, [])

    def test_reload_failure_rolls_back_disk_and_live_state(self):
        self.run_cli("register", "one", "--port", "5173")
        before = (self.root / "current").readlink()
        self.server.failures = 1
        self.run_cli("register", "two", "--port", "5174", code=1)
        self.assertEqual((self.root / "current").readlink(), before)
        self.assertEqual(set(self.routes()), {"one"})
        self.assertEqual(set(self.server.live["fixture"]["routes"]), {"one"})
        self.assertFalse((self.root / "pending.json").exists())

    def test_uncertain_reload_requires_recovery(self):
        self.server.failures = 2
        self.run_cli("register", "one", "--port", "5173", code=1)
        self.assertTrue((self.root / "pending.json").exists())
        loads = len(self.server.loads)
        self.run_cli("register", "two", "--port", "5174", code=1)
        self.assertEqual(len(self.server.loads), loads)
        self.run_cli("list", code=1)
        self.run_cli("recover")
        self.assertFalse((self.root / "pending.json").exists())
        self.assertEqual(self.routes(), {})

    def test_interrupted_publish_recovers_published_generation(self):
        registry = preview.Registry(preview.parser().parse_args(self.options + ["list"]))
        with mock.patch.object(registry, "reload", side_effect=KeyboardInterrupt):
            with registry.locked(), self.assertRaises(KeyboardInterrupt):
                registry.apply(registry.current(), {"one": {"port": 5173, "owner": os.getuid(), "expires": time.time() + 10}})
        self.assertTrue((self.root / "pending.json").exists())
        self.run_cli("recover")
        self.assertEqual(set(self.server.live["fixture"]["routes"]), {"one"})

    def test_ownership_protected(self):
        self.run_cli("register", "one", "--port", "5173")
        path = self.root / "current/state.json"
        data = json.loads(path.read_text())
        data["routes"]["one"]["owner"] = os.getuid() + 1
        path.write_text(json.dumps(data))
        self.run_cli("register", "one", "--port", "5173", code=1)
        self.run_cli("unregister", "one", code=1)

    def test_restrictive_umask_and_file_modes(self):
        original = os.umask(0o077)
        try:
            self.run_cli("register", "one", "--port", "5173")
        finally:
            os.umask(original)
        self.assertEqual((self.root / "current").stat().st_mode & 0o777, 0o755)
        self.assertEqual((self.root / "current/routes").stat().st_mode & 0o777, 0o755)
        self.assertEqual((self.root / "current/routes/one.caddy").stat().st_mode & 0o777, 0o644)
        self.assertEqual((self.root / "current/state.json").stat().st_mode & 0o777, 0o600)

    def test_concurrent_processes_do_not_lose_updates(self):
        commands = [[str(CLI), *self.options, "register", f"route-{i}", "--port", str(5100 + i)]
                    for i in range(6)]
        processes = [subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for cmd in commands]
        for process in processes:
            out, err = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, (out, err))
        self.assertEqual(len(self.routes()), 6)
        self.assertEqual(len(self.server.live["fixture"]["routes"]), 6)

    def test_admin_port_refused(self):
        self.run_cli("register", "admin", "--port", "2019", code=1)
        self.run_cli("register", "admin", "--port", str(self.server.server_port), code=1)
        self.assertEqual(self.server.loads, [])

    def test_noop_prune_and_init_never_reload(self):
        self.run_cli("prune")
        self.assertEqual(self.server.loads, [])
        self.run_cli("init", code=1)

    def test_non_loopback_admin_refused(self):
        self.options += ["--admin", "http://example.com:2019"]
        self.run_cli("list", code=1)

    def test_redirect_not_followed(self):
        handler = preview.NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "", {}, "http://example.com"))

    def test_writable_shared_directory_refused(self):
        self.root.chmod(0o770)
        self.run_cli("list", code=1)

    @unittest.skipUnless(shutil.which("caddy"), "real caddy not installed")
    def test_real_caddy_adapts_combined_config_without_starting_server(self):
        # Internal issuer only; no credentials, DNS, TLS issuance or live API calls.
        self.config.write_text('''{
    auto_https disable_redirects
}
(cloudflare_tls) {
    bind 100.90.71.107
    tls internal
}
existing.example {
    respond "unchanged"
}
import "''' + str(self.root / "current/Caddyfile") + '"\n')
        self.options += ["--caddy", shutil.which("caddy")]
        self.run_cli("recover")  # Empty glob must also adapt.
        self.run_cli("register", "checkout-test", "--port", "5173")
        self.run_cli("register", "another", "--port", "5174")
        config = self.server.live
        servers = config["apps"]["http"]["servers"].values()
        serialized = json.dumps(list(servers))
        self.assertIn("existing.example", serialized)
        self.assertIn("checkout-test.dev.hp.affablegecko.net", serialized)
        self.assertIn("127.0.0.1:5173", serialized)
        self.assertIn("100.90.71.107", serialized)
        self.run_cli("unregister", "checkout-test")
        self.config.write_text(self.config.read_text() + '\nreserved.dev.hp.affablegecko.net {\n respond "existing service"\n}\n')
        before = len(self.server.loads)
        self.run_cli("register", "reserved", "--port", "5175", code=1)
        self.assertEqual(len(self.server.loads), before)


class InputTests(unittest.TestCase):
    def test_names(self):
        for bad in ("../oops", "A", "a.b", "-a", "a-", "a" * 64, "x\n}"):
            with self.subTest(bad=bad), self.assertRaises(Exception):
                preview.name_value(bad)
        for good in ("a", "a-b", "a" * 63):
            self.assertEqual(preview.name_value(good), good)

    def test_ports_and_ttls(self):
        for bad in ("0", "1023", "65536", "http", "5173\nrespond"):
            with self.subTest(bad=bad), self.assertRaises(Exception):
                preview.port_value(bad)
        for bad in ("0h", "-2h", "2", "8d", "1.5h", "1h\n}"):
            with self.subTest(bad=bad), self.assertRaises(Exception):
                preview.ttl_value(bad)
        self.assertEqual(preview.ttl_value("2h"), 7200)
        self.assertEqual(preview.port_value("65535"), 65535)


if __name__ == "__main__":
    unittest.main()
