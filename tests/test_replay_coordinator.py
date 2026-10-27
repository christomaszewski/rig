"""Failure injection for the coordinator's common release and ownership boundaries."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rig_cli import RigError, dispatch
from rig_cli.replay_coordinator import Coordinator, atomic_json, project_locks, configure_plan
from rig_cli.replay_workflow import _abort_owned, _check_owned_health, execute, load_context


class Fleet:
    def __init__(self, root, *, slow=0, settling=0):
        self.time = 1000.0
        self.slow = slow
        self.hook = None
        self.history = []
        self.held_at = {}
        self.ready_at = {}
        self.coordinator = Coordinator(root / "control", session_id="a" * 32, plan_id="b" * 64,
            participants=["player.player", "camera.camera", "player.readiness"], requirements={},
            timeout_s=30, ready_timeout_s=10, now=lambda: self.time, sleep=self.sleep,
            log=lambda _: None)
        for name in ("player", "camera"):
            self.coordinator.launched_instance(name)
        self.tick()

    def sleep(self, duration):
        self.time += duration
        self.tick()

    def tick(self):
        c = self.coordinator
        command = json.loads((c.directory / "command.json").read_text())
        op = command["op"]
        self.history.append((self.time, op))
        for participant in c.participants:
            state = {"hold": "held", "arm": "ready", "prepare_release": "release_ack",
                     "commit": "release_ack", "abort": "aborted"}[op]
            if participant == "camera.camera" and self.time < 1000 + self.slow:
                state = "preparing"
            value = dict(c.identity, participant=participant, state=state, updated_at_unix_s=self.time)
            if state != "preparing":
                value["held_at_unix_s"] = self.held_at.setdefault(participant, self.time)
            if op in ("arm", "prepare_release", "commit"):
                value["ready_at_unix_s"] = self.ready_at.setdefault(participant, self.time)
            if c.release_at is not None:
                value["release_at_unix_s"] = c.release_at
            if op == "commit" and self.time >= c.release_at:
                value.update(state="running", actual_release_at_unix_s=self.time)
            if self.hook:
                self.hook(self, participant, value, op)
            atomic_json(c.directory / "statuses" / (participant + ".json"), value)


class CoordinationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_slow_open_and_zero_delay_still_share_one_future_release(self):
        fleet = Fleet(self.root, slow=3)
        result = fleet.coordinator.release(0)
        self.assertGreaterEqual(result["ready_at_unix_s"], 1003)
        self.assertGreaterEqual(result["start_at_unix_s"], result["ready_at_unix_s"] + 2)
        actual = [v["actual_release_at_unix_s"] for v in result["participants"].values()]
        self.assertEqual(len(set(actual)), 1)
        self.assertGreaterEqual(actual[0], result["start_at_unix_s"])
        self.assertEqual(set(op for _, op in fleet.history), {"hold", "arm", "prepare_release", "commit"})

    def test_settling_occurs_after_ready(self):
        fleet = Fleet(self.root, slow=2)
        result = fleet.coordinator.release(3)
        self.assertGreaterEqual(result["start_at_unix_s"], 1007)

    def test_immediate_ack_during_atomic_command_write_is_valid(self):
        fleet = Fleet(self.root)
        def write(path, value):
            atomic_json(path, value)
            if value.get("op") == "arm":
                fleet.tick()  # A fast participant observes the replace before the writer returns.
                fleet.time += .01
        with patch("rig_cli.replay_coordinator.atomic_json", side_effect=write):
            result = fleet.coordinator.release()
        self.assertEqual(len(result["participants"]), 3)

    def test_stale_or_foreign_status_cannot_release(self):
        for field, value, message in (("session_id", "c" * 32, "foreign"),
                                      ("plan_id", "d" * 64, "foreign"),
                                      ("updated_at_unix_s", 900, "heartbeat"),
                                      ("participant", "other.player", "malformed")):
            with self.subTest(field=field):
                folder = self.root / field
                fleet = Fleet(folder)
                path = fleet.coordinator.directory / "statuses/player.player.json"
                doc = json.loads(path.read_text())
                doc[field] = value
                atomic_json(path, doc)
                with self.assertRaisesRegex(RigError, message):
                    fleet.coordinator.release()
                self.assertNotIn("commit", [op for _, op in fleet.history])

    def test_lost_readiness_during_settling_never_commits(self):
        fleet = Fleet(self.root)
        def lose(f, p, value, op):
            if p == "camera.camera" and op == "arm" and f.time > 1000.5:
                value["state"] = "preparing"
        fleet.hook = lose
        with self.assertRaisesRegex(RigError, "lost readiness"):
            fleet.coordinator.release(2)

    def test_failed_participant_identifies_phase_and_error(self):
        fleet = Fleet(self.root)
        fleet.hook = lambda f, p, value, op: value.update(state="failed", error="decoder failed") if p == "camera.camera" else None
        fleet.tick()
        with self.assertRaisesRegex(RigError, "camera.camera: failed during hold: decoder failed"):
            fleet.coordinator.release()

    def test_missing_ack_and_mismatched_ack_cannot_commit(self):
        for fault in ("missing", "different"):
            fleet = Fleet(self.root / fault)
            def mutate(f, p, value, op):
                if p == "camera.camera" and op == "prepare_release":
                    if fault == "missing":
                        value["state"] = "ready"
                    else:
                        value["release_at_unix_s"] += 1
            fleet.hook = mutate
            with self.assertRaisesRegex(RigError, "timed out|different release"):
                fleet.coordinator.release()
            self.assertNotIn("commit", [op for _, op in fleet.history])

    def test_participant_timeout_and_no_precommit_data(self):
        fleet = Fleet(self.root, slow=20)
        with self.assertRaisesRegex(RigError, "camera.camera: timed out during hold"):
            fleet.coordinator.release()
        self.assertTrue(all(op == "hold" for _, op in fleet.history))

    def test_already_accepted_statuses_cannot_bypass_deadlines(self):
        fleet = Fleet(self.root)
        fleet.time = 1040
        fleet.tick()
        with self.assertRaisesRegex(RigError, "startup deadline"):
            fleet.coordinator.wait({"held"})

    def test_late_held_evidence_cannot_bypass_participant_timeout(self):
        fleet = Fleet(self.root, slow=20)
        fleet.time = 1020
        fleet.tick()
        with self.assertRaisesRegex(RigError, "camera.camera: timed out during hold"):
            fleet.coordinator.wait({"held"})

    def test_abort_retains_identity_and_session_cannot_be_reused(self):
        fleet = Fleet(self.root)
        fleet.coordinator.abort()
        value = json.loads((fleet.coordinator.directory / "command.json").read_text())
        self.assertEqual(value["op"], "abort")
        self.assertEqual(value["session_id"], "a" * 32)
        with self.assertRaises(FileExistsError):
            Fleet(self.root)

    def test_early_running_evidence_fails(self):
        fleet = Fleet(self.root)
        fleet.hook = lambda f, p, value, op: value.update(state="running") if p == "camera.camera" else None
        fleet.tick()
        with self.assertRaisesRegex(RigError, "before coordinated commit"):
            fleet.coordinator.release()

    def test_locks_exclude_other_deployments_for_same_project(self):
        manifest = SimpleNamespace(vehicle_id=812049)
        with project_locks(manifest, ["coordtest"], {}):
            with self.assertRaisesRegex(RigError, "Another replay"):
                with project_locks(manifest, ["coordtest"], {"DOCKER_CONTEXT": "another-alias"}):
                    pass
        with project_locks(manifest, ["coordtest"], {}):
            pass

    def test_cleanup_uses_only_exact_owned_ids(self):
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            result = _abort_owned({"a" * 64, "b" * 64})
        self.assertEqual(run.call_args.args[0], ["docker", "rm", "--force", "a" * 64, "b" * 64])
        self.assertFalse(result["failed"])

    def test_successful_cleanup_drains_before_removing_exact_ids(self):
        from rig_cli.replay_workflow import _finish_owned
        ident = "a" * 64
        doc = dict(Id=ident, State=dict(Status="exited", Running=False, ExitCode=0))
        results = [subprocess.CompletedProcess([], 0, "", ""),
                   subprocess.CompletedProcess([], 0, json.dumps([doc]), ""),
                   subprocess.CompletedProcess([], 0, "", "")]
        with patch("subprocess.run", side_effect=results) as run:
            _finish_owned([ident])
        self.assertEqual([c.args[0] for c in run.call_args_list], [
            ["docker", "stop", "--time", "30", ident],
            ["docker", "inspect", "--type", "container", ident], ["docker", "rm", ident]])

    def test_forced_shutdown_retains_containers_instead_of_sealing_success(self):
        from rig_cli.replay_workflow import _finish_owned
        ident = "a" * 64
        doc = dict(Id=ident, State=dict(Status="exited", Running=False, ExitCode=137))
        results = [subprocess.CompletedProcess([], 0, "", ""),
                   subprocess.CompletedProcess([], 0, json.dumps([doc]), "")]
        with patch("subprocess.run", side_effect=results) as run:
            with self.assertRaisesRegex(RigError, "shutdown did not finish cleanly"):
                _finish_owned([ident])
        self.assertEqual(run.call_count, 2)

    def test_runtime_restart_or_consumer_exit_revokes_readiness(self):
        doc = dict(Id="a" * 64, Config={"Labels": {"com.docker.compose.service": "consumer"}},
                   State={"Running": True, "Restarting": True})
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, json.dumps([doc]), "")):
            with self.assertRaisesRegex(RigError, "health lost"):
                _check_owned_health({"a" * 64})

    def test_old_launcher_cannot_enter_coordinated_mode(self):
        plan = dict(issues=[], player="p", capabilities={"plan": 1}, playback={}, recordings={})
        configure_plan(plan, None, {}, [SimpleNamespace(name="p")], {"startup": "coordinated"}, None)
        self.assertIn("unsupported", plan["issues"][0]["message"])
        self.assertEqual(plan["startup"]["participants"], [])

    def test_default_does_not_require_coordinated_companions(self):
        from rig_cli.cli import build_parser
        args = build_parser().parse_args(["replay", "/recorded/run"])
        self.assertEqual(args.startup, "legacy")
        plan = dict(issues=[], player="p", capabilities={"plan": 1}, playback={}, recordings={})
        probe = unittest.mock.Mock(side_effect=AssertionError("legacy must not probe coordination"))
        configure_plan(plan, None, {}, [SimpleNamespace(name="p")], {}, probe)
        self.assertEqual(plan["issues"], [])
        self.assertEqual(plan["startup"]["participants"], [])
        probe.assert_not_called()

    def test_bounded_launcher_kills_its_process_group(self):
        row = SimpleNamespace(name="test", service="test")
        desc = SimpleNamespace(repo=self.root)
        child = SimpleNamespace(pid=1234, returncode=-9, communicate=unittest.mock.Mock(
            side_effect=[subprocess.TimeoutExpired("launch", 1), (None, None)]))
        with patch("rig_cli.dispatch.instance_env", return_value={}), patch("subprocess.Popen", return_value=child), \
                patch("os.killpg") as kill:
            with self.assertRaises(subprocess.TimeoutExpired):
                dispatch.run(row, desc, {}, ["launch"], dry_run=False, timeout=1)
        kill.assert_called_once()


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        import test_replay_preparation
        self.fixture = test_replay_preparation.Preparation()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.manifest, _, self.descriptors = load_context(self.fixture.root)

    def test_partial_launch_failure_aborts_session_and_removes_exact_containers(self):
        plan = self.fixture.plan()
        plan["startup"] = dict(mode="coordinated", participants=["player.player"], requirements={},
                               timeout_s=30, ready_timeout_s=10)
        active = {}
        calls = []
        def docker(argv, **kwargs):
            calls.append(argv)
            if argv[1] == "ps":
                name = argv[-1].split("=", 2)[-1].split("-vehicle-")[0]
                return subprocess.CompletedProcess(argv, 0, active.get(name, ""), "")
            self.assertEqual(argv[1:3], ["rm", "--force"])
            return subprocess.CompletedProcess(argv, 0, "", "")
        def launch(row, desc, env, cmd, **kwargs):
            self.assertNotIn("RIG_REPLAY_START_AT_UNIX_S", env)
            self.assertEqual(env["RIG_REPLAY_PLAN_ID"], plan["fingerprint"])
            self.assertEqual(len(env["RIG_REPLAY_SESSION_ID"]), 32)
            active[row.name] = ("a" if row.name == "router" else "b") * 64
            return subprocess.CompletedProcess(cmd, 0 if row.name == "router" else 1)
        with patch("rig_cli.replay._guard_clean_host"), patch("subprocess.run", side_effect=docker), \
                patch("rig_cli.dispatch.run", side_effect=launch):
            result = execute(self.fixture.args(str(self.fixture.source), "--startup", "coordinated"),
                             self.fixture.root, self.manifest, self.descriptors, plan, self.fixture.source)
        self.assertEqual(result, 1)
        commands = list((self.fixture.root / "var/replay/sessions").glob("*/control/command.json"))
        self.assertEqual(len(commands), 1)
        self.assertEqual(json.loads(commands[0].read_text())["op"], "abort")
        self.assertIn(["docker", "rm", "--force", "a" * 64, "b" * 64], calls)
        self.assertFalse(any("down" in call for call in calls))

    def test_force_cannot_adopt_a_preexisting_coordinated_project(self):
        plan = self.fixture.plan()
        plan["startup"] = {"mode": "coordinated"}
        with patch("rig_cli.replay_workflow._project_container_ids", return_value=["a" * 64]), \
                patch("rig_cli.dispatch.run_verb") as launch:
            with self.assertRaisesRegex(RigError, "empty project"):
                execute(self.fixture.args(str(self.fixture.source), "--force", "--startup", "coordinated"),
                        self.fixture.root, self.manifest, self.descriptors, plan, self.fixture.source)
        launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
