"""`rig fleet` — roster loading, local/ssh fan-out, SIL run-dir view, network lifecycle, sync.
Run: python3 tests/test_fleet_verbs.py

Local rows exercise the REAL code path (subprocess `rig up` on real deployments — no mocks);
ssh/scp are PATH shims (the docker-shim idiom): each logs its FULL argv (SHIM_SSH_LOG /
SHIM_SCP_LOG — the transport-option contract stays checkable), then strips options; ssh runs the
command locally via sh -c (SHIM_SSH_FAIL=<host> => exit 255, the transport-failure branch), scp
copies locally (SHIM_SCP_FAIL=<host> => exit 1, the copy-failure branch).
docker is shimmed to answer `compose ls` with [] and log network verbs to SHIM_DOCKER_LOG.
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

import yaml  # noqa: E402

from rig_cli.cli import main  # noqa: E402

_REPO = pathlib.Path(__file__).resolve().parent.parent

_SSH_SHIM = """\
#!/bin/sh
[ -n "$SHIM_SSH_LOG" ] && echo "$@" >> "$SHIM_SSH_LOG"
while [ $# -gt 0 ]; do
  case "$1" in
    -o) shift 2 ;;
    -*) shift ;;
    *) break ;;
  esac
done
host="$1"; shift
if [ "$SHIM_SSH_FAIL" = "$host" ]; then echo "ssh: connect to host $host: refused" >&2; exit 255; fi
sh -c "$*"
"""

_SCP_SHIM = """\
#!/bin/sh
[ -n "$SHIM_SCP_LOG" ] && echo "$@" >> "$SHIM_SCP_LOG"
while [ $# -gt 0 ]; do
  case "$1" in
    -o) shift 2 ;;
    -*) shift ;;
    *) break ;;
  esac
done
if [ -n "$SHIM_SCP_FAIL" ]; then
  case "$1 $2" in
    *"$SHIM_SCP_FAIL":*) echo "scp: connect to host $SHIM_SCP_FAIL: refused" >&2; exit 1 ;;
  esac
fi
src="${1#*:}"; dst="${2#*:}"
cp -r "$src" "$dst"
"""

_DOCKER_SHIM = """\
#!/bin/sh
[ -n "$SHIM_DOCKER_LOG" ] && echo "$@" >> "$SHIM_DOCKER_LOG"
if [ "$1" = "compose" ] && [ "$2" = "ls" ]; then echo "[]"; exit 0; fi
if [ "$1" = "network" ] && [ "$2" = "inspect" ]; then
  [ "$SHIM_NET_EXISTS" = "1" ] && exit 0 || exit 1
