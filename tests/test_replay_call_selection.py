"""Call-mode precedence through the current replay command, without ROS or Docker."""
import contextlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_replay_preparation as fixtures
from rig_cli import replay
from rig_cli.replay_workflow import command


class CallSelection(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.Preparation()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.script = self.fixture.write("replay/calls.yaml", {
            "schema": 1, "calls": [{"t": 0, "service": "/scripted/control",
                                    "type": "std_srvs/srv/Trigger", "request": {}}]}).resolve()

    def check(self, doc, *flags):
        f = self.fixture
        f.write("replay/selection.yaml", {"version": 1, "source": str(f.source), **doc})
        with f.image_label_runtime({}), contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            rc = command(f.args("check", str(f.source), *flags,
                                "--recipe", "selection", "--json"), f.root)
        return rc, json.loads(out.getvalue()), err.getvalue()

    def test_cli_script_overrides_missing_recipe_service_events(self):
        doc = {"playback": {"services": ["/missing/control"]}}
        rc, plan, _ = self.check(doc, "--calls", str(self.script))
        self.assertEqual(rc, 0, plan["issues"])
        self.assertEqual(plan["playback"]["services"], [])
        self.assertEqual(plan["playback"]["calls"], str(self.script))
        self.assertEqual(plan["calls_sha"], replay.validate_calls(self.script))
        self.assertNotIn("service_events", {issue["code"] for issue in plan["issues"]})

    def test_script_precedence_bypasses_recorded_service_selection(self):
        f = self.fixture
        f.namespace_graph()
        f.declare_app_namespace("/robot/controller")
        for doc, flags in (({}, ["--calls", str(self.script)]),
                           ({"playback": {"calls": "calls.yaml"}}, [])):
            with self.subTest(doc=doc), patch("rig_cli.replay.select_services") as select:
                rc, plan, _ = self.check(doc, "new", "--selection", "recorded", *flags)
                self.assertEqual(rc, 0, plan["issues"])
                self.assertEqual(plan["playback"]["services"], [])
                self.assertEqual(plan["playback"]["calls"], str(self.script))
                select.assert_not_called()

    def test_cli_script_overrides_missing_recipe_script(self):
        rc, plan, _ = self.check({"playback": {"calls": "missing.yaml"}},
                                 "--calls", str(self.script))
        self.assertEqual(rc, 0, plan["issues"])
        self.assertEqual(plan["playback"]["calls"], str(self.script))

    def test_invalid_script_does_not_fall_back_to_recorded_events(self):
        self.script.write_text("schema: 1\ncalls: []\n")
        rc, plan, _ = self.check({"playback": {"services": ["/missing/control"]}},
                                 "--calls", str(self.script))
        self.assertEqual(rc, 1)
        self.assertEqual(plan["playback"]["services"], [])
        self.assertIn("calls", {issue["code"] for issue in plan["issues"]})
        self.assertNotIn("service_events", {issue["code"] for issue in plan["issues"]})

    def test_recorded_targets_still_require_events_without_a_script(self):
        rc, plan, _ = self.check({"playback": {"services": ["/missing/control"]}})
        self.assertEqual(rc, 1)
        self.assertIn("service_events", {issue["code"] for issue in plan["issues"]})

    def test_cli_script_does_not_hide_a_malformed_recipe(self):
        rc, plan, _ = self.check({"playback": {"services": ["/control"], "calls": "calls.yaml"}},
                                 "--calls", str(self.script))
        self.assertEqual(rc, 1)
        self.assertEqual(plan["issues"][0]["code"], "input")
        self.assertIn("mutually exclusive", plan["issues"][0]["message"])

    def test_malformed_graph_does_not_claim_outputs_or_calls(self):
        f = self.fixture
        f.declare_app_namespace("/robot/controller")
        f.write_source("graph/logger/epoch_bad.yaml", {"schema": 1, "nodes": {
            "/robot/controller/server": {"pubs": [{"topic": "/result"}],
                                         "provides": [{"service": "/control"}]},
            "/malformed/node": {"subs": "not a list"}}})
        rc, plan, err = self.check({}, "new", "--selection", "recorded")
        self.assertEqual(rc, 0, plan["issues"])
        self.assertIn("/result", plan["playback"]["topics"])
        self.assertEqual(plan["playback"]["services"], [])
        self.assertIn("epoch_bad.yaml", err)
        self.assertIn("expected a list", err)


if __name__ == "__main__":
    unittest.main()
