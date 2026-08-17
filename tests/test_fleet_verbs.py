"""`rig fleet` — roster loading, local/ssh fan-out, SIL run-dir view, network lifecycle, sync.
Run: python3 tests/test_fleet_verbs.py

Local rows exercise the REAL code path (subprocess `rig up` on real deployments — no mocks);
ssh/scp are PATH shims (the docker-shim idiom): ssh strips options and runs the command locally
via sh -c (SHIM_SSH_FAIL=<host> => exit 255, the transport-failure branch), scp copies locally.
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
while [ $# -gt 0 ]; do
  case "$1" in
    -o) shift 2 ;;
    -*) shift ;;
    *) break ;;
  esac
done
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
    with _env(RIG_FLEET=None):  # no file anywhere -> the convention pointer
        cwd = os.getcwd()
        os.chdir(tempfile.mkdtemp())
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
