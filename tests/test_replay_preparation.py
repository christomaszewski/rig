"""Replay planning/preparation contracts, without ROS or a Docker daemon."""
import contextlib
import dataclasses
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rig_cli import RigError, cli, dispatch
from rig_cli.manifest import load_manifest
from rig_cli.replay_workflow import load_context, command
from rig_cli import replay_plan as planner, replay_prepare as prepare, replay_recipe as recipe
from rig_cli.replay_sources import inspect_source, inventory_fingerprint, source_fingerprint
from rig_cli.targets import build_selections


class Preparation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "deployment"
        self.root.mkdir()
        self.env = patch.dict(os.environ, RIG_HOME=str(self.root / "user"),
                              RIG_VEHICLE_LOCAL=str(self.root / "absent.yaml"))
        self.env.start()
        self.addCleanup(self.env.stop)
        self.service("router", tier="infra")
        self.service("app")
        self.service("camera", source=True)
        self.service("ros2-bag-player", player=True)
        self.rows = {
            "infra": [dict(name="router", service="router", config="router.yaml")],
            "autonomy": [
                dict(name="new", service="app", config="new.yaml", enabled=False,
                     replay={"start": True}),
                dict(name="idle", service="app", config="idle.yaml"),
                dict(name="player", service="ros2-bag-player", config="player.yaml", enabled=False)],
            "sensors": [dict(name="cam", service="camera", config="cam.yaml",
                            replay={"start": False})]}
        for tier in self.rows.values():
            for row in tier:
                self.write(row["config"], {"name": row["name"], "service": row["service"]})
        self.vehicle()
        self.write("services.yaml", {"services": {n: {"path": "services/" + n} for n in
                                                ("app", "router", "camera", "ros2-bag-player")}})
        self.source = Path(self.tmp.name) / "source"
        self.source.mkdir()
        self.bag(self.source / "bags" / "logger" / "session-1", start=100_000_000_000, topic="/old/input")
        self.write_source("manifest.yaml", {"run": "source", "ended": "2026-01-01T00:00:00Z"})

    def write(self, path, doc):
        p = self.root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(yaml.safe_dump(doc, sort_keys=False))
        return p

    def write_source(self, path, doc):
        p = self.source / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(yaml.safe_dump(doc, sort_keys=False))
        return p

    def vehicle(self, **extra):
        self.write("vehicle.yaml", dict(vehicle="test", vehicle_id=1,
                                       ros={"rmw": "rmw_cyclonedds_cpp"}, **self.rows, **extra))

    def service(self, name, *, tier="autonomy", source=False, player=False):
        desc = dict(service=name, launcher="up", tier=tier,
                    verbs={"runtime-images": "runtime-images"}, build={"architectures": ["amd64", "arm64"]})
        if source:
            desc["replay"] = {"source": {"data": "recordings/{name}",
                                         "overrides": {"replay": {"path": "{{replay_source}}/recordings/{{name}}",
                                                                  "epoch": "{{replay_epoch_unix_ns}}"}}}}
        if player:
            desc["verbs"].update({"replay-capabilities": "replay-capabilities", "replay-reindex": "replay-reindex",
                                  "replay-inspect-calls": "replay-inspect-calls"})
        self.write("services/" + name + "/rigging.yaml", desc)
        launcher = self.root / "services" / name / "up"
        launcher.write_text("#!/bin/sh\n"
                            "if [ \"$2\" = replay-capabilities ]; then\n"
                            "echo '{\"plan\":1,\"remap\":true,\"clock_only\":true,\"reindex\":true,\"inspect_calls\":1,\"image_labels\":{\"bag-player\":{\"io.rig.rosbag2.replay\":\"1\"}}}'\n"
                            "elif [ \"$2\" = replay-inspect-calls ]; then\n"
                            'echo \'{"version":1,"source_mode":"service","services":{"/control":{"status":"usable","request_count":1,"other_origin_request_count":0,"duplicate_count":0,"type":"std_srvs/srv/Trigger","reason":"Fixture request"}}}\'\n'
                            "elif [ \"$2\" = runtime-images ]; then\n"
                            "ref=${RIG_BASE_IMAGE:-fixture/runtime:latest}\n"
                            + ("ref=${CAM_DEV_IMAGE:-${RIG_IMAGE_REGISTRY:+$RIG_IMAGE_REGISTRY/}cam-dev:${RIG_IMAGE_TAG:-latest}}\n"
                               "printf '{\"version\":1,\"images\":[\"%s\"],\"roles\":{\"cam-dev\":\"%s\"}}\\n' \"$ref\" \"$ref\"\n"
                               if name == "camera" else
                               "printf '{\"version\":1,\"images\":[\"%s\"],\"roles\":{\"bag-player\":\"%s\"}}\\n' \"$ref\" \"$ref\"\n")
                            + "else exit 42; fi\n")
        launcher.chmod(0o755)

    def bag(self, folder, *, start=100_000_000_000, topic="/input"):
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "data.mcap").write_bytes(b"original bag data")
        info = dict(storage_identifier="mcap", relative_file_paths=["data.mcap"],
                    starting_time={"nanoseconds_since_epoch": start}, duration={"nanoseconds": 10_000_000_000},
                    topics_with_message_count=[{"topic_metadata": {"name": t, "type": "std_msgs/msg/String"},
                                               "message_count": 10} for t in
                                              (topic, "/result", "/tf", "/clock", "/rosout",
                                               "/control/_service_event")])
        (folder / "metadata.yaml").write_text(yaml.safe_dump({"rosbag2_bagfile_information": info}))

    def plan(self, doc=None, opts=None, ref=None):
        m, c, d = load_context(self.root)
        plan = planner.build(m, c, d, self.root, str(ref or self.source),
                             doc or {"version": 1}, opts or {}, host=False)
        # These tests isolate source preparation/startup from Docker image acquisition.
        # Workflow image tests exercise the real inventory and readiness gate separately.
        plan["image_pins"] = {name: {"version": 1, "images": {
            "fixture/runtime:latest": "sha256:" + "1" * 64}} for name in plan["selected"]}
        return plan

    def args(self, *args):
        return cli.build_parser().parse_args(cli.translate_argv(["replay", *args]))

    def test_default_broad_start_policy_and_no_writes(self):
        before = inventory_fingerprint(self.root)
        plan = self.plan()
        self.assertEqual(plan["selected"], ["router", "new", "player"])
        self.assertEqual(plan["playback"]["topics"], ["/old/input", "/result", "/tf"])
        self.assertEqual(plan["playback"]["services"], [])
        self.assertTrue(plan["ready_to_prepare"])
        self.assertFalse(plan["ready_to_run"])
        self.assertEqual(before, inventory_fingerprint(self.root))
        self.assertIn("cam", plan["omitted"])

    def test_cli_overrides_and_source_requires_explicit_live(self):
        self.assertIn("idle", self.plan(opts={"names": ["idle"]})["selected"])
        plan = self.plan(opts={"names": ["cam"]})
        self.assertTrue(any(i["code"] == "missing_recording" for i in plan["issues"]))
        self.assertFalse(plan["ready_to_prepare"])
        plan = self.plan(opts={"live": ["cam"]})
        self.assertIn("cam", plan["selected"])
        self.assertFalse(any(i["code"] == "missing_recording" for i in plan["issues"]))
        self.assertNotIn("new", self.plan(opts={"skip_instances": ["new"]})["selected"])

    def test_prepared_generation_rejects_the_previous_selection_policy(self):
        recipe_path = self.write("replay/policy.yaml", {"version": 1, "source": str(self.source)})
        doc = yaml.safe_load(recipe_path.read_text())
        plan = self.plan(doc)
        manifest, _, descriptors = load_context(self.root)
        runtime = prepare.create(self.root, recipe_path, doc, plan, manifest, descriptors)
        self.assertEqual(prepare.prepared(self.root, recipe_path, doc, self.source), runtime)
        key = prepare.generation(plan)
        with patch("rig_cli.replay_plan.SELECTION_VERSION", 1):
            self.assertNotEqual(prepare.generation(plan), key)
        provenance = prepare.location(self.root, recipe_path, self.source) / "provenance.yaml"
        saved = yaml.safe_load(provenance.read_text())
        saved.pop("selection_version")
        provenance.write_text(yaml.safe_dump(saved))
        self.assertIsNone(prepare.prepared(self.root, recipe_path, doc, self.source))
        self.assertNotIn("playback", yaml.safe_load(recipe_path.read_text()))

    def test_policy_conflict_local_override_and_build_scope(self):
        self.vehicle(replay={"instances": {"new": {"start": False}}})
        with self.assertRaisesRegex(RigError, "conflicting"):
            load_manifest(self.root)
        self.vehicle(replay={"services": {"app": {"start": False}}})
        self.write("vehicle.local.yaml", {"replay": {"instances": {"new": {"start": False}}}})
        self.assertNotIn("new", self.plan()["selected"])
        self.write("vehicle.local.yaml", {"replay": {"instances": {"idle": {"start": True}}}})
        plan = self.plan()
        self.assertIn("new", plan["selected"])
        self.assertIn("idle", plan["selected"])
        m, _, d = load_context(self.root)
        selections, _ = build_selections(m, d, ["replay"])
        self.assertIn("app", selections)

    def test_camera_skips_override_recipe_keeps_and_follow_recorded_aliases(self):
        self.rows["sensors"][0]["replay"] = {"start": True}
        self.vehicle()
        self.bag(self.source / "bags/logger/session-1", topic="/old_cam/image")
        doc = {"version": 1, "instances": {"cam": {"recorded_as": "old_cam"}},
               "playback": {"keep": ["/old_cam/image", "/result", "/tf"]}}
        for graph_present in (False, True):
            if graph_present:
                self.write_source("graph/logger/epoch_20260101T000000Z.yaml", {
                    "schema": 1, "nodes": {
                        "/old_cam/bridge": {"pubs": [
                            {"topic": "/old_cam/image", "type": "sensor_msgs/msg/Image"},
                            {"topic": "/result", "type": "sensor_msgs/msg/Image"},
                            {"topic": "/tf", "type": "tf2_msgs/msg/TFMessage"}]},
                        "/other/node": {"pubs": [{"topic": "/tf", "type": "tf2_msgs/msg/TFMessage"}]}}})
            for selection in ({"skip_services": ["camera"]}, {"skip_instances": ["cam"]}):
                with self.subTest(graph=graph_present, selection=selection):
                    plan = self.plan(doc, opts=selection)
                    self.assertTrue(plan["ready_to_prepare"], plan["issues"])
                    self.assertNotIn("cam", plan["selected"])
                    self.assertEqual("/old_cam/image" in plan["playback"]["topics"], not graph_present)
                    self.assertIn("/tf", plan["playback"]["topics"])
                    self.assertEqual("/result" in plan["playback"]["topics"], not graph_present)

    def test_skip_camera_service_check_prepare_and_execution_with_missing_proxies(self):
        self.reader_label_camera()
        self.rows["sensors"][0]["replay"] = {"start": True}
        self.rows["sensors"].append(dict(name="cam_second", service="camera", config="cam_second.yaml",
                                         replay={"start": True}))
        self.write("cam_second.yaml", {"name": "cam_second", "service": "camera"})
        self.vehicle(platform="dev", images={"registry": "example.test", "tag": "v1"})
        # Skipping must bypass corrupt originals as well as missing proxies/images.
        (self.source / "recordings/cam/overlap.json").write_text("corrupt recording")
        self.write("replay/no-video.yaml", {"version": 1, "source": str(self.source)})
        flags = [str(self.source), "--recipe", "no-video", "--skip-service", "camera",
                 "--skip-instance", "idle", "--use-proxies"]
        with self.image_label_runtime({}) as inspected, \
                patch("rig_cli.service_exports.resolve_proxy_binding") as proxy, \
                patch("rig_cli.dispatch.run_verb", return_value=[]) as launch:
            for verb in ("check", "prepare"):
                with contextlib.redirect_stdout(io.StringIO()) as out, \
                        contextlib.redirect_stderr(io.StringIO()) as err:
                    rc = command(self.args(verb, *flags, "--json"), self.root)
                plan = json.loads(out.getvalue())
                self.assertEqual(rc, 0, plan["issues"])
                self.assertEqual(plan["selected"], ["router", "new", "player"])
                self.assertEqual(plan["recordings"], {})
                launch.assert_not_called()
                if verb == "prepare":
                    hint = next(line for line in err.getvalue().splitlines() if line.startswith("Replay with:"))
                    self.assertIn("--skip-service camera", hint)
                    self.assertIn("--skip-instance idle", hint)
                    self.assertIn("--use-proxies", hint)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(command(self.args(*flags, "--dry-run"), self.root), 0)
            self.assertEqual([row.name for row, _ in launch.call_args.args[0]], ["router", "new", "player"])
            proxy.assert_not_called()
            self.assertEqual(inspected, [])

    def test_camera_skip_flags_work_on_replay_aliases(self):
        for prefix in (["replay"], ["replay", "check"], ["replay", "prepare"],
                       ["run", "replay"], ["run", "replay", "check"], ["run", "replay", "prepare"],
                       ["replay-check"], ["replay-prepare"]):
            args = cli.build_parser().parse_args(cli.translate_argv(
                [*prefix, str(self.source), "--skip-service", "camera", "--skip-instance", "idle"]))
            self.assertEqual(args.skip_services, ["camera"])
            self.assertEqual(args.skip_instances, ["idle"])

    def test_exact_bag_session_and_ambiguous_logger(self):
        self.bag(self.source / "bags" / "logger" / "session-2", start=200_000_000_000, topic="/later")
        plan = self.plan({"version": 1, "bag": {"session": "session-1"}})
        self.assertEqual(plan["source"]["bag"]["start_ns"], 100_000_000_000)
        self.assertNotIn("/later", plan["playback"]["topics"])
        self.bag(self.source / "bags" / "other" / "session-3")
        self.assertFalse(self.plan()["ready_to_prepare"])
        self.assertTrue(self.plan({"version": 1, "bag": {"logger": "logger"}})["ready_to_prepare"])

    def test_malformed_legacy_index_reports_repair(self):
        path = self.source / "bags/logger/session-1/metadata.yaml"
        doc = yaml.safe_load(path.read_text())
        doc["rosbag2_bagfile_information"]["topics_with_message_count"] = "corrupt"
        path.write_text(yaml.safe_dump(doc))
        plan = self.plan()
        self.assertTrue(plan["source"]["bag"]["repair"])
        self.assertTrue(plan["ready_to_prepare"])
        self.assertFalse(plan["ready_to_run"])
        self.assertIn("reindex", {i["code"] for i in plan["issues"]})

    def test_remap_collision_window_and_missing_topic(self):
        doc = {"version": 1, "playback": {"remap": {"/old/input": "/result"}}}
        self.assertFalse(self.plan(doc)["ready_to_prepare"])
        self.assertFalse(self.plan({"version": 1, "playback": {"topics": ["/missing"]}})["ready_to_prepare"])
        self.assertFalse(self.plan(opts={"window_from": 10})["ready_to_prepare"])
        plan = self.plan(opts={"window_from": 2, "window_to": 100})
        self.assertEqual(plan["window"], {"start": 2, "end": 10})
        self.assertTrue(self.plan({"version": 1, "playback": {"topics": []}})["playback"]["clock_only"])

    def test_renamed_graph_owner_shared_topics_and_keep(self):
        self.write_source("graph/logger/epoch_20260101T000000Z.yaml", {
            "schema": 1, "nodes": {
                "/old/node": {"pubs": [{"topic": "/result", "type": "std_msgs/msg/String"},
                                       {"topic": "/tf", "type": "tf2_msgs/msg/TFMessage"}]},
                "/unknown/node": {"pubs": [{"topic": "/tf", "type": "tf2_msgs/msg/TFMessage"}]}}})
        doc = {"version": 1, "instances": {"new": {"recorded_as": "old"}}}
        plan = self.plan(doc)
        self.assertNotIn("/result", plan["playback"]["topics"])
        self.assertIn("/tf", plan["playback"]["topics"])
        doc["playback"] = {"keep": ["/result"]}
        self.assertIn("/result", self.plan(doc)["playback"]["topics"])

    def namespace_graph(self, *, subscribes=True):
        self.bag(self.source / "bags/logger/session-1", topic="/robot/controller/input")
        return self.write_source("graph/logger/epoch_20260101T000000Z.yaml", {
            "schema": 1, "nodes": {
                "/robot/controller/server": {
                    "pubs": [{"topic": "/result", "type": "std_msgs/msg/String"},
                             {"topic": "/tf", "type": "tf2_msgs/msg/TFMessage"}],
                    "subs": [{"topic": "/robot/controller/input", "type": "std_msgs/msg/String"}]
                            if subscribes else [],
                    "provides": [{"service": "/control", "type": "std_srvs/srv/Trigger"}]},
                "/other/node": {"pubs": [{"topic": "/tf", "type": "tf2_msgs/msg/TFMessage"}]}}})

    def declare_app_namespace(self, namespace, *, keep_idle=False):
        path = self.root / "services/app/rigging.yaml"
        doc = yaml.safe_load(path.read_text())
        doc["ros_namespace"] = namespace
        path.write_text(yaml.safe_dump(doc))
        if not keep_idle:
            self.rows["autonomy"] = [r for r in self.rows["autonomy"] if r["name"] != "idle"]
            self.vehicle()

    def test_descriptor_namespace_associates_outputs_and_services_without_recipe(self):
        self.namespace_graph()
        self.declare_app_namespace("/robot/controller")
        before = inventory_fingerprint(self.root)
        plan = self.plan()
        self.assertTrue(plan["ready_to_prepare"], plan["issues"])
        self.assertEqual(plan["recorded_namespaces"], {"new": "/robot/controller"})
        self.assertEqual(plan["recorded_namespace_sources"], {"new": "rigging.yaml"})
        self.assertNotIn("/result", plan["playback"]["topics"])
        self.assertIn("/robot/controller/input", plan["playback"]["topics"])
        self.assertIn("/tf", plan["playback"]["topics"])
        self.assertEqual(plan["playback"]["services"], ["/control"])  # Pending payload inspection.
        self.assertEqual(plan["service_selection"]["mode"], "auto")
        recorded = self.plan(opts={"names": ["new"], "selection": "recorded"})
        self.assertEqual(recorded["playback"]["services"], ["/control"])
        self.assertEqual(before, inventory_fingerprint(self.root))

    def test_recipe_namespace_overrides_current_service_for_historical_run(self):
        self.namespace_graph()
        self.declare_app_namespace("/changed/controller")
        unmapped = self.plan()
        self.assertIn("/result", unmapped["playback"]["topics"])
        doc = {"version": 1, "instances": {"new": {"recorded_namespace": "/robot/controller"}}}
        plan = self.plan(doc, opts={"names": ["new"], "selection": "recorded"})
        self.assertTrue(plan["ready_to_prepare"], plan["issues"])
        self.assertNotIn("/result", plan["playback"]["topics"])
        self.assertEqual(plan["playback"]["services"], ["/control"])
        self.assertEqual(plan["recorded_namespace_sources"], {"new": "recipe"})

    def test_descriptor_namespace_template_uses_recorded_instance_identity(self):
        self.namespace_graph()
        self.declare_app_namespace("/robot/{name}", keep_idle=True)
        doc = {"version": 1, "instances": {"new": {"recorded_as": "controller"}}}
        plan = self.plan(doc, opts={"names": ["new"], "selection": "recorded"})
        self.assertTrue(plan["ready_to_prepare"], plan["issues"])
        self.assertEqual(plan["recorded_namespaces"], {"new": "/robot/controller", "idle": "/robot/idle"})
        self.assertEqual(plan["playback"]["services"], ["/control"])
        self.assertNotIn("/result", plan["playback"]["topics"])

    def test_descriptor_namespace_skips_and_no_graph_input_preservation(self):
        graph_path = self.namespace_graph()
        self.declare_app_namespace("/robot/controller")
        plan = self.plan(opts={"skip_services": ["app"]})
        self.assertTrue(plan["ready_to_prepare"], plan["issues"])
        self.assertNotIn("/result", plan["playback"]["topics"])
        self.assertIn("/robot/controller/input", plan["playback"]["topics"])
        self.assertIn("/tf", plan["playback"]["topics"])
        graph_path.unlink()
        plan = self.plan()
        self.assertTrue(plan["ready_to_prepare"], plan["issues"])
        self.assertIn("/robot/controller/input", plan["playback"]["topics"])
        self.assertIn("/result", plan["playback"]["topics"])
        self.assertEqual(plan["playback"]["services"], [])

    def test_descriptor_fixed_namespace_cannot_claim_two_instances(self):
        self.namespace_graph()
        self.declare_app_namespace("/robot/controller", keep_idle=True)
        plan = self.plan(opts={"names": ["new"], "selection": "recorded"})
        self.assertFalse(plan["ready_to_prepare"])
        self.assertEqual(plan["playback"]["services"], [])
        self.assertTrue(any(i["code"] == "namespace_mapping" and "multiple recorded owners" in i["message"]
                            for i in plan["issues"]))
        doc = {"version": 1, "instances": {"idle": {"recorded_namespace": "/robot/idle"}}}
        self.assertTrue(self.plan(doc)["ready_to_prepare"])

    def test_descriptor_namespace_expansion_error_is_reported_by_check(self):
        self.declare_app_namespace("/robot/{name}")
        doc = {"version": 1, "instances": {"new": {"recorded_as": "legacy-name"}}}
        plan = self.plan(doc)
        self.assertFalse(plan["ready_to_prepare"])
        self.assertTrue(any(i["code"] == "namespace_mapping" for i in plan["issues"]))
        doc["instances"]["new"]["recorded_namespace"] = "/robot/valid_name"
        self.assertTrue(self.plan(doc)["ready_to_prepare"])

    def test_descriptor_namespace_survives_prepare_and_recipe_reuse(self):
        self.namespace_graph()
        self.declare_app_namespace("/robot/controller")
        with self.image_label_runtime({}):
            for verb in ("prepare", "check"):
                with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
                    rc = command(self.args(verb, str(self.source), "new", "--recipe", "declared-namespace",
                                           "--selection", "recorded", "--json"), self.root)
                plan = json.loads(out.getvalue())
                self.assertEqual(rc, 0, plan["issues"])
                self.assertEqual(plan["playback"]["services"], ["/control"])
                self.assertNotIn("/result", plan["playback"]["topics"])
                self.assertEqual(plan["recorded_namespace_sources"], {"new": "rigging.yaml"})
        saved = yaml.safe_load((self.root / "replay/declared-namespace.yaml").read_text())
        self.assertNotIn("instances", saved)

    def test_recorded_namespace_fixes_output_ownership_and_explicit_calls(self):
        self.namespace_graph()
        unmapped = self.plan()
        self.assertIn("/result", unmapped["playback"]["topics"])
        self.assertTrue(any(i["code"] == "unselected_service_events" for i in unmapped["issues"]))
        doc = {"version": 1, "instances": {"new": {"recorded_namespace": "/robot/controller"}},
               "playback": {"services": ["/control"]}}
        recipe.validate(doc)
        plan = self.plan(doc)
        self.assertTrue(plan["ready_to_prepare"], plan["issues"])
        self.assertEqual(plan["recorded_namespaces"], {"new": "/robot/controller"})
        self.assertNotIn("/result", plan["playback"]["topics"])
        self.assertIn("/robot/controller/input", plan["playback"]["topics"])
        self.assertIn("/tf", plan["playback"]["topics"])
        self.assertEqual(plan["playback"]["services"], ["/control"])
        self.assertFalse(any(i["code"] == "unselected_service_events" for i in plan["issues"]))
        from rig_cli.replay_workflow import report
        with contextlib.redirect_stderr(io.StringIO()) as out:
            report(plan)
        self.assertIn("recorded ROS namespace new: /robot/controller", out.getvalue())
        self.assertIn("service calls (manual, service origin): /control", out.getvalue())
        self.assertIn("omit regenerated outputs: /result", out.getvalue())
        doc["playback"]["keep"] = ["/result"]
        self.assertIn("/result", self.plan(doc)["playback"]["topics"])

    def test_recorded_namespace_applies_to_recorded_selection_and_empty_inputs(self):
        doc = {"version": 1, "instances": {"new": {
            "recorded_as": "old_driver", "recorded_namespace": "/robot/controller"}}}
        for subscribes in (True, False):
            with self.subTest(subscribes=subscribes):
                self.namespace_graph(subscribes=subscribes)
                plan = self.plan(doc, opts={"names": ["new"], "selection": "recorded"})
                self.assertTrue(plan["ready_to_prepare"], plan["issues"])
                self.assertEqual(plan["playback"]["services"], ["/control"])
                if not subscribes:
                    self.assertEqual(plan["playback"]["topics"], [])
                # Status is outside the node namespace but has observed publisher evidence.
                self.assertNotIn("/result", plan["playback"]["topics"])

    def test_namespace_only_does_not_guess_outputs_or_enable_service_calls(self):
        self.bag(self.source / "bags/logger/session-1", topic="/robot/controller/input")
        for identity in ({}, {"recorded_as": "old_driver"}):
            doc = {"version": 1, "instances": {"new": {
                **identity, "recorded_namespace": "/robot/controller"}}}
            plan = self.plan(doc)
            self.assertIn("/robot/controller/input", plan["playback"]["topics"])
            self.assertIn("/result", plan["playback"]["topics"])
            self.assertEqual(plan["playback"]["services"], [])
            self.assertTrue(any(i["code"] == "unselected_service_events" for i in plan["issues"]))
            doc["playback"] = {"exclude": ["/result"], "services": ["/control"]}
            explicit = self.plan(doc)
            self.assertNotIn("/result", explicit["playback"]["topics"])
            self.assertEqual(explicit["playback"]["services"], ["/control"])

    def test_namespace_ambiguity_does_not_claim_an_inactive_instances_calls(self):
        self.write_source("graph/logger/epoch_20260101T000000Z.yaml", {
            "schema": 1, "nodes": {"/idle/server": {
                "pubs": [{"topic": "/result", "type": "std_msgs/msg/String"}],
                "provides": [{"service": "/control", "type": "std_srvs/srv/Trigger"}]}}})
        doc = {"version": 1, "instances": {"new": {"recorded_namespace": "/idle"}}}
        plan = self.plan(doc, opts={"names": ["new"], "selection": "recorded"})
        self.assertEqual(plan["playback"]["services"], [])
        self.assertIn("/result", plan["playback"]["topics"])
        self.assertTrue(any("unambiguous" in i["message"] for i in plan["issues"]))

    def test_namespace_parent_does_not_own_an_inactive_nested_instance(self):
        path = self.namespace_graph()
        graph_doc = yaml.safe_load(path.read_text())
        graph_doc["nodes"]["/robot/root"] = {
            "subs": [{"topic": "/result", "type": "std_msgs/msg/String"}]}
        path.write_text(yaml.safe_dump(graph_doc))
        doc = {"version": 1, "instances": {
            "new": {"recorded_namespace": "/robot"},
            "idle": {"recorded_namespace": "/robot/controller"}}}
        plan = self.plan(doc, opts={"names": ["new"], "selection": "recorded"})
        self.assertEqual(plan["playback"]["services"], [])
        self.assertIn("/result", plan["playback"]["topics"])

    def test_skips_use_recorded_namespace_and_preserve_shared_data(self):
        self.namespace_graph()
        doc = {"version": 1, "instances": {"new": {
            "recorded_as": "old_driver", "recorded_namespace": "/robot/controller"}},
            "playback": {"keep": ["/result", "/robot/controller/input", "/tf"]}}
        for opts in ({"skip_services": ["app"]}, {"skip_instances": ["new"]}):
            plan = self.plan(doc, opts=opts)
            self.assertTrue(plan["ready_to_prepare"], plan["issues"])
            self.assertNotIn("/result", plan["playback"]["topics"])
            self.assertIn("/robot/controller/input", plan["playback"]["topics"])
            self.assertIn("/tf", plan["playback"]["topics"])

    def test_recorded_namespace_does_not_change_recording_identity(self):
        self.proxy_camera()
        self.recording("old_cam")
        doc = {"version": 1, "instances": {"cam": {
            "recorded_as": "old_cam", "recorded_namespace": "/robot/front_camera"}}}
        plan = self.plan(doc, opts={"names": ["cam"]})
        self.assertTrue(plan["ready_to_prepare"], plan["issues"])
        self.assertEqual(plan["recordings"]["cam"]["path"], str((self.source / "recordings/old_cam").resolve()))

    def test_recorded_namespace_validation_and_ambiguous_ownership(self):
        for namespace in (None, "", "/", "relative", "/trailing/", "/double//slash", "/wild/*", "/bad-name", 3):
            with self.subTest(namespace=namespace), self.assertRaises(RigError):
                recipe.validate({"version": 1, "instances": {"new": {"recorded_namespace": namespace}}})
        for instances in (
                {"new": {"recorded_namespace": "/same"}, "idle": {"recorded_namespace": "/same"}},
                {"new": {"recorded_as": "old", "recorded_namespace": "/one"},
                 "idle": {"recorded_as": "old", "recorded_namespace": "/two"}}):
            with self.assertRaises(RigError):
                recipe.validate({"version": 1, "instances": instances})
        recipe.validate({"version": 1, "instances": {
            "new": {"recorded_namespace": "/parent"}, "idle": {"recorded_namespace": "/parent/child"}}})

    def test_standalone_prepare_idempotence_and_edit_protection(self):
        raw = Path(self.tmp.name) / "raw"
        self.bag(raw)
        doc = {"version": 1, "source": str(raw)}
        path = self.write("replay/demo.yaml", doc)
        plan = self.plan(doc, ref=raw)
        m, _, d = load_context(self.root)
        before = inventory_fingerprint(raw)
        runtime = prepare.create(self.root, path, doc, plan, m, d)
        self.assertEqual(before, inventory_fingerprint(raw))
        copied = runtime / "bags" / "bag_logger" / "raw" / "data.mcap"
        self.assertEqual(copied.read_bytes(), (raw / "data.mcap").read_bytes())
        self.assertNotEqual(copied.stat().st_ino, (raw / "data.mcap").stat().st_ino)
        stamp = copied.stat().st_mtime_ns
        self.assertEqual(runtime, prepare.create(self.root, path, doc, plan, m, d))
        self.assertEqual(stamp, copied.stat().st_mtime_ns)
        resolved = prepare.location(self.root, path, raw) / "resolved.yaml"
        resolved.write_text("user: edit\n")
        with self.assertRaisesRegex(RigError, "changed"):
            prepare.prepared(self.root, path, doc, raw)

    def test_legacy_reindex_uses_copy_and_rejects_source_mutation(self):
        folder = self.source / "bags" / "logger" / "session-1"
        (folder / "metadata.yaml").unlink()
        doc = {"version": 1}
        path = self.write("replay/legacy.yaml", doc)
        plan = self.plan(doc)
        self.assertTrue(plan["source"]["bag"]["repair"])
        m, _, d = load_context(self.root)
        def reindex(argv, **kwargs):
            target = Path(argv[-1])
            self.assertNotEqual(target, folder)
            self.bag(target, topic="/repaired")
            return __import__("subprocess").CompletedProcess(argv, 0, "", "")
        with patch("rig_cli.replay_prepare.subprocess.run", side_effect=reindex):
            runtime = prepare.create(self.root, path, doc, plan, m, d)
        self.assertFalse((folder / "metadata.yaml").exists())
        checked = self.plan(doc, opts={"_prepared_source": str(runtime)})
        self.assertIn("/repaired", checked["playback"]["topics"])
        self.assertEqual(checked["source"]["bag"]["start_ns"], 100_000_000_000)

    def test_camera_alias_filters_by_observed_time_and_copies(self):
        rec = self.source / "recordings" / "old_cam"
        rec.mkdir(parents=True)
        for prefix, t in (("early", 1), ("overlap", 101)):
            (rec / (prefix + ".json")).write_text(json.dumps(dict(pixel_format="Mono8", width=1, height=1)))
            (rec / (prefix + ".csv")).write_text(f"timestamp_ns,system_ns\n{t*10**9},{t*10**9}\n")
            (rec / (prefix + "-00000.mkv")).write_bytes(b"video")
        doc = {"version": 1, "instances": {"cam": {"recorded_as": "old_cam"}}}
        plan = self.plan(doc, opts={"names": ["cam"]})
        self.assertEqual(plan["recordings"]["cam"]["sessions"], ["overlap"])
        path = self.write("replay/camera.yaml", doc)
        m, _, d = load_context(self.root)
        runtime = prepare.create(self.root, path, doc, plan, m, d)
        self.assertTrue((runtime / "recordings/cam/overlap.json").exists())
        self.assertFalse((runtime / "recordings/cam/early.json").exists())
        self.assertEqual(len(list(rec.glob("*.json"))), 2)

    def test_recovered_timing_prepares_camera_subset_in_one_command(self):
        folder = self.source / "bags/logger/session-1"
        (folder / "metadata.yaml").unlink()
        rec = self.source / "recordings/cam"
        rec.mkdir(parents=True)
        for prefix, t in (("early", 1), ("overlap", 101)):
            (rec / (prefix + ".json")).write_text(json.dumps(dict(pixel_format="Mono8", width=1, height=1)))
            (rec / (prefix + ".csv")).write_text(f"timestamp_ns,system_ns\n{t*10**9},{t*10**9}\n")
            (rec / (prefix + "-00000.mkv")).write_bytes(b"video")
        doc = {"version": 1, "source": str(self.source)}
        path = self.write("replay/recovered.yaml", doc)
        original_run = __import__("subprocess").run
        def process(argv, **kwargs):
            if "replay-reindex" in argv:
                self.bag(Path(argv[-1]), topic="/repaired")
                return __import__("subprocess").CompletedProcess(argv, 0, "", "")
            if argv[:3] == ["docker", "image", "inspect"]:
                return __import__("subprocess").CompletedProcess(argv, 0, json.dumps({
                    "Id": "sha256:" + "1" * 64, "Os": "linux", "Architecture": "arm64",
                    "Config": {"Labels": {"io.rig.rosbag2.replay": "1"}}}), "")
            return original_run(argv, **kwargs)
        with patch("rig_cli.image_catalog.docker_arch", return_value="arm64"), \
                patch("rig_cli.replay._guard_clean_host"), \
                patch("rig_cli.replay_prepare.subprocess.run", side_effect=process):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                command(self.args("prepare", str(self.source), "cam", "--recipe", "recovered", "--json"), self.root)
            plan = json.loads(out.getvalue())
            self.assertEqual(prepare.location(self.root, path, self.source).name, prepare.generation(plan))
            runtime = prepare.prepared(self.root, path, doc, self.source)
            self.assertTrue((runtime / "recordings/cam/overlap.json").is_file())
            self.assertFalse((runtime / "recordings/cam/early.json").exists())
            with contextlib.redirect_stdout(io.StringIO()) as out:
                command(self.args("check", str(self.source), "cam", "--recipe", "recovered", "--json"), self.root)
            self.assertFalse({"normalization", "stale_preparation"} &
                             {i["code"] for i in json.loads(out.getvalue())["issues"]})
        self.assertFalse((folder / "metadata.yaml").exists())
        self.assertEqual(len(list(rec.glob("*.json"))), 2)

    def test_source_inventory_tracks_single_bag_index_and_symlink_targets(self):
        raw = Path(self.tmp.name) / "raw"
        self.bag(raw)
        bag = raw / "data.mcap"
        before = inventory_fingerprint(bag)
        index = raw / "metadata.yaml"
        index.write_text(index.read_text() + "\n# amended index\n")
        self.assertNotEqual(before, inventory_fingerprint(bag))
        linked = self.source / "bags/external"
        linked.symlink_to(raw, target_is_directory=True)
        before = inventory_fingerprint(self.source)
        bag.write_bytes(b"changed recording")
        self.assertNotEqual(before, inventory_fingerprint(self.source))
        # Directory cycles terminate without dropping the source's normal files.
        (raw / "cycle").symlink_to(self.source, target_is_directory=True)
        self.assertTrue(inventory_fingerprint(self.source))
        m, _, _ = load_context(self.root)
        with self.assertRaisesRegex(RigError, "expected a ROS 2"):
            inspect_source(m, str(index))

    def test_check_and_prepare_dryrun_write_nothing(self):
        before = inventory_fingerprint(self.root)
        with patch("rig_cli.image_catalog.docker_arch", side_effect=RigError("offline")):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(command(self.args("check", str(self.source), "--json"), self.root), 1)
            self.assertFalse(json.loads(out.getvalue())["ready_to_run"])
            self.assertEqual(command(self.args("prepare", str(self.source), "--recipe", "new",
                                              "--dry-run"), self.root), 0)
        self.assertEqual(before, inventory_fingerprint(self.root))

    def test_missing_configs_aggregate_and_omitted_rows_do_not_block(self):
        (self.root / "router.yaml").unlink()
        (self.root / "new.yaml").unlink()
        (self.root / "cam.yaml").unlink()
        plan = self.plan()
        errors = [i["message"] for i in plan["issues"] if i["code"] == "config"]
        self.assertEqual(len(errors), 2)
        self.assertFalse(any("cam" in e for e in errors))

    def test_environment_is_reset_and_recipe_validation(self):
        m, _, d = load_context(self.root)
        with patch.dict(os.environ, RIG_REPLAY_PLAN="/leaked.json"):
            self.assertNotIn("RIG_REPLAY_PLAN", dispatch.fleet_env(m, d))
        for doc in ({"version": 2}, {"version": 1, "unknown": True},
                    {"version": 1, "playback": {"remap": {"/a": "/clock"}}},
                    {"version": 1, "playback": {"exclude": ["/a"], "keep": ["/a"]}}):
            with self.assertRaises(RigError):
                recipe.validate(doc)
        self.assertEqual(self.args("--", "check").run, "check")
        self.assertEqual(self.args("--recipe", "demo").run, None)

    def test_prepare_command_keeps_recipe_and_reports_after_preparation(self):
        doc = {"version": 1, "source": str(self.source),
               "playback": {"remap": {"/old/input": "/new/input"}}}
        path = self.write("replay/demo.yaml", doc)
        original = path.read_bytes()
        original_run = __import__("subprocess").run
        with patch("rig_cli.image_catalog.docker_arch", return_value="amd64"), \
                patch("rig_cli.replay._guard_clean_host"), \
                patch("rig_cli.replay_plan.subprocess.run", wraps=__import__("subprocess").run) as process:
            process.side_effect = lambda argv, **kw: (
                __import__("subprocess").CompletedProcess(argv, 0, json.dumps({"Id": "sha256:" + "1" * 64, "Os": "linux", "Architecture": "amd64", "Config": {"Labels": {"io.rig.rosbag2.replay": "1"}}}), "")
                if argv[:3] == ["docker", "image", "inspect"] else original_run(argv, **kw))
            with contextlib.redirect_stdout(io.StringIO()) as out:
                result = command(self.args("prepare", "--recipe", "demo", "--json"), self.root)
        self.assertEqual(result, 0)
        reported = json.loads(out.getvalue())
        self.assertTrue(reported["ready_to_run"])
        self.assertEqual(original, path.read_bytes())
        dest = prepare.location(self.root, path, self.source)
        self.assertEqual(yaml.safe_load((dest / "report.yaml").read_text())["ready_to_run"], True)
        self.assertEqual(prepare.prepared(self.root, path, doc, self.source), self.source.resolve())

    def test_explicit_base_replaces_provider_tag_during_readiness(self):
        p = self.root / "services/router/rigging.yaml"
        desc = yaml.safe_load(p.read_text())
        desc["build"].update(provides="base", images=["fleet-ros"])
        p.write_text(yaml.safe_dump(desc))
        self.vehicle(images={"base": "custom-base:tested"})
        m, c, d = load_context(self.root)
        original_run = __import__("subprocess").run
        inspected = []
        def process(argv, **kwargs):
            if argv[:3] == ["docker", "image", "inspect"]:
                inspected.append(argv[-1] if argv[3] == "--format" else argv[3])
                return __import__("subprocess").CompletedProcess(argv, 0, json.dumps({"Id": "sha256:" + "1" * 64, "Os": "linux", "Architecture": "arm64", "Config": {"Labels": {"io.rig.rosbag2.replay": "1"}}}), "")
            return original_run(argv, **kwargs)
        with patch("rig_cli.image_catalog.docker_arch", return_value="arm64"), \
                patch("rig_cli.replay._guard_clean_host"), \
                patch("rig_cli.replay_plan.subprocess.run", side_effect=process):
            plan = planner.build(m, c, d, self.root, str(self.source), {"version": 1}, {})
        self.assertTrue(plan["ready_to_run"], plan["issues"])
        self.assertEqual(inspected, ["custom-base:tested"])

    def test_execution_handoff_and_disabled_config_snapshot(self):
        from rig_cli.replay_workflow import execute
        doc = {"version": 1, "playback": {"remap": {"/old/input": "/new/input"}}}
        plan = self.plan(doc)
        m, _, d = load_context(self.root)
        m.data_dir = str(self.root / "data")
        args = self.args(str(self.source))
        captured = {}
        def run_verb(pairs, env, verb, **kw):
            captured.update(pairs=pairs, env=env, verb=verb)
            player = json.loads(Path(env["RIG_REPLAY_PLAN"]).read_text())
            self.assertEqual(player["bag"]["session"], "session-1")
            self.assertEqual(player["remap"], {"/old/input": "/new/input"})
            self.assertNotIn("RIG_REPLAY_SERVICES", env)
            return []
        with patch("rig_cli.replay._guard_clean_host"), patch("rig_cli.runs.new_run", return_value="new_run"), \
                patch("rig_cli.replay_workflow._empty_projects", return_value=set(plan["selected"])), \
                patch("rig_cli.runs.snapshot") as snapshot, patch("rig_cli.dispatch.run_verb", side_effect=run_verb):
            self.assertEqual(execute(args, self.root, m, d, plan, self.source), 0)
        self.assertEqual([r.name for r, _ in captured["pairs"]], plan["selected"])
        snap = snapshot.call_args.args[0]
        self.assertTrue(next(r for r in snap.sensors if r.name == "new").enabled)
        self.assertTrue((self.root / "data/runs/new_run/.rig/replay/resolved.yaml").is_file())

    def test_capture_finishes_before_release_deadline_and_snapshot_matches_launch(self):
        from rig_cli import runs
        from rig_cli.replay_workflow import execute
        self.write("new.yaml", {"name": "new", "release": "{{replay_start_at_unix_s}}"})
        plan = self.plan()
        m, _, d = load_context(self.root)
        m.data_dir, m.run_capture = str(self.root / "data"), False
        clock = [1000.0]
        original_open = runs.new_run
        def capture(*args, **kwargs):
            clock[0] += 45
            return original_open(*args, **kwargs)
        launched = []
        def launch(row, desc, env, cmd, **kwargs):
            launched.append(row.name)
            self.assertEqual(float(env["RIG_REPLAY_START_AT_UNIX_S"]), 1065.0)
            if row.name == "new":
                self.assertEqual(yaml.safe_load(row.config.read_text())["release"], 1065.0)
            return __import__("subprocess").CompletedProcess(cmd, 0)
        with patch("rig_cli.replay._guard_clean_host"), \
                patch("rig_cli.replay_workflow._empty_projects", return_value=set(plan["selected"])), \
                patch("rig_cli.replay_workflow.time.time", side_effect=lambda: clock[0]), \
                patch("rig_cli.runs.new_run", side_effect=capture), \
                patch("rig_cli.dispatch.run", side_effect=launch):
            self.assertEqual(execute(self.args(str(self.source)), self.root, m, d, plan, self.source), 0)
        self.assertEqual(launched, plan["selected"])
        _, run_dir, metadata = runs.current_run(Path(m.data_dir))
        self.assertEqual(metadata["replay"]["start_at_unix_s"], 1065.0)
        self.assertEqual(metadata["replay"]["startup"]["status"], "started")
        snap = run_dir / ".rig/config" / metadata["config"] / "rendered/new.yaml"
        self.assertEqual(yaml.safe_load(snap.read_text())["release"], 1065.0)

    def test_startup_failure_stops_dependents_and_cleans_only_new_attempted_projects(self):
        from rig_cli.replay_workflow import execute
        plan = self.plan()
        m, _, d = load_context(self.root)
        for problem in (1, OSError("launcher failed"), KeyboardInterrupt()):
            events = []
            def launch(row, desc, env, cmd, **kwargs):
                verb = cmd[2]
                events.append((verb, row.name))
                if verb == "up" and row.name == "new":
                    if isinstance(problem, BaseException):
                        raise problem
                    return __import__("subprocess").CompletedProcess(cmd, problem)
                return __import__("subprocess").CompletedProcess(cmd, 0)
            with self.subTest(problem=repr(problem)), patch("rig_cli.replay._guard_clean_host"), \
                    patch("rig_cli.replay_workflow._empty_projects", return_value={"new", "player"}), \
                    patch("rig_cli.replay_workflow._quiesce_projects", return_value=True), \
                    patch("rig_cli.dispatch.run", side_effect=launch):
                expected = 130 if isinstance(problem, KeyboardInterrupt) else 1
                self.assertEqual(execute(self.args(str(self.source)), self.root, m, d, plan, self.source), expected)
            self.assertEqual(events, [("up", "router"), ("up", "new"), ("down", "new")])

    def test_ordinary_dispatch_keeps_continue_on_failure_behavior(self):
        m, _, d = load_context(self.root)
        pairs = [(row, d[row.service]) for row in m.sensors]
        with patch("rig_cli.dispatch.run", return_value=__import__("subprocess").CompletedProcess([], 1)) as run:
            result = dispatch.run_verb(pairs, {}, "up")
        self.assertEqual(len(result), len(pairs))
        self.assertEqual(run.call_count, len(pairs))

    def test_failed_outcome_write_does_not_prevent_startup_cleanup(self):
        from rig_cli.replay_workflow import execute
        plan = self.plan()
        m, _, d = load_context(self.root)
        failed = [False]
        events = []
        write = prepare.write_yaml
        def save(path, value):
            if failed[0]:
                raise OSError("disk full")
            return write(path, value)
        def launch(row, desc, env, cmd, **kwargs):
            events.append((cmd[2], row.name))
            failed[0] = True
            return __import__("subprocess").CompletedProcess(cmd, 1 if cmd[2] == "up" else 0)
        with patch("rig_cli.replay._guard_clean_host"), \
                patch("rig_cli.replay_workflow._empty_projects", return_value={"router"}), \
                patch("rig_cli.replay_workflow._quiesce_projects", return_value=True), \
                patch("rig_cli.replay_prepare.write_yaml", side_effect=save), \
                patch("rig_cli.dispatch.run", side_effect=launch):
            self.assertEqual(execute(self.args(str(self.source)), self.root, m, d, plan, self.source), 1)
        self.assertEqual(events, [("up", "router"), ("down", "router")])

    def test_project_inventory_is_required_and_preexisting_stopped_projects_are_not_owned(self):
        from rig_cli.replay_workflow import _empty_projects
        m, _, d = load_context(self.root)
        pairs = [(row, d[row.service]) for row in m.sensors][:2]
        def inspect(argv, **kwargs):
            self.assertEqual(argv[:3], ["docker", "ps", "-aq"])
            self.assertIn("com.docker.compose.project=", argv[-1])
            return __import__("subprocess").CompletedProcess(argv, 0, "a" * 64 if pairs[0][0].name in argv[-1] else "", "")
        with patch("rig_cli.replay_workflow.subprocess.run", side_effect=inspect):
            self.assertEqual(_empty_projects(m, pairs), {pairs[1][0].name})
        for output, rc in (("{}", 0), ("", 1)):
            with patch("rig_cli.replay_workflow.subprocess.run", return_value=
                       __import__("subprocess").CompletedProcess([], rc, output, "")):
                with self.assertRaisesRegex(RigError, "Cannot inspect replay project"):
                    _empty_projects(m, pairs)

    def test_zero_delay_still_has_release_gate_and_expired_positive_gate_starts_nothing(self):
        from rig_cli.replay_workflow import execute
        plan = self.plan()
        m, _, d = load_context(self.root)
        clock = [1000.0]
        with patch("rig_cli.replay._guard_clean_host"), \
                patch("rig_cli.replay_workflow._empty_projects", return_value=set(plan["selected"])), \
                patch("rig_cli.replay_workflow.time.time", side_effect=lambda: clock[0]), \
                patch("rig_cli.dispatch.run_verb", return_value=[]) as launch:
            self.assertEqual(execute(self.args(str(self.source), "--start-delay", "0"),
                                     self.root, m, d, plan, self.source), 0)
            self.assertEqual(launch.call_args.args[1]["RIG_REPLAY_START_AT_UNIX_S"], "1000.000")
            m.data_dir = str(self.root / "data")
            def slow_snapshot(*args, **kwargs):
                clock[0] += 30
            launch.reset_mock()
            with patch("rig_cli.runs.new_run", return_value="slow"), \
                    patch("rig_cli.runs.snapshot", side_effect=slow_snapshot):
                with self.assertRaisesRegex(RigError, "no services started"):
                    execute(self.args(str(self.source)), self.root, m, d, plan, self.source)
            launch.assert_not_called()

    def test_first_project_inspection_cannot_consume_the_release_delay(self):
        from rig_cli.replay_workflow import execute
        plan = self.plan()
        m, _, d = load_context(self.root)
        clock = [1000.0]
        def inspect(manifest, pairs):
            clock[0] += 25
            return {row.name for row, _ in pairs}
        with patch("rig_cli.replay._guard_clean_host"), \
                patch("rig_cli.replay_workflow._empty_projects", side_effect=inspect), \
                patch("rig_cli.replay_workflow.time.time", side_effect=lambda: clock[0]), \
                patch("rig_cli.dispatch.run") as launch:
            self.assertEqual(execute(self.args(str(self.source)), self.root, m, d, plan, self.source), 1)
        launch.assert_not_called()

    def test_capture_cannot_change_validated_launch_configuration(self):
        from rig_cli.replay_workflow import execute
        self.write("new.yaml", {"name": "new", "mode": "validated"})
        plan = self.plan()
        m, _, d = load_context(self.root)
        m.data_dir = str(self.root / "data")
        def capture(*args, **kwargs):
            self.write("new.yaml", {"name": "new", "mode": "changed"})
            return "changed"
        with patch("rig_cli.replay._guard_clean_host"), \
                patch("rig_cli.runs.new_run", side_effect=capture), \
                patch("rig_cli.dispatch.run") as launch:
            with self.assertRaisesRegex(RigError, "Deployment changed since planning"):
                execute(self.args(str(self.source)), self.root, m, d, plan, self.source)
        launch.assert_not_called()

    def test_failure_stops_waiting_sources_before_slow_log_capture(self):
        from rig_cli.replay_workflow import execute
        self.recording()
        plan = self.plan(opts={"names": ["cam"]})
        m, _, d = load_context(self.root)
        m.data_dir = str(self.root / "data")
        clock, events, stopped = [1000.0], [], [False]
        def launch(row, desc, env, cmd, **kwargs):
            events.append((cmd[2], row.name))
            return __import__("subprocess").CompletedProcess(cmd, int(cmd[2] == "up" and row.name == "new"))
        def stop(argv, **kwargs):
            self.assertEqual(argv[:4], ["docker", "stop", "--time", "1"])
            stopped[0] = True
            events.append(("stop", "containers"))
            return __import__("subprocess").CompletedProcess(argv, 0, "", "")
        def logs(*args, **kwargs):
            self.assertTrue(stopped[0], "a held source could release during log capture")
            clock[0] += 30
            events.append(("logs", "containers"))
        with patch("rig_cli.replay._guard_clean_host"), \
                patch("rig_cli.replay_workflow._empty_projects", return_value=set(plan["selected"])), \
                patch("rig_cli.replay_workflow._project_container_ids", return_value=["a" * 64]), \
                patch("rig_cli.replay_workflow.subprocess.run", side_effect=stop), \
                patch("rig_cli.replay_workflow.time.time", side_effect=lambda: clock[0]), \
                patch("rig_cli.runs.new_run", return_value="failed"), patch("rig_cli.runs.snapshot"), \
                patch("rig_cli.runs.capture_docker_logs", side_effect=logs), \
                patch("rig_cli.dispatch.run", side_effect=launch):
            self.assertEqual(execute(self.args(str(self.source), "cam"), self.root, m, d, plan, self.source), 1)
        self.assertIn(("up", "cam"), events)
        self.assertNotIn(("up", "player"), events)
        self.assertLess(events.index(("stop", "containers")), events.index(("logs", "containers")))
        self.assertLess(events.index(("logs", "containers")), events.index(("down", "cam")))

    def test_capture_cannot_change_or_create_an_overlay_base_pin(self):
        from rig_cli.replay_workflow import execute
        self.rows["autonomy"][0]["overlays"] = ["test/tune@1.0.0"]
        self.vehicle()
        self.write("new.yaml", {"name": "new", "mode": 1})
        self.write("config/.overlays/test--tune--1.0.0.yaml", {"mode": 2})
        pin = self.root / "config/.pins/new.yaml"
        for initially_present in (True, False):
            if initially_present:
                self.write("config/.pins/new.yaml", {"mode": 1})
            else:
                pin.unlink(missing_ok=True)
            plan = self.plan()
            self.assertEqual(plan["configs"]["new"]["mode"], 2)
            m, _, d = load_context(self.root)
            m.data_dir = str(self.root / "data")
            def capture(*args, **kwargs):
                self.write("config/.pins/new.yaml", {"mode": 2})
                return "pin-change"
            with self.subTest(initially_present=initially_present), \
                    patch("rig_cli.replay._guard_clean_host"), \
                    patch("rig_cli.runs.new_run", side_effect=capture), \
                    patch("rig_cli.dispatch.run") as launch:
                with self.assertRaisesRegex(RigError, "Deployment changed since planning"):
                    execute(self.args(str(self.source)), self.root, m, d, plan, self.source)
            launch.assert_not_called()

    def test_failed_preparation_rolls_back_player_wiring(self):
        self.rows["autonomy"] = [r for r in self.rows["autonomy"] if r["name"] != "player"]
        self.vehicle()
        before = {n: (self.root / n).read_bytes() for n in ("vehicle.yaml", "services.yaml")}
        with patch("rig_cli.image_catalog.docker_arch", side_effect=RigError("offline")), \
                patch("rig_cli.replay._guard_clean_host"), \
                patch("rig_cli.replay_prepare.create", side_effect=RigError("repair failed")):
            with self.assertRaisesRegex(RigError, "repair failed"):
                command(self.args("prepare", str(self.source), "--recipe", "failed",
                                  "--player", str(self.root / "services/ros2-bag-player")), self.root)
        self.assertEqual(before, {n: (self.root / n).read_bytes() for n in before})
        self.assertFalse((self.root / "config/autonomy/bag_player.yaml").exists())
        self.assertFalse((self.root / "replay/failed.yaml").exists())
        self.assertFalse(list((self.root / "var/replay/failed").glob("*/provenance.yaml")))

    def test_a_failed_backup_restores_nothing_and_deletes_nothing(self):
        # A backup copy failing (an unreadable config, a full /tmp) changed nothing in the
        # deployment — the rollback used to rmtree the live dirs and copy the partial backup back.
        self.rows["autonomy"] = [r for r in self.rows["autonomy"] if r["name"] != "player"]
        self.vehicle()
        keep = self.root / "config" / "autonomy" / "calibration.yaml"
        keep.parent.mkdir(parents=True, exist_ok=True)
        keep.write_text("k: 1\n")
        def tree():
            return sorted(str(p.relative_to(self.root)) for d in ("config", "services")
                          for p in (self.root / d).rglob("*"))
        files = tree()
        real = shutil.copytree

        def broken(src, dst, *args, **kwargs):
            if Path(src).name == "services":
                raise OSError("No space left on device")
            return real(src, dst, *args, **kwargs)
        with patch("rig_cli.image_catalog.docker_arch", side_effect=RigError("offline")), \
                patch("rig_cli.replay._guard_clean_host"), \
                patch("shutil.copytree", side_effect=broken), \
                patch("rig_cli.reconstruct.wire_player") as wire:
            with self.assertRaisesRegex(OSError, "No space left"):
                command(self.args("prepare", str(self.source), "--recipe", "nospace",
                                  "--player", str(self.root / "services/ros2-bag-player")), self.root)
        wire.assert_not_called()
        self.assertEqual(keep.read_text(), "k: 1\n")
        self.assertEqual(files, tree())  # services/ was rmtree'd and "restored" empty before

    def test_preparation_rejects_source_changes_and_lock_contention(self):
        raw = Path(self.tmp.name) / "raw"
        self.bag(raw)
        doc = {"version": 1}
        path = self.write("replay/changing.yaml", doc)
        m, _, d = load_context(self.root)
        plan = self.plan(doc, ref=raw)
        copy = prepare.copy_file
        def mutate(src, dst):
            copy(src, dst)
            if Path(src).suffix == ".mcap":
                (raw / "data.mcap").write_bytes(b"writer is still running")
        with patch("rig_cli.replay_prepare.copy_file", side_effect=mutate):
            with self.assertRaisesRegex(RigError, "changed during preparation"):
                prepare.create(self.root, path, doc, plan, m, d)
        self.assertFalse(list(recipe.workspace(self.root, path).glob("*/provenance.yaml")))
        import fcntl
        lock_path = recipe.workspace(self.root, path) / ".prepare.lock"
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch("rig_cli.image_catalog.docker_arch", side_effect=RigError("offline")), \
                    patch("rig_cli.replay._guard_clean_host"):
                with self.assertRaisesRegex(RigError, "Another preparation"):
                    command(self.args("prepare", str(raw), "--recipe", "changing"), self.root)

    def test_open_source_and_nested_bag_are_both_rejected(self):
        m, _, _ = load_context(self.root)
        with patch("rig_cli.runs.is_open", side_effect=lambda manifest, p: Path(p) == self.source.resolve()):
            for source in (self.source, self.source / "bags/logger/session-1/data.mcap"):
                with self.assertRaisesRegex(RigError, "OPEN run"):
                    inspect_source(m, str(source))

    def test_completions_grouped_check_prepare_and_recipe_names(self):
        from rig_cli.completions import candidates
        self.write("replay/demo.yaml", {"version": 1})
        prefix = ["--root", str(self.root)]
        for words in (["replay", ""], ["run", "replay", ""]):
            result = candidates(prefix + words, len(prefix + words) - 1)
            self.assertIn("check", result)
            self.assertIn("prepare", result)
        words = prefix + ["run", "replay", "check", "--recipe", ""]
        self.assertIn("demo", candidates(words, len(words) - 1))

    def proxy_camera(self):
        path = self.root / "services/camera/rigging.yaml"
        doc = yaml.safe_load(path.read_text())
        doc["replay"]["source"].update(binding="direct", overrides={"replay": {
            "path": "{{replay_input_path}}", "sessions": "{{replay_input_sessions}}",
            "run": None, "proxy": "{{replay_input_proxy}}"}})
        doc["export"] = dict(data="recordings/{name}", modes=["service"],
                             command=["tools/export-recordings"], default_profile="h264",
                             profiles={"h264": "export-profiles/h264.yaml"})
        helper = self.root / "services/camera/tools/export-recordings"
        helper.parent.mkdir(parents=True, exist_ok=True)
        helper.write_text("#!/bin/sh\nexit 0\n")
        helper.chmod(0o755)
        self.write("services/camera/export-profiles/h264.yaml", {
            "version": 1, "id": "h264", "revision": 1, "format": "camera-recording-proxy/v1", "lossy": True})
        path.write_text(yaml.safe_dump(doc))

    def recording(self, name="cam", session="overlap", start=101):
        rec = self.source / "recordings" / name
        rec.mkdir(parents=True, exist_ok=True)
        (rec / (session + ".json")).write_text(json.dumps(dict(pixel_format="Mono8", width=2, height=2)))
        (rec / (session + ".csv")).write_text(f"timestamp_ns,system_ns\n{start*10**9},{start*10**9}\n")
        (rec / (session + "-00000.mkv")).write_bytes(b"video")
        return rec

    @contextlib.contextmanager
    def proxy_resolver(self):
        """Isolate replay's selection contract; service-export tests validate actual artifacts."""
        import shutil
        from unittest.mock import Mock
        def resolve(root, binding, *, service, profile=None, export_id=None, descriptor=None, verification=None):
            export = Path(root) / "exports/services" / service / (profile or "h264") / (export_id or "generation-a")
            rec = export / "recordings" / binding["recorded_as"]
            if not rec.is_dir():
                raise RigError("Missing proxy; run rig run export RUN --service camera")
            return {**binding, "path": str(rec), "original": dict(binding),
                    "files": [str(rec / Path(p).name) for p in binding["files"]],
                    "fingerprint": inventory_fingerprint(rec), "proxy": {
                        "export_id": export.name, "profile": profile or "h264", "profile_hash": "profile-a",
                        "manifest": str(export / "export.yaml"), "lossy": True}}
        def make(name="cam", export_id="generation-a"):
            export = self.source / "exports/services/camera/h264" / export_id
            rec = export / "recordings" / name
            rec.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(self.source / "recordings" / name, rec)
            (export / "export.yaml").write_text("version: 1\ncomplete: true\n")
            return export
        resolver = Mock(side_effect=resolve)
        with patch("rig_cli.service_exports.resolve_proxy_binding", resolver):
            yield make, resolver

    def test_direct_binding_alias_and_exact_sessions_do_not_copy_bag(self):
        self.proxy_camera()
        rec = self.recording("old_cam")
        self.recording("old_cam", "early", 1)
        self.write("cam.yaml", dict(name="cam", service="camera", replay={
            "run": "standalone-selector", "sessions": ["old-session"],
            "proxy": {"export_id": "stale-export"}}))
        doc = {"version": 1, "instances": {"cam": {"recorded_as": "old_cam"}}}
        plan = self.plan(doc, opts={"names": ["cam"]})
        self.assertEqual(plan["configs"]["cam"]["replay"]["path"], str(rec.resolve()))
        self.assertEqual(plan["configs"]["cam"]["replay"]["sessions"], ["overlap"])
        self.assertNotIn("run", plan["configs"]["cam"]["replay"])
        self.assertNotIn("proxy", plan["configs"]["cam"]["replay"])
        self.assertFalse(plan["needs_preparation"])
        path = self.write("replay/direct.yaml", doc)
        m, _, d = load_context(self.root)
        self.assertEqual(prepare.create(self.root, path, doc, plan, m, d), self.source.resolve())
        self.assertFalse((prepare.location(self.root, path, self.source) / "source").exists())

    def test_proxy_opt_in_preferences_and_explicit_original(self):
        self.proxy_camera()
        self.recording()
        doc = {"version": 1, "proxies": {"services": {"camera": {"profile": "h264"}},
                                       "instances": {"cam": {"export": "generation-a"}}}}
        with self.proxy_resolver() as (make, resolver):
            artifact = make()
            original = self.plan(doc, opts={"names": ["cam"]})
            resolver.assert_not_called()
            self.assertNotIn("proxy", original["recordings"]["cam"])
            proxy = self.plan(doc, opts={"names": ["cam"], "use_proxies": True})
            self.assertTrue(proxy["ready_to_prepare"], proxy["issues"])
            self.assertEqual(proxy["configs"]["cam"]["replay"]["path"], str((artifact / "recordings/cam").resolve()))
            self.assertEqual(proxy["recordings"]["cam"]["proxy"]["export_id"], "generation-a")
            doc["proxies"]["instances"]["cam"] = {"use": "original"}
            resolver.reset_mock()
            original = self.plan(doc, opts={"names": ["cam"], "use_proxies": True})
            resolver.assert_not_called()
            self.assertNotIn("proxy", original["recordings"]["cam"])

    def test_missing_proxies_error_but_live_and_skipped_instances_ignore_them(self):
        self.proxy_camera()
        self.recording()
        with self.proxy_resolver() as (_, resolver):
            plan = self.plan(opts={"names": ["cam"], "use_proxies": True})
            self.assertFalse(plan["ready_to_prepare"])
            self.assertTrue(any("rig run export" in i["message"] for i in plan["issues"]))
            resolver.reset_mock()
            self.assertTrue(self.plan(opts={"live": ["cam"], "use_proxies": True})["ready_to_prepare"])
            self.assertTrue(self.plan(opts={"use_proxies": True})["ready_to_prepare"])
            resolver.assert_not_called()

    def test_old_reader_cannot_use_proxy_even_with_profile_declared(self):
        self.proxy_camera()
        self.recording()
        path = self.root / "services/camera/rigging.yaml"
        doc = yaml.safe_load(path.read_text())
        del doc["replay"]["source"]["binding"]
        path.write_text(yaml.safe_dump(doc))
        plan = self.plan(opts={"names": ["cam"], "use_proxies": True})
        self.assertFalse(plan["ready_to_prepare"])
        self.assertTrue(any("binding: direct" in i["message"] for i in plan["issues"]))

    def test_original_preparation_ignores_root_exports_but_not_nested_data(self):
        doc = {"version": 1}
        path = self.write("replay/original.yaml", doc)
        plan = self.plan(doc)
        m, _, d = load_context(self.root)
        prepare.create(self.root, path, doc, plan, m, d)
        before = source_fingerprint(self.source)
        self.write_source("exports/services/camera/h264/unused/export.yaml", {"version": 1})
        self.assertEqual(source_fingerprint(self.source), before)
        self.assertEqual(prepare.prepared(self.root, path, doc, self.source), self.source.resolve())
        self.write_source("recordings/exports/capture.yaml", {"source": "real data"})
        self.assertNotEqual(source_fingerprint(self.source), before)
        self.assertIsNone(prepare.prepared(self.root, path, doc, self.source))

    def test_proxy_prepare_isolated_from_originals_and_preserves_exact_pin(self):
        self.proxy_camera()
        self.recording()
        doc = {"version": 1, "source": str(self.source)}
        path = self.write("replay/proxy.yaml", doc)
        m, _, d = load_context(self.root)
        with self.proxy_resolver() as (make, resolver):
            artifact = make()
            plan = self.plan(doc, opts={"names": ["cam"], "use_proxies": True})
            self.assertFalse(plan["needs_preparation"])
            self.assertEqual(prepare.create(self.root, path, doc, plan, m, d), self.source.resolve())
            proxy_location = prepare.location(self.root, path, self.source, use_proxies=True)
            self.assertFalse((proxy_location / "source").exists())
            self.assertIsNone(prepare.prepared(self.root, path, doc, self.source))
            originals = self.plan(doc, opts={"names": ["cam"]})
            prepare.create(self.root, path, doc, originals, m, d)
            self.assertNotEqual(prepare.location(self.root, path, self.source), proxy_location)
            self.assertEqual(prepare.prepared(self.root, path, doc, self.source), self.source.resolve())
            self.assertEqual(prepare.prepared(self.root, path, doc, self.source, use_proxies=True), self.source.resolve())
            make(export_id="generation-b")
            pins = prepare.proxy_pins(self.root, path, doc, self.source)
            checked = self.plan(doc, opts={"names": ["cam"], "use_proxies": True, "_prepared_proxies": pins})
            self.assertEqual(checked["recordings"]["cam"]["proxy"]["export_id"], "generation-a")
            __import__("shutil").rmtree(artifact)
            checked = self.plan(doc, opts={"names": ["cam"], "use_proxies": True,
                                           "_prepared_proxies": prepare.proxy_pins(self.root, path, doc, self.source)})
            self.assertFalse(checked["ready_to_prepare"])
            self.assertEqual(resolver.call_args.kwargs["export_id"], "generation-a")

    def test_selected_proxy_manifest_change_invalidates_preparation(self):
        self.proxy_camera()
        self.recording()
        doc = {"version": 1}
        path = self.write("replay/proxy.yaml", doc)
        m, _, d = load_context(self.root)
        with self.proxy_resolver() as (make, _):
            artifact = make()
            plan = self.plan(doc, opts={"names": ["cam"], "use_proxies": True})
            prepare.create(self.root, path, doc, plan, m, d)
            (artifact / "export.yaml").write_text("version: 1\ncomplete: false\n")
            self.assertIsNone(prepare.prepared(self.root, path, doc, self.source, use_proxies=True))
            from rig_cli.replay_sources import verify_binding
            with self.assertRaisesRegex(RigError, "proxy manifest changed"):
                verify_binding(plan["recordings"]["cam"])

    def test_proxy_recipe_validation_and_prevent_opt_in_bypass(self):
        good = {"version": 1, "proxies": {"services": {"camera": {"profile": "h264"}},
                                        "instances": {"cam": {"use": "original"}}}}
        self.assertEqual(recipe.validate(good), good)
        for bad in ({"instances": {"cam": {"use": "proxy"}}},
                    {"instances": {"cam": {"use": "original", "export": "x"}}},
                    {"instances": {"cam": {"export": "../x"}}},
                    {"services": {"camera": {"profile": ""}}}, {"enabled": True}):
            with self.assertRaises(RigError):
                recipe.validate({"version": 1, "proxies": bad})
        self.assertFalse(self.plan({"version": 1, "proxies": {
            "instances": {"missing": {"use": "original"}}}})["ready_to_prepare"])
        self.proxy_camera()
        self.recording()
        with self.proxy_resolver() as (make, _):
            artifact = make()
            for enabled in (False, True):
                plan = self.plan({"version": 1, "recordings": {"cam": {
                    "from": str(artifact / "recordings/cam")}}}, opts={"names": ["cam"], "use_proxies": enabled})
                self.assertFalse(plan["ready_to_prepare"])
                self.assertTrue(any("recordings.from" in i["message"] for i in plan["issues"]))

    def test_direct_camera_mounts_stay_independent_of_legacy_bag_repair(self):
        self.proxy_camera()
        self.recording()
        (self.source / "bags/logger/session-1/metadata.yaml").unlink()
        doc = {"version": 1}
        path = self.write("replay/legacy-proxy.yaml", doc)
        m, _, d = load_context(self.root)
        with self.proxy_resolver() as (make, _):
            make()
            plan = self.plan(doc, opts={"names": ["cam"], "use_proxies": True})
            def reindex(argv, **kwargs):
                self.bag(Path(argv[-1]))
                return __import__("subprocess").CompletedProcess(argv, 0, "", "")
            with patch("rig_cli.replay_prepare.subprocess.run", side_effect=reindex):
                runtime = prepare.create(self.root, path, doc, plan, m, d)
            self.assertTrue((runtime / "bags/logger/session-1/metadata.yaml").is_file())
            self.assertFalse((runtime / "recordings").exists())
            checked = self.plan(doc, opts={"names": ["cam"], "use_proxies": True, "_prepared_source": str(runtime)})
            self.assertEqual(checked["configs"]["cam"]["replay"]["path"], plan["recordings"]["cam"]["path"])

    def test_execution_uses_same_direct_bindings_and_records_proxy_mode(self):
        from rig_cli.replay_workflow import execute
        self.proxy_camera()
        self.recording()
        m, _, d = load_context(self.root)
        m.data_dir = str(self.root / "data")
        with self.proxy_resolver() as (make, _):
            make()
            plan = self.plan(opts={"names": ["cam"], "use_proxies": True})
            def launch(pairs, env, verb, **kwargs):
                camera = next(row for row, _ in pairs if row.name == "cam")
                config = yaml.safe_load(camera.config.read_text())
                self.assertEqual(config["replay"], plan["configs"]["cam"]["replay"])
                self.assertEqual(json.loads(Path(env["RIG_REPLAY_PLAN"]).read_text())["source"], str(self.source))
                return []
            with patch("rig_cli.replay._guard_clean_host"), patch("rig_cli.runs.new_run", return_value="proxy-run") as new_run, \
                    patch("rig_cli.replay_workflow._empty_projects", return_value=set(plan["selected"])), \
                    patch("rig_cli.runs.snapshot"), patch("rig_cli.dispatch.run_verb", side_effect=launch):
                self.assertEqual(execute(self.args(str(self.source), "cam"), self.root, m, d, plan, self.source), 0)
                provenance = new_run.call_args.kwargs["replay"]
                self.assertTrue(provenance["use_proxies"])
                self.assertEqual(provenance["sources"]["cam"]["proxy"]["export_id"], "generation-a")

    def proxy_artifact(self, name="cam", *, variation="default"):
        """Publish a genuine service-export contract fixture without running a converter."""
        from rig_cli import service_exports
        import shutil
        _, _, descriptors = load_context(self.root)
        profile = service_exports.profile_for(descriptors["camera"])
        source = self.source / "recordings" / name
        base = self.source / "exports/services/camera/h264"
        stage = base / (".fixture-" + variation)
        recording = stage / "recordings" / name
        recording.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, recording)
        sessions = []
        for header in sorted(source.glob("*.json")):
            prefix = header.stem
            produced = recording / header.name
            value = json.loads(produced.read_text())
            value["proxy"] = {"version": 1, "format": "camera-recording-proxy/v1", "lossy": True}
            produced.write_text(json.dumps(value))
            (recording / (prefix + "-00000.mkv")).write_bytes(("h264-" + variation).encode())
            paths = [header.name, prefix + ".csv", prefix + "-00000.mkv"]
            def entries(root):
                return [dict(path=f"recordings/{name}/{p}", size=(root / p).stat().st_size,
                             sha256=service_exports.file_sha256(root / p)) for p in paths]
            sessions.append(dict(id=prefix, source_files=entries(source), files=entries(recording)))
        manifest = dict(version=1, kind="rig-service-export", complete=True, service="camera",
                        profile=profile, validation={"ok": True},
                        recordings=[dict(name=name, source_path=f"recordings/{name}",
                                         path=f"recordings/{name}", sessions=sessions)])
        manifest["export_id"] = service_exports.digest(manifest)
        (stage / "export.yaml").write_text(yaml.safe_dump(manifest))
        final = base / manifest["export_id"]
        stage.rename(final)
        return final

    def test_real_artifact_alias_window_selection_and_relocated_run(self):
        import shutil
        self.proxy_camera()
        self.recording("old_cam", "early", 101)
        self.recording("old_cam", "late", 109)
        artifact = self.proxy_artifact("old_cam")
        doc = {"version": 1, "instances": {"cam": {"recorded_as": "old_cam"}}}
        opts = {"names": ["cam"], "use_proxies": True, "window_from": 5}
        plan = self.plan(doc, opts=opts)
        self.assertTrue(plan["ready_to_prepare"], plan["issues"])
        self.assertEqual(plan["recordings"]["cam"]["sessions"], ["late"])
        self.assertEqual(plan["configs"]["cam"]["replay"]["path"], str((artifact / "recordings/old_cam").resolve()))
        moved = Path(self.tmp.name) / "copied-run"
        shutil.copytree(self.source, moved)
        relocated = self.plan(doc, opts=opts, ref=moved)
        self.assertTrue(relocated["ready_to_prepare"], relocated["issues"])
        self.assertEqual(relocated["recordings"]["cam"]["proxy"]["export_id"], artifact.name)
        self.assertTrue(Path(relocated["configs"]["cam"]["replay"]["path"]).is_relative_to(moved.resolve()))

    def test_real_artifacts_require_pin_when_ambiguous_and_reject_corruption(self):
        self.proxy_camera()
        self.recording()
        first = self.proxy_artifact()
        second = self.proxy_artifact(variation="second")
        opts = {"names": ["cam"], "use_proxies": True}
        ambiguous = self.plan(opts=opts)
        self.assertFalse(ambiguous["ready_to_prepare"])
        self.assertTrue(any("multiple compatible" in i["message"] for i in ambiguous["issues"]))
        doc = {"version": 1, "proxies": {"instances": {"cam": {"export": first.name}}}}
        selected = self.plan(doc, opts=opts)
        self.assertTrue(selected["ready_to_prepare"], selected["issues"])
        self.assertEqual(selected["recordings"]["cam"]["proxy"]["export_id"], first.name)
        (first / "recordings/cam/overlap-00000.mkv").write_bytes(b"corrupted media")
        broken = self.plan(doc, opts=opts)
        self.assertFalse(broken["ready_to_prepare"])
        self.assertTrue(any(first.name in i["message"] for i in broken["issues"]))
        # Another valid export never overrides the requested pin.
        self.assertTrue(second.is_dir())
        doc["proxies"]["instances"]["cam"]["export"] = second.name
        self.assertTrue(self.plan(doc, opts=opts)["ready_to_prepare"])
        (self.source / "recordings/cam/overlap-00000.mkv").write_bytes(b"changed source")
        self.assertFalse(self.plan(doc, opts=opts)["ready_to_prepare"])

    def test_proxy_sidecars_outside_artifact_cannot_bypass_opt_in(self):
        self.proxy_camera()
        rec = self.recording()
        header = rec / "overlap.json"
        value = json.loads(header.read_text())
        value["proxy"] = {"version": 1, "format": "camera-recording-proxy/v1", "lossy": True}
        header.write_text(json.dumps(value))
        plan = self.plan(opts={"names": ["cam"]})
        self.assertFalse(plan["ready_to_prepare"])
        self.assertTrue(any("derived proxy" in i["message"] for i in plan["issues"]))

    def test_command_opt_in_never_leaks_from_proxy_preparation(self):
        self.proxy_camera()
        self.recording()
        artifact = self.proxy_artifact()
        self.write("replay/cli-proxy.yaml", {"version": 1, "source": str(self.source)})
        with patch("rig_cli.image_catalog.docker_arch", side_effect=RigError("offline")), \
                patch("rig_cli.replay._guard_clean_host"):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                command(self.args("prepare", str(self.source), "cam", "--recipe", "cli-proxy",
                                  "--use-proxies", "--json"), self.root)
            prepared_plan = json.loads(out.getvalue())
            self.assertTrue(prepared_plan["use_proxies"])
            self.assertEqual(prepared_plan["recordings"]["cam"]["proxy"]["export_id"], artifact.name)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                command(self.args("check", str(self.source), "cam", "--recipe", "cli-proxy", "--json"), self.root)
            original_plan = json.loads(out.getvalue())
            self.assertFalse(original_plan["use_proxies"])
            self.assertNotIn("proxy", original_plan["recordings"]["cam"])
            self.assertEqual(original_plan["configs"]["cam"]["replay"]["path"],
                             str((self.source / "recordings/cam").resolve()))
            # A later export does not change the generation selected by preparation.
            self.proxy_artifact(variation="newer")
            with contextlib.redirect_stdout(io.StringIO()) as out:
                command(self.args("check", str(self.source), "cam", "--recipe", "cli-proxy",
                                  "--use-proxies", "--json"), self.root)
            checked = json.loads(out.getvalue())
            self.assertTrue(checked["ready_to_prepare"], checked["issues"])
            self.assertEqual(checked["recordings"]["cam"]["proxy"]["export_id"], artifact.name)

    def test_recovered_index_survives_direct_proxy_session_narrowing(self):
        self.proxy_camera()
        self.recording("cam", "outside", 1)
        self.recording("cam", "first", 101)
        self.recording("cam", "last", 109)
        artifact = self.proxy_artifact()
        (self.source / "bags/logger/session-1/metadata.yaml").unlink()
        doc = {"version": 1, "source": str(self.source)}
        path = self.write("replay/legacy-direct.yaml", doc)
        original_run = __import__("subprocess").run
        def process(argv, **kwargs):
            if "replay-reindex" in argv:
                self.bag(Path(argv[-1]))
                return __import__("subprocess").CompletedProcess(argv, 0, "", "")
            if argv[:3] == ["docker", "image", "inspect"]:
                return __import__("subprocess").CompletedProcess(argv, 0, json.dumps({
                    "Id": "sha256:" + "1" * 64, "Os": "linux", "Architecture": "arm64",
                    "Config": {"Labels": {"io.rig.rosbag2.replay": "1"}}}), "")
            return original_run(argv, **kwargs)
        base = [str(self.source), "cam", "--recipe", "legacy-direct", "--use-proxies", "--json"]
        with patch("rig_cli.image_catalog.docker_arch", return_value="arm64"), \
                patch("rig_cli.replay._guard_clean_host"), \
                patch("rig_cli.replay_prepare.subprocess.run", side_effect=process):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                command(self.args("prepare", *base), self.root)
            plan = json.loads(out.getvalue())
            self.assertTrue(plan["ready_to_prepare"], plan["issues"])
            self.assertEqual(plan["recordings"]["cam"]["sessions"], ["first", "last"])
            runtime = prepare.prepared(self.root, path, doc, self.source, use_proxies=True)
            self.assertNotEqual(runtime, self.source.resolve())
            self.assertTrue((runtime / "bags/logger/session-1/metadata.yaml").exists())
            self.assertFalse((runtime / "recordings").exists())
            with contextlib.redirect_stdout(io.StringIO()) as out:
                command(self.args("check", *base, "--from", "5"), self.root)
            changed = json.loads(out.getvalue())
            self.assertTrue(changed["source"]["bag"]["repair"])
            self.assertTrue(changed["needs_preparation"])
            self.assertNotIn("runtime_root", changed["source"])
            with contextlib.redirect_stdout(io.StringIO()) as out:
                command(self.args("prepare", *base, "--from", "5"), self.root)
            narrowed = json.loads(out.getvalue())
            self.assertTrue(narrowed["ready_to_prepare"], narrowed["issues"])
            self.assertEqual(narrowed["recordings"]["cam"]["sessions"], ["last"])
            self.assertEqual(narrowed["recordings"]["cam"]["proxy"]["export_id"], artifact.name)
            runtime = prepare.prepared(self.root, path, doc, self.source, use_proxies=True)
            self.assertTrue((runtime / "bags/logger/session-1/metadata.yaml").exists())
            self.assertFalse((self.source / "bags/logger/session-1/metadata.yaml").exists())

    def reader_label_camera(self):
        self.proxy_camera()
        self.recording()
        path = self.root / "services/camera/rigging.yaml"
        desc = yaml.safe_load(path.read_text())
        desc["build"].update(platforms=["dev", "hardware"], images=["cam-core", "cam-dev"],
                             variants={"dev": {"images": ["cam-dev"], "architectures": ["arm64", "amd64"]},
                                       "hardware": {"images": ["cam-core"], "architectures": ["arm64"]}})
        desc["images"] = {"override_env": {"cam-dev": "CAM_DEV_IMAGE", "cam-core": "CAM_CORE_IMAGE"}}
        desc["replay"]["source"]["image_labels"] = {
            image: {"io.rig.camera.replay-bindings": "1"} for image in ("cam-core", "cam-dev")}
        path.write_text(yaml.safe_dump(desc))
        self.vehicle(platform="dev", images={"registry": "example.test", "tag": "v1"})

    @contextlib.contextmanager
    def image_label_runtime(self, labels):
        original = __import__("subprocess").run
        inspected = []
        def process(argv, **kwargs):
            if argv[:3] == ["docker", "image", "inspect"]:
                ref = argv[-1] if argv[3] == "--format" else argv[3]
                if ref != "fixture/runtime:latest" and not ref.startswith("example.test/runtime") :
                    inspected.append(ref)
                output = json.dumps({"Id": "sha256:" + "1" * 64, "Os": "linux", "Architecture": "arm64",
                                     "RepoDigests": [ref] if "@sha256:" in ref else [],
                                     "Config": {"Labels": {"io.rig.rosbag2.replay": "1", **labels}}})
                return __import__("subprocess").CompletedProcess(argv, 0, output, "")
            return original(argv, **kwargs)
        with patch("rig_cli.image_catalog.docker_arch", return_value="arm64"), \
                patch("rig_cli.replay._guard_clean_host"), \
                patch("rig_cli.replay_plan.subprocess.run", side_effect=process):
            yield inspected

    def test_direct_reader_requires_runtime_label_for_originals_and_proxies(self):
        self.reader_label_camera()
        self.proxy_artifact()
        m, c, d = load_context(self.root)
        for proxies in (False, True):
            opts = {"names": ["cam"], "use_proxies": proxies, "force": True}
            with self.image_label_runtime({}) as inspected:
                plan = planner.build(m, c, d, self.root, str(self.source), {"version": 1}, opts)
            self.assertFalse(plan["ready_to_run"])
            errors = [i for i in plan["issues"] if i["code"] == "runtime_images" and i["status"] == "error"]
            self.assertEqual(len(errors), 1, plan["issues"])
            self.assertEqual(errors[0]["status"], "error")
            self.assertIn("io.rig.camera.replay-bindings", errors[0]["message"])
            self.assertEqual(inspected, ["example.test/cam-dev:v1-dev"])
            with self.image_label_runtime({"io.rig.camera.replay-bindings": "1"}) as inspected:
                plan = planner.build(m, c, d, self.root, str(self.source), {"version": 1}, opts)
            self.assertTrue(plan["ready_to_run"], plan["issues"])
            self.assertEqual(inspected, ["example.test/cam-dev:v1-dev"])

    def test_direct_reader_label_checks_dispatch_override_and_catalog_pin(self):
        self.reader_label_camera()
        m, c, d = load_context(self.root)
        m.extra_env["CAM_DEV_IMAGE"] = "other.test/custom-camera:old"
        with self.image_label_runtime({}) as inspected:
            plan = planner.build(m, c, d, self.root, str(self.source), {"version": 1}, {"names": ["cam"]})
        self.assertFalse(plan["ready_to_run"])
        self.assertEqual(inspected, ["other.test/custom-camera:old"])
        m.extra_env.clear()
        m.build_catalog = {"example.test/cam-dev:v1-dev": {
            "image": "cam-dev", "digest": "sha256:" + "a" * 64, "architectures": ["arm64"],
            "children": {"arm64": "sha256:" + "b" * 64}},
            "fixture/runtime:latest": {"image": "runtime", "digest": "sha256:" + "c" * 64,
                "architectures": ["arm64"], "children": {"arm64": "sha256:" + "d" * 64}}}
        m.image_base = "example.test/runtime:latest"
        pinned = "example.test/cam-dev@sha256:" + "b" * 64
        with self.image_label_runtime({}) as inspected:
            plan = planner.build(m, c, d, self.root, str(self.source), {"version": 1}, {"names": ["cam"]})
        self.assertFalse(plan["ready_to_run"])
        self.assertEqual(inspected, [pinned])
        with self.image_label_runtime({"io.rig.camera.replay-bindings": "1"}) as inspected:
            plan = planner.build(m, c, d, self.root, str(self.source), {"version": 1}, {"names": ["cam"]})
        self.assertTrue(plan["ready_to_run"], plan["issues"])
        self.assertEqual(inspected, [pinned])

    def test_direct_reader_labels_ignore_live_skipped_and_legacy_bindings(self):
        self.reader_label_camera()
        m, c, d = load_context(self.root)
        for opts in ({"live": ["cam"]}, {}):
            with self.image_label_runtime({}) as inspected:
                plan = planner.build(m, c, d, self.root, str(self.source), {"version": 1}, opts)
            self.assertTrue(plan["ready_to_run"], plan["issues"])
            self.assertEqual(inspected, ["example.test/cam-dev:v1-dev"] if opts else [])
            self.assertFalse(any("io.rig.camera.replay-bindings" in item["required_labels"]
                                 for item in plan["image_inventory"]["images"].values()))
        with self.image_label_runtime({}) as inspected:
            result = planner.required_image_labels(d, m.sensors, {"cam": {"direct": False}})
        self.assertEqual(inspected, [])
        self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main()
