"""Fleet artifacts + rig provision: auto-detection, unresolved staging, one-artifact-N-vehicles,
identity gate. Run: python3 tests/test_fleet.py"""
import contextlib
import io
import os
import pathlib
import sys
import tarfile
import tempfile
import textwrap

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from rig_cli import RigError  # noqa: E402
from rig_cli.bake import fleet_refs, is_fleet  # noqa: E402
from rig_cli.cli import main  # noqa: E402
from rig_cli.manifest import load_manifest  # noqa: E402
from rig_cli.resolve import materialize_manifest  # noqa: E402


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
        rc = main(list(argv))
    return rc, out.getvalue(), err.getvalue()


def _fleet_deployment() -> pathlib.Path:
    """A deployment with mandatory identity markers + one templated sensor + a routed service."""
    root = pathlib.Path(tempfile.mkdtemp()) / "veh"
    svc = pathlib.Path(tempfile.mkdtemp()) / "camsvc"
    (svc / "config").mkdir(parents=True)
    (svc / "rigging.yaml").write_text("service: camsvc\nlauncher: camsvc-up\n"
                                      "launch_surface: [camsvc-up]\n")
    (svc / "camsvc-up").write_text("#!/bin/sh\n")
    (svc / "camsvc-up").chmod(0o755)
    (root / "config" / "sensors").mkdir(parents=True)
    (root / "vehicle.yaml").write_text(textwrap.dedent(f"""\
        vehicle: "{{{{vehicle}}}}"
        vehicle_id: "{{{{vehicle_id}}}}"
        vars: {{rtsp_port: 8554}}
        sensors:
          - {{name: cam, service: camsvc, config: config/sensors/cam.yaml}}
        """))
    (root / "config" / "sensors" / "cam.yaml").write_text(
        "service: camsvc\nname: cam\n"
        "rtsp: {url: 'rtsp://10.160.{{vehicle_id}}.80:{{rtsp_port}}/main'}\n")
    (root / "services.yaml").write_text(f"services:\n  camsvc: {{ path: {svc} }}\n")
    return root


def test_fleet_detection():
    root = _fleet_deployment()
    assert is_fleet(root)
    assert fleet_refs(root) == {"vehicle", "vehicle_id", "rtsp_port"}
    plain = pathlib.Path(tempfile.mkdtemp())
    (plain / "vehicle.yaml").write_text("vehicle: t\nvehicle_id: 1\nsensors: []\n")
    assert not is_fleet(plain)


def test_fleet_bake_stages_unresolved_and_flags_reject():
    root = _fleet_deployment()
    with _env(RIG_VEHICLE_LOCAL=str(pathlib.Path(tempfile.mkdtemp()) / "none.yaml")):
        rc, _, err = _run("--root", str(root), "artifact", "bake", "--tag", "f1")
        assert rc == 0, err
        assert "FLEET artifact" in err
        staging = root / "var" / "bake" / "f1"
        assert "{{vehicle_id}}" in (staging / "vehicle.yaml").read_text()      # UNRESOLVED
        assert "{{rtsp_port}}" in (staging / "config" / "sensors" / "cam.yaml").read_text()
        assert (staging / "provision.sh").is_file()
        assert (staging / "services" / "camsvc" / "rigging.yaml").is_file()    # vendored
        assert not (staging / "up.sh").exists()                                # no compose-only form
        example = (staging / "vehicle.local.example.yaml").read_text()
        assert "vehicle_id: 1            # REQUIRED" in example
        assert "rtsp_port: 8554" in example and "optional" in example
        meta = yaml.safe_load((staging / "metadata.yaml").read_text())
        assert meta["fleet"] is True and meta["vars"] == ["rtsp_port", "vehicle", "vehicle_id"]
        rc, _, err = _run("--root", str(root), "artifact", "bake", "--tag", "f2", "--bundle-images")
        assert rc == 1 and "compose-only" in err
        rc, _, err = _run("--root", str(root), "artifact", "bake", "--tag", "f3", "--registry", "x:5000")
        assert rc == 1 and "per-vehicle" in err


