"""replay — source resolution, the graph selector, guards, provenance (rig-replay-plan).
Run: python3 tests/test_replay.py

The player itself lives in rig-infra (contract: rig-replay-player-handoff §1) — these tests cover
rig's half: what gets selected, what env is exported, what is refused. dispatch/doctor/docker are
monkeypatched at the replay module's imported references; epoch fixtures match the
graph-snapshotter's exact render (as in test_graph.py).
"""
import pathlib
import sys
import tempfile
import textwrap

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from rig_cli import RigError, replay, runs  # noqa: E402
from rig_cli.manifest import Manifest, RosSettings, Sensor  # noqa: E402

EPOCH = textwrap.dedent("""\
    schema: 1
    first: 2026-08-27T10:00:00Z
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
        - {topic: /planner/debug, type: std_msgs/msg/String}
        - {topic: /rosout, type: rcl_interfaces/msg/Log}
        subs:
        - {topic: /gnss_primary/fix, type: sensor_msgs/msg/NavSatFix}
        - {topic: /planner/debug, type: std_msgs/msg/String}
        - {topic: /parameter_events, type: rcl_interfaces/msg/ParameterEvent}
        provides: []
        requires: []
    """)


def _source_run(*, epochs=(EPOCH,), bags=True, sealed=True, name="20260827T100000Z_field"):
    run = pathlib.Path(tempfile.mkdtemp()) / name
    run.mkdir()
    if bags:
        (run / "bags" / "bag_logger").mkdir(parents=True)
    for i, text in enumerate(epochs):
        d = run / "graph" / "bag_logger"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"epoch_2026082{7 + i}T100000Z.yaml").write_text(text)
    if sealed:
        (run / "manifest.yaml").write_text(f"run: {name}\nended: 2026-08-27T11:01:00Z\n")
    return run


def _row(name, service="svc", tier="sensor", enabled=True, order=0):
    return Sensor(name=name, service=service, config=pathlib.Path("/dev/null"),
                  enabled=enabled, order=order, tier=tier)


def _manifest(rows, data_dir=None):
    return Manifest(vehicle="veh", vehicle_id=1, sensors=rows, data_dir=data_dir,
                    ros=RosSettings(domain_id=1, rmw="rmw_zenoh_cpp", distro=None))


PLAYER = _row("bag_player", service=replay.PLAYER_SERVICE, tier="autonomy",
              enabled=False, order=999)


def test_resolve_source_by_path_and_unsealed_warn():
    run = _source_run(sealed=False)
    rid, rdir = replay.resolve_source(_manifest([]), str(run))  # WARNs unsealed, still resolves
    assert (rid, rdir) == (run.name, run)


def test_resolve_source_by_id_and_missing():
    data = pathlib.Path(tempfile.mkdtemp())
    src = _source_run()
    (data / "runs").mkdir()
    src.rename(data / "runs" / src.name)
    m = _manifest([], data_dir=str(data))
    rid, _ = replay.resolve_source(m, src.name)
    assert rid == src.name
    try:
        replay.resolve_source(m, "nope")
        assert False
    except RigError as exc:
        assert "rig runs" in str(exc)


def test_resolve_source_refuses_the_open_run():
    data = pathlib.Path(tempfile.mkdtemp())
    src = _source_run(sealed=False)
    (data / "runs").mkdir()
    src = src.rename(data / "runs" / src.name)
    (data / "current").symlink_to(pathlib.Path("runs") / src.name)
    try:
        replay.resolve_source(_manifest([], data_dir=str(data)), src.name)
        assert False, "the OPEN run must be refused"
    except RigError as exc:
        assert "OPEN" in str(exc)


def test_resolve_source_refuses_no_bags():
    run = _source_run(bags=False)
    try:
        replay.resolve_source(_manifest([]), str(run))
        assert False
    except RigError as exc:
        assert "bags" in str(exc)


