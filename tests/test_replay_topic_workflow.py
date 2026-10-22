"""Topic policy through the planner, including remaps, CLI skips and source-only runs."""
from pathlib import Path
import contextlib
import io
import json
import shutil
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_replay_preparation as fixtures
from rig_cli import replay_workflow


class TopicWorkflow(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.Preparation()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def graph(self, nodes):
        self.f.write_source("graph/logger/epoch_one.yaml", {"schema": 1, "nodes": nodes})

    def test_shared_publishers_retained_until_every_owner_is_started(self):
        self.graph({"/new/node": {"pubs": [{"topic": "/result"}]},
                    "/idle/node": {"pubs": [{"topic": "/result"}]}})
        for names, included in (([], True), (["idle"], False)):
            plan = self.f.plan(opts={"names": names})
            self.assertEqual("/result" in plan["playback"]["topics"], included)
            row = next(r for r in plan["topic_selection"] if r["original"] == "/result")
            self.assertEqual(row["owners"], ["idle", "new"])
            self.assertEqual(row["decision"], "keep" if included else "exclude")

    def test_unknown_publisher_cannot_be_hidden_by_selected_publisher(self):
        self.graph({"/new/node": {"pubs": [{"topic": "/result"}]},
                    "/unmapped/node": {"pubs": [{"topic": "/result"}]}})
        plan = self.f.plan()
        self.assertIn("/result", plan["playback"]["topics"])
        row = next(r for r in plan["topic_selection"] if r["original"] == "/result")
        self.assertEqual(row["reason"], "unassigned_publishers")

    def test_skip_never_claims_an_input_by_its_namespace(self):
        self.f.bag(self.f.source / "bags/logger/session-1", topic="/new/input")
        self.graph({"/new/node": {"pubs": [{"topic": "/result"}], "subs": [{"topic": "/new/input"}]},
                    "/idle/node": {"pubs": [{"topic": "/new/input"}]}})
        plan = self.f.plan({"version": 1, "playback": {"keep": ["/result"]}},
                           opts={"skip_instances": ["new"]})
        self.assertIn("/new/input", plan["playback"]["topics"])
        self.assertNotIn("/result", plan["playback"]["topics"])
        row = next(r for r in plan["topic_selection"] if r["original"] == "/result")
        self.assertEqual(row["reason"], "skipped_instance")
        self.assertTrue(row["keep_override"])

    def test_remap_destination_without_bag_channel_still_conflicts_with_live_output(self):
        self.graph({"/new/node": {"pubs": [{"topic": "/live/output"}]}})
        recipe = {"version": 1, "playback": {"remap": {"/old/input": "/live/output"}}}
        plan = self.f.plan(recipe)
        self.assertTrue(any(i["code"] == "live_destination" for i in plan["issues"]))
        recipe["playback"]["keep"] = ["/old/input"]
        plan = self.f.plan(recipe)
        self.assertFalse(any(i["code"] == "live_destination" for i in plan["issues"]))
        row = next(r for r in plan["topic_selection"] if r["original"] == "/old/input")
        self.assertEqual(row["destination"], "/live/output")
        self.assertEqual(row["reason"], "explicit_keep")

    def test_recorded_subscriber_selection_uses_conservative_suppression(self):
        self.graph({"/new/node": {"pubs": [{"topic": "/result"}], "subs": [{"topic": "/result"}]},
                    "/idle/node": {"pubs": [{"topic": "/result"}]}})
        plan = self.f.plan(opts={"names": ["new"], "selection": "recorded"})
        self.assertEqual(plan["playback"]["topics"], ["/result"])

    def test_shared_transform_remains_eligible_but_explicit_exclusion_wins(self):
        self.graph({"/new/node": {"pubs": [{"topic": "/tf"}]}})
        self.assertIn("/tf", self.f.plan()["playback"]["topics"])
        self.assertNotIn("/tf", self.f.plan({"version": 1, "playback": {"exclude": ["/tf"]}})
                         ["playback"]["topics"])

    def test_source_only_replay_does_not_require_a_recorded_call_helper(self):
        shutil.rmtree(self.f.source / "bags")
        self.f.rows["sensors"][0]["replay"] = {"start": True}
        self.f.vehicle()
        self.f.write_source("recordings/cam/session.json", {"source": "camera"})
        self.graph({"/new/node": {"provides": [{"service": "/control"}]}})
        plan = self.f.plan()
        self.assertIsNone(plan["source"]["bag"])
        self.assertEqual(plan["playback"]["services"], [])
        self.assertEqual(plan["service_selection"]["candidates"], [])
        self.assertFalse(any(i["code"] == "recorded_calls" for i in plan["issues"]))
        manual = self.f.plan({"version": 1, "playback": {"services": ["/control"]}})
        self.assertTrue(any(i["status"] == "error" and i["code"] == "service_events"
                            for i in manual["issues"]))

    def test_invalid_window_with_selected_server_returns_validation_error(self):
        self.graph({"/new/node": {"provides": [{"service": "/control"}]}})
        plan = self.f.plan(opts={"window_from": 3, "window_to": 2})
        self.assertFalse(plan["ready_to_prepare"])
        self.assertTrue(any(i["code"] == "window" for i in plan["issues"]))
        for flags in (("--from", "abc"), ("--from", "3", "--to", "2"), ("--to", "0.0000000001")):
            with self.f.image_label_runtime({}), contextlib.redirect_stdout(io.StringIO()) as out, \
                    contextlib.redirect_stderr(io.StringIO()):
                code = replay_workflow.command(self.f.args("check", str(self.f.source), *flags, "--json"), self.f.root)
            plan = json.loads(out.getvalue())
            self.assertEqual(code, 1)
            self.assertTrue(any(i["code"] == "window" for i in plan["issues"]))

    def test_standalone_empty_service_selection_warns_about_recipe_opt_out(self):
        self.f.write("player.yaml", {"name": "player", "service": "ros2-bag-player", "play": {"services": []}})
        plan = self.f.plan()
        warning = next(i for i in plan["issues"] if i["code"] == "shadowed_player_selection")
        self.assertIn("playback.services: []", warning["message"])

    def test_top_level_standalone_call_script_warns_about_recipe_override(self):
        self.f.write("player.yaml", {"name": "player", "service": "ros2-bag-player",
                                      "calls": {"file": "standalone.yaml"}})
        plan = self.f.plan()
        warning = next(i for i in plan["issues"] if i["code"] == "shadowed_player_selection")
        self.assertIn("Player calls", warning["message"])
        self.assertIn("playback.calls or --calls", warning["message"])

    def test_standalone_window_does_not_change_the_inspected_replay_window(self):
        self.f.write("player.yaml", {"name": "player", "service": "ros2-bag-player",
                                      "play": {"start_offset_s": 2, "end_offset_s": 4}})
        plan = self.f.plan()
        self.assertEqual(plan["window"], {"start": 0, "end": None})
        warnings = [i["message"] for i in plan["issues"] if i["code"] == "shadowed_player_selection"]
        self.assertTrue(any("use --from" in message for message in warnings))
        self.assertTrue(any("use --to" in message for message in warnings))


if __name__ == "__main__":
    unittest.main()
