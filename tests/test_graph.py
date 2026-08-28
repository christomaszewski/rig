"""graph — the epoch reader and its derived views (rig-graph-plan). Run: python3 tests/test_graph.py

Fixtures are hand-written epoch files matching the graph-snapshotter's exact render (rig-infra ≥
v1.7.0; rig-graph-capture-handoff §1.2) — including UNQUOTED ISO stamps, which YAML parses as
timestamps, the trap the reader must normalize. The writer is dumb, so everything interesting
(union, grouping, plumbing filter, contract scaffold, declared-vs-observed checks) is exercised
here, reader-side, without ROS or the sidecar.
"""
import pathlib
import sys
import tempfile
import textwrap

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from rig_cli import RigError, graph  # noqa: E402
from rig_cli.descriptor import InterfaceEdge  # noqa: E402
from rig_cli.manifest import Manifest, RosSettings, Sensor  # noqa: E402

EPOCH_A = textwrap.dedent("""\
    schema: 1
    first: 2026-08-27T10:00:00Z
    last: 2026-08-27T10:05:00Z
    rmw: rmw_zenoh_cpp
    domain: 7
    nodes:
      /gnss_primary/novatel_node:
        pubs:
        - {topic: /gnss_primary/fix, type: sensor_msgs/msg/NavSatFix}
        - {topic: /tf, type: tf2_msgs/msg/TFMessage}
        - {topic: /rosout, type: rcl_interfaces/msg/Log}
        subs:
        - {topic: /gnss_primary/rtcm, type: rtcm_msgs/msg/Message}
        provides:
        - {service: /gnss_primary/reset, type: std_srvs/srv/Trigger}
        - {service: /gnss_primary/novatel_node/get_parameters, type: rcl_interfaces/srv/GetParameters}
        requires: []
      /rosbag2_recorder:
        pubs: []
        subs:
        - {topic: /gnss_primary/fix, type: sensor_msgs/msg/NavSatFix}
        provides: []
        requires: []
    """)

# Same graph minus the recorder plus a planner — a CHANGED epoch, later window.
EPOCH_B = textwrap.dedent("""\
    schema: 1
    first: 2026-08-27T10:06:00Z
    last: 2026-08-27T11:00:00Z
    rmw: rmw_zenoh_cpp
    domain: 7
    nodes:
      /gnss_primary/novatel_node:
        pubs:
        - {topic: /gnss_primary/fix, type: sensor_msgs/msg/NavSatFix}
        subs: []
        provides: []
        requires: []
      /planner/planner_node:
        pubs:
        - {topic: /planner/cmd_vel, type: geometry_msgs/msg/Twist}
        subs:
        - {topic: /gnss_primary/fix, type: sensor_msgs/msg/NavSatFix}
        provides: []
        requires:
        - {service: /gnss_primary/reset, type: std_srvs/srv/Trigger}
    """)


def _run_dir(*epochs, writer="bag_logger") -> pathlib.Path:
    run = pathlib.Path(tempfile.mkdtemp()) / "20260827T100000Z_test"
    d = run / "graph" / writer
    d.mkdir(parents=True)
    for i, text in enumerate(epochs):
        (d / f"epoch_2026082{7 + i}T100000Z.yaml").write_text(text)
    return run


def _manifest(rows, data_dir=None) -> Manifest:
    return Manifest(vehicle="veh", vehicle_id=1, ros=RosSettings(domain_id=1, rmw="rmw_zenoh_cpp",
                                                                 distro=None), sensors=rows,
                    data_dir=data_dir)


def _sensor(name, service="svc", tier="sensor") -> Sensor:
    return Sensor(name=name, service=service, config=pathlib.Path("/dev/null"), enabled=True,
                  order=0, tier=tier)


class _Desc:  # the one attribute graph.check reads; a real Descriptor is file-loading overkill here
    def __init__(self, interface):
        self.interface = interface


def test_load_normalizes_yaml_timestamp_stamps_to_strings():
    epochs = graph.load_epochs(_run_dir(EPOCH_A))
    assert len(epochs) == 1
    assert epochs[0].first == "2026-08-27T10:00:00Z"  # datetime -> the contract string, not repr
    assert epochs[0].last == "2026-08-27T10:05:00Z"
    assert epochs[0].writer == "bag_logger"


def test_load_missing_graph_dir_is_inert():
    run = pathlib.Path(tempfile.mkdtemp())
    assert graph.load_epochs(run) == []


