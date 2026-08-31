"""run capture + reconstruct/retrofit (rig-reconstruct-plan). Run: python3 tests/test_reconstruct.py

A mini deployment fixture (fake vendorable service repos, real vendor/bake staging code) drives
the LEAN capture for real; reconstruct/retrofit run against real tarballs and real content-
addressed snapshots. Docker is absent on CI — image-digest capture must degrade to nulls, never
fail (that IS one of the tests).
"""
import io
import contextlib
import pathlib
import sys
import tarfile
import tempfile
import textwrap

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import rig_cli.bake as bake  # noqa: E402
from rig_cli import RigError, reconstruct, runs  # noqa: E402
from rig_cli.common import load_yaml  # noqa: E402
from rig_cli.manifest import Manifest, RosSettings, Sensor  # noqa: E402


def _service_repo(base: pathlib.Path, name: str) -> pathlib.Path:
    repo = base / f"{name}-repo"
    repo.mkdir(parents=True)
    (repo / "rigging.yaml").write_text(textwrap.dedent(f"""\
        service: {name}
        launcher: {name}-up
        launch_surface: [{name}-up]
        """))
    (repo / f"{name}-up").write_text("#!/bin/sh\nexit 0\n")
    return repo


def _deployment(*, run_capture=True):
    """(root, manifest) — two services (one row disabled), path-routed like a real dev tree."""
    base = pathlib.Path(tempfile.mkdtemp())
    root = base / "deploy"
    (root / "config" / "sensors").mkdir(parents=True)
    repo_a = _service_repo(base, "sensa")
    repo_b = _service_repo(base, "playr")
    (root / "services.yaml").write_text(textwrap.dedent(f"""\
        services:
          sensa: {{ path: {repo_a} }}
          playr: {{ path: {repo_b} }}
        """))
    cfg_a = root / "config" / "sensors" / "cam.yaml"
    cfg_a.write_text("service: sensa\nname: cam\nvalue: original\n")
    cfg_b = root / "config" / "sensors" / "bag_player.yaml"
    cfg_b.write_text("service: playr\nname: bag_player\n")
    rows = [Sensor(name="cam", service="sensa", config=cfg_a, enabled=True, order=10),
            Sensor(name="bag_player", service="playr", config=cfg_b, enabled=False, order=999,
                   tier="autonomy")]
    manifest = Manifest(vehicle="veh", vehicle_id=1, sensors=rows, data_dir=str(base / "data"),
                        run_capture=run_capture,
                        ros=RosSettings(domain_id=1, rmw="rmw_zenoh_cpp", distro=None))
    return root, manifest


def _open(manifest, root, label="t"):
    data = pathlib.Path(manifest.data_dir)
    data.mkdir(parents=True, exist_ok=True)
    return runs._open_run(manifest, root, data, label), data


def test_capture_rides_open_and_stamps_manifest():
    root, m = _deployment()
    rid, data = _open(m, root)
    run_dir = data / "runs" / rid
    doc = load_yaml(run_dir / "manifest.yaml")
    tar = run_dir / ".rig" / "artifact.tar.gz"
    assert tar.is_file() and doc["capture"]["sha256"] == bake._sha256(tar)
    with tarfile.open(tar) as tf:
        names = tf.getnames()
    # disabled row's surface AND config are inside; rig is bundled; metadata marks the kind
    assert any(n.endswith("services/playr/playr-up") for n in names)
    assert any(n.endswith("config/sensors/bag_player.yaml") for n in names)
    assert any("/rig_cli/" in n for n in names)
    with tarfile.open(tar) as tf:
        import yaml as _y
        member = next(n for n in tf.getnames() if n.endswith("capture/metadata.yaml"))
        kind = _y.safe_load(tf.extractfile(member).read())["kind"]
    assert kind == "run-capture"
    # docker absent here: identity capture degraded to nulls (or wrote nothing) without failing
    images = run_dir / ".rig" / "images.yaml"
    if images.exists():
        assert set((load_yaml(images).get("images") or {}).values()) <= {None}


def test_capture_opt_out_and_fail_soft():
    root, m = _deployment(run_capture=False)
    rid, data = _open(m, root)
    doc = load_yaml(data / "runs" / rid / "manifest.yaml")
    assert "capture" not in doc
    assert not (data / "runs" / rid / ".rig" / "artifact.tar.gz").exists()

    root2, m2 = _deployment()
    orig = bake.capture_run
    bake.capture_run = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rid2, data2 = _open(m2, root2)
        assert "capture failed" in err.getvalue()  # WARNed…
        assert (data2 / "runs" / rid2 / "manifest.yaml").exists()  # …and the run still opened
        assert "capture" not in load_yaml(data2 / "runs" / rid2 / "manifest.yaml")
    finally:
        bake.capture_run = orig


