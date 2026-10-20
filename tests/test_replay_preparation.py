"""Replay planning/preparation contracts, without ROS or a Docker daemon."""
import contextlib
import dataclasses
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rig_cli import RigError, cli, dispatch
from rig_cli.manifest import load_manifest
from rig_cli.replay_workflow import load_context, command
from rig_cli import replay_plan as planner, replay_prepare as prepare, replay_recipe as recipe
from rig_cli.replay_sources import inspect_source, inventory_fingerprint
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
                    build={"architectures": ["amd64", "arm64"]})
        if source:
            desc["replay"] = {"source": {"data": "recordings/{name}",
                                         "overrides": {"replay": {"path": "{{replay_source}}/recordings/{{name}}",
                                                                  "epoch": "{{replay_epoch_unix_ns}}"}}}}
        if player:
            desc["verbs"] = {"replay-capabilities": "replay-capabilities", "replay-reindex": "replay-reindex"}
        self.write("services/" + name + "/rigging.yaml", desc)
        launcher = self.root / "services" / name / "up"
        launcher.write_text("#!/bin/sh\n"
                            "if [ \"$2\" = replay-capabilities ]; then\n"
                            "echo '{\"plan\":1,\"remap\":true,\"clock_only\":true,\"reindex\":true}'\n"
                            "else exit 42; fi\n")
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
        return planner.build(m, c, d, self.root, str(ref or self.source),
                             doc or {"version": 1}, opts or {}, host=False)

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
            return original_run(argv, **kwargs)
        with patch("rig_cli.image_catalog.docker_arch", side_effect=RigError("offline")), \
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
                __import__("subprocess").CompletedProcess(argv, 0, "linux/amd64\n", "")
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
                inspected.append(argv[3])
                return __import__("subprocess").CompletedProcess(argv, 0, "linux/arm64\n", "")
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
                patch("rig_cli.runs.snapshot") as snapshot, patch("rig_cli.dispatch.run_verb", side_effect=run_verb):
            self.assertEqual(execute(args, self.root, m, d, plan, self.source), 0)
        self.assertEqual([r.name for r, _ in captured["pairs"]], plan["selected"])
        snap = snapshot.call_args.args[0]
        self.assertTrue(next(r for r in snap.sensors if r.name == "new").enabled)
        self.assertTrue((self.root / "data/runs/new_run/.rig/replay/resolved.yaml").is_file())

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


if __name__ == "__main__":
    unittest.main()
