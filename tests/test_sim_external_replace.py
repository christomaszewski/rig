"""Preflight messages name the simulator, never a sensor the recipe replaces.

`simulation.replace` is walked by the same function that words the selection and output
authority messages, so both providers are checked here with a replacement present. The
Isaac wording is v0.2.84's. Fixtures are test_sim_external's.
"""
import copy
from pathlib import Path
import sys
import unittest

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_sim_external as external

SELECTION = ("camera_rear: {} attach starts infrastructure and autonomy only; "
             "physical sensors and bag players must be omitted")
AUTHORITY = ("planner and {} both publish /planner/cmd; give each output one authority "
             "(shared TF requires explicit scene/config separation)")


def add_rear_camera(case):
    """A second physical sensor: one is replaced while the other is wrongly selected."""
    vehicle = yaml.safe_load((case.root / "vehicle.yaml").read_text())
    vehicle["sensors"].append({"name": "camera_rear", "service": "camera", "config": "camera_rear.yaml"})
    case.f.write("vehicle.yaml", vehicle)
    case.f.write("camera_rear.yaml", {"name": "camera_rear", "service": "camera", "gain": 1})


def messages(plan, code):
    return [i["message"] for i in plan["issues"] if i["code"] == code and i["status"] == "error"]


class IsaacWithAReplacedSensor(external.Session):
    def test_selection_and_authority_messages_still_say_isaac(self):
        add_rear_camera(self)
        doc = copy.deepcopy(self.f.doc)
        self.assertEqual(doc["simulation"]["replace"], ["camera"])
        doc["session"]["instances"].append("camera_rear")
        doc["simulation"]["subscribes"] = []
        doc["simulation"]["publishes"].append({"topic": "/planner/cmd", "type": "geometry_msgs/msg/Twist"})
        plan = self.plan(doc, host=False)
        self.assertEqual(messages(plan, "replacement"), [])
        self.assertEqual(messages(plan, "selection"), [SELECTION.format("Isaac")])
        self.assertEqual(messages(plan, "authority"), [AUTHORITY.format("Isaac")])


class ExternalWithAReplacedSensor(external.ExternalSession):
    def replacing(self):
        doc = copy.deepcopy(self.f.doc)
        doc["simulation"]["replace"] = ["camera"]
        doc["simulation"]["publishes"].append(
            {"topic": "/camera/image", "type": "sensor_msgs/msg/Image", "instance": "camera"})
        return doc

    def test_selection_and_authority_messages_name_the_simulator(self):
        add_rear_camera(self)
        doc = self.replacing()
        doc["session"]["instances"].append("camera_rear")
        doc["simulation"]["publishes"].append({"topic": "/planner/cmd", "type": "geometry_msgs/msg/Twist"})
        plan = self.plan(doc, host=False)
        self.assertEqual(messages(plan, "replacement"), [])
        self.assertEqual(messages(plan, "selection"), [SELECTION.format("worldsim")])
        self.assertEqual(messages(plan, "authority"), [AUTHORITY.format("worldsim")])
        self.assertFalse(external.says_isaac(plan["issues"]), plan["issues"])

    def test_an_external_simulator_may_replace_a_sensor(self):
        with self.host():
            plan = self.plan(self.replacing())
        self.assertTrue(plan["ready_to_run"], plan["issues"])
        self.assertEqual((plan["provider"], plan["simulator"]), ("external", external.SIMULATOR))
        self.assertEqual(plan["omitted"], ["camera"])
        self.assertFalse(external.says_isaac(plan["issues"]), plan["issues"])


if __name__ == "__main__":
    unittest.main()