def test_load_skips_unknown_schema_major_but_keeps_the_rest():
    bad = EPOCH_A.replace("schema: 1", "schema: 2")
    epochs = graph.load_epochs(_run_dir(bad, EPOCH_B))
    assert len(epochs) == 1 and "/planner/planner_node" in epochs[0].nodes


def test_load_skips_unparseable_file_but_keeps_the_rest():
    epochs = graph.load_epochs(_run_dir("nodes: {broken", EPOCH_B))
    assert len(epochs) == 1


def test_union_windows_and_dedup():
    u = graph.union(graph.load_epochs(_run_dir(EPOCH_A, EPOCH_A, EPOCH_B)))  # A duplicated: restart
    assert u.epochs == 3
    assert u.first == "2026-08-27T10:00:00Z" and u.last == "2026-08-27T11:00:00Z"
    nov = u.nodes["/gnss_primary/novatel_node"]
    fix = graph.Edge("pubs", "/gnss_primary/fix", "sensor_msgs/msg/NavSatFix")
    assert nov[fix] == ("2026-08-27T10:00:00Z", "2026-08-27T11:00:00Z")  # present in A and B
    tf = graph.Edge("pubs", "/tf", "tf2_msgs/msg/TFMessage")
    assert nov[tf] == ("2026-08-27T10:00:00Z", "2026-08-27T10:05:00Z")  # A only — narrower window
    assert u.windows["/planner/planner_node"] == ("2026-08-27T10:06:00Z", "2026-08-27T11:00:00Z")


def test_grouping_longest_prefix_and_unassigned():
    groups = graph.group_nodes(
        ["/gnss_primary/novatel_node", "/gnss/x", "/rosbag2_recorder", "/gnss_primary"],
        ["gnss_primary", "gnss"])
    assert groups["gnss_primary"] == ["/gnss_primary", "/gnss_primary/novatel_node"]
    assert groups["gnss"] == ["/gnss/x"]  # NOT swallowed by a shorter/other instance
    assert groups[graph.UNASSIGNED] == ["/rosbag2_recorder"]


def test_plumbing_filter():
    assert graph.is_plumbing(graph.Edge("pubs", "/rosout", "rcl_interfaces/msg/Log"))
    assert graph.is_plumbing(graph.Edge("provides", "/a/b/get_parameters", "x/srv/Y"))
    assert not graph.is_plumbing(graph.Edge("pubs", "/gnss_primary/fix", "sensor_msgs/msg/NavSatFix"))
    assert not graph.is_plumbing(graph.Edge("provides", "/gnss_primary/reset", "std_srvs/srv/Trigger"))


def test_render_groups_and_hides_plumbing():
    u = graph.union(graph.load_epochs(_run_dir(EPOCH_A, EPOCH_B)))
    out = graph.render(u, "run1", ["gnss_primary", "planner"])
    assert "gnss_primary" in out and "planner" in out and graph.UNASSIGNED in out
    assert "/rosout" not in out and "get_parameters" not in out and "plumbing" in out
    assert out.index("gnss_primary") < out.index("planner")  # manifest order, not alphabetical


def test_union_yaml_is_epoch_shaped_and_keeps_plumbing():
    import yaml
    u = graph.union(graph.load_epochs(_run_dir(EPOCH_A)))
    doc = yaml.safe_load(graph.union_yaml(u, "run1"))
    assert doc["schema"] == 1 and doc["unioned"] == 1
    nov = doc["nodes"]["/gnss_primary/novatel_node"]
    assert {"topic": "/rosout", "type": "rcl_interfaces/msg/Log"} in nov["pubs"]  # raw, unfiltered
    assert set(nov) == {"pubs", "subs", "provides", "requires"}


def test_contract_scaffold_relative_absolute_and_plumbing():
    u = graph.union(graph.load_epochs(_run_dir(EPOCH_A, EPOCH_B)))
    out = graph.contract(u, "gnss_primary", "novatel", "run1")
    assert "- {topic: fix, type: sensor_msgs/msg/NavSatFix}" in out      # ns-stripped -> relative
    assert "- {topic: /tf, type: tf2_msgs/msg/TFMessage}" in out         # shared-bus stays absolute
    assert "- {service: reset, type: std_srvs/srv/Trigger}" in out
    assert "/rosout" not in out and "get_parameters" not in out          # plumbing dropped
    assert "cmd_vel" not in out                                          # other instances' edges too