fi
exit 0
"""


def _shim_dir() -> str:
    d = pathlib.Path(tempfile.mkdtemp())
    for name, body in (("ssh", _SSH_SHIM), ("scp", _SCP_SHIM), ("docker", _DOCKER_SHIM)):
        (d / name).write_text(body)
        (d / name).chmod(0o755)
    (d / "rig").write_text(f"#!/bin/sh\nexec python3 {_REPO / 'rig'} \"$@\"\n")
    (d / "rig").chmod(0o755)
    return str(d)


os.environ["PATH"] = _shim_dir() + ":" + os.environ["PATH"]
os.environ["RIG_VEHICLE_LOCAL"] = str(pathlib.Path(tempfile.mkdtemp()) / "absent.yaml")


@contextlib.contextmanager
def _env(**over):
    old = {k: os.environ.get(k) for k in over}
    for k, v in over.items():
        os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, str(v))
    try:
        yield
    finally:
        for k, v in old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def _run(*argv) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = main(list(argv))
        except SystemExit as exc:  # argparse
            rc = int(exc.code or 0)
    return rc, out.getvalue(), err.getvalue()


def _sil_tree(name: str) -> pathlib.Path:
    """A fleet-artifact-style deployment: templated identity + data_dir, one no-op stub stack."""
    root = pathlib.Path(tempfile.mkdtemp()) / name
    svc = pathlib.Path(tempfile.mkdtemp()) / "simsvc"
    svc.mkdir(parents=True)
    (svc / "rigging.yaml").write_text("service: simsvc\nlauncher: sim-up\ntier: infra\n"
                                      "launch_surface: [sim-up]\n")
    (svc / "sim-up").write_text('#!/bin/sh\n'  # env dump lands in the DEPLOYMENT root ($1=config)
                                'echo "$RIG_NETWORK $RIG_VEHICLE_IP" >> "$(dirname "$1")/../../env.log"\n'
                                'exit 0\n')
    (svc / "sim-up").chmod(0o755)
    (root / "config" / "infra").mkdir(parents=True)
    (root / "vehicle.yaml").write_text(textwrap.dedent("""\
        vehicle: "{{vehicle}}"
        vehicle_id: "{{vehicle_id}}"
        data_dir: "{{data_dir}}"
        infra:
          - {name: sim, service: simsvc, config: config/infra/sim.yaml}
        """))
    (root / "config" / "infra" / "sim.yaml").write_text("service: simsvc\nname: sim\nrate: 1\n")
    (root / "services.yaml").write_text(f"services:\n  simsvc: {{ path: {svc} }}\n")
    return root


def _fleet_yaml(rows: list[dict], *, mode="sil", data_root=None, network=None,
                gcs="10.0.0.10") -> pathlib.Path:
    doc: dict = {"fleet": "testfleet", "mode": mode, "gcs_ip": gcs, "vehicles": rows}
    sil = {}
    if data_root:
        sil["data_root"] = str(data_root)
    if network:
        sil["network"] = network
    if sil:
        doc["sil"] = sil
    path = pathlib.Path(tempfile.mkdtemp()) / "fleet.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    return path


def test_status_format_json_is_the_machine_contract():
    root = pathlib.Path(tempfile.mkdtemp()) / "veh"
    root.mkdir(parents=True)
    (root / "vehicle.yaml").write_text("vehicle: t\nvehicle_id: 4\nsensors: []\n")
    (root / "services.yaml").write_text("services: {}\n")
    rc, out, _ = _run("--root", str(root), "status", "--format", "json")
    assert rc == 0
    doc = json.loads(out)
    assert doc["vehicle"] == "t" and doc["vehicle_id"] == 4
    assert doc["stacks"] == [] and doc["run"] is None


def test_roster_loader_validation():
    from rig_cli import RigError
    from rig_cli.fleet import load_fleet
    base = {"id": 1, "name": "a", "path": "/x"}
    for doc, msg in (
        ({"fleet": "f", "bogus": 1, "vehicles": [base]}, "unknown key"),
        ({"fleet": "f", "mode": "sim", "vehicles": [base]}, "mode must be"),
        ({"fleet": "f", "mode": "field", "sil": {"data_root": "/d"},
          "vehicles": [base]}, "requires `mode: sil`"),
        ({"fleet": "f", "mode": "sil", "sil": {"bogus": 1}, "vehicles": [base]}, "carries only"),
        ({"fleet": "f", "mode": "sil", "sil": {"network": {"name": "n"}},
          "vehicles": [base]}, "sil.network needs"),
        ({"fleet": "f", "vars": [1, 2], "vehicles": [base]}, "must be a mapping"),
        ({"fleet": "f", "vehicles": [{"name": "a"}]}, "needs id, name, path"),
        ({"fleet": "f", "vehicles": [dict(base, extra=1)]}, "unknown key"),
        ({"fleet": "f", "vehicles": []}, "no vehicles"),
        ({"fleet": "f", "vehicles": [base, dict(base)]}, "duplicate"),
    ):
        p = pathlib.Path(tempfile.mkdtemp()) / "fleet.yaml"
        p.write_text(yaml.safe_dump(doc))
        try:
            load_fleet(str(p))
            raise AssertionError(f"expected RigError for {doc}")
        except RigError as exc:
            assert msg in str(exc), f"{doc}: {exc}"
    # The miss branches, without leaning on the tmpdir's ancestors (a stray /tmp/fleet.yaml on
    # someone's box must never fail this test): explicit path and $RIG_FLEET are fully controlled…
    try:
        load_fleet(str(pathlib.Path(tempfile.mkdtemp()) / "absent" / "fleet.yaml"))
        raise AssertionError("expected RigError for a missing explicit --fleet path")
    except RigError as exc:
        assert "not found" in str(exc)
    with _env(RIG_FLEET=str(pathlib.Path(tempfile.mkdtemp()) / "gone.yaml")):
        try:
            load_fleet(None)
            raise AssertionError("expected RigError for a dangling $RIG_FLEET")
        except RigError as exc:
            assert "$RIG_FLEET" in str(exc)
    # …and the walk-miss pointer is only asserted when NO ancestor carries a roster (the walk
    # ends at /, which no tmpdir choice can control).
    with _env(RIG_FLEET=None):
        cwd = os.getcwd()
        nest = pathlib.Path(tempfile.mkdtemp()).resolve() / "deep" / "er"
        nest.mkdir(parents=True)
        os.chdir(nest)
        try:
            if not any((d / "fleet.yaml").is_file() for d in (nest, *nest.parents)):
                try:
                    load_fleet(None)
                    raise AssertionError("expected RigError")
                except RigError as exc:
                    assert "CHEATSHEET" in str(exc)
        finally:
            os.chdir(cwd)


def test_fleet_list_marks_unreachable_and_missing():
    a, b = _sil_tree("veh_a"), _sil_tree("veh_b")
    fy = _fleet_yaml([{"id": 1, "name": "veh_a", "path": str(a)},
                      {"id": 2, "name": "veh_b", "path": str(b)},
                      {"id": 3, "name": "ghost", "path": "/nope/nowhere"}],
                     mode="field")
    rc, out, _ = _run("fleet", "list", "--fleet", str(fy))
    assert rc == 1                                     # one bad row => nonzero
    assert "veh_a" in out and "ok" in out and "no deployment at path" in out
    with _env(SHIM_SSH_FAIL="farhost"):
        fy2 = _fleet_yaml([{"id": 1, "name": "veh_a", "path": str(a)},
                           {"id": 9, "name": "far", "host": "farhost", "path": str(b)}],
                          mode="field")
        rc, out, _ = _run("fleet", "list", "--fleet", str(fy2))
        assert rc == 1 and "UNREACHABLE" in out and "ok" in out  # sweep continued


def test_fleet_up_sil_view_snapshot_and_sync_round_trip():
    a, b = _sil_tree("veh3"), _sil_tree("veh9")
    data_root = pathlib.Path(tempfile.mkdtemp()) / "sil-data"
    fy = _fleet_yaml([{"id": 3, "name": "veh3", "path": str(a)},
                      {"id": 9, "name": "veh9", "path": str(b)}],
                     data_root=data_root)
    rc, _, err = _run("fleet", "up", "--fleet", str(fy), "--run", "silcheck", "-j", "1")
    assert rc == 0, err
    runs = {}
    for name, vid in (("veh3", "3"), ("veh9", "9")):
        run_dirs = list((data_root / name / "runs").iterdir())     # per-vehicle registry
        assert len(run_dirs) == 1 and run_dirs[0].name.endswith("_silcheck")
        runs[name] = run_dirs[0]
        doc = yaml.safe_load((run_dirs[0] / "manifest.yaml").read_text())
        assert str(doc["vehicle_id"]) == vid                       # roster identity applied
        snap = run_dirs[0] / ".rig" / "config" / doc["config"]
        vars_doc = yaml.safe_load((snap / "vars.yaml").read_text())
        assert vars_doc["vars"]["fleet_mode"] == "sil"             # SIL vs field, recorded
        assert vars_doc["vars"]["data_dir"] == str(data_root / name)  # per-vehicle subfolder
        assert (snap / "fleet.yaml").is_file()                     # pushed roster captured
        assert (pathlib.Path(a if name == "veh3" else b) / "fleet.yaml").is_file()
    view = next((data_root / "runs").iterdir())                    # the shared-run-dir VIEW
    assert view.name.startswith("silcheck-")
    for name in ("veh3", "veh9"):
        assert (view / name).is_symlink()
        assert (view / name).resolve() == runs[name].resolve()     # live link into the registry
    assert (view / "fleet.yaml").is_file()                         # fleet-level provenance
    # seal + harvest: sync materializes the SAME layout the view shows live
    rc, _, err = _run("fleet", "down", "--fleet", str(fy), "--end-run", "-j", "1")
    assert rc == 0, err
    into = pathlib.Path(tempfile.mkdtemp()) / "harvest"
    rc, _, err = _run("fleet", "sync", "--fleet", str(fy), "--into", str(into))
    assert rc == 0, err
    for name in ("veh3", "veh9"):
        dest = into / "silcheck" / name / runs[name].name
        assert (dest / "manifest.yaml").is_file()
        assert "ended:" in (dest / "manifest.yaml").read_text()
    rc, _, err = _run("fleet", "sync", "--fleet", str(fy), "--into", str(into))
    assert rc == 0 and "0 run(s) pulled" in err                    # idempotent re-sync


def test_fleet_up_network_lifecycle_and_env_contract():
    a = _sil_tree("veh1")
    data_root = pathlib.Path(tempfile.mkdtemp()) / "sil-data"
    log = pathlib.Path(tempfile.mkdtemp()) / "docker.log"
    fy = _fleet_yaml([{"id": 1, "name": "veh1", "path": str(a), "ip": "10.99.0.1"}],
                     data_root=data_root,
                     network={"name": "rig-sil", "subnet": "10.99.0.0/24"})
    with _env(SHIM_DOCKER_LOG=str(log), SHIM_NET_EXISTS="0"):
        rc, _, err = _run("fleet", "up", "--fleet", str(fy), "--run", "net1", "-j", "1")
        assert rc == 0, err
        assert "network create --subnet 10.99.0.0/24 rig-sil" in log.read_text()
    with _env(SHIM_DOCKER_LOG=str(log), SHIM_NET_EXISTS="1"):   # second up: idempotent
        rc, _, err = _run("fleet", "up", "--fleet", str(fy), "--run", "net1", "-j", "1")
        assert rc == 0, err
        assert log.read_text().count("network create") == 1
        rc, _, err = _run("fleet", "down", "--fleet", str(fy), "-j", "1")
        assert rc == 0, err
        assert "network rm rig-sil" in log.read_text()          # full-fleet down removes it
    body = (a / "env.log").read_text() if (a / "env.log").is_file() else ""
    assert "rig-sil" in body and "10.99.0.1" in body            # RIG_NETWORK/RIG_VEHICLE_IP
    #                                                             reached the stack's launcher


def test_fleet_status_aggregates_and_survives_unreachable():
    a = _sil_tree("veh_a")
    data_root = pathlib.Path(tempfile.mkdtemp()) / "sil-data"
    fy = _fleet_yaml([{"id": 1, "name": "veh_a", "path": str(a)},
                      {"id": 2, "name": "far", "host": "downhost", "path": str(a)}],
                     data_root=data_root)
    assert _run("fleet", "up", "--fleet", str(fy), "veh_a", "-j", "1")[0] == 0  # mint identity
    with _env(SHIM_SSH_FAIL="downhost"):
        rc, out, _ = _run("fleet", "status", "--fleet", str(fy))
        assert rc == 1                                          # the marred row
        assert "veh_a" in out and "0/1 up" in out               # real aggregate from real json
        assert "UNREACHABLE" in out                             # …and the sweep completed


def test_fleet_up_fails_soft_over_unreachable_row():
    """The MUTATING verbs share `list`/`status`'s DDIL posture: an unreachable row mars its line,
    never the sweep — the healthy vehicle's run still opens, and ssh rode BatchMode (never a
    credential prompt hanging a field fan-out)."""
    a, b = _sil_tree("veh_a"), _sil_tree("veh_b")
    data_root = pathlib.Path(tempfile.mkdtemp()) / "sil-data"
    log = pathlib.Path(tempfile.mkdtemp()) / "ssh.log"
    fy = _fleet_yaml([{"id": 1, "name": "veh_a", "path": str(a)},
                      {"id": 2, "name": "far", "host": "downhost", "path": str(b)}],
                     data_root=data_root)
    with _env(SHIM_SSH_FAIL="downhost", SHIM_SSH_LOG=str(log)):
        rc, _, err = _run("fleet", "up", "--fleet", str(fy), "--run", "soft1", "-j", "1")
    assert rc == 1                                              # the marred row
    assert "UNREACHABLE" in err and "1/2 ok" in err             # …but the sweep completed
    run_dirs = list((data_root / "veh_a" / "runs").iterdir())
    assert len(run_dirs) == 1 and run_dirs[0].name.endswith("_soft1")  # survivor's run opened
    view = next((data_root / "runs").iterdir())
    assert (view / "veh_a").is_symlink() and not (view / "far").exists()
    assert "BatchMode=yes" in log.read_text()                   # the transport contract, verbatim


def test_fleet_up_view_links_only_survivors_and_down_fails_soft():
    """fleet.py:461's filter: a LOCAL row whose up FAILED must not lend its (stale) `current` to
    the new label's view — only rc==0 vehicles are linked. Same fail-soft shape for down."""
    a, b = _sil_tree("veh_a"), _sil_tree("veh_b")
    data_root = pathlib.Path(tempfile.mkdtemp()) / "sil-data"
    fy = _fleet_yaml([{"id": 1, "name": "veh_a", "path": str(a)},
                      {"id": 2, "name": "veh_b", "path": str(b)}],
                     data_root=data_root)
    rc, _, err = _run("fleet", "up", "--fleet", str(fy), "--run", "one", "-j", "1")
    assert rc == 0, err                                         # both rows healthy; both linked
    (b / "vehicle.yaml").write_text("vehicle: [broken\n")       # veh_b's next up fails…
    rc, _, err = _run("fleet", "up", "--fleet", str(fy), "--run", "two", "-j", "1")
    assert rc == 1 and "1/2 ok" in err                          # …softly
    assert any(d.name.endswith("_two") for d in (data_root / "veh_a" / "runs").iterdir())
    two = next(v for v in (data_root / "runs").iterdir() if v.name.startswith("two-"))
    assert (two / "veh_a").is_symlink()
    assert not (two / "veh_b").exists()          # veh_b still HAS `current` (run one) — filtered
    rc, _, err = _run("fleet", "down", "--fleet", str(fy), "--end-run", "-j", "1")
    assert rc == 1 and "1/2 ok" in err                          # down mars the row, not the sweep
    a_two = next(d for d in (data_root / "veh_a" / "runs").iterdir() if d.name.endswith("_two"))
    assert "ended:" in (a_two / "manifest.yaml").read_text()    # the healthy vehicle still sealed


