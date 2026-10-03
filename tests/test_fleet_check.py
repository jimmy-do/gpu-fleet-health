"""Tests for scripts/fleet_check.py. Run: python3 -m unittest discover -s tests -v"""
import contextlib
import http.server
import io
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
sys.path.insert(0, str(ROOT / "scripts"))
import fleet_check  # noqa: E402


def run(*argv):
    """Run fleet_check.main, returning (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = fleet_check.main([str(a) for a in argv])
    return code, out.getvalue(), err.getvalue()


def free_port():
    """A localhost port with nothing listening on it."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FixtureTests(unittest.TestCase):
    def test_healthy_fleet_exits_zero_without_commands(self):
        code, out, _ = run(FIXTURES / "healthy.json")
        self.assertEqual(code, 0)
        self.assertNotIn("kubectl", out)
        self.assertIn("10 evaluated, 0 unhealthy", out)

    def test_unhealthy_node_is_dry_run_by_default(self):
        with mock.patch("subprocess.run") as run_mock:
            code, out, _ = run(FIXTURES / "unhealthy.json")
        self.assertEqual(code, 1)
        run_mock.assert_not_called()
        self.assertIn("UNHEALTHY gpu-node-03: GPU missing (7/8); 1 XID error(s)", out)
        self.assertIn("DRY RUN: would run: kubectl cordon gpu-node-03", out)
        self.assertIn("DRY RUN: would run: kubectl drain gpu-node-03", out)

    def test_execute_runs_cordon_then_drain(self):
        with mock.patch("subprocess.run") as run_mock:
            code, _, _ = run(FIXTURES / "unhealthy.json", "--execute")
        self.assertEqual(code, 1)
        cmds = [c.args[0][:3] for c in run_mock.call_args_list]
        self.assertEqual(cmds, [["kubectl", "cordon", "gpu-node-03"],
                                ["kubectl", "drain", "gpu-node-03"]])

    def test_execute_stops_when_kubectl_fails(self):
        err = fleet_check.subprocess.CalledProcessError(1, ["kubectl"])
        with mock.patch("subprocess.run", side_effect=err) as run_mock:
            code, _, stderr = run(FIXTURES / "unhealthy.json", "--execute")
        self.assertEqual(code, 4)
        self.assertEqual(run_mock.call_count, 1)  # never drains after a failed cordon
        self.assertIn("failed", stderr)

    def test_incomplete_records_are_skipped_with_reason(self):
        code, out, _ = run(FIXTURES / "incomplete.json")
        for expected in [
            "SKIP gpu-node-04: missing 'max_temp_c'",
            "SKIP gpu-node-05: missing 'gpus_found'",
            "SKIP gpu-node-06: 'max_temp_c' has wrong type (str)",
            "SKIP record[6]: missing 'name'",
            "SKIP gpu-node-08: 'xid_errors' has wrong type (bool)",
            "SKIP --all: invalid node name '--all'",
        ]:
            self.assertIn(expected, out)
        self.assertIn("11 evaluated, 1 unhealthy, 6 skipped", out)
        self.assertEqual(code, 1)  # 1/11 = 9% is under the limit
        self.assertNotIn("kubectl cordon --all", out)

    def test_refuses_when_over_ten_percent_unhealthy(self):
        with mock.patch("subprocess.run") as run_mock:
            code, out, _ = run(FIXTURES / "too_many_unhealthy.json", "--execute")
        self.assertEqual(code, 2)
        run_mock.assert_not_called()
        self.assertIn("REFUSING TO ACT: 3/10 nodes (30%)", out)
        self.assertNotIn("kubectl", out)

    def test_limit_is_configurable(self):
        code, out, _ = run(FIXTURES / "too_many_unhealthy.json", "--max-unhealthy-pct", "50")
        self.assertEqual(code, 1)
        self.assertEqual(out.count("would run: kubectl cordon"), 3)


class SourceErrorTests(unittest.TestCase):
    def test_connection_refused(self):
        code, _, err = run(f"http://127.0.0.1:{free_port()}/nodes.json")
        self.assertEqual(code, 3)
        self.assertIn("connection refused", err)

    def test_timeout(self):
        # Listening socket that never accepts: the TCP connect succeeds via
        # the backlog, but no HTTP response ever comes back.
        with socket.socket() as srv:
            srv.bind(("127.0.0.1", 0))
            srv.listen(1)
            url = f"http://127.0.0.1:{srv.getsockname()[1]}/nodes.json"
            code, _, err = run(url, "--timeout", "0.5")
        self.assertEqual(code, 3)
        self.assertIn("timed out after 0.5s", err)

    def test_missing_file(self):
        code, _, err = run(FIXTURES / "does-not-exist.json")
        self.assertEqual(code, 3)
        self.assertIn("cannot read", err)

    def test_bad_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text('[{"name": "gpu-node-01"}]')
            code, _, err = run(bad)
        self.assertEqual(code, 3)
        self.assertIn('"nodes" list', err)

    def test_reads_report_over_http(self):
        body = (FIXTURES / "healthy.json").read_bytes()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):  # keep test output quiet
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        code, out, _ = run(f"http://127.0.0.1:{server.server_port}/nodes.json")
        self.assertEqual(code, 0)
        self.assertIn("10 evaluated", out)


if __name__ == "__main__":
    unittest.main()