def test_select_topics_graph_mode_subs_minus_pubs_and_plumbing():
    mode, value, notices = replay.select_topics(_source_run(), ["planner"])
    assert mode == "topics"
    assert value == "/gnss_primary/fix"  # /planner/debug self-echoed away; plumbing filtered
    assert any("self-echo" in n and "/planner/debug" in n for n in notices)


def test_select_topics_unobserved_instance_falls_back():
    mode, value, notices = replay.select_topics(_source_run(), ["planner", "brand_new"])
    assert mode == "exclude"
    assert value == "^/(?:planner|brand_new)(?:/.*)?$"
    assert any("brand_new" in n for n in notices)


def test_select_topics_no_epochs_falls_back():
    mode, value, notices = replay.select_topics(_source_run(epochs=()), ["planner"])
    assert mode == "exclude" and any("no graph epochs" in n for n in notices)
    import re
    assert re.match(value, "/planner/cmd_vel") and re.match(value, "/planner")
    assert not re.match(value, "/planner_b/x") and not re.match(value, "/gnss/fix")


def test_player_row_detection():
    assert replay._player_row(_manifest([_row("a"), PLAYER])).name == "bag_player"
    try:
        replay._player_row(_manifest([_row("a")]))
        assert False
    except RigError as exc:
        assert "autonomy" in str(exc)  # the error carries the row to paste
    two = _manifest([PLAYER, _row("p2", service=replay.PLAYER_SERVICE, tier="autonomy")])
    try:
        replay._player_row(two)
        assert False
    except RigError as exc:
        assert "one player" in str(exc)


def test_clean_host_guard_fails_closed_and_force_bypasses():
    m = _manifest([_row("a")])
    orig = replay.runs_mod.running_projects
    try:
        replay.runs_mod.running_projects = lambda _m: ["a-vehicle-1"]
        try:
            replay._guard_clean_host(m, force=False)
            assert False
        except RigError as exc:
            assert "quiet host" in str(exc)
        replay._guard_clean_host(m, force=True)  # force bypasses without even asking docker

        def _broken(_m):
            raise RigError("docker wedged")
        replay.runs_mod.running_projects = _broken
        try:
            replay._guard_clean_host(m, force=False)
            assert False, "cannot-tell must fail closed"
        except RigError as exc:
            assert "docker wedged" in str(exc)
    finally:
        replay.runs_mod.running_projects = orig


def test_new_run_stamps_replay_provenance_and_listing_shows_it():
    data = pathlib.Path(tempfile.mkdtemp())
    m = _manifest([_row("a")], data_dir=str(data))
    doc = {"of": "srcrun", "source": "/x/srcrun", "with": ["planner"]}
    rid = runs.new_run(m, data, "replay-srcrun", force=True, replay=doc)
    from rig_cli.common import load_yaml
    assert load_yaml(data / "runs" / rid / "manifest.yaml")["replay"] == doc
    rows = runs.list_runs(m)
    assert [r.replay_of for r in rows] == ["srcrun"]
    rid2 = runs.new_run(m, data, "plain", force=True)  # no replay kwarg -> no key, no marker
    assert "replay" not in load_yaml(data / "runs" / rid2 / "manifest.yaml")


