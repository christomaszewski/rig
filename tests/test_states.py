"""Operational states (v0.2.35) — declaration-gated `rig standby`/`rig activate` fan-out, the
`rig up --standby/--active` posture token, the RIG_TARGET_STATE pop, and the status OP column /
additive op_state JSON key. Run: python3 tests/test_states.py

Stub launchers are pure sh (no docker): they append their argv to $STATES_LOG, so dispatch
order, verb identity, and env delivery are all asserted from the log. The certify-side checks
live in tests/test_certify.py (the contract reference); this file covers dispatch + status.
"""
import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile
import textwrap

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from rig_cli import dispatch  # noqa: E402
from rig_cli.cli import main  # noqa: E402
from rig_cli.descriptor import (DEFAULT_VERBS, STATE_VERBS, Descriptor,  # noqa: E402
                                load_descriptor)
from rig_cli.manifest import Manifest, RosSettings, Sensor  # noqa: E402
from rig_cli.status import Row, as_json, gather, render  # noqa: E402


def _run(*argv) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = main(list(argv))
        except SystemExit as exc:  # argparse (e.g. mutually exclusive flags)
            rc = int(exc.code or 0)
    return rc, out.getvalue(), err.getvalue()


def _service(svc: str, *, trio: bool = True, partial: bool = False,
             launcher_body: str | None = None) -> pathlib.Path:
    """A stub service repo: rigging (optionally declaring the state trio) + an sh launcher that
    logs `<service> <verb...>` to $STATES_LOG and answers ps/state well-formed."""
    repo = pathlib.Path(tempfile.mkdtemp(prefix=f"states-{svc}-"))
    verbs = ""
    if partial:
        verbs = "verbs: { standby: standby }\n"  # broken claim: standby without activate/state
    elif trio:
        verbs = "verbs: { standby: standby, activate: activate, state: state }\n"
    (repo / "rigging.yaml").write_text(f"service: {svc}\nlauncher: {svc}-up\n{verbs}")
    body = launcher_body or textwrap.dedent("""\
        #!/bin/sh
        CONFIG="$1"; shift
        [ -n "$STATES_LOG" ] && echo "@SVC@ $* rts=${RIG_TARGET_STATE:-unset}" >> "$STATES_LOG"
        case "$1" in
          ps) echo '[]' ;;
          state) printf '{"state": "standby", "detail": "lifecycle:inactive"}\\n' ;;
        esac
        exit 0
        """).replace("@SVC@", svc)
    launcher = repo / f"{svc}-up"
    launcher.write_text(body)
    launcher.chmod(0o755)
    return repo


def _deployment(services: dict[str, pathlib.Path], rows: list[tuple[str, str, int]]) -> pathlib.Path:
    """A literal-identity deployment tree: rows are (name, service, order) sensor entries."""
    root = pathlib.Path(tempfile.mkdtemp(prefix="states-dep-")) / "dep"
    (root / "config").mkdir(parents=True)
    entries = "\n".join(f"  - {{name: {n}, service: {s}, config: config/{n}.yaml, order: {o}}}"
                        for n, s, o in rows)
    (root / "vehicle.yaml").write_text(f"vehicle: t\nvehicle_id: 1\nsensors:\n{entries}\n")
    for n, s, _ in rows:
        (root / "config" / f"{n}.yaml").write_text(f"service: {s}\nname: {n}\n")
    (root / "services.yaml").write_text(
        "services:\n" + "".join(f"  {s}: {{ path: {p} }}\n" for s, p in services.items()))
    return root


