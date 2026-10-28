"""Shared recipe intent, legacy compatibility, and replay execution without ROS/Docker."""
import contextlib
import copy
import dataclasses
import io
import json
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_replay_preparation as fixtures
from rig_cli import RigError, resolve, session_recipe as sessions
from rig_cli import replay_recipe, replay_prepare, replay_workflow
from rig_cli.replay_sources import inventory_fingerprint


def recipe(**session):
    return {"version": 2, "session": {"instances": ["router", "idle"], **session}, "replay": {}}


def simulation():
    return {"provider": "isaac", "mode": "attach", "replace": ["cam"],
            "ros": {"distro": "jazzy", "rmw": "rmw_cyclonedds_cpp", "domain_id": 1},
            "scene": "scenes/dockyard.usd", "seed": 1,
            "publishes": [{"topic": "/cam/image", "type": "sensor_msgs/msg/Image", "instance": "cam"}],
            "subscribes": [{"topic": "/cmd_vel", "type": "geometry_msgs/msg/Twist"}]}


class SessionRecipes(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.Preparation()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        locks = patch("rig_cli.replay_coordinator.tempfile", SimpleNamespace(
            gettempdir=lambda: str(self.f.root), mkstemp=tempfile.mkstemp))
        locks.start()
        self.addCleanup(locks.stop)
        engine = patch("rig_cli.session_runtime.engine_id", return_value="fixture-engine")
        engine.start()
        self.addCleanup(engine.stop)
        inventory = patch("rig_cli.replay_workflow._project_container_ids", return_value=[])
        inventory.start()
        self.addCleanup(inventory.stop)

    def write(self, doc, name="shared"):
        return self.f.write(f"sessions/{name}.yaml", doc)

    def command(self, *args):
        with self.f.image_label_runtime({}), contextlib.redirect_stdout(io.StringIO()) as output, \
                contextlib.redirect_stderr(io.StringIO()):
            rc = replay_workflow.command(self.f.args(*args, "--json"), self.f.root)
        return rc, json.loads(output.getvalue())

    def plan(self, doc):
        manifest, catalog, descriptors = replay_workflow.load_context(self.f.root)
        manifest = sessions.apply_session(manifest, doc)
        plan = replay_workflow._build_plan(manifest, catalog, descriptors, self.f.root,
                    str(self.f.source), doc, {"wall_clock": doc["session"].get("clock") == "wall"}, host=False)
        plan["image_pins"] = {name: {"version": 1, "images": {
            "fixture/runtime:latest": "sha256:" + "1" * 64}} for name in plan["selected"]}
        return manifest, descriptors, plan

    def test_provider_sections_are_explicit_and_legacy_recipe_still_loads(self):
        v1 = {"version": 1, "source": str(self.f.source), "playback": {"topics": "all"}}
        path = self.f.write("replay/old.yaml", v1)
        self.assertEqual(sessions.load(self.f.root, "old", "replay"), (path.resolve(), v1))
        self.assertEqual(replay_recipe.load(self.f.root, "old"), (path.resolve(), v1))
        doc = recipe()
        doc["simulation"] = simulation()
        shared = self.write(doc)
        self.f.write("replay/shared.yaml", v1)
        self.assertEqual(replay_recipe.load(self.f.root, "shared"), (shared.resolve(), doc))
        self.assertEqual(sessions.provider_recipe(doc, "replay"), {"version": 1})
        self.assertEqual(sessions.provider_recipe(doc, "simulation"), simulation())
        del doc["replay"]
        with self.assertRaisesRegex(RigError, "no replay section"):
            sessions.provider_recipe(doc, "replay")
        with self.assertRaises(RigError):
            replay_recipe.validate({"version": 1, "session": {"instances": ["idle"]}})

    def test_selection_and_config_layers_do_not_mutate_the_deployment(self):
        manifest, _, _ = replay_workflow.load_context(self.f.root)
        row = next(r for r in manifest.sensors if r.name == "idle")
        self.f.write("idle.yaml", {"name": "idle", "service": "app",
                                 "nested": {"old": 1, "keep": 2}, "remove": True})
        row = dataclasses.replace(row, overrides={"nested": None, "remove": "overridden"})
        manifest.sensors = [row if r.name == "idle" else r for r in manifest.sensors]
        original = copy.deepcopy(manifest)
        doc = recipe(instances=["idle"], overrides={"idle": {"nested": {"new": 3}, "remove": None}})
        applied = sessions.apply_session(manifest, doc)
        active = next(r for r in applied.sensors if r.name == "idle")
        self.assertEqual(resolve._layered_dict(active, self.f.root),
                         {"name": "idle", "service": "app", "nested": {"new": 3}})
        self.assertEqual([r.name for r in applied.sensors if r.enabled], ["idle"])
        self.assertEqual(manifest, original)
        self.assertFalse(applied.replay_policy)
        self.assertFalse(next(r for r in applied.sensors if r.name == "new").replay_start)

    def test_invalid_session_and_simulator_bindings_fail_before_execution(self):
        cases = []
        for session in ({"instances": []}, {"instances": ["idle", "idle"]},
                        {"instances": ["idle"], "clock": "sim"},
                        {"instances": ["idle"], "overrides": {"new": {}}},
                        {"instances": ["idle"], "overrides": {"idle": {"service": "other"}}}):
            cases.append({"version": 2, "session": session, "replay": {}})
        for changes in ({"provider": "unknown"}, {"mode": "launch"}, {"timeout_s": float("nan")},
                        {"timeout_s": float("inf")}, {"timeout_s": 10 ** 1000},
                        {"timeout_s": True}, {"probe": "python3"}, {"seed": -1},
                        {"ros": {"distro": "jazzy", "rmw": "rmw_cyclonedds_cpp", "domain_id": True}},
                        {"publishes": [{"topic": "/clock", "type": "rosgraph_msgs/msg/Clock"}]},
                        {"publishes": [{"topic": "/a", "type": "sensor_msgs/msg/Image", "instance": "other"}]},
                        {"publishes": [{"topic": "/cmd_vel", "type": "geometry_msgs/msg/Twist"}]}):
            doc = recipe()
            doc["simulation"] = {**simulation(), **changes}
            cases.append(doc)
        for doc in cases:
            with self.subTest(doc=doc), self.assertRaises(RigError):
                sessions.validate(doc)
        manifest, _, _ = replay_workflow.load_context(self.f.root)
        for selected in (["missing"], ["player"]):
            with self.assertRaises(RigError):
                sessions.apply_session(manifest, recipe(instances=selected))
        for malformed in (None, [], "invalid"):
            with self.subTest(document=malformed), self.assertRaises(RigError):
                sessions.provider_recipe(malformed, "replay")
            with self.subTest(document=malformed), self.assertRaises(RigError):
                sessions.apply_session(manifest, malformed)

    def test_shared_check_is_read_only_and_overrides_legacy_start_policy(self):
        doc = recipe(instances=["idle"], overrides={"idle": {"gain": 7}})
        doc["replay"]["source"] = str(self.f.source)
        self.write(doc)
        before = inventory_fingerprint(self.f.root)
        rc, plan = self.command("check", "--recipe", "shared")
        self.assertEqual(rc, 0, plan["issues"])
        self.assertEqual(plan["selected"], ["idle", "player"])
        self.assertEqual(plan["configs"]["idle"]["gain"], 7)
        self.assertEqual(plan["session_recipe"], doc)
        self.assertEqual(plan["recipe"], {"version": 1, **doc["replay"]})
        self.assertEqual(before, inventory_fingerprint(self.f.root))

    def test_prepare_preserves_recipe_and_changed_shared_intent_invalidates_generation(self):
        doc = recipe(overrides={"idle": {"gain": 7}})
        doc["replay"]["source"] = str(self.f.source)
        path = self.write(doc)
        original = path.read_bytes()
        rc, plan = self.command("prepare", "--recipe", "shared")
        self.assertEqual(rc, 0, plan["issues"])
        first_generation = replay_prepare.generation(plan)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(replay_prepare.prepared(self.f.root, path, doc, self.f.source), self.f.source.resolve())
        self.assertEqual(replay_recipe.bound_source(self.f.root, path, doc), str(self.f.source.resolve()))
        changed = copy.deepcopy(doc)
        changed["session"]["overrides"]["idle"]["gain"] = 9
        self.assertIsNone(replay_prepare.prepared(self.f.root, path, changed, self.f.source))
        self.write(changed)
        rc, plan = self.command("prepare", "--recipe", "shared")
        self.assertEqual(rc, 0, plan["issues"])
        self.assertNotEqual(replay_prepare.generation(plan), first_generation)
        self.assertEqual(plan["configs"]["idle"]["gain"], 9)
        self.assertTrue((replay_recipe.workspace(self.f.root, path) / first_generation).exists())

    def test_clock_and_cli_selection_conflicts_are_explicit(self):
        doc = recipe()
        doc["replay"]["source"] = str(self.f.source)
        self.write(doc)
        for args in (("--wall-clock",), ("--skip-instance", "idle"),
                     ("--include-service", "camera"), ("--live", "cam")):
            rc, report = self.command("check", "--recipe", "shared", *args)
            self.assertEqual(rc, 1)
            self.assertEqual(report["issues"][0]["code"], "input")
        doc["session"]["clock"] = "wall"
        self.write(doc)
        rc, plan = self.command("check", "--recipe", "shared")
        self.assertEqual(rc, 0, plan["issues"])
        self.assertFalse(plan["sim_time"])

    def test_recordings_only_source_cannot_claim_a_simulated_clock(self):
        shutil.rmtree(self.f.source / "bags")
        self.f.recording()
        doc = recipe(instances=["cam"])
        _, _, plan = self.plan(doc)
        self.assertFalse(plan["ready_to_prepare"])
        self.assertTrue(any(issue["code"] == "session_clock" for issue in plan["issues"]))
        doc["session"]["clock"] = "wall"
        _, _, plan = self.plan(doc)
        self.assertTrue(plan["ready_to_prepare"], plan["issues"])
        self.assertFalse(plan["sim_time"])
        self.assertIsNone(plan["player"])

    def test_run_capture_keeps_shared_recipe_and_applies_config_and_clock(self):
        doc = recipe(instances=["idle"], overrides={"idle": {"gain": 7}})
        doc["simulation"] = simulation()
        manifest, descriptors, plan = self.plan(doc)
        manifest.data_dir = str(self.f.root / "data")
        def launch(pairs, env, verb, **kwargs):
            self.assertEqual([row.name for row, _ in pairs], ["idle", "player"])
            self.assertEqual(env["RIG_SIM_TIME"], "1")
            self.assertEqual(yaml.safe_load(pairs[0][0].config.read_text())["gain"], 7)
            return []
        with patch("rig_cli.replay._guard_clean_host"), \
                patch("rig_cli.runs.new_run", return_value="session-run") as new_run, \
                patch("rig_cli.replay_workflow._empty_projects", return_value=set(plan["selected"])), \
                patch("rig_cli.runs.snapshot"), patch("rig_cli.dispatch.run_verb", side_effect=launch), \
                contextlib.redirect_stderr(io.StringIO()):
            args = self.f.args(str(self.f.source), "--start-delay", "0")
            self.assertEqual(replay_workflow.execute(args, self.f.root, manifest, descriptors,
                                                    plan, self.f.source), 0)
        self.assertEqual(new_run.call_args.kwargs["replay"]["recipe"], doc)
        captured = self.f.root / "data/runs/session-run/.rig/replay/recipe.yaml"
        self.assertEqual(yaml.safe_load(captured.read_text()), doc)

    def test_native_capture_contains_session_config_and_keeps_player_disabled(self):
        from rig_cli import runs
        for path in (self.f.root / "services").glob("*/rigging.yaml"):
            descriptor = yaml.safe_load(path.read_text())
            descriptor["launch_surface"] = ["up", "rigging.yaml"]
            path.write_text(yaml.safe_dump(descriptor))
        doc = recipe(instances=["idle"], overrides={"idle": {"gain": 7}})
        manifest, descriptors, plan = self.plan(doc)
        manifest.data_dir = str(self.f.root / "data")
        with patch("rig_cli.replay._guard_clean_host"), \
                patch("rig_cli.replay_workflow._empty_projects", return_value=set(plan["selected"])), \
                patch("rig_cli.dispatch.run_verb", return_value=[]), \
                contextlib.redirect_stderr(io.StringIO()):
            args = self.f.args(str(self.f.source), "--start-delay", "0")
            self.assertEqual(replay_workflow.execute(args, self.f.root, manifest, descriptors,
                                                    plan, self.f.source), 0)
        _, output, metadata = runs.current_run(Path(manifest.data_dir))
        snapshot = yaml.safe_load((output / ".rig/config" / metadata["config"] /
                                   "rendered/idle.yaml").read_text())
        with tarfile.open(output / ".rig/artifact.tar.gz") as archive:
            captured = yaml.safe_load(archive.extractfile("capture/config/autonomy/idle.yaml"))
            vehicle = yaml.safe_load(archive.extractfile("capture/vehicle.yaml"))
            # Omitted physical rows remain available for later reconstruction/replay.
            self.assertIsNotNone(archive.extractfile("capture/config/sensors/cam.yaml"))
        self.assertEqual(captured, snapshot)
        self.assertEqual(captured["gain"], 7)
        self.assertFalse(next(row for row in vehicle["autonomy"] if row["name"] == "player")["enabled"])
        self.assertEqual(metadata["ups"][-1]["stacks"], ["idle", "player"])

    def test_active_simulation_blocks_replay_and_call_export_before_mutation(self):
        manifest, descriptors, plan = self.plan(recipe(instances=["idle"]))
        for export in (False, True):
            args = self.f.args(str(self.f.source), "--start-delay", "0")
            args.export_calls = export
            with self.subTest(export=export), \
                    patch("rig_cli.replay_workflow._verify_launch_inputs") as verify, \
                    patch("rig_cli.runs.new_run") as new_run, \
                    patch("rig_cli.session_runtime.active_session", return_value=True), \
                    self.assertRaisesRegex(RigError, "saved session"):
                replay_workflow._execute(args, self.f.root, manifest, descriptors, plan, self.f.source)
            verify.assert_not_called()
            new_run.assert_not_called()

    def test_legacy_replay_and_call_export_hold_project_locks(self):
        manifest, descriptors, plan = self.plan(recipe(instances=["idle"]))
        plan.pop("session_recipe")  # Legacy v1 can reuse projects with its existing --force policy.
        events = []
        @contextlib.contextmanager
        def session_locked(*args):
            events.append("session-locked")
            try:
                yield
            finally:
                events.append("session-unlocked")
        @contextlib.contextmanager
        def locked(*args):
            self.assertEqual(events[-1], "session-locked")
            events.append("locked")
            try:
                yield
            finally:
                events.append("unlocked")
        def execute(*args):
            self.assertEqual(events[-1], "locked")
            events.append("executed")
            return 0
        for export in (False, True):
            args = self.f.args(str(self.f.source), "--start-delay", "0")
            args.export_calls = export
            with self.subTest(export=export), \
                    patch("rig_cli.session_runtime.session_lock", side_effect=session_locked), \
                    patch("rig_cli.replay_coordinator.project_locks", side_effect=locked), \
                    patch("rig_cli.replay_workflow._project_container_ids") as inventory, \
                    patch("rig_cli.replay_workflow._execute", side_effect=execute):
                self.assertEqual(replay_workflow.execute(args, self.f.root, manifest, descriptors,
                                                        plan, self.f.source), 0)
            inventory.assert_not_called()
            self.assertEqual(events[-5:], ["session-locked", "locked", "executed", "unlocked", "session-unlocked"])


if __name__ == "__main__":
    unittest.main()
