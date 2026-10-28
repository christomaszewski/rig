"""Isaac attachment lifecycle without a GPU, ROS installation, or Docker daemon."""
import contextlib
import copy
import dataclasses
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rig_cli import RigError, cli, dispatch, sim_workflow as sim
from rig_cli.replay_sources import inspect_source


class Simulation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, RIG_HOME=str(self.root / "user"),
                              RIG_VEHICLE_LOCAL=str(self.root / "absent"))
        self.env.start()
        self.addCleanup(self.env.stop)
        # Keep real flock semantics without colliding with parallel fixture deployments.
        locks = patch("rig_cli.replay_coordinator.tempfile", SimpleNamespace(
            gettempdir=lambda: str(self.root), mkstemp=tempfile.mkstemp))
        locks.start()
        self.addCleanup(locks.stop)
        self.rows = {"infra": [{"name": "logger", "service": "ros2-bag-logger", "config": "logger.yaml"}],
                     "autonomy": [{"name": "planner", "service": "planner", "config": "planner.yaml", "enabled": False}],
                     "sensors": [{"name": "camera", "service": "camera", "config": "camera.yaml"}]}
        self.write("vehicle.yaml", dict(vehicle="test", vehicle_id=7, ros={"distro": "jazzy", "rmw": "rmw_fastrtps_cpp"},
                   data_dir=str(self.root / "data"), run_capture=False, **self.rows))
        for tier, rows in self.rows.items():
            for row in rows:
                self.write(row["config"], {"name": row["name"], "service": row["service"], "gain": 1})
                service = row["service"]
                self.write(f"services/{service}/rigging.yaml", dict(service=service, launcher="up",
                    tier={"infra": "infra", "autonomy": "autonomy", "sensors": "sensor"}[tier],
                    replay={"sim_time": True}, verbs={"runtime-images": "runtime-images"},
                    interface={"publishes": [{"topic": "cmd", "type": "geometry_msgs/msg/Twist"}]} if service == "planner" else {}))
                p = self.root / f"services/{service}/up"
                p.write_text('#!/bin/sh\nif [ "$2" = runtime-images ]; then\n'
                             "echo '{\"version\":1,\"images\":[\"fixture:latest\"]}'\n"
                             'elif [ "$2" = ps ]; then echo \'[{"State":"running","Health":"healthy"}]\'\n'
                             'else exit 0; fi\n')
                p.chmod(0o755)
        self.write("services.yaml", {"services": {s: {"path": f"services/{s}"} for s in ("planner", "camera", "ros2-bag-logger")}})
        self.doc = dict(version=2, session={"instances": ["logger", "planner"], "clock": "source",
                                          "overrides": {"planner": {"gain": 3}}},
                        replay={}, simulation=dict(provider="isaac", scene="dockyard.usd", replace=["camera"],
                            ros={"distro": "jazzy", "rmw": "rmw_fastrtps_cpp", "domain_id": 7},
                            publishes=[dict(topic="/camera/image", type="sensor_msgs/msg/Image", instance="camera")],
                            subscribes=[dict(topic="/planner/cmd", type="geometry_msgs/msg/Twist")]))
        self.path = self.write("sessions/dockyard.yaml", self.doc)
        self.calls = []

    def write(self, path, doc):
        p = self.root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(yaml.safe_dump(doc, sort_keys=False))
        return p

    @contextlib.contextmanager
    def host(self, probe=None, fail=None):
        def inspection(inventory, **kwargs):
            return dict(ready=True, pins={n: {"version": 1, "images": {"fixture:latest": "sha256:" + "1" * 64}}
                                         for n in inventory["instances"]}, errors=[], proposed_pulls=[], images={})
        def run_verb(pairs, env, verb, **kwargs):
            results = []
            for row, desc in pairs:
                if kwargs.get("on_start"):
                    kwargs["on_start"](row, desc)
                self.calls.append(dict(name=row.name, verb=verb, config=yaml.safe_load(row.config.read_text()), env=dict(env)))
                results.append(dispatch.Outcome(row, 23 if fail == (row.name, verb) else 0))
                if results[-1].returncode and kwargs.get("stop_on_failure"):
                    break
            return results
        with patch("rig_cli.image_catalog.docker_arch", return_value="amd64"), \
             patch("rig_cli.sim_workflow._engine_id", return_value="fixture-engine"), \
             patch("rig_cli.session_runtime.engine_id", return_value="fixture-engine"), \
             patch("rig_cli.runs.running_projects", return_value=[]), \
             patch("rig_cli.runs.capture_docker_logs", return_value=0), \
             patch("rig_cli.replay_workflow._project_container_ids", return_value=[]), \
             patch("rig_cli.runtime_images.inspect_images", side_effect=inspection), \
             patch("rig_cli.runtime_images.acquire_images", side_effect=lambda inventory, **kw: kw["inspection"]), \
             patch("rig_cli.sim_workflow.probe", side_effect=probe if callable(probe) else None,
                   return_value=probe if isinstance(probe, dict) else {"version": 1, "ready": True, "issues": []}), \
             patch("rig_cli.dispatch.run_verb", side_effect=run_verb):
            yield

    def build(self, doc=None, **kwargs):
        return sim.build(self.root, self.path, doc or self.doc, **kwargs)

    def test_clock_context_is_explicit_and_does_not_mutate_fleet_env(self):
        original = {"RIG_SIM_TIME": "poison", "OTHER": "kept"}
        self.assertNotIn("RIG_SIM_TIME", dispatch.with_clock(original, provider=None))
        self.assertEqual(dispatch.with_clock(original, provider="isaac")["RIG_SIM_TIME"], "1")
        self.assertEqual(original["RIG_SIM_TIME"], "poison")

    def test_check_and_dry_run_select_exact_workload_without_writes(self):
        before = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        with self.host(), contextlib.redirect_stdout(io.StringIO()) as out:
            rc = cli.main(["--root", str(self.root), "sim", "check", "--recipe", "dockyard", "--json"])
        self.assertEqual(rc, 0, out.getvalue())
        plan = json.loads(out.getvalue())
        self.assertEqual(plan["selected"], ["logger", "planner"])
        self.assertEqual(plan["configs"]["planner"]["gain"], 3)
        self.assertEqual(before, {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()})
        self.assertEqual(self.calls, [])
        with self.host():
            self.assertEqual(cli.main(["--root", str(self.root), "sim", "up", "--recipe", "dockyard", "--dry-run"]), 0)
        self.assertFalse(sim.active_session(self.root))
        self.assertEqual(self.calls, [])

    def test_ros_mismatch_physical_start_and_output_conflicts_block(self):
        cases = []
        doc = copy.deepcopy(self.doc)
        doc["simulation"]["ros"]["distro"] = "lyrical"
        cases.append((doc, "ros"))
        doc = copy.deepcopy(self.doc)
        doc["session"]["instances"].append("camera")
        doc["simulation"]["replace"] = []
        doc["simulation"]["publishes"][0].pop("instance")
        cases.append((doc, "selection"))
        doc = copy.deepcopy(self.doc)
        doc["simulation"]["publishes"].append({"topic": "/planner/cmd", "type": "geometry_msgs/msg/Twist"})
        doc["simulation"]["subscribes"] = []
        cases.append((doc, "authority"))
        for doc, code in cases:
            _, _, plan = self.build(doc, host=False)
            self.assertFalse(plan["ready_to_run"])
            self.assertTrue(any(i["code"] == code and i["status"] == "error" for i in plan["issues"]), plan)

    def test_failed_probe_never_starts_or_opens_run(self):
        with self.host(probe={"version": 1, "ready": False, "issues": [{"status": "error", "code": "clock", "message": "paused"}]}):
            self.assertEqual(cli.main(["--root", str(self.root), "sim", "up", "--recipe", "dockyard"]), 1)
        self.assertEqual(self.calls, [])
        self.assertFalse((self.root / "data/current").exists())

    def test_start_records_normal_run_and_down_uses_frozen_configuration(self):
        with self.host():
            manifest, desc, plan = self.build()
            self.assertTrue(plan["ready_to_run"], plan["issues"])
            self.assertEqual(sim.execute(self.root, self.path, manifest, desc, plan), 0)
            state_path, state = sim._read_state(self.root)
            run = Path(state["output_run"])
            doc = yaml.safe_load((run / "manifest.yaml").read_text())
            self.assertEqual(doc["simulation"]["provider"], "isaac")
            self.assertNotIn("replay", doc)
            self.assertEqual(doc["ups"][-1]["stacks"], ["logger", "planner"])
            self.assertEqual(yaml.safe_load((run / ".rig/simulation/recipe.yaml").read_text()), self.doc)
            self.assertEqual(self.calls[-1]["env"]["RIG_SIM_TIME"], "1")
            self.assertIn("RIG_RUNTIME_IMAGE_MAP", self.calls[-1]["env"])
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli.main(["--root", str(self.root), "status", "--format", "json"]), 0)
            self.assertEqual([r["sensor"] for r in json.loads(out.getvalue())["stacks"]], ["logger", "planner"])
            # A subsequent edit must not alter the configuration used by teardown.
            self.write("planner.yaml", {"name": "planner", "service": "planner", "gain": 99})
            self.doc["session"]["overrides"]["planner"]["gain"] = 100
            self.write("sessions/dockyard.yaml", self.doc)
            self.assertEqual(cli.main(["--root", str(self.root), "down", "--end-run"]), 0)
            self.assertEqual([c["name"] for c in self.calls if c["verb"] == "down"], ["planner", "logger"])
            self.assertEqual([c for c in self.calls if c["verb"] == "down"][0]["config"]["gain"], 3)
            self.assertFalse(sim.active_session(self.root))
            self.assertIn("ended", yaml.safe_load((run / "manifest.yaml").read_text()))
            # Ordinary bag inventory accepts a simulator run; no Isaac-specific replay path.
            self.write(str(run / "bags/logger/session/metadata.yaml"), {"rosbag2_bagfile_information": {
                "storage_identifier": "mcap", "relative_file_paths": ["data.mcap"],
                "starting_time": {"nanoseconds_since_epoch": 100}, "duration": {"nanoseconds": 1000},
                "topics_with_message_count": [{"topic_metadata": {"name": "/camera/image", "type": "sensor_msgs/msg/Image"}, "message_count": 2}]}})
            (run / "bags/logger/session/data.mcap").write_bytes(b"fixture")
            self.assertIsNotNone(inspect_source(manifest, str(run), {})["bag"])

    def test_partial_start_rolls_back_attempted_instances_and_retains_evidence(self):
        with self.host(fail=("planner", "up")):
            manifest, desc, plan = self.build()
            with self.assertRaisesRegex(RigError, "Startup failed"):
                sim.execute(self.root, self.path, manifest, desc, plan)
        self.assertEqual([(c["name"], c["verb"]) for c in self.calls],
                         [("logger", "up"), ("planner", "up"), ("planner", "down"), ("logger", "down")])
        _, state = sim._read_state(self.root)
        self.assertEqual(state["status"], "failed")
        self.assertTrue(Path(state["output_run"]).is_dir())

    def test_clock_conflict_after_start_rolls_back(self):
        observations = iter([{"version": 1, "ready": True, "issues": []}] * 3 +
                            [{"version": 1, "ready": False, "issues": [dict(status="error", code="clock", message="two clocks")]}])
        with self.host(probe=lambda *args: next(observations)):
            manifest, desc, plan = self.build()
            with self.assertRaisesRegex(RigError, "two clocks"):
                sim.execute(self.root, self.path, manifest, desc, plan)
        self.assertEqual(len([c for c in self.calls if c["verb"] == "down"]), 2)

    def test_changed_launch_inputs_are_rejected_before_start(self):
        with self.host():
            manifest, desc, plan = self.build()
            self.write("planner.yaml", {"name": "planner", "service": "planner", "gain": 9})
            with self.assertRaisesRegex(RigError, "changed"):
                sim.execute(self.root, self.path, manifest, desc, plan)
        self.assertEqual(self.calls, [])
        self.assertFalse(sim.active_session(self.root))

    def test_json_start_failure_is_a_single_document_and_retains_state(self):
        with self.host(fail=("planner", "up")), contextlib.redirect_stdout(io.StringIO()) as out:
            rc = cli.main(["--root", str(self.root), "sim", "up", "--recipe", "dockyard", "--json"])
        self.assertEqual(rc, 1)
        self.assertFalse(json.loads(out.getvalue())["ready_to_run"])
        self.assertTrue(sim.active_session(self.root))

    def test_capture_time_connection_loss_starts_no_services(self):
        observations = iter([{"version": 1, "ready": True, "issues": []}] * 2 +
                            [{"version": 1, "ready": False, "issues": [dict(status="error", code="clock", message="paused")]}])
        with self.host(probe=lambda *args: next(observations)):
            manifest, desc, plan = self.build()
            with self.assertRaisesRegex(RigError, "during run capture"):
                sim.execute(self.root, self.path, manifest, desc, plan)
        self.assertEqual(self.calls, [])
        self.assertEqual(sim._read_state(self.root)[1]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
