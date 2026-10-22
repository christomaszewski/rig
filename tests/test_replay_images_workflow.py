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

    def test_check_prepare_and_dry_run_never_pull(self):
        for flags in (("check", str(self.f.source), "--json"),
                      ("prepare", str(self.f.source), "--recipe", "offline", "--json"),
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

    def test_prepare_repair_requires_verified_helper_without_pulling(self):
        (self.f.source / "bags/logger/session-1/metadata.yaml").unlink()
        code, execute, out, _, _ = self.invoke("prepare", str(self.f.source),
                                               "--recipe", "repair", "--json")
        plan = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual(plan["source"]["path"], str(self.f.source.resolve()))
        self.assertFalse(plan["ready_to_prepare"])
        self.assertIn("repair_image", {i["code"] for i in plan["issues"]})
        self.assertFalse((self.f.source / "bags/logger/session-1/metadata.yaml").exists())
        execute.assert_not_called()
        self.assertEqual(self.pulls, [])

    def test_cold_repair_rolls_back_new_player_wiring_and_reports_once(self):
        self.f.rows["autonomy"] = [r for r in self.f.rows["autonomy"] if r["name"] != "player"]
        self.f.vehicle()
        (self.f.source / "bags/logger/session-1/metadata.yaml").unlink()
        before = {name: (self.f.root / name).read_bytes()
                  for name in ("vehicle.yaml", "services.yaml")}
        code, execute, out, _, _ = self.invoke(
            "prepare", str(self.f.source), "--recipe", "repair",
            "--player", str(self.f.root / "services/ros2-bag-player"), "--json")
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
