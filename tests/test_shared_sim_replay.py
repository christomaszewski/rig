"""One session recipe connects simulated recording to ordinary source replay.

The ROS/Docker boundary is simulated; bag inventory, session selection, run
creation/sealing, preparation, rendered configs, and provenance are real.
"""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_replay_preparation as replay_fixtures
import test_sim_workflow as sim_fixtures
from rig_cli import cli, replay_workflow, sim_workflow
from rig_cli.replay_sources import source_fingerprint


class SharedSimReplay(unittest.TestCase):
    def setUp(self):
        self.f = sim_fixtures.Simulation()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        locks = patch("rig_cli.replay_coordinator.tempfile", SimpleNamespace(
            gettempdir=lambda: str(self.f.root), mkstemp=tempfile.mkstemp))
        locks.start()
        self.addCleanup(locks.stop)
        replay_fixtures.Preparation.service(self.f, "ros2-bag-player", player=True)
        vehicle = yaml.safe_load((self.f.root / "vehicle.yaml").read_text())
        vehicle["autonomy"].append(dict(name="player", service="ros2-bag-player",
                                       config="player.yaml", enabled=False))
        self.f.write("vehicle.yaml", vehicle)
        self.f.write("player.yaml", dict(name="player", service="ros2-bag-player"))
        services = yaml.safe_load((self.f.root / "services.yaml").read_text())
        services["services"]["ros2-bag-player"] = dict(path="services/ros2-bag-player")
        self.f.write("services.yaml", services)
        self.f.doc["replay"] = {"playback": {"exclude": ["/planner/cmd"]}}
        self.f.write("sessions/dockyard.yaml", self.f.doc)

    def replay(self, *tokens):
        args = cli.build_parser().parse_args(cli.translate_argv([
            "replay", *tokens, "--recipe", "dockyard"]))
        args.json = True
        with contextlib.redirect_stdout(io.StringIO()) as output, \
                contextlib.redirect_stderr(io.StringIO()):
            rc = replay_workflow.command(args, self.f.root)
        return rc, json.loads(output.getvalue())

    def record_simulation(self):
        with self.f.host(), contextlib.redirect_stderr(io.StringIO()):
            manifest, descriptors, plan = self.f.build()
            self.assertTrue(plan["ready_to_run"], plan["issues"])
            self.assertEqual(sim_workflow.execute(self.f.root, self.f.path, manifest,
                                                 descriptors, plan), 0)
            _, state = sim_workflow._read_state(self.f.root)
            source = Path(state["output_run"])
            folder = source / "bags/logger/session-1"
            replay_fixtures.Preparation.bag(self.f, folder, topic="/camera/image")
            metadata = yaml.safe_load((folder / "metadata.yaml").read_text())
            topics = metadata["rosbag2_bagfile_information"]["topics_with_message_count"]
            topics[:] = [dict(topic_metadata=dict(name=topic, type=message_type), message_count=10)
                for topic, message_type in [("/camera/image", "sensor_msgs/msg/Image"),
                    ("/planner/cmd", "geometry_msgs/msg/Twist"), ("/clock", "rosgraph_msgs/msg/Clock")]]
            self.f.write(str(folder / "metadata.yaml"), metadata)
            self.assertEqual(cli.main(["--root", str(self.f.root), "down", "--end-run"]), 0)
        self.assertFalse(sim_workflow.active_session(self.f.root))
        self.assertIn("ended", yaml.safe_load((source / "manifest.yaml").read_text()))
        return source.resolve()

    def test_same_recipe_records_then_replays_with_identical_workload_and_overrides(self):
        recipe_bytes = self.f.path.read_bytes()
        source = self.record_simulation()
        fingerprint = source_fingerprint(source)
        with replay_fixtures.Preparation.image_label_runtime(self.f, {}):
            rc, prepared = self.replay("prepare", str(source))
            self.assertEqual(rc, 0, prepared["issues"])
            self.assertEqual(prepared["selected"], ["logger", "planner", "player"])
            self.assertEqual(prepared["configs"]["planner"]["gain"], 3)
            self.assertEqual(prepared["playback"]["topics"], ["/camera/image"])
            self.assertTrue(prepared["sim_time"])
            self.assertEqual(prepared["session_recipe"], self.f.doc)
            self.assertEqual(self.f.path.read_bytes(), recipe_bytes)
            # Preparation binds this recording locally without adding provider source
            # state to the shared, reusable YAML.
            rc, checked = self.replay("check")
            self.assertEqual(rc, 0, checked["issues"])
            self.assertEqual(checked["source"]["path"], str(source))
            with self.f.host(), patch("rig_cli.session_runtime.engine_id", return_value="fixture-engine"):
                rc, launched = self.replay(str(source), "--start-delay", "0", "--label", "replayed-sim")
        self.assertEqual(rc, 0, launched["issues"])
        ups = [call for call in self.f.calls if call["verb"] == "up"]
        self.assertEqual([call["name"] for call in ups],
                         ["logger", "planner", "logger", "planner", "player"])
        self.assertEqual([call["config"]["gain"] for call in ups if call["name"] == "planner"], [3, 3])
        self.assertTrue(all(call["env"].get("RIG_SIM_TIME") == "1" for call in ups))
        current = (self.f.root / "data/current").resolve()
        self.assertNotEqual(current, source)
        replay_doc = yaml.safe_load((current / "manifest.yaml").read_text())
        self.assertEqual(replay_doc["replay"]["of"], source.name)
        self.assertEqual(replay_doc["replay"]["recipe"], self.f.doc)
        self.assertEqual(yaml.safe_load((current / ".rig/replay/recipe.yaml").read_text()), self.f.doc)
        with self.f.host(), patch("rig_cli.session_runtime.engine_id", return_value="fixture-engine"), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["--root", str(self.f.root), "down", "--end-run"]), 0)
        self.assertIn("ended", yaml.safe_load((current / "manifest.yaml").read_text()))
        self.assertNotIn("camera", [call["name"] for call in self.f.calls])
        self.assertEqual(self.f.path.read_bytes(), recipe_bytes)
        self.assertEqual(source_fingerprint(source), fingerprint)


if __name__ == "__main__":
    unittest.main()