def test_one_artifact_two_vehicles():
    root = _fleet_deployment()
    with _env(RIG_VEHICLE_LOCAL=str(pathlib.Path(tempfile.mkdtemp()) / "none.yaml")):
        rc, _, err = _run("--root", str(root), "artifact", "bake", "--tag", "f1")
        assert rc == 0, err
    artifact = root / "var" / "artifacts" / "f1.tar.gz"
    urls = {}
    for vid in (3, 9):
        vehicle_home = pathlib.Path(tempfile.mkdtemp())
        with tarfile.open(artifact) as tf:
            tf.extractall(vehicle_home, filter="data")
        tree = vehicle_home / "f1"
        machine = vehicle_home / "machine.yaml"
        machine.write_text(f"vehicle: skiff-{vid:02d}\nvehicle_id: {vid}\n")
        with _env(RIG_VEHICLE_LOCAL=str(machine)):
            m = load_manifest(tree)
            assert m.vehicle == f"skiff-{vid:02d}" and m.vehicle_id == vid
            assert m.ros.domain_id == vid
            mm = materialize_manifest(m, tree)
            cfg = yaml.safe_load(pathlib.Path(mm.sensors[0].config).read_text())
            urls[vid] = cfg["rtsp"]["url"]
    assert urls == {3: "rtsp://10.160.3.80:8554/main", 9: "rtsp://10.160.9.80:8554/main"}


def test_unprovisioned_vehicle_gates_identity_consumers_only():
    root = _fleet_deployment()
    with _env(RIG_VEHICLE_LOCAL=str(pathlib.Path(tempfile.mkdtemp()) / "none.yaml"),
              RIG_VEHICLE_ID=None, RIG_VEHICLE_NAME=None):
        rc, _, err = _run("--root", str(root), "up", "--dry-run")      # consumes identity -> gated
        assert rc == 1 and "rig provision" in err
        rc, out, _ = _run("--root", str(root), "pkg", "list")          # management verb -> works
        assert rc == 0 and "no registry packages installed" in out
        rc, out, _ = _run("--root", str(root), "config", "diff")       # raw-file diff -> works
        assert rc == 0


def test_provision_write_show_check_and_force_gate():
    machine = pathlib.Path(tempfile.mkdtemp()) / "id.yaml"
    with _env(RIG_VEHICLE_LOCAL=str(machine)):
        rc, _, err = _run("provision")                                  # show, unprovisioned
        assert rc == 0 and "NOT PROVISIONED" in err
        rc, _, err = _run("provision", "--id", "7", "--name", "skiff-07",
                          "--var", "camera_ip=10.160.7.25")
        assert rc == 0, err
        data = yaml.safe_load(machine.read_text())
        assert data == {"vehicle_id": 7, "vehicle": "skiff-07", "vars": {"camera_ip": "10.160.7.25"}}
        rc, _, err = _run("provision", "--var", "extra=1")               # vars-only: no identity change
        assert rc == 0, err
        rc, _, err = _run("provision", "--id", "8")                      # re-identify without --force
        assert rc == 1 and "ORPHANING" in err
        rc, _, err = _run("provision", "--id", "8", "--force")
        assert rc == 0 and yaml.safe_load(machine.read_text())["vehicle_id"] == 8
        rc, _, err = _run("provision", "--id", "x")
        assert rc == 1 and "numeric" in err


def test_provision_show_checks_deployment():
    root = _fleet_deployment()
    machine = pathlib.Path(tempfile.mkdtemp()) / "id.yaml"
    with _env(RIG_VEHICLE_LOCAL=str(machine)):
        rc, _, err = _run("--root", str(root), "provision")
        assert rc == 1 and "NOT satisfied" in err
        _run("provision", "--id", "4", "--name", "skiff")
        rc, _, err = _run("--root", str(root), "provision")
        assert rc == 0 and "all vars satisfied" in err and "vehicle_id=4" in err


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
