"""Recovery and ownership boundaries shared by replay and simulator sessions."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import test_sim_workflow as fixtures
from rig_cli import RigError, cli, dispatch, runs, session_runtime as runtime, sim_workflow as sim


class SavedSession(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.Simulation()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.real_capture_logs = runs.capture_docker_logs
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(self.f.host())
        self.stack.enter_context(patch("rig_cli.sim_workflow._verify_running"))
        self.stack.enter_context(contextlib.redirect_stderr(io.StringIO()))

    def start(self):
        manifest, descriptors, plan = self.f.build()
        self.assertTrue(plan["ready_to_run"], plan["issues"])
        sim.execute(self.f.root, self.f.path, manifest, descriptors, plan)
        return runtime.read_state(self.f.root)

    def test_absent_session_is_idempotent(self):
        self.assertIsNone(runtime.read_state(self.f.root))
        self.assertEqual(runtime.down(self.f.root, end_run=True), 0)
        runtime.clear(self.f.root, "a" * 32)
        self.assertFalse((self.f.root / "var/sessions").exists())

    def test_deployment_lock_prevents_disjoint_session_race(self):
        with runtime.session_lock(self.f.root):
            with self.assertRaisesRegex(RigError, "transition is in progress"):
                with runtime.session_lock(self.f.root):
                    self.fail("second transition acquired ownership")
        with runtime.session_lock(self.f.root):
            pass

    def test_corrupt_pointer_and_state_fail_closed(self):
        path, state = self.start()
        for pointer in ([], {}, {"session": "../other"}, {"session": 3}):
            with self.subTest(pointer=pointer):
                runtime.pointer(self.f.root).write_text(json.dumps(pointer))
                with self.assertRaisesRegex(RigError, "Cannot read saved session"):
                    runtime.read_state(self.f.root)
        runtime.json_write(runtime.pointer(self.f.root), {"session": state["session"]})
        bad_states = [[], {}, {**state, "version": True}, {**state, "provider": "unknown"},
                      {**state, "attempted": ["not-owned"]}, {**state, "rows": []},
                      {**state, "environment": []}, {**state, "docker_environment": {}},
                      {**state, "ros": {}}, {**state, "launch_surfaces": {"planner": []}},
                      {**state, "engine_id": None}]
        for damaged in bad_states:
            with self.subTest(state=damaged):
                runtime.json_write(path, damaged)
                with self.assertRaisesRegex(RigError, "Cannot read saved session"):
                    runtime.down(self.f.root)
        self.assertFalse(any(c["verb"] == "down" for c in self.f.calls))

    def test_provider_mismatch_never_stops_session(self):
        self.start()
        with self.assertRaisesRegex(RigError, "Active session uses isaac"):
            runtime.down(self.f.root, provider="replay")
        self.assertFalse(any(c["verb"] == "down" for c in self.f.calls))

    def test_changed_saved_config_or_launcher_blocks_teardown(self):
        _, state = self.start()
        for filename in (next(iter(state["configs"])), str(self.f.root / "services/planner/up")):
            path = Path(filename)
            original = path.read_bytes()
            path.write_bytes(original + b"\n# changed\n")
            with self.assertRaisesRegex(RigError, "Saved (config|launcher) changed"):
                runtime.down(self.f.root)
            path.write_bytes(original)
        self.assertFalse(any(c["verb"] == "down" for c in self.f.calls))
        self.assertEqual(runtime.down(self.f.root), 0)

    def test_first_teardown_exception_still_cleans_remaining_instances(self):
        self.start()
        calls = []
        def stop(pairs, env, verb, **kwargs):
            row = pairs[0][0]
            calls.append(row.name)
            if row.name == "planner":
                raise OSError("launcher unavailable")
            return [dispatch.Outcome(row, 0)]
        with patch("rig_cli.dispatch.run_verb", side_effect=stop):
            self.assertEqual(runtime.down(self.f.root, end_run=True), 1)
        self.assertEqual(calls, ["planner", "logger"])
        _, state = runtime.read_state(self.f.root)
        self.assertEqual(state["status"], "stop_failed")
        self.assertEqual(state["cleanup_failed"], ["planner"])
        self.assertEqual(runtime.down(self.f.root, end_run=True), 0)

    def test_zero_exit_with_remaining_container_retains_ownership(self):
        self.start()
        with patch("rig_cli.replay_workflow._project_container_ids", return_value=["still-live"]):
            self.assertEqual(runtime.down(self.f.root, end_run=True), 1)
        self.assertTrue(runtime.active_session(self.f.root))
        self.assertEqual(runtime.read_state(self.f.root)[1]["cleanup_failed"], ["logger", "planner"])

    def test_unattempted_selected_project_must_also_be_empty(self):
        path, state = self.start()
        state["attempted"] = ["logger"]
        runtime.save(path, state)
        with patch("rig_cli.replay_workflow._project_container_ids",
                   side_effect=lambda manifest, row, **kw: ["unknown"] if row.name == "planner" else []):
            self.assertEqual(runtime.down(self.f.root), 1)
        self.assertEqual([c["name"] for c in self.f.calls if c["verb"] == "down"], ["logger"])
        self.assertTrue(runtime.active_session(self.f.root))

    def test_unknown_container_inventory_retains_ownership(self):
        self.start()
        with patch("rig_cli.replay_workflow._project_container_ids", side_effect=RigError("daemon lost")):
            self.assertEqual(runtime.down(self.f.root), 1)
        self.assertTrue(runtime.active_session(self.f.root))

    def test_dry_run_preserves_state_and_skips_container_checks(self):
        path, _ = self.start()
        before = path.read_bytes()
        with patch("rig_cli.dispatch.run_verb", return_value=[]) as launch, \
                patch("rig_cli.replay_workflow._project_container_ids") as inventory:
            self.assertEqual(runtime.down(self.f.root, dry_run=True, end_run=True), 0)
        self.assertTrue(all(c.kwargs["dry_run"] for c in launch.call_args_list))
        inventory.assert_not_called()
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(runtime.active_session(self.f.root))

    def test_clear_cannot_remove_other_sessions_pointer(self):
        self.start()
        with self.assertRaisesRegex(RigError, "another session"):
            runtime.clear(self.f.root, "f" * 32)
        self.assertTrue(runtime.active_session(self.f.root))

    def test_saved_environment_does_not_capture_inherited_secrets(self):
        with patch.dict("os.environ", {"UNRELATED_SECRET": "not-for-evidence"}):
            _, state = self.start()
        self.assertNotIn("UNRELATED_SECRET", state["environment"])
        self.assertNotIn("not-for-evidence", json.dumps(state))

    def test_ordinary_lifecycle_cannot_race_session_capture(self):
        with runtime.session_lock(self.f.root):
            for command in (["up"], ["down"], ["new-run", "other"], ["end-run"]):
                self.assertEqual(cli.main(["--root", str(self.f.root), *command]), 1)
        self.assertEqual(self.f.calls, [])
        self.assertFalse((self.f.root / "data/current").exists())

    def test_active_session_run_cannot_be_rotated_or_force_sealed(self):
        _, state = self.start()
        before = (Path(state["output_run"]) / "manifest.yaml").read_bytes()
        for command in (["new-run", "other", "--force"], ["end-run", "--force"]):
            self.assertEqual(cli.main(["--root", str(self.f.root), *command]), 1)
        self.assertEqual((Path(state["output_run"]) / "manifest.yaml").read_bytes(), before)

    def test_logs_captured_before_teardown_using_saved_environment(self):
        self.start()
        before = len(self.f.calls)
        def capture(manifest, *, env):
            self.assertEqual(len(self.f.calls), before)
            self.assertEqual(env["RIG_SIM_TIME"], "1")
            self.assertEqual([r.name for r in manifest.sensors], ["logger", "planner"])
            return 2
        with patch("rig_cli.runs.capture_docker_logs", side_effect=capture) as logs:
            self.assertEqual(runtime.down(self.f.root, end_run=True), 0)
        logs.assert_called_once()

    def test_log_inventory_and_stream_both_use_saved_docker_environment(self):
        _, state = self.start()
        manifest, _, env = runtime.saved_context(state)
        seen = []
        def docker(argv, **kwargs):
            seen.append((argv[1], kwargs.get("env")))
            if argv[1] == "ps":
                return subprocess.CompletedProcess(argv, 0, "fixture-container\n", "")
            kwargs["stdout"].write(b"captured log\n")
            return subprocess.CompletedProcess(argv, 0)
        # The fixture patches the helper, so restore its real implementation for this test.
        with patch("rig_cli.runs.subprocess.run", side_effect=docker):
            self.assertEqual(self.real_capture_logs(manifest, env=env), 2)
        self.assertEqual([verb for verb, _ in seen], ["ps", "logs", "ps", "logs"])
        self.assertTrue(all(saved == env for _, saved in seen))

    def test_changed_engine_after_capture_never_launches(self):
        manifest, descriptors, plan = self.f.build()
        with patch("rig_cli.sim_workflow._engine_id", side_effect=["fixture-engine", "other-engine"]):
            with self.assertRaisesRegex(RigError, "engine changed during run capture"):
                sim.execute(self.f.root, self.f.path, manifest, descriptors, plan)
        self.assertEqual(self.f.calls, [])
        self.assertEqual(runtime.read_state(self.f.root)[1]["attempted"], [])


class EngineIdentity(unittest.TestCase):
    def test_unavailable_or_timed_out_docker_is_actionable(self):
        for error in (FileNotFoundError("docker"), subprocess.TimeoutExpired("docker info", 15)):
            with patch("rig_cli.session_runtime.subprocess.run", side_effect=error):
                with self.assertRaisesRegex(RigError, "Cannot verify Docker"):
                    runtime.engine_id({})

    def test_invalid_or_failed_identity_is_rejected(self):
        # Use the real helper, independently of the simulated lifecycle fixture.
        for output, rc in (("", 0), ("daemon one\ndaemon two", 0), ("daemon", 1)):
            with self.subTest(output=output, rc=rc), patch("rig_cli.session_runtime.subprocess.run",
                    return_value=subprocess.CompletedProcess([], rc, output, "")):
                with self.assertRaisesRegex(RigError, "Cannot verify Docker"):
                    runtime.engine_id({})


if __name__ == "__main__":
    unittest.main()
