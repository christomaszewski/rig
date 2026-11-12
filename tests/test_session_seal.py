"""Sealing a session's run: the config dirty-check compares like with like.

`rig sim down --end-run` (and `rig down --end-run` on a recipe session) seals the run through
the saved session. The seal's advisory check, "config changed after this run's last `up`",
must stay quiet when nothing in the deployment changed, and must still speak when something did.
"""
import contextlib
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_sim_workflow as fixtures
from rig_cli import sim_workflow as sim


class SessionSeal(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.Simulation()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    @contextlib.contextmanager
    def host(self):
        self.stderr = io.StringIO()
        with self.f.host(), patch("rig_cli.sim_workflow._verify_running"), \
                contextlib.redirect_stderr(self.stderr):
            yield

    def start(self):
        manifest, descriptors, plan = self.f.build()
        self.assertTrue(plan["ready_to_run"], plan["issues"])
        self.assertEqual(sim.execute(self.f.root, self.f.path, manifest, descriptors, plan), 0)
        return sim._read_state(self.f.root)

    def sealed(self, state):
        return yaml.safe_load((Path(state["output_run"]) / "manifest.yaml").read_text())

    def test_an_untouched_session_is_not_sealed_as_edited(self):
        with self.host():
            _, state = self.start()
            self.assertEqual(sim.down(self.f.root, end_run=True), 0)
        run = self.sealed(state)
        self.assertIn("ended", run)
        self.assertNotIn("config_dirty_at_seal", run)
        self.assertNotIn("config changed after this run's last", self.stderr.getvalue())

    def test_a_deployment_edited_after_up_is_still_reported(self):
        with self.host():
            _, state = self.start()
            with (self.f.root / "vehicle.yaml").open("a") as handle:
                handle.write("\n# edited while the session ran\n")
            self.assertEqual(sim.down(self.f.root, end_run=True), 0)
        run = self.sealed(state)
        self.assertIn("ended", run)
        self.assertTrue(run.get("config_dirty_at_seal"))
        self.assertIn("config changed after this run's last", self.stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
