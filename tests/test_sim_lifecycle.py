"""Simulation teardown uses frozen identity and cannot consume a replacement session."""
import contextlib
import io
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_sim_workflow as fixtures
from rig_cli import RigError, sim_workflow as sim, session_runtime


class SimulationLifecycle(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.Simulation()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    @contextlib.contextmanager
    def host(self):
        with self.f.host(), patch("rig_cli.sim_workflow._verify_running"), \
                contextlib.redirect_stderr(io.StringIO()):
            yield

    def start(self):
        manifest, descriptors, plan = self.f.build()
        self.assertTrue(plan["ready_to_run"], plan["issues"])
        self.assertEqual(sim.execute(self.f.root, self.f.path, manifest, descriptors, plan), 0)
        return sim._read_state(self.f.root)

    def test_malformed_current_manifest_cannot_strand_saved_services(self):
        with self.host():
            _, state = self.start()
            (self.f.root / "vehicle.yaml").write_text("vehicle: [broken YAML")
            self.assertEqual(sim.down(self.f.root, end_run=True), 0)
        down = [c for c in self.f.calls if c["verb"] == "down"]
        self.assertEqual([c["name"] for c in down], ["planner", "logger"])
        self.assertEqual(down[0]["config"]["gain"], 3)
        self.assertEqual(down[0]["env"]["VEHICLE_ID"], "7")
        self.assertFalse(sim.active_session(self.f.root))
        self.assertIn("ended", yaml.safe_load((Path(state["output_run"]) / "manifest.yaml").read_text()))

    def test_changed_docker_engine_blocks_teardown_without_side_effects(self):
        with self.host():
            _, state = self.start()
            before = list(self.f.calls)
            with patch("rig_cli.session_runtime.engine_id", return_value="different-engine"):
                with self.assertRaisesRegex(RigError, "Docker engine differs"):
                    sim.down(self.f.root, end_run=True)
        self.assertEqual(self.f.calls, before)
        self.assertTrue(sim.active_session(self.f.root))
        self.assertNotIn("ended", yaml.safe_load((Path(state["output_run"]) / "manifest.yaml").read_text()))

    def test_saved_docker_routing_including_absence_reaches_teardown_and_guard(self):
        with self.host(), patch.dict(os.environ, {"DOCKER_CONTEXT": "original"}):
            os.environ.pop("DOCKER_HOST", None)
            self.start()
            os.environ["DOCKER_CONTEXT"] = "unrelated"
            os.environ["DOCKER_HOST"] = "tcp://unrelated.invalid:2375"
            checked = []
            def running(manifest, *, env=None):
                checked.append(env)
                return []
            with patch("rig_cli.runs.running_projects", side_effect=running):
                self.assertEqual(sim.down(self.f.root, end_run=True), 0)
        down = [c for c in self.f.calls if c["verb"] == "down"]
        for env in [*(c["env"] for c in down), *checked]:
            self.assertEqual(env["DOCKER_CONTEXT"], "original")
            self.assertNotIn("DOCKER_HOST", env)

    def test_stale_state_read_does_not_stop_replacement_session(self):
        with self.host():
            stale = self.start()
            self.assertEqual(sim.down(self.f.root, end_run=True), 0)
            current = self.start()
            before = list(self.f.calls)
            read_state = session_runtime.read_state
            first = [True]
            def stale_first(root):
                if first[0]:
                    first[0] = False
                    return stale
                return read_state(root)
            with patch("rig_cli.session_runtime.read_state", side_effect=stale_first):
                with self.assertRaisesRegex(RigError, "[Ss]ession changed before teardown"):
                    sim.down(self.f.root, end_run=True)
            self.assertEqual(sim._read_state(self.f.root)[1]["session"], current[1]["session"])
        self.assertEqual(self.f.calls, before)


if __name__ == "__main__":
    unittest.main()