def test_check_warns_both_directions_and_type_drift():
    u = graph.union(graph.load_epochs(_run_dir(EPOCH_A, EPOCH_B)))
    manifest = _manifest([_sensor("gnss_primary", "novatel"), _sensor("planner", "planner")])
    descriptors = {
        "novatel": _Desc({"publishes": (InterfaceEdge("fix", "sensor_msgs/msg/NavSatFix"),
                                        InterfaceEdge("/tf", None),
                                        InterfaceEdge("aux_status", None)),      # never observed
                          "subscribes": (InterfaceEdge("rtcm", "mavros_msgs/msg/RTCM"),),  # type drift
                          "provides": (InterfaceEdge("reset", "std_srvs/srv/Trigger"),),
                          "requires": ()}),
        "planner": _Desc(None),  # undeclared — nudged, never WARN-per-edge
    }
    warns = graph.check(u, manifest, descriptors)
    text = "\n".join(warns)
    assert "declared but not observed — pub /gnss_primary/aux_status" in text
    assert "type drift on sub /gnss_primary/rtcm" in text
    assert "observed but undeclared" not in text.replace(
        "no interface: declared", "")  # gnss fully declared otherwise
    assert "no interface: declared for planner" in text
    assert not any("gnss_primary: observed but undeclared" in w for w in warns)


def test_check_clean_when_declaration_matches():
    u = graph.union(graph.load_epochs(_run_dir(EPOCH_B)))
    manifest = _manifest([_sensor("planner", "planner")])
    descriptors = {"planner": _Desc({"publishes": (InterfaceEdge("cmd_vel", None),),
                                     "subscribes": (InterfaceEdge("/gnss_primary/fix", None),),
                                     "provides": (),
                                     "requires": (InterfaceEdge("/gnss_primary/reset", None),)})}
    assert graph.check(u, manifest, descriptors) == []


def test_check_declared_but_instance_never_observed():
    u = graph.union(graph.load_epochs(_run_dir(EPOCH_B)))
    manifest = _manifest([_sensor("cam_front", "camera-service")])
    descriptors = {"camera-service": _Desc({"publishes": (InterfaceEdge("image", None),),
                                            "subscribes": (), "provides": (), "requires": ()})}
    warns = graph.check(u, manifest, descriptors)
    assert len(warns) == 1 and "NO nodes observed" in warns[0]


def test_resolve_run_by_id_path_and_default_newest():
    run = _run_dir(EPOCH_A)
    data = run.parent.parent  # not a registry — resolve by PATH must still work
    rid, rdir = graph.resolve_run(_manifest([]), str(run))
    assert (rid, rdir) == (run.name, run)
    # a registry layout: data_dir/runs/<id>, no `current` -> newest wins
    data = pathlib.Path(tempfile.mkdtemp())
    for name in ("20260101T000000Z_old", "20260827T000000Z_new"):
        (data / "runs" / name).mkdir(parents=True)
    m = _manifest([], data_dir=str(data))
    rid, rdir = graph.resolve_run(m, None)
    assert rid == "20260827T000000Z_new"
    rid, _ = graph.resolve_run(m, "20260101T000000Z_old")
    assert rid == "20260101T000000Z_old"
    try:
        graph.resolve_run(m, "nope")
        assert False, "unknown id must raise"
    except RigError:
        pass


def test_descriptor_interface_parse():
    import tempfile as tf
    repo = pathlib.Path(tf.mkdtemp())
    from rig_cli.descriptor import load_descriptor

    def _load(block):
        (repo / "rigging.yaml").write_text("service: svc\nlauncher: svc-up\n" + block)
        return load_descriptor("svc", repo)

    d = _load("interface:\n  publishes:\n    - {topic: fix, type: sensor_msgs/msg/NavSatFix}\n"
              "    - /tf\n  requires:\n    - {service: /x/reset}\n")
    assert d.interface["publishes"] == (InterfaceEdge("fix", "sensor_msgs/msg/NavSatFix"),
                                        InterfaceEdge("/tf", None))
    assert d.interface["subscribes"] == () and d.interface["requires"][0].name == "/x/reset"
    assert _load("").interface is None  # undeclared stays None, distinct from declared-empty
    for bad in ("interface:\n  publish: []\n",                       # typo'd kind
                "interface:\n  publishes: [{service: x}]\n",         # service key on a topic kind
                "interface:\n  publishes: [{topic: 'a b'}]\n",       # not a topic name
                "interface:\n  publishes: [{topic: a//b}]\n",        # doubled slash
                "interface:\n  publishes: [{topic: x, type: NavSatFix}]\n"):  # short type form
        try:
            _load(bad)
            assert False, f"must refuse: {bad!r}"
        except RigError:
            pass


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print("FAIL", name, "->", exc)
    sys.exit(1 if failures else 0)