def test_cmd_dry_run_env_and_ordering():
    src = _source_run()
    rows = [_row("zenoh-router", service="zr", tier="infra", order=0),
            _row("gnss_primary", service="nov", tier="sensor", order=10),
            _row("planner", service="plan", tier="autonomy", order=20), PLAYER]
    m = _manifest(rows)
    descriptors = {s.service: object() for s in rows}
    calls = {}
    orig = (replay.doctor_mod.collect, replay.dispatch.fleet_env, replay.dispatch.run_verb)
    try:
        replay.doctor_mod.collect = lambda *a, **k: []
        replay.dispatch.fleet_env = lambda *a, **k: {"BASE": "1"}
        def _run_verb(pairs, env, verb, dry_run=False, **k):
            calls.update(pairs=pairs, env=env, verb=verb, dry_run=dry_run)
            class O:  # noqa: N801
                returncode = 0
                sensor = pairs[0][0]
            return [O()]
        replay.dispatch.run_verb = _run_verb

        rc = replay.cmd(m, {}, descriptors, pathlib.Path("."), run_ref=str(src),
                        names=["planner"], label=None, wall_clock=False, force=False,
                        dry_run=True)
        assert rc == 0
        names = [s.name for s, _ in calls["pairs"]]
        # enabled infra + with-set + player (auto-added, LAST); the disabled sensor row is absent
        assert names == ["zenoh-router", "planner", "bag_player"]
        assert calls["env"]["RIG_REPLAY_SOURCE"] == str(src)
        assert calls["env"]["RIG_REPLAY_TOPICS"] == "/gnss_primary/fix"
        assert "RIG_REPLAY_EXCLUDE" not in calls["env"]  # ONE selector mode, never both
        assert calls["env"]["RIG_SIM_TIME"] == "1"

        rc = replay.cmd(m, {}, descriptors, pathlib.Path("."), run_ref=str(src),
                        names=["planner"], label=None, wall_clock=True, force=False,
                        dry_run=True)
        assert rc == 0 and "RIG_SIM_TIME" not in calls["env"]

        for bad_names, needle in (([], "name the instance"), (["bag_player"], "player")):
            try:
                replay.cmd(m, {}, descriptors, pathlib.Path("."), run_ref=str(src),
                           names=bad_names, label=None, wall_clock=False, force=False,
                           dry_run=True)
                assert False, f"must refuse names={bad_names}"
            except RigError as exc:
                assert needle in str(exc)
    finally:
        replay.doctor_mod.collect, replay.dispatch.fleet_env, replay.dispatch.run_verb = orig


def test_descriptor_replay_block_and_strictness():
    import tempfile as tf
    repo = pathlib.Path(tf.mkdtemp())
    from rig_cli.descriptor import load_descriptor

    def _load(block):
        (repo / "rigging.yaml").write_text("service: svc\nlauncher: svc-up\n" + block)
        return load_descriptor("svc", repo)

    assert _load("replay: { sim_time: true }\n").replay_sim_time is True
    assert _load("replay: { sim_time: false }\n").replay_sim_time is False
    assert _load("").replay_sim_time is False
    for bad in ("replay: { simtime: true }\n",        # typo'd key
                "replay: { sim_time: yes please }\n",  # not a bool
                "replay: sim_time\n"):                 # not a mapping
        try:
            _load(bad)
            assert False, f"must refuse: {bad!r}"
        except RigError:
            pass


def test_replay_issues_warn_ok_and_wallclock():
    from rig_cli import doctor

    class D:  # the one attribute replay_issues reads
        def __init__(self, adopted):
            self.replay_sim_time = adopted

    m = _manifest([_row("planner", service="plan", tier="autonomy"),
                   _row("gnss_primary", service="nov")])
    both = ["planner", "gnss_primary"]
    warns = doctor.replay_issues(m, {"plan": D(True), "nov": D(False)}, both, sim_time=True)
    assert [i.level for i in warns] == [doctor.WARN]
    assert "gnss_primary" in warns[0].message and "sim_time" in warns[0].message
    ok = doctor.replay_issues(m, {"plan": D(True), "nov": D(True)}, both, sim_time=True)
    assert [i.level for i in ok] == [doctor.OK]
    info = doctor.replay_issues(m, {"plan": D(False), "nov": D(False)}, both, sim_time=False)
    assert [i.level for i in info] == [doctor.INFO]  # wall clock: informational, never nagging


