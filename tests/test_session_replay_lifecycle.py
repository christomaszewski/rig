"""Shared replay keeps enough immutable state for safe manual teardown."""
import contextlib
import io
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_session_recipe as fixtures
from rig_cli import RigError, cli, dispatch, replay_workflow, session_runtime


class ReplaySessionLifecycle(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.SessionRecipes()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.root = self.f.f.root
        self.calls = []

    @contextlib.contextmanager
    def host(self, *, fail=None):
        def launch(pairs, env, verb, **kwargs):
            result = []
            for row, desc in pairs:
                if kwargs.get("on_start"):
                    kwargs["on_start"](row, desc)
                self.calls.append(dict(name=row.name, verb=verb, env=dict(env),
                                       config=yaml.safe_load(row.config.read_text())))
                result.append(dispatch.Outcome(row, 23 if (row.name, verb) == fail else 0))
                if result[-1].returncode and kwargs.get("stop_on_failure"):
                    break
            return result
        with patch("rig_cli.replay._guard_clean_host"), \
                patch("rig_cli.replay_workflow._project_container_ids", return_value=[]), \
                patch("rig_cli.runs.running_projects", return_value=[]), \
                patch("rig_cli.runs.capture_docker_logs"), \
                patch("rig_cli.dispatch.run_verb", side_effect=launch), \
                contextlib.redirect_stderr(io.StringIO()):
            yield

    def start(self, *, data=True, auto_end=None, force=False):
        document = fixtures.recipe(instances=["idle"], overrides={"idle": {"gain": 7}})
        manifest, descriptors, plan = self.f.plan(document)
        manifest.data_dir = str(self.root / "data") if data else None
        manifest.run_capture = False
        args = self.f.f.args(str(self.f.f.source), "--start-delay", "0")
        args.auto_end = auto_end
        args.force = force
        result = replay_workflow.execute(args, self.root, manifest, descriptors, plan, self.f.f.source)
        return result, plan

    def test_bare_down_uses_frozen_replay_config_clock_and_exact_selection(self):
        with self.host():
            self.assertEqual(self.start()[0], 0)
            _, state = session_runtime.read_state(self.root)
            self.assertEqual(state["provider"], "replay")
            self.assertEqual(state["status"], "running")
            self.assertEqual(state["attempted"], ["idle", "player"])
            self.f.f.write("idle.yaml", dict(name="idle", service="app", gain=99))
            (self.root / "vehicle.yaml").write_text("vehicle: [broken YAML")
            with patch.dict(os.environ, {"RIG_SIM_TIME": "poison", "RIG_REPLAY_SOURCE": "/changed"}):
                self.assertEqual(cli.main(["--root", str(self.root), "down", "--end-run"]), 0)
        down = [call for call in self.calls if call["verb"] == "down"]
        self.assertEqual([call["name"] for call in down], ["player", "idle"])
        self.assertEqual(down[1]["config"]["gain"], 7)
        self.assertEqual(down[1]["env"]["RIG_SIM_TIME"], "1")
        self.assertEqual(down[1]["env"]["RIG_REPLAY_SOURCE"], str(self.f.f.source))
        self.assertIn("RIG_RUNTIME_IMAGE_MAP", down[1]["env"])
        self.assertFalse(session_runtime.active_session(self.root))
        self.assertIn("ended", yaml.safe_load((Path(state["output_run"]) / "manifest.yaml").read_text()))

    def test_no_data_directory_still_has_frozen_manual_teardown(self):
        with self.host():
            self.assertEqual(self.start(data=False)[0], 0)
            _, state = session_runtime.read_state(self.root)
            self.assertIsNone(state["run_id"])
            self.assertIsNone(state["output_run"])
            self.assertEqual(session_runtime.down(self.root, end_run=True), 0)
        self.assertFalse(session_runtime.active_session(self.root))
        self.assertEqual([call["name"] for call in self.calls if call["verb"] == "down"], ["player", "idle"])

    def test_failed_start_retains_attempted_rows_for_retrying_manual_cleanup(self):
        with self.host(fail=("player", "up")):
            self.assertEqual(self.start()[0], 1)
            _, state = session_runtime.read_state(self.root)
            self.assertEqual(state["status"], "failed")
            self.assertEqual(state["attempted"], ["idle", "player"])
            self.assertEqual(state["cleanup"]["failed"], [])
            self.assertEqual(session_runtime.down(self.root, end_run=True), 0)
        self.assertFalse(session_runtime.active_session(self.root))

    def test_force_cannot_claim_a_preexisting_shared_session_project(self):
        with self.host(), patch("rig_cli.replay_workflow._project_container_ids", return_value=["a" * 64]), \
                patch("rig_cli.runs.new_run") as capture:
            with self.assertRaisesRegex(RigError, "shared session startup requires an empty project"):
                self.start(force=True)
        capture.assert_not_called()
        self.assertEqual(self.calls, [])
        self.assertFalse(session_runtime.active_session(self.root))
        self.assertFalse((self.root / "var/replay/sessions").exists())

    def test_project_appearing_during_capture_is_never_started_or_claimed(self):
        from rig_cli import runs
        original = runs.new_run
        def capture(*args, **kwargs):
            result = original(*args, **kwargs)
            inventory.return_value = ["a" * 64]
            return result
        with self.host(), patch("rig_cli.replay_workflow._project_container_ids", return_value=[]) as inventory, \
                patch("rig_cli.runs.new_run", side_effect=capture):
            self.assertEqual(self.start(force=True)[0], 1)
        self.assertEqual(self.calls, [])
        _, state = session_runtime.read_state(self.root)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["attempted"], [])
        self.assertEqual(state["cleanup"]["stopped"], [])
        self.assertIn("appeared after shared session startup preflight", state["error"])

    def test_auto_end_retires_pointer_only_after_selected_projects_are_gone(self):
        with self.host(), patch("rig_cli.replay.auto_end", return_value=0):
            self.assertEqual(self.start(auto_end=0)[0], 0)
        self.assertFalse(session_runtime.active_session(self.root))

    def test_auto_end_retains_pointer_when_project_remains(self):
        def completed(*args, **kwargs):
            inventory.return_value = ["a" * 64]
            return 0
        with self.host(), patch("rig_cli.replay_workflow._project_container_ids", return_value=[]) as inventory, \
                patch("rig_cli.replay.auto_end", side_effect=completed):
            with self.assertRaisesRegex(RigError, "Auto-end left replay projects"):
                self.start(auto_end=0)
        self.assertTrue(session_runtime.active_session(self.root))
        self.assertEqual(session_runtime.read_state(self.root)[1]["status"], "failed")

    def test_engine_change_during_capture_never_starts_services(self):
        with self.host(), patch("rig_cli.session_runtime.engine_id", side_effect=["original", "changed"]):
            with self.assertRaisesRegex(RigError, "Docker engine changed"):
                self.start()
        self.assertEqual(self.calls, [])
        self.assertFalse(session_runtime.active_session(self.root))

    def test_changed_frozen_player_plan_blocks_teardown_without_dispatch(self):
        with self.host():
            self.assertEqual(self.start()[0], 0)
            _, state = session_runtime.read_state(self.root)
            player_plan = Path(state["environment"]["RIG_REPLAY_PLAN"])
            player_plan.write_text("{}")
            before = list(self.calls)
            with self.assertRaisesRegex(RigError, "Saved config changed"):
                session_runtime.down(self.root)
        self.assertEqual(self.calls, before)
        self.assertTrue(session_runtime.active_session(self.root))

    def test_source_change_during_capture_never_starts_services(self):
        from rig_cli import runs
        original = runs.new_run
        def capture(*args, **kwargs):
            result = original(*args, **kwargs)
            (self.f.f.source / "bags/logger/session-1/data.mcap").write_bytes(b"recording changed")
            return result
        with self.host(), patch("rig_cli.runs.new_run", side_effect=capture):
            with self.assertRaisesRegex(RigError, "Source changed during replay run capture"):
                self.start()
        self.assertEqual(self.calls, [])
        self.assertFalse(session_runtime.active_session(self.root))


if __name__ == "__main__":
    unittest.main()
