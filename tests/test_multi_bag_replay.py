"""Multi-input replay selection, preparation, and explicit camera ROS authority."""
import copy
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import yaml

import test_replay_preparation as preparation_fixtures
from rig_cli import replay_plan, replay_prepare
from rig_cli.replay_sources import inventory_fingerprint
from rig_cli.replay_workflow import load_context


class MultiBagReplay(unittest.TestCase):
    def setUp(self):
        self.f = preparation_fixtures.Preparation()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        launcher = self.f.root / "services/ros2-bag-player/up"
        launcher.write_text(launcher.read_text().replace('"plan":1',
            '"plan":2,"multi_input":1,"multi_input_image_labels":'
            '{"bag-player":{"io.rig.rosbag2.multi_input":"1"}}').replace('"inspect_calls":1', '"inspect_calls":2'))
        self.other = self.f.source / "bags/other/session-2"
        self.f.bag(self.other, start=200_000_000_000, topic="/second/input")

    def test_default_all_ignores_player_logger_and_preserves_counts(self):
        self.f.write("player.yaml", {"name": "player", "service": "ros2-bag-player",
                                     "source": {"logger": "logger", "session": "latest"}})
        before = inventory_fingerprint(self.f.source)
        plan = self.f.plan()
        self.assertEqual([(b["logger"], b["session"]) for b in plan["source"]["bags"]],
                         [("logger", "session-1"), ("other", "session-2")])
        self.assertEqual(plan["source"]["bag"]["start_ns"], 100_000_000_000)
        self.assertEqual(plan["source"]["bag"]["duration_ns"], 110_000_000_000)
        self.assertEqual(plan["source"]["bag"]["topics"]["/result"]["count"], 20)
        self.assertIn("/old/input", plan["playback"]["topics"])
        self.assertIn("/second/input", plan["playback"]["topics"])
        self.assertNotIn("path", plan["source"]["bag"])
        self.assertEqual(before, inventory_fingerprint(self.f.source))
        self.assertFalse(any(i["status"] == "error" for i in plan["issues"]))
        document = replay_prepare.player_document(plan, self.f.source)
        self.assertEqual(document["version"], 2)
        self.assertEqual(document["timeline"], {"start_ns": 100_000_000_000, "duration_ns": 110_000_000_000})
        self.assertEqual([b["relative_path"] for b in document["bags"]],
                         ["bags/logger/session-1", "bags/other/session-2"])

    def test_explicit_subset_latest_and_legacy_one_input(self):
        self.f.bag(self.f.source / "bags/logger/session-3", start=300_000_000_000, topic="/latest/input")
        latest = self.f.plan({"version": 1, "bags": [{"logger": "logger", "session": "latest"}]})
        self.assertEqual(len(latest["source"]["bags"]), 1)
        self.assertEqual(latest["source"]["bag"]["session"], "session-3")
        self.assertEqual(replay_prepare.player_document(latest, self.f.source)["version"], 1)
        subset = self.f.plan({"version": 1, "bags": [{"logger": "logger", "session": "session-1"},
                                                   {"logger": "other", "session": "all"}]})
        self.assertEqual(len(subset["source"]["bags"]), 2)
        self.assertNotIn("/latest/input", subset["playback"]["topics"])
        legacy = self.f.plan({"version": 1, "bag": {"logger": "logger", "session": "session-1"}})
        self.assertEqual(legacy["source"]["bag"]["session"], "session-1")

    def test_split_files_are_one_input_and_duplicate_selection_is_rejected(self):
        (self.other / "part-2.mcap").write_bytes(b"next split")
        plan = self.f.plan()
        self.assertEqual(len(plan["source"]["bags"]), 2)
        self.assertEqual(len(plan["source"]["bags"][1]["files"]), 2)
        invalid = self.f.plan({"version": 1, "bags": [{"logger": "other", "session": "all"},
                                                    {"logger": "other", "session": "session-2"}]})
        self.assertFalse(invalid["ready_to_prepare"])
        self.assertTrue(any("more than once" in i["message"] for i in invalid["issues"]))

    def test_union_rejects_conflicting_type_schema_and_serialization(self):
        path = self.other / "metadata.yaml"
        original = yaml.safe_load(path.read_text())
        first = self.f.source / "bags/logger/session-1/metadata.yaml"
        first_doc = yaml.safe_load(first.read_text())
        for doc in (original, first_doc):
            for row in doc["rosbag2_bagfile_information"]["topics_with_message_count"]:
                row["topic_metadata"].update(serialization_format="cdr", type_description_hash="schema-a")
        first.write_text(yaml.safe_dump(first_doc))
        for field, value in (("type", "std_msgs/msg/Int32"), ("serialization_format", "other"),
                             ("type_description_hash", "schema-b")):
            with self.subTest(field=field):
                doc = copy.deepcopy(original)
                row = next(r for r in doc["rosbag2_bagfile_information"]["topics_with_message_count"]
                           if r["topic_metadata"]["name"] == "/result")
                row["topic_metadata"][field] = value
                path.write_text(yaml.safe_dump(doc))
                plan = self.f.plan()
                self.assertFalse(plan["ready_to_prepare"])
                self.assertTrue(any("incompatible" in i["message"] for i in plan["issues"]))

    def test_repair_copies_each_selected_bag_and_preserves_originals(self):
        (self.other / "metadata.yaml").unlink()
        doc = {"version": 1}
        recipe_path = self.f.write("replay/multiple.yaml", doc)
        plan = self.f.plan(doc)
        self.assertTrue(plan["source"]["bag"]["repair"])
        before = inventory_fingerprint(self.f.source)
        manifest, _, descriptors = load_context(self.f.root)
        repaired = []
        def reindex(argv, **kwargs):
            target = Path(argv[-1])
            repaired.append(target)
            self.f.bag(target, start=200_000_000_000, topic="/repaired")
            return subprocess.CompletedProcess(argv, 0, "", "")
        with patch("rig_cli.replay_prepare.subprocess.run", side_effect=reindex):
            runtime = replay_prepare.create(self.f.root, recipe_path, doc, plan, manifest, descriptors)
        self.assertEqual(before, inventory_fingerprint(self.f.source))
        self.assertEqual(len(repaired), 1)
        self.assertTrue((runtime / "bags/logger/session-1/metadata.yaml").is_file())
        self.assertTrue((runtime / "bags/other/session-2/metadata.yaml").is_file())
        checked = self.f.plan(doc, opts={"_prepared_source": str(runtime)})
        self.assertFalse(checked["source"]["bag"]["repair"])
        self.assertIn("/repaired", checked["playback"]["topics"])
        self.assertIn("/old/input", checked["playback"]["topics"])
        self.assertEqual(checked["source"]["bag"]["duration_ns"], 110_000_000_000)

    def test_old_player_reports_multi_input_capability_error(self):
        with patch.object(replay_plan, "capabilities", return_value={
                "plan": 1, "image_labels": {"bag-player": {"io.rig.rosbag2.replay": "1"}}}):
            plan = self.f.plan()
        self.assertTrue(any(i["code"] == "player_capability" and "multi-input" in i["message"]
                            for i in plan["issues"]))

    def test_one_timestamp_bag_has_minimal_timeline_without_metadata_mutation(self):
        path = self.other / "metadata.yaml"
        doc = yaml.safe_load(path.read_text())
        info = doc["rosbag2_bagfile_information"]
        info["duration"]["nanoseconds"] = 0
        path.write_text(yaml.safe_dump(doc))
        original = path.read_bytes()
        recipe = {"version": 1, "bags": [{"logger": "other", "session": "session-2"}]}
        plan = self.f.plan(recipe)
        self.assertEqual(plan["source"]["bag"]["duration_ns"], 1)
        self.assertFalse(plan["source"]["bag"]["repair"])
        self.assertTrue(plan["ready_to_prepare"])
        self.assertEqual(path.read_bytes(), original)
        for entry in info["topics_with_message_count"]:
            entry["message_count"] = 0
        path.write_text(yaml.safe_dump(doc))
        empty = self.f.plan(recipe)
        self.assertTrue(empty["source"]["bag"]["repair"])
        self.assertIn("no recorded messages", empty["source"]["bag"]["repair_reason"])

    def test_call_inspector_receives_all_inputs_and_global_window(self):
        plan = self.f.plan({"version": 1, "playback": {"services": ["/control"]}},
                           opts={"window_from": 5, "window_to": 105})
        manifest, _, descriptors = load_context(self.f.root)
        with patch("rig_cli.replay_call_inspection.inspect_calls", return_value={
                "status": "not_checked", "result": None}) as inspector:
            replay_plan.inspect_recorded_calls(plan, manifest, descriptors, self.f.root)
        request = inspector.call_args.args[4]
        self.assertEqual(request["version"], 2)
        self.assertNotIn("session", request)
        self.assertEqual(request["source"], str(self.f.source.resolve()))
        self.assertEqual(request["start_ns"], 100_000_000_000)
        self.assertEqual((request["from_s"], request["to_s"]), (5, 105))
        self.assertEqual([b["relative_path"] for b in request["bags"]],
                         ["bags/logger/session-1", "bags/other/session-2"])

    def test_single_proxy_plan_uses_exact_path_and_top_level_policy(self):
        plan = self.f.plan({"version": 1, "bag": {"logger": "logger", "session": "session-1"}})
        selected = plan["source"]["bags"][0]
        selected.update(logger="bag-proxy", session="a" * 64,
                        relative_path="exports/inputs/bags/sensor/abc/bag",
                        proxy=dict(export_id="a" * 64, time_source="sensor", deduplication="conservative"))
        document = replay_prepare.player_document(plan, self.f.source)
        self.assertEqual(document["version"], 2)
        self.assertEqual(document["proxy"], selected["proxy"])
        self.assertEqual(set(document["bags"][0]), {"logger", "session", "relative_path", "storage"})
        self.assertEqual(document["bags"][0]["relative_path"], selected["relative_path"])