def test_cmd_dry_run_survives_a_busy_host():
    src = _source_run()
    rows = [_row("planner", service="plan", tier="autonomy", order=20), PLAYER]
    m = _manifest(rows)
    descriptors = {s.service: object() for s in rows}
    orig = (replay.doctor_mod.collect, replay.dispatch.fleet_env, replay.dispatch.run_verb,
            replay.runs_mod.running_projects)
    try:
        replay.doctor_mod.collect = lambda *a, **k: []
        replay.dispatch.fleet_env = lambda *a, **k: {}
        replay.dispatch.run_verb = lambda pairs, env, verb, dry_run=False, **k: []
        replay.runs_mod.running_projects = lambda _m: ["planner-vehicle-1"]  # busy host
        rc = replay.cmd(m, {}, descriptors, pathlib.Path("."), run_ref=str(src),
                        names=["planner"], label=None, wall_clock=False, force=False,
                        dry_run=True)
        assert rc == 0  # dry-run WARNs about the would-be refusal instead of dying
        try:
            replay.cmd(m, {}, descriptors, pathlib.Path("."), run_ref=str(src),
                       names=["planner"], label=None, wall_clock=False, force=False,
                       dry_run=False)
            assert False, "a REAL replay on a busy host must refuse"
        except RigError:
            pass
    finally:
        (replay.doctor_mod.collect, replay.dispatch.fleet_env, replay.dispatch.run_verb,
         replay.runs_mod.running_projects) = orig




# --- the service-call half (rig-svc-replay plan; rig-replay-calls-handoff §1) ----------------

EPOCH_SVC = textwrap.dedent("""\
    schema: 1
    first: 2026-08-27T10:00:00Z
    last: 2026-08-27T11:00:00Z
    rmw: rmw_zenoh_cpp
    domain: 7
    nodes:
      /gnss_primary/novatel_node:
        pubs:
        - {topic: /gnss_primary/fix, type: sensor_msgs/msg/NavSatFix}
        subs: []
        provides:
        - {service: /gnss_primary/reset, type: std_srvs/srv/Trigger}
        requires: []
      /planner/planner_node:
        pubs:
        - {topic: /planner/set_mode/_service_event, type: my_msgs/srv/SetMode_Event}
        subs:
        - {topic: /gnss_primary/fix, type: sensor_msgs/msg/NavSatFix}
        provides:
        - {service: /planner/set_mode, type: my_msgs/srv/SetMode}
        - {service: /planner/planner_node/get_parameters, type: rcl_interfaces/srv/GetParameters}
        requires:
        - {service: /gnss_primary/reset, type: std_srvs/srv/Trigger}
    """)


def test_select_services_provides_minus_requires_and_plumbing():
    src = _source_run(epochs=(EPOCH_SVC,))
    services, notices = replay.select_services(src, ["planner"])
    assert services == "/planner/set_mode"  # parameter plumbing dropped; requires untouched here
    # requires subtraction: planner+gnss together — gnss's reset is provided AND required in-set
    services2, notices2 = replay.select_services(src, ["planner", "gnss_primary"])
    assert services2 == "/planner/set_mode"
    assert any("self-echo" in n and "/gnss_primary/reset" in n for n in notices2)


def test_select_services_epochs_only_no_fallback():
    assert replay.select_services(_source_run(epochs=()), ["planner"])[0] is None
    assert replay.select_services(_source_run(epochs=(EPOCH_SVC,)), ["planner", "ghost"])[0] is None


def test_service_event_topics_stay_out_of_the_topic_selector():
    mode, value, _ = replay.select_topics(_source_run(epochs=(EPOCH_SVC,)), ["planner"])
    assert mode == "topics" and value == "/gnss_primary/fix"
    assert "_service_event" not in value  # the service CHANNEL replays as calls, never as topics


def test_validate_calls_good_and_refusals():
    d = pathlib.Path(tempfile.mkdtemp())
    good = d / "calls.yaml"
    good.write_text(textwrap.dedent("""\
        schema: 1
        timeout_s: 5
        calls:
          - {t: 12.5, service: /planner/set_mode, type: my_msgs/srv/SetMode, request: {mode: A}}
          - {t: 0, service: /planner/set_mode, type: my_msgs/srv/SetMode}
        """))
    assert len(replay.validate_calls(good)) == 64  # sha256 back for provenance
    for bad, needle in ((("schema: 2\ncalls: [{t: 1, service: /s, type: t/srv/T}]\n"), "schema"),
                        (("schema: 1\ncalls: []\n"), "non-empty"),
                        (("schema: 1\ncalls: [{t: -1, service: /s, type: t/srv/T}]\n"), "≥ 0"),
                        (("schema: 1\ncalls: [{t: 1, type: t/srv/T}]\n"), "service"),
                        (("schema: 1\ncalls: [{t: 1, service: /s, type: t/srv/T, "
                          "request: nope}]\n"), "mapping")):
        p = d / "bad.yaml"
        p.write_text(bad)
        try:
            replay.validate_calls(p)
            assert False, f"must refuse: {needle}"
        except RigError as exc:
            assert needle in str(exc)
    try:
        replay.validate_calls(d / "missing.yaml")
        assert False
    except RigError as exc:
        assert "no file" in str(exc)