def test_fleet_up_var_forwarding_and_malformed_var():
    a = _sil_tree("veh_a")
    data_root = pathlib.Path(tempfile.mkdtemp()) / "sil-data"
    fy = _fleet_yaml([{"id": 1, "name": "veh_a", "path": str(a)}], data_root=data_root)
    rc, _, err = _run("fleet", "up", "--fleet", str(fy), "--run", "varfwd",
                      "--var", "mission_tag=alpha7", "-j", "1")
    assert rc == 0, err
    run_dir = next((data_root / "veh_a" / "runs").iterdir())
    doc = yaml.safe_load((run_dir / "manifest.yaml").read_text())
    snap = run_dir / ".rig" / "config" / doc["config"]
    vars_doc = yaml.safe_load((snap / "vars.yaml").read_text())
    assert vars_doc["vars"]["mission_tag"] == "alpha7"          # RIG_VAR_* landed in provenance
    rc, _, err = _run("fleet", "up", "--fleet", str(fy), "--var", "novalue")
    assert rc == 1 and "k=v" in err                             # malformed --var errors up front


def test_vars_source_explicit_beats_derived_and_rejects_unknown_keys():
    """The VEHICLE-side read (fleet.py vars_source): fleet_ids derive from the roster, an
    explicit fleet-level vars: overrides them, and unknown top-level keys stay loud."""
    from rig_cli import RigError
    from rig_cli.fleet import vars_source
    p = pathlib.Path(tempfile.mkdtemp()) / "fleet.yaml"
    p.write_text(yaml.safe_dump({"fleet": "f", "mode": "sil", "gcs_ip": "10.0.0.10",
                                 "vehicles": [{"id": 3}, {"id": 9}]}))
    doc = vars_source(p)["vars"]
    assert doc["fleet_ids"] == [3, 9]                           # roster-DERIVED
    assert doc["gcs_ip"] == "10.0.0.10" and doc["fleet_mode"] == "sil"
    p.write_text(yaml.safe_dump({"fleet": "f", "mode": "sil",
                                 "vehicles": [{"id": 3}, {"id": 9}],
                                 "vars": {"fleet_ids": [1, 2, 7]}}))
    assert vars_source(p)["vars"]["fleet_ids"] == [1, 2, 7]     # explicit beats derived
    p.write_text(yaml.safe_dump({"fleet": "f", "surprise": 1, "vehicles": [{"id": 3}]}))
    try:
        vars_source(p)
        raise AssertionError("expected RigError for an unknown top-level key")
    except RigError as exc:
        assert "unknown key" in str(exc)