def test_reconstruct_native_capture_verifies_and_localizes():
    root, m = _deployment()
    rid, data = _open(m, root)
    run_dir = data / "runs" / rid
    dest = pathlib.Path(tempfile.mkdtemp()) / "tree"
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = reconstruct.cmd_reconstruct(None, run_ref=str(run_dir), into=str(dest), config=None)
    assert rc == 0
    assert (dest / "vehicle.yaml").exists() and (dest / "services" / "playr" / "playr-up").exists()
    local = load_yaml(dest / "vehicle.local.yaml")
    assert local["data_dir"] == str((dest / "var" / "data").resolve())  # localized off the vehicle path
    assert "rig replay" in out.getvalue()  # the next step is printed
    # tamper -> sha mismatch refused
    (run_dir / ".rig" / "artifact.tar.gz").write_bytes(b"garbage")
    try:
        reconstruct.cmd_reconstruct(None, run_ref=str(run_dir),
                                    into=str(dest.parent / "t2"), config=None)
        assert False, "sha mismatch must refuse"
    except RigError as exc:
        assert "sha256 mismatch" in str(exc)


def test_reconstruct_missing_capture_names_the_retrofit_path():
    run_dir = pathlib.Path(tempfile.mkdtemp()) / "r"
    run_dir.mkdir()
    (run_dir / "manifest.yaml").write_text("run: r\nartifact: v9\n")
    try:
        reconstruct.cmd_reconstruct(None, run_ref=str(run_dir), into=None, config=None)
        assert False
    except RigError as exc:
        assert "v9.tar.gz" in str(exc) and "retrofit" in str(exc)


def _snapshot(run_dir: pathlib.Path, files: dict[str, bytes]) -> str:
    digest = runs._config_digest(files)
    snap = run_dir / ".rig" / "config" / digest
    for rel, blob in files.items():
        (snap / rel).parent.mkdir(parents=True, exist_ok=True)
        (snap / rel).write_bytes(blob)
    return digest


def test_retrofit_then_reconstruct_overlays_last_snapshot_by_default():
    root, m = _deployment()
    # a "deploy artifact": capture the tree into a tag-shaped tarball under var/artifacts
    tmp_run = pathlib.Path(tempfile.mkdtemp()) / "x"
    tmp_run.mkdir()
    bake.capture_run(root, m, tmp_run)
    arts = root / "var" / "artifacts"
    arts.mkdir(parents=True)
    (tmp_run / ".rig" / "artifact.tar.gz").rename(arts / "v9.tar.gz")
    # an OLD run: manifest names the tag, no capture; snapshot carries a between-runs config edit
    run_dir = pathlib.Path(tempfile.mkdtemp()) / "20260829T000000Z_flight"
    run_dir.mkdir()
    snap_files = {
        "vehicle.yaml": (  # same rows the artifact has — the drift is in the rendered config
            b"vehicle: veh\nvehicle_id: 1\n"
            b"sensors:\n- {name: cam, service: sensa, config: config/sensors/cam.yaml, "
            b"enabled: true, order: 10}\n"
            b"autonomy:\n- {name: bag_player, service: playr, "
            b"config: config/sensors/bag_player.yaml, enabled: false, order: 999}\n"),
        "rendered/cam.yaml": b"service: sensa\nname: cam\nvalue: EDITED-BETWEEN-RUNS\n",
    }
    digest = _snapshot(run_dir, snap_files)
    import yaml as _y
    (run_dir / "manifest.yaml").write_text(_y.safe_dump(
        {"run": run_dir.name, "artifact": "v9", "ups": [{"at": "x", "config": digest}]}))
    rc = reconstruct.cmd_retrofit(root, run_refs=[str(run_dir)], artifact=None,
                                  from_dir=str(arts))
    assert rc == 0
    doc = load_yaml(run_dir / "manifest.yaml")
    assert doc["capture"]["retrofitted"] and doc["capture"]["sha256"]
    dest = pathlib.Path(tempfile.mkdtemp()) / "tree"
    with contextlib.redirect_stdout(io.StringIO()):
        rc = reconstruct.cmd_reconstruct(None, run_ref=str(run_dir), into=str(dest), config=None)
    assert rc == 0
    # the retrofitted default overlaid the LAST ups snapshot: the edit is in the tree
    assert b"EDITED-BETWEEN-RUNS" in (dest / "config" / "sensors" / "cam.yaml").read_bytes()
    assert (dest / "services" / "sensa" / "sensa-up").exists()  # surfaces from the artifact


def test_retrofit_refuses_mismatch_and_corrupt_snapshot_refused():
    root, m = _deployment()
    run_dir = pathlib.Path(tempfile.mkdtemp()) / "r"
    run_dir.mkdir()
    (run_dir / "manifest.yaml").write_text("run: r\nartifact: v9\n")
    bad = pathlib.Path(tempfile.mkdtemp()) / "other.tar.gz"
    bad.write_bytes(b"x")
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = reconstruct.cmd_retrofit(root, run_refs=[str(run_dir)], artifact=str(bad),
                                      from_dir=None)
    assert rc == 1 and "false provenance" in err.getvalue()

    # corrupt snapshot: dir name no longer matches its content digest -> refused at overlay
    root2, m2 = _deployment()
    rid, data = _open(m2, root2)
    run2 = data / "runs" / rid
    digest = _snapshot(run2, {"vehicle.yaml": b"vehicle: veh\n"})
    (run2 / ".rig" / "config" / digest / "vehicle.yaml").write_bytes(b"tampered: true\n")
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            reconstruct.cmd_reconstruct(None, run_ref=str(run2),
                                        into=str(run2.parent / "t3"), config=digest)
        assert False, "corrupt snapshot must refuse"
    except RigError as exc:
        assert "content-address" in str(exc)


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