@contextlib.contextmanager
def _log_env(**extra):
    log = pathlib.Path(tempfile.mkdtemp()) / "argv.log"
    old = {k: os.environ.get(k) for k in ("STATES_LOG", *extra)}
    os.environ["STATES_LOG"] = str(log)
    for k, v in extra.items():
        os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, str(v))
    try:
        yield log
    finally:
        for k, v in old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def _log_lines(log: pathlib.Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


# --- descriptor: the declaration IS the support claim -------------------------------------------

def test_state_verbs_never_default():
    # The trio must NEVER enter DEFAULT_VERBS: verb_args' bare-token fallback would hand
    # `standby` to a compose-forwarding launcher as `docker compose standby`. This is exactly
    # why dispatch gates on declaration instead of calling verb_args blind.
    assert not (set(STATE_VERBS) & set(DEFAULT_VERBS))
    undeclared = Descriptor(service="x", repo=pathlib.Path("."), launcher="x-up", verbs={},
                            ros_distro=None, external_volumes=[], host_ports=[])
    assert undeclared.verb_args("standby") == ["standby"]  # the fallback dispatch must avoid
    assert undeclared.declared_state_verbs == []
    assert not undeclared.supports_states


def test_declared_trio_and_partial():
    trio = load_descriptor("a", _service("a", trio=True))
    assert trio.declared_state_verbs == list(STATE_VERBS)
    assert trio.supports_states
    part = load_descriptor("b", _service("b", partial=True))
    assert part.declared_state_verbs == ["standby"]
    assert not part.supports_states


# --- fleet_env: RIG_TARGET_STATE is rig-owned (popped unless rig itself sets it) ----------------

def test_fleet_env_pops_inherited_target_state():
    manifest = Manifest(vehicle="t", ros=RosSettings(domain_id=0, rmw="rmw_fastrtps_cpp",
                                                     distro=None), sensors=[], vehicle_id=1)
    old = os.environ.get("RIG_TARGET_STATE")
    os.environ["RIG_TARGET_STATE"] = "standby"  # a leaked shell value must never park a fleet
    try:
        assert "RIG_TARGET_STATE" not in dispatch.fleet_env(manifest)
    finally:
        os.environ.pop("RIG_TARGET_STATE", None) if old is None \
            else os.environ.__setitem__("RIG_TARGET_STATE", old)


# --- standby/activate fan-out: gating, ordering, verb identity ----------------------------------

def _three_service_deployment():
    services = {"trio": _service("trio"), "plain": _service("plain", trio=False),
                "part": _service("part", partial=True)}
    return _deployment(services, [("t0", "trio", 10), ("p0", "plain", 20), ("q0", "part", 30)])


def test_standby_dispatches_only_declared_trio():
    root = _three_service_deployment()
    with _log_env() as log:
        rc, _, err = _run("--root", str(root), "standby")
    assert rc == 0
    lines = _log_lines(log)
    assert lines == ["trio standby rts=unset"]  # plain (undeclared) + part (broken claim) skipped
    assert "no state verbs (always active), skipping: p0" in err
    assert "partial operational-state declaration" in err and "part" in err


def test_explicitly_named_undeclared_stack_is_a_skip_not_an_error():
    root = _three_service_deployment()
    with _log_env() as log:
        rc, _, err = _run("--root", str(root), "activate", "p0")
    assert rc == 0
    assert _log_lines(log) == []
    assert "nothing to do" in err


def test_activate_producers_first_standby_reversed():
    services = {"trio": _service("trio")}
    root = _deployment(services, [("prod", "trio", 10), ("cons", "trio", 20)])
    with _log_env() as log:
        assert _run("--root", str(root), "activate")[0] == 0
        assert _run("--root", str(root), "standby")[0] == 0
    lines = _log_lines(log)
    assert lines[:2] == ["trio activate rts=unset", "trio activate rts=unset"]  # both instances
    # activate ascends (prod before cons); standby descends (cons before prod). The launcher log
    # can't carry instance names (identity comes from the config), so assert via COMPOSE project
    # order — re-run with a launcher that logs its project instead.
    proj_svc = _service("trio", launcher_body=textwrap.dedent("""\
        #!/bin/sh
        CONFIG="$1"; shift
        [ -n "$STATES_LOG" ] && echo "$COMPOSE_PROJECT_NAME $1" >> "$STATES_LOG"
        exit 0
        """))
    root2 = _deployment({"trio": proj_svc}, [("prod", "trio", 10), ("cons", "trio", 20)])
    with _log_env() as log2:
        assert _run("--root", str(root2), "activate")[0] == 0
        assert _run("--root", str(root2), "standby")[0] == 0
    lines2 = _log_lines(log2)
    assert [ln.split()[0].split("-")[0] for ln in lines2] == ["prod", "cons", "cons", "prod"]
    assert [ln.split()[1] for ln in lines2] == ["activate", "activate", "standby", "standby"]


def test_transition_failure_propagates():
    bad = _service("trio", launcher_body="#!/bin/sh\nexit 1\n")
    root = _deployment({"trio": bad}, [("t0", "trio", 10)])
    rc, _, err = _run("--root", str(root), "standby")
    assert rc == 1
    assert "1/1 failed: t0" in err


# --- rig up --standby/--active: the posture token, up-dispatch only -----------------------------

def test_up_standby_exports_target_state_and_bare_up_does_not():
    root = _three_service_deployment()
    with _log_env() as log:
        rc, _, _ = _run("--root", str(root), "up", "--standby")
        assert rc == 0
    ups = [ln for ln in _log_lines(log) if " up " in ln]
    assert ups and all("rts=standby" in ln for ln in ups)  # EVERY launcher sees the token at up
    with _log_env(RIG_TARGET_STATE="standby") as log:  # leaked shell value: popped, never honored
        rc, _, _ = _run("--root", str(root), "up")
        assert rc == 0
        assert all("rts=unset" in ln for ln in _log_lines(log))


def test_up_dry_run_echoes_target_state():
    root = _three_service_deployment()
    _, _, err = _run("--root", str(root), "up", "--active", "--dry-run")
    assert "RIG_TARGET_STATE=active" in err


def test_up_posture_flags_are_mutually_exclusive():
    root = _three_service_deployment()
    rc, _, err = _run("--root", str(root), "up", "--standby", "--active")
    assert rc == 2  # argparse rejects the pair
    assert "not allowed with" in err


def test_transition_verbs_do_not_export_target_state():
    # The token is an `up`-only channel: standby/activate transitions must not carry it (fleet_env
    # pops it; only cmd_up re-adds). The log's rts= field proves the launcher-side view.
    root = _three_service_deployment()
    with _log_env(RIG_TARGET_STATE="active") as log:
        assert _run("--root", str(root), "standby")[0] == 0
    assert _log_lines(log) == ["trio standby rts=unset"]


# --- status: the OP column + additive op_state key ----------------------------------------------

def _status_desc(svc: str, *, trio: bool, ps: str, state_line: str = "", state_rc: int = 0):
    repo = pathlib.Path(tempfile.mkdtemp(prefix=f"states-st-{svc}-"))
    verbs = {"standby": "standby", "activate": "activate", "state": "state"} if trio else {}
    body = ("#!/bin/sh\nCONFIG=\"$1\"; shift\n"
            f"case \"$1\" in\n  ps) cat <<'EOF'\n{ps}\nEOF\n;;\n"
            f"  state) printf '%s\\n' '{state_line}'; exit {state_rc} ;;\nesac\nexit 0\n")
    launcher = repo / f"{svc}-up"
    launcher.write_text(body)
    launcher.chmod(0o755)
    return Descriptor(service=svc, repo=repo, launcher=f"{svc}-up", verbs=verbs, ros_distro=None,
                      external_volumes=[], host_ports=[])


def _sensor(name: str, svc: str) -> Sensor:
    return Sensor(name=name, service=svc, config=pathlib.Path("/dev/null"), enabled=True, order=10)


def test_gather_reads_op_state_for_declaring_services_only():
    ps = '[{"State": "running", "Health": "healthy"}]'
    pairs = [
        (_sensor("a", "asvc"), _status_desc("asvc", trio=True, ps=ps,
                                            state_line='{"state": "standby", "detail": "x"}')),
        (_sensor("b", "bsvc"), _status_desc("bsvc", trio=False, ps=ps)),
        (_sensor("c", "csvc"), _status_desc("csvc", trio=True, ps=ps, state_line="not json")),
        (_sensor("d", "dsvc"), _status_desc("dsvc", trio=True, ps=ps,
                                            state_line='{"state": "REBOOTING"}')),
        (_sensor("e", "esvc"), _status_desc("esvc", trio=True, ps=ps,
                                            state_line='{"state": "active"}', state_rc=1)),
    ]
    env = {**os.environ, "ROS_DOMAIN_ID": "0", "RMW_IMPLEMENTATION": "x"}
    rows = gather(pairs, env)
    assert [r.op_state for r in rows] == ["standby", None, "unknown", "unknown", "unknown"]
    # health is UNAFFECTED by op state — parked reads healthy (the contract's whole point).
    assert rows[0].health == "healthy" and rows[0].state == "running"


def test_render_op_column_next_to_health():
    rows = [Row(_sensor("a", "asvc"), "running", "healthy", 1, 1, [], "standby"),
            Row(_sensor("b", "bsvc"), "running", "healthy", 1, 1, [])]
    table = render(rows)
    header = table.splitlines()[0].split()
    assert header == ["SENSOR", "SERVICE", "STATE", "HEALTH", "OP", "CONTAINERS"]
    body = [ln.split() for ln in table.splitlines()[2:]]
    assert body[0][3:5] == ["healthy", "standby"]
    assert body[1][4] == "-"  # undeclared renders '-', never a hole


def test_as_json_op_state_is_additive():
    manifest = Manifest(vehicle="t", ros=RosSettings(domain_id=0, rmw="x", distro=None),
                        sensors=[], vehicle_id=1)
    rows = [Row(_sensor("a", "asvc"), "running", "healthy", 1, 1, [], "active"),
            Row(_sensor("b", "bsvc"), "down", "-", 0, 0, [])]
    stacks = json.loads(as_json(manifest, rows, None))["stacks"]
    assert stacks[0]["op_state"] == "active"
    assert stacks[1]["op_state"] is None  # declared-nothing = null, distinct from "unknown"
    # the pre-existing contract keys survive untouched alongside the additive one
    assert set(stacks[0]) == {"sensor", "service", "state", "health", "op_state", "running", "total"}


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