def test_fleet_vars_flow_and_reboot_persistence():
    """The v0.1.68 tier: peer_endpoint lives ONLY in fleet.yaml, fleet_ids ONLY in the roster —
    after `fleet up`, each tree renders mutual peers, the snapshot records the roster-derived
    fleet_ids, and a REBOOT-style plain `rig up` (no fleet env) still sees the fleet values."""
    trees = {}
    for name in ("veh3", "veh9"):
        root = _sil_tree(name)
        cfg = root / "config" / "infra" / "router.yaml"
        cfg.write_text('service: simsvc\nname: routerx\n'
                       'connect: "{{map fleet_peer_ids peer_endpoint}}"\n')
        veh = root / "vehicle.yaml"
        veh.write_text(veh.read_text().replace(
            "infra:", "infra:\n  - {name: routerx, service: simsvc, "
            "config: config/infra/router.yaml}"))
        trees[name] = root
    data_root = pathlib.Path(tempfile.mkdtemp()) / "sil-data"
    fy = _fleet_yaml([{"id": 3, "name": "veh3", "path": str(trees["veh3"])},
                      {"id": 9, "name": "veh9", "path": str(trees["veh9"])}],
                     data_root=data_root)
    fy.write_text(fy.read_text() + "vars: {peer_endpoint: 'tcp/10.0.0.{}:7447'}\n")
    rc, _, err = _run("fleet", "up", "--fleet", str(fy), "--run", "vars1", "-j", "1")
    assert rc == 0, err
    for name, vid, peer in (("veh3", "3", "tcp/10.0.0.9:7447"),
                            ("veh9", "9", "tcp/10.0.0.3:7447")):
        run_dir = next((data_root / name / "runs").iterdir())
        doc = yaml.safe_load((run_dir / "manifest.yaml").read_text())
        snap = run_dir / ".rig" / "config" / doc["config"]
        rendered = yaml.safe_load((snap / "rendered" / "routerx.yaml").read_text())
        assert rendered["connect"] == [peer]                     # mutual peers from ONE roster
        vars_doc = yaml.safe_load((snap / "vars.yaml").read_text())
        assert vars_doc["vars"]["fleet_ids"] == [3, 9]           # roster-derived, in provenance
    # reboot simulation: plain `rig up` in the tree, identity file only — no fleet env at all
    import subprocess
    identity = data_root / ".identity" / "veh3.yaml"
    env = {**os.environ, "RIG_VEHICLE_LOCAL": str(identity)}
    env = {k: v for k, v in env.items() if not k.startswith("RIG_VAR_")}
    proc = subprocess.run(["rig", "--root", str(trees["veh3"]), "up"],
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    run_dirs = sorted((data_root / "veh3" / "runs").iterdir())
    doc = yaml.safe_load((run_dirs[-1] / "manifest.yaml").read_text())
    snap = run_dirs[-1] / ".rig" / "config" / doc["config"]
    rendered = yaml.safe_load((snap / "rendered" / "routerx.yaml").read_text())
    assert rendered["connect"] == ["tcp/10.0.0.9:7447"]          # the PUSHED fleet.yaml persisted


def test_fleet_sync_skips_open_runs():
    a = _sil_tree("veh_a")
    dd = pathlib.Path(tempfile.mkdtemp()) / "data"
    (dd / "runs" / "20260101T000000Z_open").mkdir(parents=True)
    (dd / "runs" / "20260101T000000Z_open" / "manifest.yaml").write_text(
        "run: 20260101T000000Z_open\nstarted: '2026-01-01'\n")   # no ended: => OPEN
    (dd / "runs" / "20260102T000000Z_done").mkdir(parents=True)
    (dd / "runs" / "20260102T000000Z_done" / "manifest.yaml").write_text(
        "run: 20260102T000000Z_done\nended: '2026-01-02'\n")
    fy = _fleet_yaml([{"id": 1, "name": "veh_a", "path": str(a), "data_dir": str(dd)}],
                     mode="field")
    into = pathlib.Path(tempfile.mkdtemp()) / "harvest"
    rc, _, err = _run("fleet", "sync", "--fleet", str(fy), "--into", str(into))
    assert rc == 0
    assert "not sealed" in err                                   # OPEN skipped, with the reason
    assert (into / "done" / "veh_a" / "20260102T000000Z_done" / "manifest.yaml").is_file()
    assert not (into / "open").exists()


def test_fleet_sync_label_filter_and_scp_fail_soft():
    """--label pulls the label AND its same-second collision suffixes (`dock-2`), never other
    labels; a failing remote copy mars its row while the rest of the harvest lands (DDIL)."""
    a = _sil_tree("veh_l")
    dl = pathlib.Path(tempfile.mkdtemp()) / "dl"
    for rid in ("20260101T000000Z_dock", "20260101T000010Z_dock-2", "20260102T000000Z_survey"):
        (dl / "runs" / rid).mkdir(parents=True)
        (dl / "runs" / rid / "manifest.yaml").write_text(f"run: {rid}\nended: '2026-01-02'\n")
    dr = pathlib.Path(tempfile.mkdtemp()) / "dr"
    (dr / "runs" / "20260103T000000Z_dock").mkdir(parents=True)
    (dr / "runs" / "20260103T000000Z_dock" / "manifest.yaml").write_text(
        "run: 20260103T000000Z_dock\nended: '2026-01-03'\n")
    fy = _fleet_yaml([{"id": 1, "name": "veh_l", "path": str(a), "data_dir": str(dl)},
                      {"id": 2, "name": "far", "host": "farhost", "path": str(a),
                       "data_dir": str(dr)}], mode="field")
    into = pathlib.Path(tempfile.mkdtemp()) / "harvest"
    log = pathlib.Path(tempfile.mkdtemp()) / "scp.log"
    with _env(SHIM_SCP_FAIL="farhost", SHIM_SCP_LOG=str(log)):
        rc, _, err = _run("fleet", "sync", "--fleet", str(fy), "--label", "dock",
                          "--into", str(into))
    assert rc == 1 and "scp failed" in err                       # the marred remote row…
    assert "2 run(s) pulled" in err                              # …and the local rows landed
    assert (into / "dock" / "veh_l" / "20260101T000000Z_dock" / "manifest.yaml").is_file()
    assert (into / "dock-2" / "veh_l" / "20260101T000010Z_dock-2").is_dir()  # suffix matched
    assert not (into / "survey").exists()                        # other label filtered out
    assert not (into / "dock" / "far" / "20260103T000000Z_dock").exists()
    assert "BatchMode=yes" in log.read_text()                    # scp rode the same ssh options


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