class CameraImageAuthority(unittest.TestCase):
    def setUp(self):
        self.f = preparation_fixtures.Preparation()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.rows["sensors"][0]["replay"] = {"start": True}
        self.f.vehicle()
        recording = self.f.source / "recordings/cam"
        recording.mkdir(parents=True)
        (recording / "session.json").write_text(json.dumps(dict(pixel_format="Mono8", width=2, height=2)))
        (self.f.source / "recordings/cam/session.csv").write_text(
            "timestamp_ns,system_ns\n101000000000,101000000000\n")
        (self.f.source / "recordings/cam/session-00000.mkv").write_bytes(b"video")
        self.plugins = [{"name": "ros2-bridge", "enabled": True, "isolation": "container",
                         "params": {"topic": "image_raw", "frame_id": "cam"}},
                        {"name": "webrtc-bridge", "enabled": True, "isolation": "container",
                         "params": {"port": 8443}}]
        self.f.write("cam.yaml", {"name": "cam", "service": "camera", "plugins": self.plugins})
        path = self.f.source / "bags/logger/session-1/metadata.yaml"
        doc = yaml.safe_load(path.read_text())
        doc["rosbag2_bagfile_information"]["topics_with_message_count"].append({
            "topic_metadata": {"name": "/cam/image_raw", "type": "sensor_msgs/msg/Image"}, "message_count": 8})
        path.write_text(yaml.safe_dump(doc))
        self.f.write_source("graph/logger/epoch_one.yaml", {"schema": 1, "nodes": {
            "/cam/bridge": {"pubs": [{"topic": "/cam/image_raw", "type": "sensor_msgs/msg/Image"}]}}})

    def plan(self, mode, **playback):
        return self.f.plan({"version": 1, "playback": {"image_sources": {"cam": mode}, **playback}})

    def test_bag_images_survive_historical_suppression_and_only_ros_bridge_is_disabled(self):
        plan = self.plan("bag")
        self.assertIn("/cam/image_raw", plan["playback"]["topics"])
        self.assertNotIn("/cam/image_raw", plan["excluded_regenerated"])
        plugins = plan["configs"]["cam"]["plugins"]
        self.assertFalse(plugins[0]["enabled"])
        self.assertEqual(plugins[0]["params"], self.plugins[0]["params"])
        self.assertEqual(plugins[1], self.plugins[1])
        self.assertTrue(self.plugins[0]["enabled"])
        self.assertEqual(plan["image_sources"]["cam"], {"source": "bag", "topics": ["/cam/image_raw"]})

    def test_camera_authority_enables_bridge_and_removes_bag_image(self):
        self.plugins[0]["enabled"] = False
        self.f.write("cam.yaml", {"name": "cam", "service": "camera", "plugins": self.plugins})
        plan = self.plan("camera")
        self.assertTrue(plan["configs"]["cam"]["plugins"][0]["enabled"])
        self.assertNotIn("/cam/image_raw", plan["playback"]["topics"])
        self.assertTrue(any(i["code"] == "image_source" for i in self.plan(
            "camera", keep=["/cam/image_raw"])["issues"]))

    def test_no_graph_uses_exact_configured_image_topic_and_missing_images_fail(self):
        (self.f.source / "graph/logger/epoch_one.yaml").unlink()
        self.assertIn("/cam/image_raw", self.plan("bag")["playback"]["topics"])
        self.f.bag(self.f.source / "bags/logger/session-1")
        missing = self.plan("bag")
        self.assertFalse(missing["ready_to_prepare"])
        self.assertTrue(any(i["code"] == "image_source" for i in missing["issues"]))

    def test_invalid_bridge_config_reports_an_actionable_plan_error(self):
        self.plugins[0]["params"] = "invalid"
        self.f.write("cam.yaml", {"name": "cam", "service": "camera", "plugins": self.plugins})
        plan = self.plan("bag")
        self.assertFalse(plan["ready_to_prepare"])
        self.assertTrue(any(i["code"] == "image_source" and "params must be a mapping" in i["message"]
                            for i in plan["issues"]))

    def add_second_camera(self):
        self.f.rows["sensors"].append(dict(name="cam2", service="camera", config="cam2.yaml",
                                          replay={"start": True}))
        self.f.vehicle()
        self.f.write("cam2.yaml", {"name": "cam2", "service": "camera", "plugins": [
            dict(name="ros2-bridge", enabled=True, isolation="container",
                 params={"topic": "/cam/image_raw"})]})
        recording = self.f.source / "recordings/cam2"
        recording.mkdir()
        for path in (self.f.source / "recordings/cam").iterdir():
            (recording / path.name).write_bytes(path.read_bytes())
        self.f.write_source("graph/logger/epoch_one.yaml", {"schema": 1, "nodes": {
            "/cam/bridge": {"pubs": [{"topic": "/cam/image_raw", "type": "sensor_msgs/msg/Image"}]},
            "/cam2/bridge": {"pubs": [{"topic": "/cam/image_raw", "type": "sensor_msgs/msg/Image"}]}}})

    def test_bag_authority_rejects_another_active_camera_without_disabling_it(self):
        self.add_second_camera()
        plan = self.plan("bag")
        self.assertFalse(plan["ready_to_prepare"])
        self.assertTrue(plan["configs"]["cam2"]["plugins"][0]["enabled"])
        self.assertTrue(any(i["code"] == "image_source" and "publisher(s): cam2" in i["message"]
                            for i in plan["issues"]))
        both = self.f.plan({"version": 1, "playback": {"image_sources": {"cam": "bag", "cam2": "bag"}}})
        self.assertTrue(both["ready_to_prepare"])
        self.assertTrue(all(not both["configs"][name]["plugins"][0]["enabled"] for name in ("cam", "cam2")))
        config = yaml.safe_load((self.f.root / "cam2.yaml").read_text())
        config["plugins"][0]["enabled"] = False
        self.f.write("cam2.yaml", config)
        self.assertTrue(self.plan("bag")["ready_to_prepare"])
        self.f.write_source("graph/logger/epoch_one.yaml", {"schema": 1, "nodes": {
            "/cam/bridge": {"pubs": [{"topic": "/cam/image_raw", "type": "sensor_msgs/msg/Image"}]},
            "/new/processor": {"pubs": [{"topic": "/cam/image_raw", "type": "sensor_msgs/msg/Image"}]}}})
        self.assertTrue(any("publisher(s): new" in i["message"] for i in self.plan("bag")["issues"]))

    def test_no_graph_detects_configured_topic_collision_and_uppercase_topic_override(self):
        self.add_second_camera()
        (self.f.source / "graph/logger/epoch_one.yaml").unlink()
        self.plugins[0]["params"]["topic"] = "unrecorded"
        self.plugins[1]["params"]["CAM_ROS_TOPIC"] = "/cam/image_raw"
        self.f.write("cam.yaml", {"name": "cam", "service": "camera", "plugins": self.plugins})
        plan = self.plan("bag")
        self.assertIn("/cam/image_raw", plan["image_sources"]["cam"]["topics"])
        self.assertTrue(any(i["code"] == "image_source" and "publisher(s): cam2" in i["message"]
                            for i in plan["issues"]))
        config = yaml.safe_load((self.f.root / "cam2.yaml").read_text())
        config["plugins"][0]["params"]["topic"] = "/shared/image"
        self.f.write("cam2.yaml", config)
        remapped = self.plan("bag", remap={"/cam/image_raw": "/shared/image"})
        self.assertTrue(any("publisher(s): cam2" in i["message"] for i in remapped["issues"]))
        self.f.rows["sensors"][1]["replay"] = {"start": False}
        self.f.vehicle()
        self.assertTrue(self.plan("bag")["ready_to_prepare"])


if __name__ == "__main__":
    unittest.main()
