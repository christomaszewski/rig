"""Hermetic request-inspection evidence and cache-boundary regressions."""
import copy
import dataclasses
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rig_cli import RigError
from rig_cli.descriptor import load_descriptor
from rig_cli.manifest import Sensor
from rig_cli import replay_call_inspection as inspection


class CallInspection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root / "player"
        self.repo.mkdir()
        self.launcher = self.repo / "up"
        self.launcher.write_text("#!/bin/sh\nexit 0\n")
        self.launcher.chmod(0o755)
        self.helper = self.repo / "inspect.py"
        self.helper.write_text("# helper identity\n")
        (self.repo / "rigging.yaml").write_text(yaml.safe_dump({
            "service": "ros2-bag-player", "launcher": "up",
            "verbs": {"replay-inspect-calls": "replay-inspect-calls"},
            "launch_surface": ["inspect.py"]}))
        self.desc = load_descriptor("ros2-bag-player", self.repo)
        self.config = {"name": "player", "service": "ros2-bag-player", "play": {"rate": 2}}
        self.original_config = self.root / "player.yaml"
        self.original_config.write_text(yaml.safe_dump(self.config))
        self.row = Sensor(name="player", service="ros2-bag-player", config=self.original_config,
                          tier="autonomy", order=999, enabled=False)
        self.bag = self.root / "bag"
        self.bag.mkdir()
        self.payload = self.bag / "part.mcap"
        self.payload.write_bytes(b"original payload")
        self.metadata = self.bag / "metadata.yaml"
        self.metadata.write_text(yaml.safe_dump({"rosbag2_bagfile_information": {
            "relative_file_paths": ["part.mcap"], "storage_identifier": "mcap"}}))
        self.request = dict(version=1, session=str(self.bag), start_ns=100_000_000_000,
                            from_s=1, to_s=9, services=["/control", "/unavailable"], source_mode="service")
        self.pins = {"version": 1, "images": {"localhost:5000/player:v1": "sha256:" + "1" * 64}}
        self.env = {"RIG_TARGET_ARCH": "arm64", "VEHICLE_ID": "7",
                    "BAG_PLAYER_IMAGE": "localhost:5000/player:v1"}
        self.response = {"version": 1, "source_mode": "service", "services": {
            "/control": self.entry("usable", count=2),
            "/unavailable": self.entry("metadata_only")}}
        self.calls = []

    @staticmethod
    def entry(status, count=0):
        return dict(status=status, request_count=count, other_origin_request_count=0,
                    duplicate_count=0, type="example/srv/Control_Event", reason="Inspected fixture events")

    def helper_run(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        self.assertEqual(argv[0], str(self.launcher))
        self.assertEqual(argv[2], "replay-inspect-calls")
        self.assertEqual(yaml.safe_load(Path(argv[1]).read_text()), self.config)
        self.assertEqual(json.loads(Path(argv[3]).read_text()), self.request)
        self.assertEqual(kwargs["cwd"], self.repo)
        self.assertEqual(kwargs["timeout"], inspection.HELPER_TIMEOUT)
        self.assertEqual(kwargs["env"]["COMPOSE_PROJECT_NAME"], "player-vehicle-7")
        self.assertNotIn("RIG_RUNTIME_IMAGE_MAP", kwargs["env"])
        self.assertEqual(json.loads(kwargs["env"]["RIG_RUNTIME_IMAGE_PINS"]), self.pins)
        return subprocess.CompletedProcess(argv, 0, json.dumps(self.response), "")

    def invoke(self, *, execute=False, **changes):
        args = dict(env=self.env, pins=self.pins, execute=execute)
        args.update(changes)
        return inspection.inspect_calls(self.root, self.row, self.desc, self.config, self.request, **args)

    def populate(self):
        with patch("subprocess.run", side_effect=self.helper_run):
            return self.invoke(execute=True)

    def test_read_only_miss_does_not_run_or_write(self):
        with patch("subprocess.run") as run:
            report = self.invoke()
        self.assertEqual(report["status"], "not_checked")
        self.assertIsNone(report["result"])
        self.assertFalse((self.root / "var").exists())
        run.assert_not_called()

    def test_helper_uses_resolved_config_pins_and_atomic_reusable_evidence(self):
        original = {p: p.read_bytes() for p in (self.payload, self.metadata, self.original_config)}
        report = self.populate()
        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["result"], self.response)
        self.assertTrue(Path(report["cache_path"]).is_file())
        self.assertFalse(Path(self.calls[0][0][1]).exists())
        with patch("subprocess.run") as run:
            self.assertEqual(self.invoke(), report)
            self.assertEqual(self.invoke(execute=True), report)
        run.assert_not_called()
        self.assertEqual(original, {p: p.read_bytes() for p in original})
        self.assertEqual(len(list(Path(report["cache_path"]).parent.iterdir())), 1)

    def test_all_nonusable_statuses_are_evidence_not_missing_targets(self):
        for status in inspection._STATUSES - {"usable"}:
            with self.subTest(status=status):
                response = copy.deepcopy(self.response)
                response["services"]["/control"] = self.entry(status)
                self.assertEqual(inspection.validate_response(response, self.request), response)

    def test_cache_keys_cover_window_origin_services_config_images_and_helpers(self):
        initial = self.populate()["cache_key"]
        mutations = [lambda: self.request.update(from_s=2),
                     lambda: self.request.update(start_ns=101_000_000_000),
                     lambda: self.request.update(source_mode="client"),
                     lambda: self.request.update(services=["/control"]),
                     lambda: self.config["play"].update(rate=3),
                     lambda: self.pins["images"].update({"localhost:5000/player:v1": "sha256:" + "2" * 64}),
                     lambda: self.env.update(BAG_PLAYER_IMAGE="localhost:5000/other:v1"),
                     lambda: self.helper.write_text("# changed helper\n")]
        for mutate in mutations:
            with self.subTest(mutation=mutate), patch("subprocess.run") as run:
                mutate()
                report = self.invoke()
                self.assertEqual(report["status"], "not_checked")
                self.assertNotEqual(report["cache_key"], initial)
                initial = report["cache_key"]
                run.assert_not_called()

    def test_same_size_payload_change_with_restored_mtime_invalidates_cache(self):
        before = self.populate()
        original = self.payload.stat()
        self.payload.write_bytes(b"modified payload")
        os.utime(self.payload, ns=(original.st_atime_ns, original.st_mtime_ns))
        self.assertEqual(self.payload.stat().st_size, original.st_size)
        self.assertEqual(self.payload.stat().st_mtime_ns, original.st_mtime_ns)
        after = self.invoke()
        self.assertEqual(after["status"], "not_checked")
        self.assertNotEqual(before["cache_key"], after["cache_key"])

    def test_payload_replacement_sidecars_and_metadata_change_invalidate_cache(self):
        before = self.populate()["cache_key"]
        for mutate in (lambda: (self.bag / "part.mcap-wal").write_bytes(b"pending"),
                       lambda: self.metadata.write_text(self.metadata.read_text() + "# changed\n"),
                       lambda: self.payload.unlink()):
            mutate()
            if not self.payload.exists():
                self.payload.write_bytes(b"original payload")
            after = self.invoke()
            self.assertEqual(after["status"], "not_checked")
            self.assertNotEqual(before, after["cache_key"])
            before = after["cache_key"]

    def test_source_or_helper_mutation_during_helper_is_never_cached(self):
        for mutate in (lambda: self.payload.write_bytes(b"changed during inspection"),
                       lambda: self.helper.write_text("# changed during inspection\n")):
            with self.subTest(mutation=mutate):
                def process(argv, **kwargs):
                    result = self.helper_run(argv, **kwargs)
                    mutate()
                    return result
                with patch("subprocess.run", side_effect=process):
                    with self.assertRaisesRegex(RigError, "changed during inspection"):
                        self.invoke(execute=True)
                self.assertFalse((self.root / "var").exists())

    def test_expected_helper_surfaces_guard_launch_identity(self):
        surfaces = inspection._helper_identity(self.desc)
        self.helper.write_text("# edit after planning\n")
        with patch("subprocess.run") as run:
            with self.assertRaisesRegex(RigError, "changed since planning"):
                self.invoke(execute=True, expected_surfaces=surfaces)
        run.assert_not_called()

    def test_symlinks_escaped_metadata_and_nonregular_payload_are_rejected(self):
        outside = self.root / "outside.mcap"
        outside.write_bytes(b"outside")
        self.payload.unlink()
        self.payload.symlink_to(outside)
        with self.assertRaisesRegex(RigError, "symlinks"):
            self.invoke()
        self.payload.unlink()
        self.payload.write_bytes(b"regular")
        for value in ("../outside.mcap", str(outside), "part.mcap/../part.mcap", "absent.mcap"):
            with self.subTest(value=value):
                self.metadata.write_text(yaml.safe_dump({"rosbag2_bagfile_information": {
                    "relative_file_paths": [value]}}))
                with self.assertRaisesRegex(RigError, "invalid bag metadata"):
                    self.invoke()
        self.metadata.write_text(yaml.safe_dump({"rosbag2_bagfile_information": {
            "relative_file_paths": ["part.mcap"]}}))
        os.mkfifo(self.bag / "unsafe")
        with self.assertRaisesRegex(RigError, "regular files"):
            self.invoke()

    def test_malformed_cache_fails_closed_for_check_and_execution(self):
        report = self.populate()
        path = Path(report["cache_path"])
        for contents in ("not json", "{}", path.read_text().replace('"request_count": 2', '"request_count": 9')):
            path.write_text(contents)
            for execute in (False, True):
                with self.subTest(contents=contents, execute=execute), patch("subprocess.run") as run:
                    with self.assertRaisesRegex(RigError, "cache is corrupt"):
                        self.invoke(execute=execute)
                    run.assert_not_called()

    def test_helper_failure_timeout_cancellation_and_invalid_response_never_cache(self):
        failures = [subprocess.CompletedProcess([], 1, "", "decode failure"),
                    subprocess.CompletedProcess([], 0, "not json", ""),
                    subprocess.CompletedProcess([], 0, json.dumps({"version": 1}), ""),
                    subprocess.TimeoutExpired("inspect", 120), KeyboardInterrupt()]
        for failure in failures:
            with self.subTest(failure=failure):
                kwargs = {"side_effect": failure} if isinstance(failure, BaseException) else {"return_value": failure}
                with patch("subprocess.run", **kwargs):
                    with self.assertRaises(KeyboardInterrupt if isinstance(failure, KeyboardInterrupt) else RigError):
                        self.invoke(execute=True)
                self.assertFalse((self.root / "var").exists())

    def test_strict_schema_rejects_missing_targets_and_inconsistent_counts(self):
        changes = [lambda r: r.update(version=True),
                   lambda r: r.update(source_mode="client"),
                   lambda r: r["services"].pop("/unavailable"),
                   lambda r: r["services"]["/control"].update(status=[]),
                   lambda r: r["services"]["/control"].update(request_count=True),
                   lambda r: r["services"]["/control"].update(request_count=0),
                   lambda r: r["services"]["/control"].update(type=None),
                   lambda r: r["services"]["/unavailable"].update(request_count=1)]
        for change in changes:
            response = copy.deepcopy(self.response)
            change(response)
            with self.subTest(response=response), self.assertRaises(RigError):
                inspection.validate_response(response, self.request)

    def test_invalid_requests_pins_and_old_helper_fail_without_launch(self):
        changes = [{"version": True}, {"start_ns": True}, {"from_s": float("nan")},
                   {"to_s": 0}, {"session": "relative"}, {"services": ["/control", "/control"]},
                   {"source_mode": "unknown"}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(RigError):
                inspection.validate_request({**self.request, **change})
        with patch("subprocess.run") as run:
            with self.assertRaisesRegex(RigError, "verified player image pins"):
                self.invoke(execute=True, pins={})
            self.desc = dataclasses.replace(self.desc, verbs={})
            with self.assertRaisesRegex(RigError, "upgrade the player"):
                self.invoke(execute=True)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