def test_cmd_services_verbatim_vs_calls_script_xor():
    src = _source_run(epochs=(EPOCH_SVC,))
    rows = [_row("gnss_primary", service="nov", tier="sensor", order=10),
            _row("planner", service="plan", tier="autonomy", order=20), PLAYER]
    m = _manifest(rows)
    descriptors = {s.service: object() for s in rows}
    seen = {}
    orig = (replay.doctor_mod.collect, replay.dispatch.fleet_env, replay.dispatch.run_verb)
    try:
        replay.doctor_mod.collect = lambda *a, **k: []
        replay.dispatch.fleet_env = lambda *a, **k: {}
        def _run_verb(pairs, env, verb, dry_run=False, **k):
            seen.update(env=env)
            class O:  # noqa: N801
                returncode = 0
                sensor = pairs[0][0]
            return [O()]
        replay.dispatch.run_verb = _run_verb

        rc = replay.cmd(m, {}, descriptors, pathlib.Path("."), run_ref=str(src),
                        names=["planner"], label=None, wall_clock=False, force=False,
                        dry_run=True)
        assert rc == 0
        assert seen["env"]["RIG_REPLAY_SERVICES"] == "/planner/set_mode"  # verbatim armed
        assert "RIG_REPLAY_CALLS" not in seen["env"]

        script = pathlib.Path(tempfile.mkdtemp()) / "calls.yaml"
        script.write_text("schema: 1\ncalls: [{t: 1.0, service: /planner/set_mode, "
                          "type: my_msgs/srv/SetMode, request: {mode: A}}]\n")
        rc = replay.cmd(m, {}, descriptors, pathlib.Path("."), run_ref=str(src),
                        names=["planner"], label=None, wall_clock=False, force=False,
                        dry_run=True, calls=str(script))
        assert rc == 0
        assert seen["env"]["RIG_REPLAY_CALLS"] == str(script.resolve())
        assert "RIG_REPLAY_SERVICES" not in seen["env"]  # script XOR verbatim
    finally:
        replay.doctor_mod.collect, replay.dispatch.fleet_env, replay.dispatch.run_verb = orig


def test_doctor_warns_undeclared_introspection_only_when_services_in_play():
    from rig_cli import doctor
    rows = [_row("planner", service="plan", tier="autonomy")]
    m = _manifest(rows)

    class _D:  # sim-time declared, introspection NOT
        replay_sim_time = True
        replay_service_introspection = False
    issues = doctor.replay_issues(m, {"plan": _D()}, ["planner"], sim_time=True, services=True)
    assert any("service_introspection" in i.message for i in issues)
    issues = doctor.replay_issues(m, {"plan": _D()}, ["planner"], sim_time=True, services=False)
    assert not any("service_introspection" in i.message for i in issues)


