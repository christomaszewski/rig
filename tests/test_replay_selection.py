"""Replay ownership decisions are pure and never infer publishers from topic prefixes."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rig_cli import graph, replay_selection as selection


def epoch(nodes, index=0):
    return graph.Epoch(Path(f"epoch_{index}.yaml"), "logger", str(index), str(index + 1),
                       {node: tuple(graph.Edge(kind, name, typ) for kind, name, typ in edges)
                        for node, edges in nodes.items()})


def pub(name):
    return ("pubs", name, "example/msg/Value")


def server(name):
    return ("provides", name, "example/srv/Command")


def client(name):
    return ("requires", name, "example/srv/Command")


def records(result, field):
    return {row[field]: row for row in result["records"]}


class ReplaySelection(unittest.TestCase):
    def test_aliases_and_recorded_namespaces_bind_nodes_not_interface_names(self):
        model = selection.ownership([epoch({"/historical/nav/worker": [pub("/global/fix"), server("/reset")],
                                           "/historical/driver/worker": [pub("/global/raw")]})],
            ["nav-old", "driver-old"], aliases={"navigation": "nav-old", "driver": "driver-old"},
            namespaces={"nav-old": "/historical/nav", "driver-old": "/historical/driver"},
            selected=["navigation"])
        topics = records(selection.topic_selection(model, ["/global/fix", "/global/raw"]), "topic")
        self.assertEqual(topics["/global/fix"]["selected_producers"], ["navigation"])
        self.assertEqual(topics["/global/fix"]["decision"], "suppress")
        self.assertEqual(topics["/global/raw"]["reason"], "producers_not_selected")
        calls = records(selection.service_selection(model, ["/reset"]), "service")
        self.assertEqual(calls["/reset"]["selected_servers"], ["navigation"])
        self.assertEqual(calls["/reset"]["owners"], ["nav-old"])
        # The model and both reports can be written directly to the resolved replay plan.
        json.dumps((model, topics, calls))

    def test_all_known_owners_delimit_nested_namespaces_and_segment_boundaries(self):
        model = selection.ownership([epoch({"/robot/sensor/node": [pub("/raw"), server("/calibrate")],
                                           "/robot/node": [pub("/output")],
                                           "/robotics/node": [pub("/unassigned")]})],
            ["robot", "sensor"], namespaces={"robot": "/robot", "sensor": "/robot/sensor"},
            selected=["robot"])
        topics = records(selection.topic_selection(model, ["/raw", "/output", "/unassigned"]), "topic")
        self.assertEqual(topics["/raw"]["owners"], ["sensor"])
        self.assertEqual(topics["/raw"]["decision"], "keep")
        self.assertEqual(topics["/output"]["decision"], "suppress")
        self.assertEqual(topics["/unassigned"]["reason"], "unassigned_publishers")
        calls = records(selection.service_selection(model, ["/calibrate"]), "service")
        self.assertEqual(calls["/calibrate"]["reason"], "server_not_selected")

    def test_equal_namespace_bindings_are_ambiguous_even_if_one_owner_is_selected(self):
        model = selection.ownership([epoch({"/shared/node": [pub("/out"), server("/act")]})],
            ["one", "two"], namespaces={"one": "/shared", "two": "/shared"}, selected=["one"])
        topic = selection.topic_selection(model, ["/out"])["records"][0]
        call = selection.service_selection(model, ["/act"])["records"][0]
        self.assertEqual(topic["reason"], "ambiguous_publishers")
        self.assertEqual(topic["ambiguous_nodes"], {"/shared/node": ["one", "two"]})
        self.assertEqual(call["reason"], "ambiguous_server_namespace")
        self.assertEqual(model["selected_without_nodes"], ["one"])

    def test_no_graph_has_no_namespace_heuristic_for_topics_or_calls(self):
        model = selection.ownership([], ["old"], aliases={"camera": "old"}, selected=["camera"])
        self.assertEqual(selection.topic_selection(model, ["/old/image"])["suppressed"], [])
        self.assertEqual(selection.service_selection(model, ["/old/start"])["candidates"], [])
        manual = selection.service_selection(model, ["/old/start"], mode="manual", manual=["/old/start"])
        self.assertEqual(manual["candidates"], ["/old/start"])
        self.assertIn("no_graph", manual["records"][0]["warnings"][0])
        self.assertEqual(selection.service_selection(model, ["/old/start"], mode="manual", manual=[])["candidates"], [])

    def test_topics_require_every_publisher_and_keep_shared_transform_diagnostic_data(self):
        model = selection.ownership([epoch({
            "/one/node": [pub("/both"), pub("/mixed"), pub("/rogue"), pub("/tf"), pub("/tf_static"), pub("/diagnostics")],
            "/two/node": [pub("/both")], "/omitted/node": [pub("/mixed")],
            "/unknown/node": [pub("/rogue")],
            "/one/consumer": [("subs", "/one/input", "example/msg/Value")],
        })], ["one", "two", "omitted"], selected=["one", "two"])
        result = selection.topic_selection(model, ["/both", "/mixed", "/rogue", "/tf", "/tf_static",
                                                   "/diagnostics", "/one/input", "/one/unobserved"])
        self.assertEqual(result["suppressed"], ["/both"])
        table = records(result, "topic")
        self.assertEqual(table["/mixed"]["reason"], "shared_publishers")
        self.assertEqual(table["/rogue"]["reason"], "unassigned_publishers")
        self.assertEqual(table["/one/input"]["reason"], "unknown_publishers")
        self.assertEqual(table["/one/unobserved"]["reason"], "unknown_publishers")
        for topic in selection.SHARED_TOPICS:
            self.assertEqual(table[topic]["reason"], "shared_topic")

    def test_calls_use_entire_started_set_and_exclude_selected_live_clients(self):
        model = selection.ownership([epoch({
            "/device/node": [server("/reset")],
            "/policy_instance/node": [server("/load")],
            "/infra/node": [server("/diagnostic_request")],
            "/consumer_old/node": [client("/reset")],
            "/omitted/node": [client("/load")],
            "/unassigned/node": [client("/diagnostic_request")],
        })], ["device", "policy_instance", "infra", "consumer_old", "omitted"],
            aliases={"consumer": "consumer_old"}, selected=["device", "policy_instance", "infra", "consumer"])
        result = selection.service_selection(model, ["/reset", "/load", "/diagnostic_request"])
        self.assertEqual(result["candidates"], ["/diagnostic_request", "/load"])
        table = records(result, "service")
        self.assertEqual(table["/reset"]["reason"], "selected_live_client")
        self.assertEqual(table["/reset"]["selected_clients"], ["consumer"])
        self.assertEqual(table["/diagnostic_request"]["unassigned_clients"], ["/unassigned/node"])
        self.assertTrue(table["/diagnostic_request"]["warnings"])
        manual = selection.service_selection(model, ["/reset"], mode="manual", manual=["/reset"])
        self.assertEqual(manual["candidates"], ["/reset"])
        self.assertIn("selected_live_client", manual["records"][0]["warnings"][0])

    def test_multiple_unassigned_omitted_and_multiply_mapped_servers_are_not_automatic(self):
        model = selection.ownership([epoch({
            "/one/node": [server("/multiple"), server("/rogue"), server("/aliased")],
            "/two/node": [server("/multiple")], "/unassigned/node": [server("/rogue")],
            "/omitted/node": [server("/off")],
        })], ["one", "two", "omitted"], aliases={"renamed_one": "one"},
            selected=["one", "renamed_one", "two"])
        result = selection.service_selection(model, ["/multiple", "/rogue", "/aliased", "/off", "/missing"])
        self.assertEqual(result["candidates"], [])
        reasons = {name: row["reason"] for name, row in records(result, "service").items()}
        self.assertEqual(reasons, {"/multiple": "multiple_server_owners", "/rogue": "unassigned_servers",
            "/aliased": "ambiguous_started_servers", "/off": "server_not_selected", "/missing": "unknown_servers"})

    def test_retained_epoch_union_preserves_owners_across_time(self):
        epochs = [epoch({"/one/node": [server("/act"), pub("/out")]}, 0),
                  epoch({"/two/node": [server("/act"), pub("/out")]}, 1)]
        model = selection.ownership(graph.union(epochs), ["one", "two"], selected=["one"])
        self.assertEqual(model["epochs"], 2)
        self.assertTrue(model["limitations"])
        self.assertEqual(selection.topic_selection(model, ["/out"])["suppressed"], [])
        self.assertEqual(selection.service_selection(model, ["/act"])["records"][0]["reason"],
                         "multiple_server_owners")

    def test_ambiguous_client_involving_started_instance_blocks_automatic_injection(self):
        model = selection.ownership([epoch({"/server/node": [server("/act")], "/shared/node": [client("/act")]})],
            ["server", "one", "two"], namespaces={"one": "/shared", "two": "/shared"}, selected=["server", "one"])
        self.assertEqual(selection.service_selection(model, ["/act"])["records"][0]["reason"],
                         "ambiguous_started_clients")

    def test_plumbing_edges_do_not_create_automatic_call_or_topic_ownership(self):
        model = selection.ownership([epoch({"/one/node": [pub("/rosout"), pub("/act/_service_event"),
                                                           server("/one/node/get_parameters")]})],
                                    ["one"], selected=["one"])
        self.assertEqual(model["topics"], {})
        self.assertEqual(model["services"], {})


if __name__ == "__main__":
    unittest.main()
