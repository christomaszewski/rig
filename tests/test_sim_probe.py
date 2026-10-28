"""ROS attach readiness and standalone probe protocol, without a ROS installation."""
import contextlib
import copy
import importlib.abc
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace, ModuleType
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rig_cli import sim_probe


def request():
    return dict(ros=dict(distro="jazzy", rmw="rmw_fastrtps_cpp", domain_id=7), timeout_s=0.05,
                publishes=[dict(topic="/camera/image", type="sensor_msgs/msg/Image", instance="camera")],
                subscribes=[dict(topic="/cmd_vel", type="geometry_msgs/msg/Twist")])


def observation():
    return dict(ros=request()["ros"], clock=dict(publishers=1, max_publishers=1, samples=2,
                first_ns=0, last_ns=100, advancing=True, reset=False, advance_age_s=0),
                topics={"/camera/image": {"publishers": [dict(type="sensor_msgs/msg/Image")]},
                        "/cmd_vel": {"subscribers": [dict(type="geometry_msgs/msg/Twist")]}})


class ProbeEvaluation(unittest.TestCase):
    def test_good_evidence_is_ready_and_keeps_diagnostics(self):
        observed = observation()
        observed["topics"]["/camera/image"]["publishers"][0].update(
            node="/camera/publisher", qos={"reliability": "BEST_EFFORT"})
        before = copy.deepcopy(observed)
        result = sim_probe.evaluate(request(), observed)
        self.assertTrue(result["ready"])
        self.assertEqual(result["version"], 1)
        self.assertEqual(result["topics"], observed["topics"])
        self.assertEqual(observed, before)

    def test_environment_must_match_actual_distro_rmw_domain(self):
        for field, value in (("distro", None), ("distro", "lyrical"),
                             ("rmw", "rmw_zenoh_cpp"), ("domain_id", 0)):
            observed = observation()
            observed["ros"][field] = value
            result = sim_probe.evaluate(request(), observed)
            self.assertFalse(result["ready"])
            self.assertTrue(any(i["code"] == "ros_" + field for i in result["issues"]))

    def test_missing_type_or_wrong_direction_fails(self):
        for replacement, expected in (({}, "missing_endpoint"),
                ({"subscribers": [{"type": "sensor_msgs/msg/Image"}]}, "missing_endpoint"),
                ({"publishers": [{"type": "sensor_msgs/msg/CompressedImage"}]}, "topic_type"),
                ({"publishers": [{"type": "sensor_msgs/msg/Image"}, {"type": "other/msg/Image"}]},
                 "topic_type")):
            observed = observation()
            observed["topics"]["/camera/image"] = replacement
            result = sim_probe.evaluate(request(), observed)
            self.assertFalse(result["ready"])
            self.assertTrue(any(i["code"] == expected for i in result["issues"]))

    def test_clock_requires_one_authority_and_advancing_samples(self):
        for changes, expected in (({"publishers": 0}, "clock_authority"),
                ({"publishers": 2}, "clock_authority"),
                ({"max_publishers": 2}, "clock_authority"),
                ({"samples": 1}, "clock_stopped"),
                ({"advancing": False}, "clock_stopped"),
                ({"last_ns": 0}, "clock_stopped"),
                ({"first_ns": None}, "clock_stopped"),
                ({"advance_age_s": None}, "clock_stale"),
                ({"advance_age_s": 3}, "clock_stale"),
                ({"reset": True}, "clock_reset")):
            observed = observation()
            observed["clock"].update(changes)
            result = sim_probe.evaluate(request(), observed)
            self.assertFalse(result["ready"])
            self.assertTrue(any(i["code"] == expected for i in result["issues"]))

    def test_multiple_tf_publishers_are_not_automatically_conflicting(self):
        req, observed = request(), observation()
        req["publishes"] = [dict(topic="/tf", type="tf2_msgs/msg/TFMessage")]
        observed["topics"]["/tf"] = {"publishers": [
            dict(node="/robot/state", type="tf2_msgs/msg/TFMessage"),
            dict(node="/world/odom", type="tf2_msgs/msg/TFMessage")]}
        self.assertTrue(sim_probe.evaluate(req, observed)["ready"])

    def test_duplicate_data_and_command_publishers_fail(self):
        for topic, typ in (("/camera/image", "sensor_msgs/msg/Image"),
                           ("/cmd_vel", "geometry_msgs/msg/Twist")):
            observed = observation()
            observed["topics"][topic]["publishers"] = [
                dict(node="/isaac/first", type=typ), dict(node="/unexpected/other", type=typ)]
            result = sim_probe.evaluate(request(), observed)
            self.assertFalse(result["ready"])
            self.assertTrue(any(i["code"] == "topic_authority" for i in result["issues"]))

    def test_qos_checks_reject_known_incompatibility_and_report_unknown(self):
        for status, ready, code in (("ok", True, None), ("warn", True, "qos_unknown"),
                                   ("error", False, "qos_incompatible")):
            observed = observation()
            observed["topics"]["/camera/image"].update(
                subscribers=[dict(node="/slam/input", type="sensor_msgs/msg/Image")],
                qos_checks=[dict(publisher="/isaac/camera", subscriber="/slam/input", status=status,
                                 reason="Offered and requested reliability")])
            result = sim_probe.evaluate(request(), observed)
            self.assertEqual(result["ready"], ready)
            if code:
                self.assertTrue(any(i["code"] == code for i in result["issues"]))

    def test_existing_opposite_endpoints_require_correct_types_and_qos_evidence(self):
        for typ, code in (("sensor_msgs/msg/CompressedImage", "endpoint_type"),
                          ("sensor_msgs/msg/Image", "qos_evidence")):
            observed = observation()
            observed["topics"]["/camera/image"]["subscribers"] = [dict(type=typ)]
            result = sim_probe.evaluate(request(), observed)
            self.assertFalse(result["ready"])
            self.assertTrue(any(i["code"] == code for i in result["issues"]))

    def test_qos_inspection_uses_offered_then_requested_and_fails_closed(self):
        pub = SimpleNamespace(node_namespace="/isaac", node_name="camera", endpoint_gid=[1],
                              qos_profile=object())
        sub = SimpleNamespace(node_namespace="/slam", node_name="input", endpoint_gid=[2],
                              qos_profile=object())
        calls = []
        def checker(offered, requested):
            calls.append((offered, requested))
            return SimpleNamespace(name="ERROR"), "Incompatible durability"
        result = sim_probe._qos_check(pub, sub, checker)
        self.assertEqual(calls, [(pub.qos_profile, sub.qos_profile)])
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["reason"], "Incompatible durability")
        def unavailable(*args):
            raise RuntimeError("unsupported")
        self.assertEqual(sim_probe._qos_check(pub, sub, unavailable)["status"], "error")

    def test_request_validation_is_bounded_and_rejects_ambiguous_inputs(self):
        bad = []
        for timeout in (0, -1, 121, float("inf"), float("nan"), True, "1"):
            bad.append(dict(request(), timeout_s=timeout))
        for domain in (-1, 233, True, "7"):
            doc = request()
            doc["ros"]["domain_id"] = domain
            bad.append(doc)
        for topic in ("camera", "/camera/", "/camera//image", "/camera/*"):
            doc = request()
            doc["publishes"][0]["topic"] = topic
            bad.append(doc)
        bad += [[], {}, dict(request(), unknown=True), dict(request(), publishes=[{}, {}]),
                dict(request(), publishes=request()["publishes"] * 2),
                dict(request(), subscribes=[dict(topic="/clock", type="rosgraph_msgs/msg/Clock")])]
        for doc in bad:
            with self.subTest(doc=doc), self.assertRaises(ValueError):
                sim_probe.validate_request(doc)

    def test_protocol_reports_invalid_request_and_import_errors(self):
        for raw, code in (("{", "request"), (json.dumps(request()), "ros_unavailable")):
            class MissingRos(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if fullname == "rclpy":
                        raise ImportError("deliberately unavailable")
            out = io.StringIO()
            with patch.object(sys, "stdin", io.StringIO(raw)), contextlib.redirect_stdout(out), \
                    patch.object(sys, "meta_path", [MissingRos(), *sys.meta_path]):
                rc = sim_probe.main()
            result = json.loads(out.getvalue())
            self.assertEqual(rc, 1)
            self.assertEqual(result["issues"][0]["code"], code)

    def test_standalone_source_requires_no_rig_or_yaml_import(self):
        # -I removes the checkout and user packages; malformed input still uses
        # the exact production stdin/stdout protocol without importing ROS.
        process = subprocess.run([sys.executable, "-I", "-c", Path(sim_probe.__file__).read_text()],
                                 input="{}", text=True, capture_output=True, timeout=5)
        self.assertEqual(process.returncode, 1)
        self.assertFalse(json.loads(process.stdout)["ready"])
        self.assertEqual(process.stderr, "")


class ProbeObserver(unittest.TestCase):
    def fake_ros(self, *, wrong_domain=False, reset=False):
        contexts, nodes, subscriptions = [], [], []
        policy = SimpleNamespace(KEEP_LAST="keep", BEST_EFFORT="best", VOLATILE="volatile")
        qos = SimpleNamespace(depth=10, reliability="best", durability="volatile",
                              history="keep", liveliness="automatic")

        def endpoint(topic):
            return SimpleNamespace(node_namespace="/isaac", node_name="bridge",
                    topic_type={"/clock": "rosgraph_msgs/msg/Clock", "/camera/image": "sensor_msgs/msg/Image",
                                "/cmd_vel": "geometry_msgs/msg/Twist"}[topic],
                    endpoint_gid=[1] * 16, qos_profile=qos)

        class Context:
            def __init__(self):
                self.stopped = False
                contexts.append(self)

            def get_domain_id(self):
                return 0 if wrong_domain else 7

            def ok(self):
                return not self.stopped

            def try_shutdown(self):
                self.stopped = True

        class Node:
            def __init__(self, **kwargs):
                self.options = kwargs
                self.destroyed = False
                nodes.append(self)

            def create_subscription(self, typ, topic, callback, qos):
                subscriptions.append((topic, callback, qos))

            def get_publishers_info_by_topic(self, topic):
                return [endpoint(topic)] if topic != "/cmd_vel" else []

            def get_subscriptions_info_by_topic(self, topic):
                return [endpoint(topic)] if topic == "/cmd_vel" else []

            def destroy_node(self):
                self.destroyed = True

        ticks = [0]

        def spin_once(node, timeout_sec):
            ticks[0] += 1
            stamp = 0 if reset and ticks[0] == 2 else ticks[0]
            subscriptions[0][1](SimpleNamespace(clock=SimpleNamespace(sec=stamp, nanosec=0)))

        class Executor:
            def __init__(self, *, context):
                self.context = context
                self.node = None

            def add_node(self, node):
                assert node.options["context"] is self.context
                self.node = node

            def spin_once(self, timeout_sec):
                spin_once(self.node, timeout_sec)

            def shutdown(self):
                self.context.executor_stopped = True

        ros = ModuleType("rclpy")
        ros.init = lambda **kwargs: None
        ros.create_node = lambda name, **kwargs: Node(**kwargs)
        ros.spin_once = lambda *args, **kwargs: self.fail("must not use the default-context executor")
        modules = {"rclpy": ros, "rclpy.context": SimpleNamespace(Context=Context),
                   "rclpy.executors": SimpleNamespace(SingleThreadedExecutor=Executor),
                   "rclpy.qos": SimpleNamespace(QoSProfile=lambda **kwargs: kwargs,
                        ReliabilityPolicy=policy, DurabilityPolicy=policy, HistoryPolicy=policy,
                        qos_check_compatible=lambda *args: (SimpleNamespace(name="OK"), "")),
                   "rclpy.utilities": SimpleNamespace(get_rmw_implementation_identifier=lambda: "rmw_fastrtps_cpp"),
                   "rosgraph_msgs": ModuleType("rosgraph_msgs"),
                   "rosgraph_msgs.msg": SimpleNamespace(Clock=object)}
        return modules, contexts, nodes, subscriptions

    def test_observer_uses_actual_domain_and_destroys_context(self):
        modules, contexts, nodes, _ = self.fake_ros(wrong_domain=True)
        with patch.dict(sys.modules, modules), patch.dict(os.environ, ROS_DISTRO="jazzy"):
            result = sim_probe.evaluate(request(), sim_probe.observe(request()))
        self.assertFalse(result["ready"])
        self.assertEqual(result["ros"]["domain_id"], 0)
        self.assertEqual(nodes, [])
        self.assertTrue(contexts[0].stopped)

    def test_observer_reads_graph_and_clock_without_command_publishers(self):
        modules, contexts, nodes, subscriptions = self.fake_ros()
        # Deterministic monotonic progression avoids a real wait and ensures at
        # least two received clock samples before the bounded observation ends.
        ticks = iter(i * .01 for i in range(100))
        with patch.dict(sys.modules, modules), patch.dict(os.environ, ROS_DISTRO="jazzy"), \
                patch.object(sim_probe.time, "monotonic", side_effect=lambda: next(ticks)):
            result = sim_probe.evaluate(request(), sim_probe.observe(request()))
        self.assertTrue(result["ready"])
        self.assertEqual([s[0] for s in subscriptions], ["/clock"])
        self.assertFalse(nodes[0].options["enable_rosout"])
        self.assertFalse(nodes[0].options["start_parameter_services"])
        self.assertTrue(nodes[0].destroyed)
        self.assertTrue(contexts[0].executor_stopped)
        self.assertTrue(contexts[0].stopped)
        self.assertEqual(result["topics"]["/camera/image"]["publishers"][0]["node"], "/isaac/bridge")

    def test_observer_records_backward_clock_jump(self):
        modules, _, _, _ = self.fake_ros(reset=True)
        ticks = iter(i * .01 for i in range(100))
        with patch.dict(sys.modules, modules), patch.dict(os.environ, ROS_DISTRO="jazzy"), \
                patch.object(sim_probe.time, "monotonic", side_effect=lambda: next(ticks)):
            result = sim_probe.evaluate(request(), sim_probe.observe(request()))
        self.assertFalse(result["ready"])
        self.assertTrue(any(i["code"] == "clock_reset" for i in result["issues"]))


if __name__ == "__main__":
    unittest.main()
