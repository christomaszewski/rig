"""Probe protocol, typed preflight and machine-readable CLI failure contracts."""
import contextlib
import copy
import io
import json
import subprocess
import unittest
from unittest.mock import patch

import test_sim_workflow as fixtures
from rig_cli import RigError, cli, sim_workflow as sim


class ProbeProtocol(unittest.TestCase):
    def request(self, stdout, rc=0):
        with patch("rig_cli.sim_workflow.subprocess.run",
                   return_value=subprocess.CompletedProcess([], rc, stdout, "diagnostic")):
            return sim.probe({"ros": {}, "probe": ["/chosen/python"], "timeout_s": 2}, {})

    def test_invalid_protocol_cannot_claim_readiness(self):
        good = {"version": 1, "ready": True, "issues": []}
        variants = [[], {**good, "version": True}, {**good, "ready": 1},
                    {**good, "issues": {}}, {**good, "issues": ["oops"]},
                    {**good, "issues": [{"status": "error", "code": "clock", "message": "paused"}]},
                    {**good, "issues": [{"status": "ok", "code": [], "message": "ok"}]}]
        for response in variants:
            with self.subTest(response=response), self.assertRaisesRegex(RigError, "ROS probe failed"):
                self.request(json.dumps(response))
        for response in ("not JSON", "{}\n{}", ""):
            with self.subTest(response=response), self.assertRaises(RigError):
                self.request(response)
        with self.assertRaisesRegex(RigError, "exited unsuccessfully"):
            self.request(json.dumps(good), rc=1)

    def test_negative_probe_preserves_actionable_diagnostics(self):
        response = {"version": 1, "ready": False, "issues": [
            {"status": "error", "code": "clock", "message": "clock paused"}]}
        self.assertEqual(self.request(json.dumps(response), rc=1), response)

    def test_missing_python_and_timeout_are_actionable(self):
        for error in (FileNotFoundError("no Python"), subprocess.TimeoutExpired("probe", 12)):
            with patch("rig_cli.sim_workflow.subprocess.run", side_effect=error):
                with self.assertRaisesRegex(RigError, "simulation.probe"):
                    sim.probe({"ros": {}}, {})

    def test_probe_argv_is_not_shell_interpreted(self):
        with patch("rig_cli.sim_workflow.subprocess.run", return_value=subprocess.CompletedProcess(
                [], 0, '{"version":1,"ready":true,"issues":[]}', "")) as run:
            sim.probe({"ros": {}, "probe": ["python path;literal", "-I"], "timeout_s": 3}, {"ROS_DOMAIN_ID": "7"})
        args, kwargs = run.call_args
        self.assertEqual(args[0][:3], ["python path;literal", "-I", "-c"])
        self.assertFalse(kwargs.get("shell", False))
        self.assertEqual(kwargs["timeout"], 13)
        self.assertEqual(json.loads(kwargs["input"])["timeout_s"], 3)


class PreflightContracts(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.Simulation()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def test_wall_clock_and_unbound_replacements_are_blocked(self):
        for mutate, expected in ((lambda d: d["session"].update(clock="wall"), "clock"),
                (lambda d: d["simulation"].update(replace=["camera", "unknown"]), "replacement"),
                (lambda d: d["simulation"].update(publishes=[]), "replacement")):
            doc = copy.deepcopy(self.f.doc)
            mutate(doc)
            _, _, plan = self.f.build(doc, host=False)
            self.assertFalse(plan["ready_to_run"])
            self.assertTrue(any(i["code"] == expected and i["status"] == "error" for i in plan["issues"]))

    def test_unknown_service_and_missing_autonomy_clock_contract_are_blocked(self):
        self.f.write("services/planner/rigging.yaml", {"service": "planner", "launcher": "up", "tier": "autonomy"})
        _, _, plan = self.f.build(host=False)
        self.assertTrue(any(i["code"] == "sim_time" and i["status"] == "error" for i in plan["issues"]))
        self.f.write("services.yaml", {"services": {}})
        _, _, plan = self.f.build(host=False)
        self.assertFalse(plan["ready_to_run"])
        self.assertTrue(any(i["code"] == "config" for i in plan["issues"]))

    def test_json_start_emits_one_complete_document(self):
        with self.f.host(), contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()):
            rc = cli.main(["--root", str(self.f.root), "sim", "up", "--recipe", "dockyard", "--json"])
        self.assertEqual(rc, 0, out.getvalue())
        doc = json.loads(out.getvalue())
        self.assertEqual(doc["execution"]["status"], "running")

    def test_json_execution_failure_is_one_document_and_retains_recovery(self):
        with self.f.host(fail=("planner", "up")), contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()):
            rc = cli.main(["--root", str(self.f.root), "sim", "up", "--recipe", "dockyard", "--json"])
        self.assertEqual(rc, 1)
        self.assertFalse(json.loads(out.getvalue())["ready_to_run"])
        self.assertTrue(sim.active_session(self.f.root))

    def test_json_missing_recipe_is_one_document_without_state(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = cli.main(["--root", str(self.f.root), "sim", "check", "--recipe", "absent", "--json"])
        self.assertEqual(rc, 1)
        self.assertFalse(json.loads(out.getvalue())["ready_to_run"])
        self.assertFalse(sim.active_session(self.f.root))


if __name__ == "__main__":
    unittest.main()
