"""Replay completion must use positive session/container evidence before automatic teardown."""
import contextlib
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from rig_cli import replay
from rig_cli.manifest import project_name


class CompletionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.manifest = SimpleNamespace(vehicle_id=1, data_dir=None)
        self.player = SimpleNamespace(name="player")
        self.project = project_name(self.player.name, self.manifest.vehicle_id)
        self.docs = [self.container("bag-player", "a" * 64)]
        self.calls = []

    def container(self, service, cid, status="exited", code=0):
        return {"Id": cid, "Image": "sha256:" + "d" * 64,
                "Created": "1970-01-01T00:16:40.500Z",
                "Config": {"Labels": {"com.docker.compose.project": self.project,
                                      "com.docker.compose.service": service,
                                      "com.docker.compose.oneoff": "False"}},
                "State": {"Status": status, "ExitCode": code,
                          "Running": status in ("running", "paused", "restarting"),
                          "Paused": status == "paused", "Restarting": status == "restarting",
                          "StartedAt": "1970-01-01T00:16:41Z",
                          "FinishedAt": "1970-01-01T00:16:42Z", "OOMKilled": False, "Error": ""}}

    def docker(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[1] == "ps":
            self.assertIn("label=com.docker.compose.project=" + self.project, argv)
            self.assertIn("--no-trunc", argv)
            out = "\n".join(d["Id"] for d in self.docs)
        else:
            self.assertEqual(argv[1:4], ["inspect", "--type", "container"])
            self.assertEqual(set(argv[4:]), {d["Id"] for d in self.docs})
            out = json.dumps(self.docs)
        return subprocess.CompletedProcess(argv, 0, out, "")

    def observe(self, **kwargs):
        with patch("subprocess.run", side_effect=self.docker):
            return replay._player_completion(self.manifest, self.player, **kwargs)

    def test_positive_terminal_evidence_and_active_states(self):
        self.assertEqual(self.observe(started_after=1000).state, "succeeded")
        for status in ("created", "running", "paused", "restarting"):
            with self.subTest(status=status):
                self.docs[0] = self.container("bag-player", "a" * 64, status)
                self.assertEqual(self.observe().state, "active")
        for status, code in (("exited", 1), ("dead", 0)):
            self.docs[0] = self.container("bag-player", "a" * 64, status, code)
            self.assertEqual(self.observe().state, "failed")
        self.docs[0] = self.container("bag-player", "a" * 64)
        self.docs[0]["State"]["OOMKilled"] = True
        self.assertEqual(self.observe().state, "failed")

    def test_unknown_inventory_inspection_and_inconsistent_state(self):
        for stdout in ("", "{}", "[]", "short-id", "a" * 64 + "\n" + "a" * 64):
            with self.subTest(inventory=stdout), patch("subprocess.run", return_value=
                    subprocess.CompletedProcess([], 0, stdout, "")):
                self.assertEqual(replay._player_completion(self.manifest, self.player).state, "unknown")
        for payload in ({}, [], [None], [{"Id": "b" * 64}], None):
            with self.subTest(inspection=payload), patch("subprocess.run", side_effect=[
                    subprocess.CompletedProcess([], 0, "a" * 64, ""),
                    subprocess.CompletedProcess([], 0, json.dumps(payload), "")]):
                self.assertEqual(replay._player_completion(self.manifest, self.player).state, "unknown")
        for change in ({"Status": "unknown"}, {"ExitCode": True}, {"Running": True},
                       {"Restarting": None}, {"Status": "removing"}):
            self.docs[0] = self.container("bag-player", "a" * 64)
            self.docs[0]["State"].update(change)
            self.assertEqual(self.observe().state, "unknown")
        for result in (OSError("missing docker"), subprocess.TimeoutExpired("docker", 15),
                       subprocess.CompletedProcess([], 1, "", "daemon unavailable")):
            with patch("subprocess.run", side_effect=result if isinstance(result, Exception) else None,
                       return_value=result):
                self.assertEqual(replay._player_completion(self.manifest, self.player).state, "unknown")

    def test_identity_missing_stale_replaced_duplicate_and_oneoff(self):
        self.assertEqual(self.observe(started_after=1002).state, "unknown")
        self.assertEqual(self.observe(expected={"bag-player": "b" * 64}).state, "unknown")
        self.docs.append(self.container("bag-player", "b" * 64))
        self.assertEqual(self.observe().state, "unknown")
        self.docs[-1]["Config"]["Labels"]["com.docker.compose.oneoff"] = "True"
        self.assertEqual(self.observe().state, "succeeded")
        self.docs = [self.container("other-service", "a" * 64)]
        self.assertEqual(self.observe().state, "unknown")
        self.docs[0]["Config"]["Labels"]["com.docker.compose.project"] = "unrelated"
        self.assertEqual(self.observe().state, "unknown")

    def test_required_injector_must_complete_successfully(self):
        self.assertEqual(self.observe(required_calls=True).state, "unknown")
        self.docs.append(self.container("call-injector", "c" * 64, "running"))
        self.assertEqual(self.observe(required_calls=True).state, "active")
        self.assertEqual(self.observe(required_calls=False).state, "succeeded")
        self.docs[-1] = self.container("call-injector", "c" * 64, code=1)
        self.assertEqual(self.observe(required_calls=True).state, "failed")
        self.docs[-1] = self.container("call-injector", "c" * 64)
        self.assertEqual(self.observe(required_calls=True).state, "succeeded")

    def open_run(self, name="first"):
        self.manifest.data_dir = str(self.root)
        folder = self.root / "runs" / name
        folder.mkdir(parents=True)
        (folder / "manifest.yaml").write_text("run: " + name + "\n")
        current = self.root / "current"
        current.unlink(missing_ok=True)
        current.symlink_to(pathlib.Path("runs") / name)
        return folder

    def monitor(self, *, run_id=None, observer=None, sleep=None, required_calls=False,
                log_capture=None, cleanup=None, participants=None, owned_containers=None):
        events = []
        def capture(*args, **kwargs):
            events.append("logs")
            if log_capture:
                log_capture()
        def down(*args, **kwargs):
            evidence = self.root / "runs" / run_id / ".rig/replay/completion.json" if run_id else None
            if evidence:
                self.assertEqual(json.loads(evidence.read_text())["state"], "succeeded")
            events.append("down")
            return cleanup() if cleanup else []
        with patch("subprocess.run", side_effect=self.docker), \
                patch.object(replay, "_player_completion", wraps=replay._player_completion) as poll, \
                patch.object(replay.runs_mod, "capture_docker_logs", side_effect=capture), \
                patch.object(replay.dispatch, "run_verb", side_effect=down), \
                patch.object(replay.runs_mod, "end_run", side_effect=lambda *a: events.append("seal")), \
                patch.object(replay.time, "sleep", side_effect=sleep), \
                contextlib.redirect_stderr(io.StringIO()):
            if observer:
                poll.side_effect = observer
            rc = replay.auto_end(self.manifest, {}, self.root, [(self.player, object())], {},
                                 self.player, 0, run_id=run_id, required_calls=required_calls,
                                 started_after=1000, observe_participants=participants,
                                 owned_containers=owned_containers)
            return rc, events, poll.call_count

    def test_failure_unknown_and_interrupt_never_down_or_seal(self):
        for state, code in (("failed", 1), ("unknown", 1)):
            observed = replay.ReplayCompletion(state, "injected " + state)
            rc, events, count = self.monitor(observer=lambda *a, **k: observed)
            self.assertEqual((rc, events), (code, []))
            self.assertEqual(count, 5 if state == "unknown" else 1)
        rc, events, _ = self.monitor(observer=lambda *a, **k: replay.ReplayCompletion("active", "waiting"),
                                     sleep=KeyboardInterrupt())
        self.assertEqual((rc, events), (130, []))

    def test_waits_for_calls_and_persists_evidence_before_cleanup(self):
        folder = self.open_run()
        self.docs.append(self.container("call-injector", "c" * 64, "running"))
        def advance(_seconds):
            self.docs[-1] = self.container("call-injector", "c" * 64)
        rc, events, count = self.monitor(run_id="first", required_calls=True, sleep=advance)
        self.assertEqual((rc, events), (0, ["logs", "down", "seal"]))
        self.assertEqual(count, 4)
        doc = json.loads((folder / ".rig/replay/completion.json").read_text())
        self.assertEqual(doc["run"], "first")
        self.assertEqual(set(doc["containers"]), {"bag-player", "call-injector"})

    def test_replacement_or_run_rotation_during_grace_prevents_teardown(self):
        folder = self.open_run()
        def replace(_seconds):
            self.docs[0] = self.container("bag-player", "b" * 64)
        rc, events, _ = self.monitor(run_id="first", sleep=replace)
        self.assertEqual((rc, events), (1, []))
        self.assertEqual(json.loads((folder / ".rig/replay/completion.json").read_text())["state"], "unknown")
        self.docs[0] = self.container("bag-player", "a" * 64)
        def rotate(_seconds):
            self.open_run("second")
        rc, events, _ = self.monitor(run_id="first", sleep=rotate)
        self.assertEqual((rc, events), (1, []))
        self.assertFalse((self.root / "runs/second/.rig/replay/completion.json").exists())

    def test_failed_player_evidence_persists_without_claiming_finished_bag(self):
        folder = self.open_run()
        self.docs[0] = self.container("bag-player", "a" * 64, code=7)
        rc, events, _ = self.monitor(run_id="first")
        self.assertEqual((rc, events), (1, []))
        doc = json.loads((folder / ".rig/replay/completion.json").read_text())
        self.assertEqual(doc["state"], "failed")
        self.assertEqual(doc["containers"]["bag-player"]["exit_code"], 7)

    def test_restart_that_finishes_during_grace_is_not_this_execution(self):
        def restart(_seconds):
            self.docs[0]["State"]["StartedAt"] = "1970-01-01T00:16:43Z"
        rc, events, _ = self.monitor(sleep=restart)
        self.assertEqual((rc, events), (1, []))

    def test_log_capture_does_not_hide_replacement_rotation_or_interruption(self):
        folder = self.open_run()
        def replace():
            self.docs[0] = self.container("bag-player", "b" * 64)
        rc, events, _ = self.monitor(run_id="first", log_capture=replace)
        self.assertEqual((rc, events), (1, ["logs"]))
        self.docs[0] = self.container("bag-player", "a" * 64)
        rc, events, _ = self.monitor(run_id="first", log_capture=lambda: self.open_run("second"))
        self.assertEqual((rc, events), (1, ["logs"]))
        self.assertFalse((self.root / "runs/second/.rig/replay/completion.json").exists())
        (self.root / "current").unlink()
        (self.root / "current").symlink_to(pathlib.Path("runs/first"))
        def interrupt():
            raise KeyboardInterrupt()
        rc, events, _ = self.monitor(run_id="first", log_capture=interrupt)
        self.assertEqual((rc, events), (130, ["logs"]))
        self.assertEqual(json.loads((folder / ".rig/replay/completion.json").read_text())["state"],
                         "interrupted")

    def test_failed_cleanup_records_failure_without_sealing(self):
        folder = self.open_run()
        rc, events, _ = self.monitor(run_id="first", cleanup=lambda: [
            SimpleNamespace(returncode=1, sensor=self.player)])
        self.assertEqual((rc, events), (1, ["logs", "down"]))
        doc = json.loads((folder / ".rig/replay/completion.json").read_text())
        self.assertEqual(doc["state"], "failed")
        self.assertIn("down failed", doc["reason"])

    def test_coordinated_finalization_failure_never_seals_run(self):
        from rig_cli import RigError
        folder = self.open_run()
        with patch("rig_cli.replay_workflow._finish_owned", side_effect=RigError("forced shutdown")):
            rc, events, _ = self.monitor(run_id="first", owned_containers=["a" * 64],
                                         participants=lambda _: None)
        self.assertEqual((rc, events), (1, ["logs"]))
        doc = json.loads((folder / ".rig/replay/completion.json").read_text())
        self.assertEqual(doc["state"], "failed")
        self.assertIn("forced shutdown", doc["reason"])

    def test_current_run_changed_during_down_is_not_sealed(self):
        folder = self.open_run()
        def rotate():
            self.open_run("second")
            return []
        rc, events, _ = self.monitor(run_id="first", cleanup=rotate)
        self.assertEqual((rc, events), (1, ["logs", "down"]))
        self.assertEqual(json.loads((folder / ".rig/replay/completion.json").read_text())["state"],
                         "unknown")
        self.assertFalse((self.root / "runs/second/.rig/replay/completion.json").exists())

    def test_coordinated_failure_overrides_an_active_bag_immediately(self):
        folder = self.open_run()
        self.docs[0] = self.container("bag-player", "a" * 64, "running")
        def failed(player):
            self.assertEqual(player.state, "active")
            return replay.ReplayCompletion("failed", "camera.camera: decoder failed",
                                           {"camera.camera": {"state": "failed"}})
        rc, events, count = self.monitor(run_id="first", participants=failed)
        self.assertEqual((rc, events, count), (1, [], 1))
        evidence = json.loads((folder / ".rig/replay/completion.json").read_text())
        self.assertEqual(evidence["state"], "failed")
        self.assertIn("camera.camera", evidence["containers"])
        self.assertIn("bag-player", evidence["containers"])

    def test_coordinated_camera_must_complete_even_after_bag_finishes(self):
        observations = iter(["active", "succeeded", "succeeded", "succeeded"])
        rc, events, count = self.monitor(participants=lambda player: replay.ReplayCompletion(
            next(observations), "camera completion"))
        self.assertEqual((rc, events, count), (0, ["down"], 4))

    def test_coordinated_success_cannot_hide_player_failure_or_unknown_evidence(self):
        success = lambda player: replay.ReplayCompletion("succeeded", "camera finished")
        for state in ("failed", "unknown"):
            with self.subTest(state=state):
                rc, events, count = self.monitor(participants=success,
                    observer=lambda *a, **k: replay.ReplayCompletion(state, "player " + state))
                self.assertEqual((rc, events), (1, []))
                self.assertEqual(count, 1 if state == "failed" else 5)

    def test_coordinated_failure_during_grace_or_logs_or_final_check_blocks_sealing(self):
        self.open_run()
        for boundary in ("grace", "logs", "cleanup"):
            with self.subTest(boundary=boundary):
                failed, seen = False, 0
                def fail(*unused):
                    nonlocal failed
                    failed = True
                def participants(player):
                    nonlocal seen
                    seen += 1
                    if failed or (boundary == "cleanup" and seen == 4):
                        return replay.ReplayCompletion("failed", "camera.camera failed")
                rc, events, _ = self.monitor(run_id="first", participants=participants,
                    sleep=fail if boundary == "grace" else None,
                    log_capture=fail if boundary == "logs" else None)
                self.assertEqual((rc, events), (1, [] if boundary == "grace" else ["logs"]))

    def test_coordinated_observer_errors_are_bounded_unknown_not_success(self):
        def unavailable(player):
            raise OSError("status unreadable")
        rc, events, count = self.monitor(participants=unavailable)
        self.assertEqual((rc, events, count), (1, [], 5))


if __name__ == "__main__":
    unittest.main()