def test_alignment_report_stacks_and_drift():
    src = _source_run(epochs=(EPOCH_SVC,))
    # source manifest: ran planner only, with a sealed snapshot of planner's rendered config
    files = {"rendered/planner.yaml": b"service: plan\nname: planner\ngain: 1\n"}
    digest = runs._config_digest(files)
    snap = src / ".rig" / "config" / digest
    for rel, blob in files.items():
        (snap / rel).parent.mkdir(parents=True, exist_ok=True)
        (snap / rel).write_bytes(blob)
    import yaml as _y
    (src / "manifest.yaml").write_text(_y.safe_dump(
        {"run": src.name, "ended": "x", "stacks": ["planner"],
         "ups": [{"at": "x", "config": digest}]}))
    cfg = pathlib.Path(tempfile.mkdtemp()) / "planner.yaml"
    cfg.write_text("service: plan\nname: planner\ngain: 1\n")  # identical
    rows = [Sensor(name="planner", service="plan", config=cfg, enabled=True, order=1,
                   tier="autonomy"),
            Sensor(name="newsvc", service="ns", config=cfg, enabled=True, order=2,
                   tier="autonomy")]
    lines, drifted = replay._alignment_report(_manifest(rows), ["planner", "newsvc"], src)
    text = "\n".join(lines)
    assert "planner: config identical" in text and drifted == []
    assert "newsvc: not in the source run's recorded stacks" in text
    cfg.write_text("service: plan\nname: planner\ngain: 2\n")  # now drifted
    lines, drifted = replay._alignment_report(_manifest(rows), ["planner"], src)
    assert drifted == ["planner"] and any("DIFFERS" in ln for ln in lines)


def test_descriptor_replay_block_service_introspection():
    d = pathlib.Path(tempfile.mkdtemp())
    from rig_cli.descriptor import load_descriptor
    (d / "rigging.yaml").write_text(
        "service: svc\nlauncher: svc-up\nreplay: {sim_time: true, service_introspection: true}\n")
    desc = load_descriptor("svc", d)
    assert desc.replay_sim_time and desc.replay_service_introspection
    (d / "rigging.yaml").write_text("service: svc\nlauncher: svc-up\nreplay: {introspection: true}\n")
    try:
        load_descriptor("svc", d)
        assert False, "typo'd replay key must refuse"
    except RigError as exc:
        assert "service_introspection" in str(exc)




def test_services_never_armed_under_the_exclude_fallback():
    """rig-infra v1.10.0's live finding: lyrical's exclude regex removes topics AND services —
    SERVICES beside the fallback EXCLUDE would be silently killed by the very regex rig exports.
    The hazard case: epochs present, all observed, provides exist, but NO external subscribes →
    topic mode falls back to exclude; services must NOT arm."""
    epoch = textwrap.dedent("""\
        schema: 1
        first: 2026-08-27T10:00:00Z
        last: 2026-08-27T11:00:00Z
        rmw: rmw_zenoh_cpp
        domain: 7
        nodes:
          /planner/planner_node:
            pubs:
            - {topic: /planner/cmd_vel, type: geometry_msgs/msg/Twist}
            subs: []
            provides:
            - {service: /planner/set_mode, type: my_msgs/srv/SetMode}
            requires: []
        """)
    src = _source_run(epochs=(epoch,))
    rows = [_row("planner", service="plan", tier="autonomy", order=20), PLAYER]
    m = _manifest(rows)
    descriptors = {s.service: object() for s in rows}
    seen = {}
    orig = (replay.doctor_mod.collect, replay.dispatch.fleet_env, replay.dispatch.run_verb)
    try:
        replay.doctor_mod.collect = lambda *a, **k: []
        replay.dispatch.fleet_env = lambda *a, **k: {}
        def _run_verb(pairs, env, verb, dry_run=False, **k):
            seen.update(env=env)
            class O:  # noqa: N801
                returncode = 0
                sensor = pairs[0][0]
            return [O()]
        replay.dispatch.run_verb = _run_verb
        rc = replay.cmd(m, {}, descriptors, pathlib.Path("."), run_ref=str(src),
                        names=["planner"], label=None, wall_clock=False, force=False,
                        dry_run=True)
        assert rc == 0
        assert "RIG_REPLAY_EXCLUDE" in seen["env"]          # the fallback fired…
        assert "RIG_REPLAY_SERVICES" not in seen["env"]     # …so services stayed unarmed
    finally:
        replay.doctor_mod.collect, replay.dispatch.fleet_env, replay.dispatch.run_verb = orig


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
