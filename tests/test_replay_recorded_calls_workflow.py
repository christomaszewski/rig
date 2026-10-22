"""Recorded requests flow through real planning/cache orchestration, without Docker/ROS."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_replay_preparation as fixtures
from rig_cli import RigError, replay_workflow
from rig_cli import replay_call_inspection


class RecordedCallsWorkflow(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.Preparation()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.namespace_graph()
        self.f.declare_app_namespace("/robot/controller")
        path = self.f.root / "services/ros2-bag-player/rigging.yaml"
        descriptor = yaml.safe_load(path.read_text())
        descriptor["verbs"]["replay-inspect-calls"] = "replay-inspect-calls"
        path.write_text(yaml.safe_dump(descriptor))
        launcher = path.parent / "up"
        launcher.write_text(launcher.read_text().replace('"reindex":true', '"reindex":true,"inspect_calls":1'))
        self.helper_calls = []
        self.order = []
        self.statuses = {}
        self.failure = None
        self.image_id = "sha256:" + "1" * 64
        self.cold = False
        self.pulls = []

    def recipe(self, playback=None, name="calls"):
        doc = {"version": 1, "source": str(self.f.source)}
        if playback is not None:
            doc["playback"] = playback
        return self.f.write("replay/" + name + ".yaml", doc)

    def helper(self, argv, kwargs):
        request = json.loads(Path(argv[-1]).read_text())
        self.assertEqual(Path(request["session"]).resolve(), (self.f.source / "bags/logger/session-1").resolve())
        self.assertEqual(request["start_ns"], 100_000_000_000)
        self.assertEqual(kwargs["timeout"], replay_call_inspection.HELPER_TIMEOUT)
        pins = json.loads(kwargs["env"]["RIG_RUNTIME_IMAGE_PINS"])
        self.assertEqual(set(pins["images"].values()), {self.image_id})
        self.assertEqual(kwargs["env"]["RIG_TARGET_ARCH"], "arm64")
        self.assertEqual(yaml.safe_load(Path(argv[1]).read_text())["name"], "player")
        self.helper_calls.append(request)
        self.order.append("inspect")
        if isinstance(self.failure, BaseException):
            raise self.failure
        if self.failure is not None:
            return subprocess.CompletedProcess(argv, 0, self.failure, "")
        entries = {}
        for name in request["services"]:
            status = self.statuses.get(name, "usable")
            entries[name] = dict(status=status, request_count=2 if status == "usable" else 0,
                                 duplicate_count=1 if status == "usable" else 0,
                                 other_origin_request_count=2 if status == "other_origin" else 0,
                                 type="std_srvs/srv/Trigger_Event", reason="Fixture " + status)
        return subprocess.CompletedProcess(argv, 0, json.dumps({
            "version": 1, "source_mode": request["source_mode"], "services": entries}), "")

    @contextlib.contextmanager
    def runtime(self):
        with self.f.image_label_runtime({}):
            native_process = subprocess.run
            def process(argv, **kwargs):
                if "replay-inspect-calls" in argv:
                    return self.helper(argv, kwargs)
                if argv[:2] == ["docker", "pull"]:
                    self.cold = False
                    self.pulls.append(argv)
                    self.order.append("pull")
                    return subprocess.CompletedProcess(argv, 0, "", "")
                if argv[:3] == ["docker", "image", "inspect"]:
                    if self.cold:
                        return subprocess.CompletedProcess(argv, 1, "", "Error: No such image")
                    result = native_process(argv, **kwargs)
                    value = json.loads(result.stdout)
                    value["Id"] = self.image_id
                    return subprocess.CompletedProcess(argv, 0, json.dumps(value), "")
                if argv[0] == "docker":
                    raise AssertionError(f"Unexpected Docker command: {argv}")
                return native_process(argv, **kwargs)
            with patch("subprocess.run", side_effect=process):
                yield

    def invoke(self, *flags, real_execute=False):
        with self.runtime(), \
                (contextlib.nullcontext() if real_execute else
                 patch("rig_cli.replay_workflow.execute", return_value=0)) as execute, \
                contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            args = self.f.args(*flags)
            args.json = True  # The run parser does not expose --json; exercise the structured reporter directly.
            code = replay_workflow.command(args, self.f.root)
        return code, json.loads(out.getvalue()), execute, err.getvalue()

    def test_auto_covers_whole_selected_server_set_for_broad_and_recorded_selection(self):
        for flags in ((), ("new",), ("--selection", "recorded"), ("new", "--selection", "recorded")):
            with self.subTest(flags=flags):
                code, plan, execute, _ = self.invoke(str(self.f.source), *flags)
                self.assertEqual(code, 0, plan["issues"])
                self.assertEqual(plan["service_selection"]["mode"], "auto")
                self.assertEqual(plan["service_selection"]["candidates"], ["/control"])
                self.assertEqual(plan["playback"]["services"], ["/control"])
                self.assertEqual(plan["playback"]["services_source"], "service")
                self.assertEqual(plan["call_inspection"]["result"]["services"]["/control"]["request_count"], 2)
                execute.assert_called_once()
        self.assertEqual(len(self.helper_calls), 1)

    def test_check_and_dry_run_cold_evidence_never_start_helper_or_write_cache(self):
        for flags in (("check", str(self.f.source)), (str(self.f.source), "--dry-run")):
            with self.subTest(flags=flags):
                code, plan, _, _ = self.invoke(*flags)
                self.assertFalse(plan["ready_to_run"])
                self.assertTrue(any(i["code"] == "recorded_calls" and i["status"] == "not_checked"
                                    for i in plan["issues"]), plan["issues"])
                self.assertEqual(self.helper_calls, [])
                self.assertFalse((self.f.root / "var/replay/call-inspections").exists())
                self.assertEqual(self.pulls, [])
                if "check" in flags:
                    self.assertEqual(code, 1)

    def test_prepare_creates_evidence_reused_by_read_only_check_and_run(self):
        recipe = self.recipe()
        before = recipe.read_bytes()
        code, plan, execute, _ = self.invoke("prepare", "--recipe", str(recipe))
        self.assertEqual(code, 0, plan["issues"])
        self.assertTrue(plan["ready_to_run"])
        self.assertEqual(plan["playback"]["services"], ["/control"])
        execute.assert_not_called()
        self.assertEqual(len(self.helper_calls), 1)
        self.assertEqual(recipe.read_bytes(), before)
        for flags in (("check", "--recipe", str(recipe)), ("--recipe", str(recipe))):
            code, plan, _, _ = self.invoke(*flags)
            self.assertEqual(code, 0, plan["issues"])
            self.assertEqual(plan["playback"]["services"], ["/control"])
            self.assertEqual(len(self.helper_calls), 1)
        self.assertEqual(self.pulls, [])

    def test_cold_image_acquisition_precedes_pending_payload_inspection(self):
        self.cold = True
        code, plan, execute, _ = self.invoke(str(self.f.source))
        self.assertEqual(code, 0, plan["issues"])
        self.assertEqual(self.order, ["pull", "inspect"])
        self.assertEqual(len(self.pulls), 1)
        self.assertTrue(plan["ready_to_run"])
        execute.assert_called_once()

    def test_unusable_auto_calls_are_omitted_with_visible_warning(self):
        self.statuses["/control"] = "metadata_only"
        code, plan, execute, _ = self.invoke(str(self.f.source))
        self.assertEqual(code, 0, plan["issues"])
        self.assertEqual(plan["playback"]["services"], [])
        self.assertEqual(plan["service_selection"]["mode"], "auto")
        warnings = [i for i in plan["issues"] if i["code"] == "recorded_calls" and i["status"] == "warning"]
        self.assertTrue(warnings, plan["issues"])
        self.assertIn("/control", " ".join(i["message"] for i in warnings))
        execute.assert_called_once()

    def test_unusable_manual_target_is_blocking(self):
        self.statuses["/control"] = "response_only"
        recipe = self.recipe({"services": ["/control"]})
        code, plan, execute, _ = self.invoke("--recipe", str(recipe))
        self.assertEqual(code, 1)
        self.assertEqual(plan["service_selection"]["mode"], "manual")
        self.assertTrue(any(i["code"] == "recorded_calls" and i["status"] == "error"
                            for i in plan["issues"]), plan["issues"])
        execute.assert_not_called()

    def test_empty_override_and_call_script_bypass_recorded_payload_inspection(self):
        recipe = self.recipe({"services": []})
        code, plan, _, _ = self.invoke("--recipe", str(recipe))
        self.assertEqual(code, 0, plan["issues"])
        self.assertEqual(plan["service_selection"]["mode"], "off")
        self.assertEqual(plan["playback"]["services"], [])
        calls = self.f.write("calls.yaml", {"schema": 1, "calls": [
            {"t": 1.0, "service": "/control", "type": "std_srvs/srv/Trigger", "request": {}}]})
        code, plan, _, _ = self.invoke(str(self.f.source), "--calls", str(calls))
        self.assertEqual(code, 0, plan["issues"])
        self.assertEqual(plan["service_selection"]["mode"], "script")
        self.assertEqual(plan["playback"]["services"], [])
        self.assertEqual(self.helper_calls, [])

    def test_skipped_server_has_no_automatic_call_candidate(self):
        code, plan, _, _ = self.invoke(str(self.f.source), "--skip-instance", "new")
        self.assertEqual(code, 0, plan["issues"])
        self.assertNotIn("new", plan["selected"])
        self.assertEqual(plan["playback"]["services"], [])
        self.assertEqual(self.helper_calls, [])

    def test_source_origin_window_and_native_image_changes_invalidate_evidence(self):
        code, plan, _, _ = self.invoke(str(self.f.source))
        self.assertEqual(code, 0, plan["issues"])
        key = plan["call_inspection"]["cache_key"]
        # Pin identity is part of evidence even if the same mutable image ref remains selected.
        self.image_id = "sha256:" + "2" * 64
        code, plan, _, _ = self.invoke("check", str(self.f.source))
        self.assertEqual(code, 1)
        self.assertFalse(plan["ready_to_run"])
        code, plan, _, _ = self.invoke(str(self.f.source))
        self.assertEqual(code, 0, plan["issues"])
        self.assertNotEqual(plan["call_inspection"]["cache_key"], key)
        self.assertEqual(len(self.helper_calls), 2)
        self.f.write("player.yaml", {"name": "player", "service": "ros2-bag-player",
                                     "play": {"services_source": "client"}})
        code, plan, _, _ = self.invoke(str(self.f.source), "--from", "2", "--to", "5")
        self.assertEqual(code, 0, plan["issues"])
        self.assertEqual(plan["playback"]["services_source"], "client")
        self.assertEqual(self.helper_calls[-1]["source_mode"], "client")
        self.assertEqual((self.helper_calls[-1]["from_s"], self.helper_calls[-1]["to_s"]), (2, 5))
        payload = self.f.source / "bags/logger/session-1/data.mcap"
        old = payload.stat()
        payload.write_bytes(b"changed bag data!")
        self.assertEqual(payload.stat().st_size, old.st_size)
        os.utime(payload, ns=(old.st_atime_ns, old.st_mtime_ns))
        code, plan, _, _ = self.invoke("check", str(self.f.source), "--from", "2", "--to", "5")
        self.assertEqual(code, 1)
        self.assertFalse(plan["ready_to_run"])
        self.assertEqual(len(self.helper_calls), 3)

    def test_helper_failure_cannot_start_services_or_publish_successful_evidence(self):
        self.failure = '{"version":1,"source_mode":"service","services":{}}'
        code, plan, execute, _ = self.invoke(str(self.f.source))
        self.assertEqual(code, 1)
        self.assertFalse(plan["ready_to_run"])
        execute.assert_not_called()
        self.assertFalse(list((self.f.root / "var/replay/call-inspections").glob("*.json")))

    def test_corrupt_cached_evidence_is_blocking_without_running_helper_again(self):
        code, plan, _, _ = self.invoke(str(self.f.source))
        self.assertEqual(code, 0, plan["issues"])
        Path(plan["call_inspection"]["cache_path"]).write_text("corrupt cache")
        for flags in (("check", str(self.f.source)), (str(self.f.source),)):
            code, plan, execute, _ = self.invoke(*flags)
            self.assertEqual(code, 1)
            self.assertFalse(plan["ready_to_run"])
            execute.assert_not_called()
            self.assertEqual(len(self.helper_calls), 1)

    def test_final_execution_revalidates_payload_evidence_before_launcher(self):
        code, plan, execute, _ = self.invoke(str(self.f.source))
        self.assertEqual(code, 0, plan["issues"])
        args, root, manifest, descriptors, _, runtime = execute.call_args.args
        payload = self.f.source / "bags/logger/session-1/data.mcap"
        old = payload.stat()
        payload.write_bytes(b"changed bag data!")
        os.utime(payload, ns=(old.st_atime_ns, old.st_mtime_ns))
        with self.runtime(), patch("rig_cli.dispatch.run_verb") as launch:
            with self.assertRaisesRegex(RigError, "Recorded request evidence changed"):
                replay_workflow.execute(args, root, manifest, descriptors, plan, runtime)
        launch.assert_not_called()

    def test_payload_mutation_during_run_capture_is_rejected_before_launcher(self):
        code, plan, execute, _ = self.invoke(str(self.f.source))
        self.assertEqual(code, 0, plan["issues"])
        args, root, manifest, descriptors, _, runtime = execute.call_args.args
        manifest.data_dir = str(self.f.root / "outputs")
        payload = self.f.source / "bags/logger/session-1/data.mcap"
        def capture(*args, **kwargs):
            old = payload.stat()
            payload.write_bytes(b"changed bag data!")
            os.utime(payload, ns=(old.st_atime_ns, old.st_mtime_ns))
            return "captured-run"
        with self.runtime(), patch("rig_cli.runs.new_run", side_effect=capture), \
                patch("rig_cli.dispatch.run_verb") as launch:
            with self.assertRaisesRegex(RigError, "Recorded request evidence changed"):
                replay_workflow.execute(args, root, manifest, descriptors, plan, runtime)
        launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
