"""Role permission smoke probes (IMPLEMENTATION-PLAN §3.1).

Engine-level: the probes describe what a sandbox must refuse, independently of
which target is being run.
"""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from orchestrator.pack import smoke


class SmokeTest(unittest.TestCase):
    """§3.1: assertions are per named path, never a universal claim."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_a_writable_path_is_reported_as_a_failure(self) -> None:
        results = smoke.write_probes("producer", [self.tmp / "approvals" / "probe"])
        self.assertEqual(results[0]["observed"], "written")
        self.assertEqual(results[0]["status"], smoke.REFUSED)

    def test_an_unwritable_path_passes(self) -> None:
        blocked = self.tmp / "ro"
        blocked.mkdir()
        blocked.chmod(0o500)
        self.addCleanup(lambda: blocked.chmod(0o700))
        results = smoke.write_probes("reviewer", [blocked / "probe"])
        self.assertEqual(results[0]["status"], smoke.RELEASED)

    # A closed port refuses identically to a blocked one, so the probe starts a
    # real listener and asks whether the sandbox stopped a reachable connection.
    def test_network_probe_starts_a_real_listener(self) -> None:
        result = smoke.network_probe("reviewer")
        self.assertTrue(result["listener_started"])
        self.assertEqual(result["observed"], "reachable")
        self.assertEqual(result["status"], smoke.REFUSED)

    def test_network_probe_passes_when_the_connection_is_blocked(self) -> None:
        result = smoke.network_probe("reviewer", connect=lambda host, port: False)
        self.assertEqual(result["status"], smoke.RELEASED)

    def test_feature_probe_records_output(self) -> None:
        def fake(argv):
            return subprocess.CompletedProcess(argv, 0, "browser_use: disabled\n", "")

        result = smoke.feature_probe("codex", run=fake)
        self.assertIn("browser_use", result["observed"])

    def test_report_is_sealed_and_hashed(self) -> None:
        probes = [smoke.network_probe("reviewer", connect=lambda h, p: False)]
        report = smoke.build_report(target_id="acme", target_version="0.1.0",
                                    cli_versions={"codex": "0.154"}, probes=probes)
        ref = smoke.seal(report, self.tmp / "smoke.json")
        self.assertTrue(ref.startswith("sha256:"))
        self.assertTrue(report["passed"])
        # The same report must hash the same, or the contract cannot bind it.
        self.assertEqual(ref, smoke.seal(report, self.tmp / "smoke2.json"))

    def test_a_failed_probe_fails_the_report(self) -> None:
        probes = [smoke.network_probe("reviewer")]
        report = smoke.build_report(target_id="acme", target_version="0.1.0",
                                    cli_versions={}, probes=probes)
        self.assertFalse(report["passed"])
        self.assertIn("network", report["failed_probes"])


if __name__ == "__main__":
    unittest.main()
