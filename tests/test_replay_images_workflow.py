"""Runtime acquisition at the replay command boundary; never starts real services."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import yaml
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_replay_preparation as fixtures
from rig_cli import RigError, dispatch
from rig_cli.replay_workflow import command, load_context


IMAGE_ID = "sha256:" + "7" * 64


class RuntimeImagesWorkflow(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.Preparation()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.local = {}
        self.pulls = []
        self.inspections = []
        self.pull_error = False
        self.labels = {}
        self.original_run = subprocess.run

    def docker(self, argv, **kwargs):
        if argv[:3] == ["docker", "image", "inspect"]:
            ref = argv[-1] if argv[3] == "--format" else argv[3]
            self.inspections.append(ref)
            info = self.local.get(ref)
            return subprocess.CompletedProcess(argv, 0 if info else 1,
                json.dumps(info) if info else "", "" if info else f"Error: No such image: {ref}")
        if argv[:2] == ["docker", "pull"]:
            self.pulls.append(argv)
            if self.pull_error:
                return subprocess.CompletedProcess(argv, 1, "", "registry unavailable")
            self.cache(argv[-1])
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[0] == "docker":
            raise AssertionError(f"Unexpected Docker operation: {argv}")
        return self.original_run(argv, **kwargs)

    def cache(self, ref="fixture/runtime:latest", arch="arm64", labels=None):
        self.local[ref] = {"Id": IMAGE_ID, "Os": "linux", "Architecture": arch,
                           "RepoDigests": [ref] if "@" in ref else [],
                           "Config": {"Labels": {"io.rig.rosbag2.replay": "1", **(self.labels if labels is None else labels)}}}

    def invoke(self, *flags, real_execute=False):
        with patch("subprocess.run", side_effect=self.docker), \
             patch("rig_cli.image_catalog.docker_arch", return_value="arm64") as arch, \
             patch("rig_cli.replay._guard_clean_host"), \
             (contextlib.nullcontext() if real_execute else
              patch("rig_cli.replay_workflow.execute", return_value=0)) as execute, \
             contextlib.redirect_stdout(io.StringIO()) as out, \
             contextlib.redirect_stderr(io.StringIO()) as err:
            try:
                code = command(self.f.args(*flags), self.f.root)
            except RigError as exc:
                print(str(exc), file=sys.stderr)
                code = 1
        return code, execute, out.getvalue(), err.getvalue(), arch

    def tree(self):
        return {str(path.relative_to(self.f.root.parent)):
                None if path.is_dir() else path.read_bytes()
                for path in self.f.root.parent.rglob("*")}

    def test_cold_cache_pulls_once_before_execution_and_pins_disabled_player(self):
        code, execute, _, _, arch = self.invoke(str(self.f.source))
        self.assertEqual(code, 0)
        arch.assert_called_once()
        self.assertEqual(self.pulls, [["docker", "pull", "--platform", "linux/arm64", "fixture/runtime:latest"]])
        plan = execute.call_args.args[4]
        self.assertTrue(plan["ready_to_run"], plan["issues"])
        self.assertEqual(set(plan["image_pins"]), {"router", "new", "player"})
        self.assertTrue(all(p["images"] == {"fixture/runtime:latest": IMAGE_ID}
                            for p in plan["image_pins"].values()))
        self.assertEqual(len(self.inspections), 2)  # shared across three selected instances

    def test_warm_cache_needs_no_pull(self):
        self.cache()
        code, execute, *_ = self.invoke(str(self.f.source))
        self.assertEqual(code, 0)
        execute.assert_called_once()
        self.assertEqual(self.pulls, [])
        self.assertEqual(self.inspections, ["fixture/runtime:latest"])

    def test_call_export_acquires_only_the_player_helper(self):
        code, execute, *_ = self.invoke(str(self.f.source), "--export-calls")
        self.assertEqual(code, 0)
        plan = execute.call_args.args[4]
        self.assertEqual(plan["selected"], ["player"])
        self.assertEqual(set(plan["image_pins"]), {"player"})

    def test_foreign_cache_is_pulled_natively(self):
        self.cache(arch="amd64")
        code, execute, *_ = self.invoke(str(self.f.source))
        self.assertEqual(code, 0)
        execute.assert_called_once()
        self.assertEqual(len(self.pulls), 1)
        self.assertIn("linux/arm64", self.pulls[0])

    def test_never_stops_before_launch_and_always_refreshes(self):
        code, execute, *_ = self.invoke(str(self.f.source), "--pull", "never")
        self.assertEqual(code, 1)
        execute.assert_not_called()
        self.assertEqual(self.pulls, [])
        self.cache()
        code, execute, *_ = self.invoke(str(self.f.source), "--pull", "always")
        self.assertEqual(code, 0)
        execute.assert_called_once()
        self.assertEqual(len(self.pulls), 1)

    def test_check_and_all_dry_run_forms_never_pull_or_write(self):
        before = self.tree()
        for flags in (("check", str(self.f.source), "--json"),
                      ("check", str(self.f.source), "--dry-run", "--json"),
                      ("prepare", str(self.f.source), "--recipe", "offline", "--dry-run", "--json"),
                      ("prepare", str(self.f.source), "--recipe", "offline", "--dry-run", "--json",
                       "--player", str(self.f.root / "services/ros2-bag-player")),
                      (str(self.f.source), "--dry-run")):
            with self.subTest(flags=flags):
                code, execute, out, err, _ = self.invoke(*flags, "--pull", "always")
                if "--json" in flags:
                    report = json.loads(out)
                    self.assertFalse(report["ready_to_run"])
                    self.assertEqual(report["image_inspection"]["proposed_pulls"], ["fixture/runtime:latest"])
                    execute.assert_not_called()
                else:
                    self.assertEqual(code, 0, err)
                self.assertEqual(self.pulls, [])
                self.assertEqual(self.local, {})
                self.assertEqual(self.tree(), before)

    def test_prepare_default_pulls_recipe_images_without_starting_deployment(self):
        self.f.reader_label_camera()
        self.labels = {"io.rig.camera.replay-bindings": "1"}
        self.f.write("replay/analysis.yaml", {"version": 2,
            "session": {"instances": ["router", "cam", "new"], "clock": "source"},
            "replay": {"source": str(self.f.source), "playback": {"services": []}}})
        with patch("rig_cli.runs.new_run") as capture, patch("rig_cli.dispatch.run_verb") as launch:
            code, execute, out, err, _ = self.invoke("prepare", "--recipe", "analysis", "--json")
        self.assertEqual(code, 0, err)
        plan = json.loads(out)
        self.assertTrue(plan["ready_to_run"], plan["issues"])
        self.assertEqual({argv[-1] for argv in self.pulls},
                         {"fixture/runtime:latest", "example.test/cam-dev:v1-dev"})
        self.assertEqual(len(self.pulls), 2)
        self.assertTrue(all(argv[:4] == ["docker", "pull", "--platform", "linux/arm64"]
                            for argv in self.pulls))
        self.assertEqual(set(plan["image_pins"]), {"router", "new", "cam", "player"})
        self.assertTrue((self.f.root / "var/replay/analysis/bindings.yaml").is_file())
        self.assertTrue((Path(plan["preparation"]) / "provenance.yaml").is_file())
        execute.assert_not_called()
        capture.assert_not_called()
        launch.assert_not_called()

    def test_prepare_warm_cache_and_never_preserve_cached_images(self):
        self.cache()
        for flags in ((), ("--pull", "never")):
            with self.subTest(flags=flags):
                code, execute, out, err, _ = self.invoke("prepare", str(self.f.source),
                    "--recipe", "warm", "--json", *flags)
                self.assertEqual(code, 0, err)
                self.assertTrue(json.loads(out)["ready_to_run"])
                execute.assert_not_called()
                self.assertEqual(self.pulls, [])

    def test_prepare_never_missing_refuses_before_saving_recipe(self):
        code, execute, out, err, _ = self.invoke("prepare", str(self.f.source),
            "--recipe", "uncached", "--pull", "never", "--json")
        self.assertEqual(code, 1, err)
        plan = json.loads(out)
        self.assertFalse(plan["ready_to_run"])
        self.assertIn("missing with --pull never", str(plan["issues"]))
        self.assertEqual(self.pulls, [])
        self.assertFalse((self.f.root / "replay/uncached.yaml").exists())
        self.assertFalse((self.f.root / "var/replay/uncached/bindings.yaml").exists())
        execute.assert_not_called()

    def test_prepare_always_refreshes_once_across_replans(self):
        self.cache()
        code, execute, out, err, _ = self.invoke("prepare", str(self.f.source),
            "--recipe", "refresh", "--pull", "always", "--json")
        self.assertEqual(code, 0, err)
        self.assertTrue(json.loads(out)["ready_to_run"])
        self.assertEqual(self.pulls, [["docker", "pull", "--platform", "linux/arm64", "fixture/runtime:latest"]])
        self.assertGreater(len(self.inspections), 2)  # Preparation really did re-inspect its replanned inventory.
        execute.assert_not_called()

    def test_prepare_failed_pull_does_not_capture_launch_or_publish(self):
        self.pull_error = True
        with patch("rig_cli.runs.new_run") as capture, patch("rig_cli.dispatch.run_verb") as launch:
            code, execute, out, err, _ = self.invoke("prepare", str(self.f.source),
                "--recipe", "failed", "--json")
        self.assertEqual(code, 1, err)
        self.assertIn("could not pull", str(json.loads(out)))
        self.assertEqual(len(self.pulls), 1)
        self.assertFalse((self.f.root / "replay/failed.yaml").exists())
        self.assertFalse((self.f.root / "var/replay/failed/bindings.yaml").exists())
        self.assertFalse(list((self.f.root / "var/replay/failed").glob("*/provenance.yaml")))
        execute.assert_not_called()
        capture.assert_not_called()
        launch.assert_not_called()

    def test_prepare_late_image_change_restores_previous_binding(self):
        from rig_cli import replay_prepare
        self.cache()
        code, _, out, err, _ = self.invoke("prepare", str(self.f.source),
            "--recipe", "stable", "--json")
        self.assertEqual(code, 0, err)
        old_plan = json.loads(out)
        binding = self.f.root / "var/replay/stable/bindings.yaml"
        saved_binding = binding.read_bytes()
        recipe = self.f.root / "replay/stable.yaml"
        updated = yaml.safe_load(recipe.read_text())
        updated["playback"] = {"exclude": ["/result"]}
        recipe.write_text(yaml.safe_dump(updated))
        create = replay_prepare.create
        staged = []

        def change_image_after_generation(*args, **kwargs):
            result = create(*args, **kwargs)
            staged.append(binding.read_bytes())
            self.local["fixture/runtime:latest"]["Id"] = "sha256:" + "8" * 64
            return result

        with patch("rig_cli.replay_prepare.create", side_effect=change_image_after_generation), \
                patch("rig_cli.runs.new_run") as capture, patch("rig_cli.dispatch.run_verb") as launch:
            code, execute, out, err, _ = self.invoke("prepare", str(self.f.source),
                "--recipe", "stable", "--json")
        self.assertEqual(code, 1, err)
        self.assertIn("runtime image changed during preparation", str(json.loads(out)))
        self.assertTrue(staged)
        self.assertNotEqual(staged[0], saved_binding)  # A new generation was selected before failure.
        self.assertEqual(binding.read_bytes(), saved_binding)
        self.assertEqual(yaml.safe_load(recipe.read_text()), updated)
        self.assertTrue((Path(old_plan["preparation"]) / "provenance.yaml").is_file())
        self.assertEqual(self.pulls, [])
        execute.assert_not_called()
        capture.assert_not_called()
        launch.assert_not_called()

    def test_prepare_config_change_during_pull_refuses_stale_plan(self):
        docker = self.docker
        def process(argv, **kwargs):
            result = docker(argv, **kwargs)
            if argv[:2] == ["docker", "pull"]:
                self.f.write("new.yaml", {"name": "new", "service": "app", "gain": 17})
            return result
        self.docker = process
        with patch("rig_cli.runs.new_run") as capture, patch("rig_cli.dispatch.run_verb") as launch:
            code, execute, out, err, _ = self.invoke("prepare", str(self.f.source),
                "--recipe", "changed", "--json")
        self.assertEqual(code, 1, err)
        self.assertIn("Deployment changed since planning", str(json.loads(out)))
        self.assertEqual(len(self.pulls), 1)
        self.assertFalse((self.f.root / "replay/changed.yaml").exists())
        self.assertFalse((self.f.root / "var/replay/changed/bindings.yaml").exists())
        execute.assert_not_called()
        capture.assert_not_called()
        launch.assert_not_called()

    def test_failed_pull_cannot_capture_or_start_run(self):
        self.pull_error = True
        code, execute, _, err, _ = self.invoke(str(self.f.source))
        self.assertEqual(code, 1)
        execute.assert_not_called()
        self.assertIn("pull", err)
        self.assertEqual(len(self.pulls), 1)

    def test_partial_cache_dry_run_can_print_all_launchers_without_pins(self):
        self.f.reader_label_camera()
        self.cache()  # Router/player cached; camera absent.
        code, _, _, err, _ = self.invoke(str(self.f.source), "cam", "--dry-run", real_execute=True)
        self.assertEqual(code, 0, err)
        self.assertIn("cam [camera]", err)
        self.assertIn("player [ros2-bag-player]", err)
        self.assertEqual(self.pulls, [])

    def test_prepare_repair_never_requires_verified_helper_without_pulling(self):
        (self.f.source / "bags/logger/session-1/metadata.yaml").unlink()
        code, execute, out, _, _ = self.invoke("prepare", str(self.f.source),
                                               "--recipe", "repair", "--pull", "never", "--json")
        plan = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual(plan["source"]["path"], str(self.f.source.resolve()))
        self.assertFalse(plan["ready_to_prepare"])
        self.assertIn("repair_image", {i["code"] for i in plan["issues"]})
        self.assertFalse((self.f.source / "bags/logger/session-1/metadata.yaml").exists())
        execute.assert_not_called()
        self.assertEqual(self.pulls, [])

    def test_never_repair_rolls_back_new_player_wiring_and_reports_once(self):
        self.f.rows["autonomy"] = [r for r in self.f.rows["autonomy"] if r["name"] != "player"]
        self.f.vehicle()
        (self.f.source / "bags/logger/session-1/metadata.yaml").unlink()
        before = {name: (self.f.root / name).read_bytes()
                  for name in ("vehicle.yaml", "services.yaml")}
        code, execute, out, _, _ = self.invoke(
            "prepare", str(self.f.source), "--recipe", "repair",
            "--player", str(self.f.root / "services/ros2-bag-player"), "--pull", "never", "--json")
        plan = json.loads(out)  # Exactly one detailed report, not a second generic error document.
        self.assertEqual(code, 1)
        self.assertFalse(plan["ready_to_prepare"])
        self.assertIn("repair_image", {i["code"] for i in plan["issues"]})
        self.assertEqual(before, {name: (self.f.root / name).read_bytes() for name in before})
        self.assertFalse((self.f.root / "config/autonomy/bag_player.yaml").exists())
        self.assertFalse((self.f.root / "replay/repair.yaml").exists())
        self.assertFalse(list((self.f.root / "var/replay/repair").glob("*/provenance.yaml")))
        self.assertFalse((self.f.source / "bags/logger/session-1/metadata.yaml").exists())
        execute.assert_not_called()
        self.assertEqual(self.pulls, [])

    def prepare_cold_repair(self, *, add_player):
        if add_player:
            self.f.rows["autonomy"] = [r for r in self.f.rows["autonomy"] if r["name"] != "player"]
            self.f.vehicle()
        (self.f.source / "bags/logger/session-1/metadata.yaml").unlink()
        seen = []
        docker = self.docker
        def process(argv, **kwargs):
            if "replay-reindex" in argv:
                self.assertEqual(len(self.pulls), 1)  # Pull and image validation must precede repair.
                seen.append(kwargs["env"])
                self.f.bag(Path(argv[-1]), topic="/repaired")
                return subprocess.CompletedProcess(argv, 0, "", "")
            return docker(argv, **kwargs)
        self.docker = process
        flags = ("--player", str(self.f.root / "services/ros2-bag-player")) if add_player else ()
        with patch("rig_cli.runs.new_run") as capture, patch("rig_cli.dispatch.run_verb") as launch:
            code, execute, out, err, _ = self.invoke("prepare", str(self.f.source),
                "--recipe", "cold-repair", "--json", *flags)
        self.assertEqual(code, 0, err)
        plan = json.loads(out)
        self.assertTrue(plan["ready_to_run"], plan["issues"])
        self.assertEqual(self.pulls, [["docker", "pull", "--platform", "linux/arm64", "fixture/runtime:latest"]])
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["RIG_TARGET_ARCH"], "arm64")
        self.assertEqual(json.loads(seen[0]["RIG_RUNTIME_IMAGE_PINS"]),
                         {"version": 1, "images": {"fixture/runtime:latest": IMAGE_ID}})
        self.assertIn(plan["player"], plan["image_pins"])
        self.assertFalse((self.f.source / "bags/logger/session-1/metadata.yaml").exists())
        self.assertTrue((self.f.root / "var/replay/cold-repair/bindings.yaml").is_file())
        if add_player:
            self.assertEqual(plan["player"], "bag_player")
            self.assertTrue((self.f.root / "config/autonomy/bag_player.yaml").is_file())
        execute.assert_not_called()
        capture.assert_not_called()
        launch.assert_not_called()

    def test_prepare_pulls_helper_before_cold_metadata_repair(self):
        self.prepare_cold_repair(add_player=False)

    def test_prepare_added_player_pulls_helper_before_cold_metadata_repair(self):
        self.prepare_cold_repair(add_player=True)

    def test_prepare_pull_failure_rolls_back_new_player(self):
        self.f.rows["autonomy"] = [r for r in self.f.rows["autonomy"] if r["name"] != "player"]
        self.f.vehicle()
        before = {name: (self.f.root / name).read_bytes()
                  for name in ("vehicle.yaml", "services.yaml")}
        self.pull_error = True
        code, execute, out, err, _ = self.invoke("prepare", str(self.f.source),
            "--recipe", "failed-player", "--json",
            "--player", str(self.f.root / "services/ros2-bag-player"))
        self.assertEqual(code, 1, err)
        self.assertIn("could not pull", str(json.loads(out)))
        self.assertEqual(len(self.pulls), 1)
        self.assertEqual(before, {name: (self.f.root / name).read_bytes() for name in before})
        self.assertFalse((self.f.root / "config/autonomy/bag_player.yaml").exists())
        self.assertFalse((self.f.root / "replay/failed-player.yaml").exists())
        self.assertFalse((self.f.root / "var/replay/failed-player/bindings.yaml").exists())
        self.assertFalse(list((self.f.root / "var/replay/failed-player").glob("*/provenance.yaml")))
        execute.assert_not_called()

    def test_prepare_invalid_topic_selection_does_not_pull(self):
        self.f.write("replay/invalid.yaml", {"version": 1, "source": str(self.f.source),
            "playback": {"exclude": ["/not_recorded"]}})
        code, execute, out, err, _ = self.invoke("prepare", "--recipe", "invalid", "--json")
        self.assertEqual(code, 1, err)
        self.assertIn("missing_topics", {item["code"] for item in json.loads(out)["issues"]})
        self.assertEqual(self.pulls, [])
        self.assertFalse((self.f.root / "var/replay/invalid/bindings.yaml").exists())
        execute.assert_not_called()

    def test_reindex_helper_uses_verified_engine_architecture_and_image_id(self):
        from rig_cli import replay_prepare as prepare
        (self.f.source / "bags/logger/session-1/metadata.yaml").unlink()
        self.cache()
        seen = []
        docker = self.docker
        def process(argv, **kwargs):
            if "replay-reindex" in argv:
                seen.append(kwargs["env"])
                self.f.bag(Path(argv[-1]), topic="/repaired")
                return subprocess.CompletedProcess(argv, 0, "", "")
            return docker(argv, **kwargs)
        self.docker = process
        manifest, _, _ = load_context(self.f.root)
        self.assertIsNone(manifest.arch)
        code, execute, out, err, _ = self.invoke(
            "prepare", str(self.f.source), "--recipe", "native-repair", "--json")
        self.assertEqual(code, 0, err)
        self.assertTrue(json.loads(out)["ready_to_run"])
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["RIG_TARGET_ARCH"], "arm64")
        self.assertEqual(json.loads(seen[0]["RIG_RUNTIME_IMAGE_PINS"]),
                         {"version": 1, "images": {"fixture/runtime:latest": IMAGE_ID}})
        self.assertNotIn("RIG_RUNTIME_IMAGE_MAP", seen[0])
        self.assertFalse((self.f.source / "bags/logger/session-1/metadata.yaml").exists())
        self.assertIsNotNone(prepare.prepared(self.f.root, self.f.root / "replay/native-repair.yaml",
            {"version": 1, "source": "source"}, self.f.source))
        execute.assert_not_called()
        self.assertEqual(self.pulls, [])

    def test_old_launcher_reports_upgrade_without_guessing_build_outputs(self):
        path = self.f.root / "services/ros2-bag-player/rigging.yaml"
        doc = yaml.safe_load(path.read_text())
        doc["verbs"].pop("runtime-images")
        path.write_text(yaml.safe_dump(doc))
        code, execute, _, err, _ = self.invoke(str(self.f.source))
        self.assertEqual(code, 1)
        execute.assert_not_called()
        self.assertIn("runtime-images", err)
        self.assertEqual(self.pulls, [])

    def test_labels_fail_cached_image_but_explicit_refresh_can_upgrade(self):
        self.f.reader_label_camera()
        self.cache()
        self.cache("example.test/cam-dev:v1-dev", labels={})
        self.labels = {"io.rig.camera.replay-bindings": "1"}
        code, execute, *_ = self.invoke(str(self.f.source), "cam")
        self.assertEqual(code, 1)
        execute.assert_not_called()
        self.assertEqual(self.pulls, [])
        code, execute, *_ = self.invoke(str(self.f.source), "cam", "--pull", "always")
        self.assertEqual(code, 0)
        execute.assert_called_once()
        self.assertEqual(len(self.pulls), 2)

    def test_native_player_without_runtime_capability_cannot_start(self):
        self.cache()
        self.local["fixture/runtime:latest"]["Config"]["Labels"] = {}
        code, execute, _, err, _ = self.invoke(str(self.f.source))
        self.assertEqual(code, 1)
        execute.assert_not_called()
        self.assertIn("io.rig.rosbag2.replay=1", err)
        self.assertEqual(self.pulls, [])

    def test_pin_environment_is_scoped_and_ambient_values_cannot_leak(self):
        manifest, _, descriptors = load_context(self.f.root)
        with patch.dict("os.environ", RIG_RUNTIME_IMAGE_MAP="bad", RIG_RUNTIME_IMAGE_PINS="bad"):
            env = dispatch.fleet_env(manifest, descriptors)
        self.assertNotIn("RIG_RUNTIME_IMAGE_MAP", env)
        self.assertNotIn("RIG_RUNTIME_IMAGE_PINS", env)
        row = next(r for r in manifest.sensors if r.name == "player")
        pin = {"version": 1, "images": {"fixture/runtime:latest": IMAGE_ID}}
        env["RIG_RUNTIME_IMAGE_MAP"] = json.dumps({"player": pin})
        effective = dispatch.instance_env(env, row, descriptors[row.service])
        self.assertEqual(json.loads(effective["RIG_RUNTIME_IMAGE_PINS"]), pin)
        self.assertNotIn("RIG_RUNTIME_IMAGE_MAP", effective)


if __name__ == "__main__":
    unittest.main()
