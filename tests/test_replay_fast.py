"""Fast replay reuses media hashes without weakening fresh planning or launch checks."""
from collections import Counter
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_bag_proxies as fixtures
from rig_cli import RigError, cli, dispatch, replay_workflow, runs, service_exports


class FastReplay(unittest.TestCase):
    def setUp(self):
        self.proxy = fixtures.BagProxies()
        self.proxy.setUp()
        self.addCleanup(self.proxy.doCleanups)
        self.f = self.proxy.f
        launcher = self.proxy.repo / "up"
        launcher.write_text(launcher.read_text().replace('"multi_input":1',
            '"multi_input":1,"multi_input_image_labels":'
            '{"bag-player":{"io.rig.rosbag2.multi_input":"1"}}'))
        self.binding = self.proxy.build()
        self.f.vehicle(data_dir=str(self.f.root / "data"), run_capture=False)
        # Exercise real project locks without colliding with other fixtures or test processes.
        vehicle = yaml.safe_load((self.f.root / "vehicle.yaml").read_text())
        vehicle["vehicle_id"] = "fast-" + self.f.root.parent.name
        vehicle["ros"]["domain_id"] = 1
        self.f.write("vehicle.yaml", vehicle)
        self.recipe = self.f.write("replay/analysis.yaml", {
            "version": 1, "source": str(self.f.source), "bags": "all",
            "inputs": {"bags": {"proxy": self.binding["export_id"]}},
            "playback": {"services": []},
        })
        self.cache = self.f.root / "var/replay/verification-cache.json"
        self.output = Path(self.binding["path"]) / "bag/data.mcap"
        self.payloads = {self.output.resolve(),
                         (self.f.source / "bags/logger/session-1/data.mcap").resolve(),
                         (self.f.source / "bags/logger2/session-2/data.mcap").resolve()}
        self.launches = []

    @contextlib.contextmanager
    def host(self):
        def launch(pairs, env, verb, **kwargs):
            outcomes = []
            for row, descriptor in pairs:
                if kwargs.get("on_start"):
                    kwargs["on_start"](row, descriptor)
                self.launches.append({"name": row.name, "verb": verb,
                    "config": yaml.safe_load(row.config.read_text()) if not kwargs.get("dry_run") else None})
                outcomes.append(dispatch.Outcome(row, 0))
            return outcomes

        with self.f.image_label_runtime({"io.rig.rosbag2.multi_input": "1"}), \
                patch("rig_cli.service_exports._verification_cache_scope", return_value="fixture-host:boot-1"), \
                patch("rig_cli.replay_workflow._project_container_ids", return_value=[]), \
                patch("rig_cli.runs.running_projects", return_value=[]), \
                patch("rig_cli.runs.capture_docker_logs"), \
                patch("rig_cli.dispatch.run_verb", side_effect=launch):
            yield

    def command(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        parsed = self.f.args(*args, "--recipe", "analysis", "--start-delay", "0")
        parsed.json = True  # Capture plans from the run verb as well as check/prepare.
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = replay_workflow.command(parsed, self.f.root)
        # A late execution error follows the initial, valid plan as a second JSON report.
        text, reports = stdout.getvalue().strip(), []
        while text:
            report, end = json.JSONDecoder().raw_decode(text)
            reports.append(report)
            text = text[end:].lstrip()
        self.assertTrue(reports, stderr.getvalue())
        return result, reports[-1]

    @contextlib.contextmanager
    def payload_hashes(self):
        counts, original = Counter(), service_exports.file_sha256

        def checksum(path):
            path = Path(path).resolve()
            if path in self.payloads:
                counts[path] += 1
            return original(path)

        with patch("rig_cli.service_exports.file_sha256", side_effect=checksum):
            yield counts

    @contextlib.contextmanager
    def no_payload_reads(self):
        original = Path.open

        def open_file(path, *args, **kwargs):
            if path.resolve() in self.payloads:
                self.fail(f"Fast replay unexpectedly opened unchanged bag payload: {path}")
            return original(path, *args, **kwargs)

        with patch.object(Path, "open", open_file):
            yield

    def seed(self):
        result, plan = self.command("prepare")
        self.assertEqual(result, 0, plan.get("issues"))
        self.assertTrue(self.cache.is_file())
        self.assertEqual(plan["verification"]["mode"], "full")
        return plan

    @staticmethod
    def change_preserving_size_and_mtime(path):
        before = path.stat()
        data = path.read_bytes()
        path.write_bytes(bytes([data[0] ^ 1]) + data[1:])
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))

    def tree(self):
        return {str(path.relative_to(self.f.root)): path.read_bytes()
                for path in self.f.root.rglob("*") if path.is_file()}

    def test_fast_flag_is_explicit_for_all_replay_verbs(self):
        parser = cli.build_parser()
        for command in (["replay"], ["replay", "check"], ["replay", "prepare"],
                        ["replay-check"], ["replay-prepare"]):
            with self.subTest(command=command):
                self.assertFalse(parser.parse_args(cli.translate_argv(command)).fast)
                self.assertTrue(parser.parse_args(cli.translate_argv([*command, "--fast"])).fast)

    def test_prepare_seeds_fast_prepared_replay_through_run_capture(self):
        with self.host(), self.payload_hashes() as hashes:
            self.seed()
        self.assertEqual(hashes, Counter({path: 1 for path in self.payloads}))
        self.assertEqual(self.launches, [])
        with self.host(), self.no_payload_reads(), patch("rig_cli.runs.new_run", wraps=runs.new_run) as capture:
            result, plan = self.command("--fast")
        self.assertEqual(result, 0, plan)
        capture.assert_called_once()
        self.assertTrue(self.launches)
        self.assertIn("preparation", plan)
        self.assertEqual(plan["verification"]["mode"], "cached")
        self.assertGreaterEqual(plan["verification"]["cached_files"], len(self.payloads))
        execution = list((self.f.root / "data/runs").glob("*/.rig/replay/execution.yaml"))
        self.assertEqual(len(execution), 1)
        evidence = yaml.safe_load(execution[0].read_text())["verification"]
        self.assertEqual(evidence["mode"], "cached")
        self.assertEqual(evidence["hashed_bytes"], 0)

    def test_default_replay_rehashes_even_after_a_cache_was_seeded(self):
        with self.host():
            self.seed()
            with self.payload_hashes() as hashes:
                result, plan = self.command()
        self.assertEqual(result, 0, plan)
        self.assertEqual(plan["verification"]["mode"], "full")
        self.assertEqual(plan["verification"]["cached_files"], 0)
        self.assertEqual(hashes, Counter({path: 1 for path in self.payloads}))

    def test_fast_check_and_dry_run_do_not_create_or_update_cache(self):
        with self.host():
            before = self.tree()
            with self.payload_hashes() as hashes:
                result, plan = self.command("check", "--fast")
            self.assertIn(result, (0, 1))  # An unprepared source can require preparation.
            self.assertTrue(plan["ready_to_prepare"], plan)
            self.assertEqual(hashes, Counter({path: 1 for path in self.payloads}))
            self.assertEqual(self.tree(), before)
            self.assertFalse(self.cache.exists())
            self.seed()
            before, cache_stat = self.tree(), self.cache.stat()
            with self.no_payload_reads():
                for args in (("check", "--fast"), ("--fast", "--dry-run"),
                             ("prepare", "--fast", "--dry-run")):
                    result, plan = self.command(*args)
                    self.assertEqual(result, 0, plan)
            self.assertEqual(self.tree(), before)
            self.assertEqual(self.cache.stat().st_mtime_ns, cache_stat.st_mtime_ns)
            self.assertEqual(self.cache.stat().st_ino, cache_stat.st_ino)

    def test_fast_rejects_changed_original_and_proxy_with_restored_mtime(self):
        for path in sorted(self.payloads):
            with self.subTest(path=path):
                original = path.read_bytes()
                try:
                    with self.host():
                        self.seed()
                        self.change_preserving_size_and_mtime(path)
                        with patch("rig_cli.runs.new_run") as capture:
                            result, plan = self.command("--fast")
                    self.assertEqual(result, 1, plan)
                    self.assertIn("changed file", str(plan))
                    capture.assert_not_called()
                    self.assertEqual(self.launches, [])
                finally:
                    path.write_bytes(original)

    def test_fast_rechecks_proxy_after_capture_before_starting_services(self):
        original_capture = runs.new_run

        def capture(*args, **kwargs):
            result = original_capture(*args, **kwargs)
            self.change_preserving_size_and_mtime(self.output)
            return result

        with self.host():
            self.seed()
            with patch("rig_cli.runs.new_run", side_effect=capture) as captured, self.payload_hashes() as hashes:
                result, plan = self.command("--fast")
        self.assertEqual(result, 1, plan)
        self.assertIn("changed file", str(plan))
        captured.assert_called_once()
        self.assertEqual(hashes, Counter({self.output.resolve(): 1}))
        self.assertEqual(self.launches, [])

    def test_fast_uses_current_service_configuration(self):
        with self.host():
            self.seed()
            self.f.write("new.yaml", {"name": "new", "service": "app", "gain": 17})
            with self.no_payload_reads():
                result, plan = self.command("--fast")
        self.assertEqual(result, 0, plan)
        launched = next(row for row in self.launches if row["name"] == "new")
        self.assertEqual(launched["config"]["gain"], 17)

    def test_fast_revalidates_artifact_manifest(self):
        with self.host():
            self.seed()
            path = Path(self.binding["path"]) / "export.yaml"
            manifest = yaml.safe_load(path.read_text())
            manifest["complete"] = False
            path.write_text(yaml.safe_dump(manifest))
            with self.no_payload_reads(), patch("rig_cli.runs.new_run") as capture:
                result, plan = self.command("--fast")
        self.assertEqual(result, 1, plan.get("issues"))
        self.assertIn("incomplete or invalid manifest", str(plan.get("issues")))
        capture.assert_not_called()
        self.assertEqual(self.launches, [])

    def test_fast_revalidates_runtime_image_capabilities(self):
        with self.host():
            self.seed()
            with self.f.image_label_runtime({"io.rig.rosbag2.multi_input": "0"}), \
                    self.no_payload_reads(), patch("rig_cli.runs.new_run") as capture:
                result, plan = self.command("--fast")
        self.assertEqual(result, 1, plan.get("issues"))
        self.assertTrue(any(issue["code"] == "runtime_images" and issue["status"] == "error"
                            for issue in plan["issues"]), plan["issues"])
        capture.assert_not_called()
        self.assertEqual(self.launches, [])

    def test_fast_still_checks_host_and_launch_controls(self):
        with self.host():
            self.seed()
            with patch("rig_cli.replay._guard_clean_host", side_effect=RigError("fixture busy host")), \
                    patch("rig_cli.runs.new_run") as captured:
                result, plan = self.command("--fast")
            self.assertEqual(result, 1, plan)
            self.assertIn("fixture busy host", str(plan))
            captured.assert_not_called()
            self.assertEqual(self.launches, [])

            original_capture = runs.new_run

            def capture(*args, **kwargs):
                result = original_capture(*args, **kwargs)
                self.f.write("new.yaml", {"name": "new", "service": "app", "gain": 99})
                return result

            with patch("rig_cli.runs.new_run", side_effect=capture), self.no_payload_reads():
                result, plan = self.command("--fast")
        self.assertEqual(result, 1, plan)
        self.assertIn("Deployment changed since planning", str(plan))
        self.assertEqual(self.launches, [])


if __name__ == "__main__":
    unittest.main()
