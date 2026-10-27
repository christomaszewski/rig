"""Coordinated replay requires explicit endpoint evidence, never container state."""
import copy
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rig_cli import graph, replay_readiness, replay_selection


MSG, SRV = "example/msg/Value", "example/srv/Command"


def model(nodes, selected=("nav",), aliases=None, namespaces=None):
    epoch = graph.Epoch(Path("epoch.yaml"), "logger", "0", "1",
                        {name: tuple(graph.Edge(*edge) for edge in edges) for name, edges in nodes.items()})
    known = set(selected) | set((aliases or {}).values()) | set((namespaces or {}))
    return replay_selection.ownership([epoch], known, selected=selected,
                                       aliases=aliases, namespaces=namespaces)


class Readiness(unittest.TestCase):
    def setUp(self):
        self.rows = [SimpleNamespace(name="nav", service="navigation")]
        self.descriptors = {"navigation": SimpleNamespace(ros_namespace="/current/{name}")}
        self.plan = dict(selected=["nav"], player="player", roles={"nav": {"role": "consumer"}},
                         ownership=model({"/nav/worker": [("subs", "/raw", MSG)]}),
                         playback={"topics": ["/raw"], "services": [], "remap": {}},
                         source={"bag": {"topics": {"/raw": {"type": MSG}}}}, configs={}, window={})

    def derive(self):
        return replay_readiness.requirements(self.plan, None, self.descriptors, self.rows)

    def test_exact_observed_node_and_type_map_to_current_namespace(self):
        self.plan["ownership"] = model({"/historic/nav/worker": [("subs", "/raw", MSG)]},
            aliases={"nav": "old"}, namespaces={"old": "/historic/nav"})
        before = copy.deepcopy(self.plan)
        result = self.derive()
        self.assertEqual(result["subscriptions"], [dict(instance="nav", topic="/raw", type=MSG,
                                                        node="/current/nav/worker")])
        self.assertEqual(result["unsupported"], [])
        self.assertEqual(self.plan, before)

    def test_remap_requires_subscription_on_actual_destination(self):
        self.plan["playback"]["remap"] = {"/raw": "/input"}
        self.assertEqual(self.derive()["subscriptions"][0]["topic"], "/input")

    def test_empty_graph_and_declared_interface_do_not_invent_nodes(self):
        self.plan["ownership"] = replay_selection.ownership([], ["nav"], selected=["nav"])
        self.descriptors["navigation"].interface = {"subscribes": [SimpleNamespace(name="/raw", type=MSG)]}
        result = self.derive()
        self.assertEqual(result["subscriptions"], [])
        self.assertIn("unsupported", result["unsupported"][0]["reason"])

    def test_unknown_and_conflicting_types_are_not_readiness_proofs(self):
        for types in (("?",), (MSG, "other/msg/Value")):
            self.plan["ownership"] = model({"/nav/worker": [("subs", "/raw", typ) for typ in types]})
            result = self.derive()
            self.assertEqual(result["subscriptions"], [])
            self.assertTrue(result["unsupported"])
        self.plan["ownership"] = model({"/nav/worker": [("subs", "/raw", MSG)]})
        self.plan["source"]["bag"]["topics"]["/raw"]["type"] = "other/msg/Value"
        self.assertTrue(self.derive()["unsupported"])

    def test_multiple_current_aliases_do_not_share_one_historical_node(self):
        self.rows.append(SimpleNamespace(name="other", service="navigation"))
        self.plan["selected"].append("other")
        self.plan["ownership"] = model({"/nav/worker": [("subs", "/raw", MSG)]},
            selected=("nav", "other"), aliases={"other": "nav"})
        self.assertEqual(self.derive()["subscriptions"], [])
        self.assertTrue(self.derive()["unsupported"])

    def test_service_request_requires_exact_selected_server_and_type(self):
        self.plan["playback"].update(topics=[], services=["/reset"])
        self.plan["ownership"] = model({"/nav/worker": [("provides", "/reset", SRV)]})
        result = self.derive()
        self.assertEqual(result["services"], [dict(instance="nav", service="/reset", type=SRV,
                                                   node="/current/nav/worker")])
        self.assertEqual(result["unsupported"], [])
        self.plan["call_inspection"] = {"result": {"services": {"/reset": {"type": "other/srv/Command"}}}}
        self.assertEqual(self.derive()["services"], [])
        self.assertTrue(self.derive()["unsupported"])

    def test_cold_request_inspection_keeps_graph_requirements_until_final_selection(self):
        self.plan["playback"].update(topics=[], services=["/reset"])
        self.plan["ownership"] = model({"/nav/worker": [("provides", "/reset", SRV)]})
        self.plan["call_inspection"] = {"result": None, "status": "not_checked"}
        result = self.derive()
        self.assertEqual(result["services"], [dict(instance="nav", service="/reset", type=SRV,
                                                   node="/current/nav/worker")])
        self.assertEqual(result["unsupported"], [])
        self.plan["playback"]["services"] = []
        self.assertEqual(self.derive()["services"], [])

    def test_script_window_is_half_open_and_manual_unknown_servers_fail(self):
        self.plan["playback"].update(topics=[], calls="calls.yaml")
        self.plan["window"] = {"start": 2, "end": 4}
        self.plan["ownership"] = model({"/nav/worker": [("provides", "/reset", SRV)]})
        with patch.object(replay_readiness, "load_yaml", return_value={"calls": [
                {"t": 1, "service": "/before", "type": SRV},
                {"t": 2, "service": "/reset", "type": SRV},
                {"t": 4, "service": "/after", "type": SRV}]}):
            self.assertEqual(self.derive()["unsupported"], [])
            self.assertEqual(self.derive()["services"][0]["service"], "/reset")
        self.plan["playback"] = {"topics": [], "services": ["/unknown"]}
        self.assertTrue(any("No unique recorded server" in r["reason"] for r in self.derive()["unsupported"]))

    def test_recording_source_can_supply_its_bridge_and_downstream_consumers(self):
        self.rows.append(SimpleNamespace(name="camera", service="camera-service"))
        self.plan["selected"].append("camera")
        self.plan["roles"]["camera"] = {"role": "recording-source"}
        self.plan["playback"]["topics"] = []
        self.plan["ownership"] = model({"/camera/driver": [("pubs", "/image", MSG)],
            "/camera/bridge": [("subs", "/image", MSG)], "/nav/worker": [("subs", "/image", MSG)]},
            selected=("camera", "nav"))
        result = self.derive()
        self.assertEqual({r["instance"] for r in result["subscriptions"]}, {"camera", "nav"})
        self.assertEqual(result["unsupported"], [])

    def test_router_listener_must_be_explicit_and_bounded(self):
        self.rows = [SimpleNamespace(name="router", service="zenoh-router")]
        self.plan.update(selected=["router"], roles={"router": {"role": "infra"}})
        self.assertTrue(self.derive()["unsupported"])
        for endpoint, expected in (("tcp/0.0.0.0:7447", "127.0.0.1"), ("tcp/[::]:7448", "::1")):
            self.plan["configs"] = {"router": {"router_config": {"listen": {"endpoints": [endpoint]}}}}
            result = self.derive()
            self.assertEqual(result["transports"][0]["host"], expected)
            self.assertEqual(result["unsupported"], [])
        for endpoint in ("udp/127.0.0.1:7447", "tcp/router.test:7447", "tcp/127.0.0.1:0",
                         "tcp/224.0.0.1:7447"):
            self.plan["configs"]["router"]["router_config"]["listen"]["endpoints"] = [endpoint]
            self.assertTrue(self.derive()["unsupported"])

    def test_player_self_subscriptions_do_not_cover_an_unproven_consumer(self):
        self.rows.append(SimpleNamespace(name="player", service="ros2-bag-player"))
        self.plan["selected"].append("player")
        self.plan["ownership"] = model({"/player/node": [("subs", "/raw", MSG)]}, selected=("player", "nav"))
        self.assertEqual(self.derive()["subscriptions"], [])
        self.assertEqual(self.derive()["unsupported"][0]["instance"], "nav")


if __name__ == "__main__":
    unittest.main()
