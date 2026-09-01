"""run-registry lifecycle QoL — provision --data-dir, run rm, run import (v0.2.38).
Run: python3 tests/test_run_admin.py

Real filesystem throughout; provision writes to a temp file via RIG_VEHICLE_LOCAL (the same env
the manifest loader honors). The invariants under test: rm never touches the OPEN run or escapes
runs/ containment; import never touches `current`; provision refuses relative data_dirs (the
registry-fork trap runs._root guards against)."""
import contextlib
import io
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from rig_cli import RigError, provision, runs  # noqa: E402
from rig_cli.common import load_yaml  # noqa: E402
from rig_cli.manifest import Manifest, RosSettings  # noqa: E402


def _manifest(data_dir):
    return Manifest(vehicle="t", vehicle_id=1, sensors=[], data_dir=str(data_dir),
                    run_capture=False,  # lifecycle tests need no capture fixtures
                    ros=RosSettings(domain_id=1, rmw="rmw_zenoh_cpp", distro=None))


def _run(data, name, *, sealed=True, kb=2048):
    d = data / "runs" / name
    d.mkdir(parents=True)
    body = f"run: {name}\ndisk_kb: {kb}\n" + ("ended: 2026-09-01T00:00:00Z\n" if sealed else "")
    (d / "manifest.yaml").write_text(body)
    return d


def test_rm_sealed_reports_freed_and_refuses_unsealed_without_force():
    data = pathlib.Path(tempfile.mkdtemp())
    _run(data, "a_sealed")
    _run(data, "b_open_less", sealed=False)
    m = _manifest(data)
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = runs.remove_runs(m, ["a_sealed", "b_open_less"])
    assert rc == 1
    assert not (data / "runs" / "a_sealed").exists()
    assert (data / "runs" / "b_open_less").exists()  # unsealed survived…
    assert "not sealed" in err.getvalue() and "freed 2 MB" in err.getvalue()
    with contextlib.redirect_stderr(io.StringIO()):
        assert runs.remove_runs(m, ["b_open_less"], force=True) == 0  # …until --force
    assert not (data / "runs" / "b_open_less").exists()


def test_rm_never_removes_the_open_run_even_forced():
    data = pathlib.Path(tempfile.mkdtemp())
    run = _run(data, "x_open", sealed=False)
    (data / "current").symlink_to(pathlib.Path("runs") / "x_open")
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = runs.remove_runs(_manifest(data), ["x_open"], force=True)
    assert rc == 1 and run.exists() and "OPEN run" in err.getvalue()


def test_rm_ids_only_containment():
    data = pathlib.Path(tempfile.mkdtemp())
    (data / "runs").mkdir()
    outside = data / "victim"
    outside.mkdir()
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = runs.remove_runs(_manifest(data), ["../victim", "/etc", "nope"])
    assert rc == 1 and outside.exists() and err.getvalue().count("no run") == 3


def test_import_copy_collision_and_move():
    data = pathlib.Path(tempfile.mkdtemp())
    src_base = pathlib.Path(tempfile.mkdtemp())
    src = src_base / "20260901T000000Z_arch"
    (src / "bags").mkdir(parents=True)
    (src / "manifest.yaml").write_text("run: x\nended: y\n")
    m = _manifest(data)
    with contextlib.redirect_stderr(io.StringIO()):
        assert runs.import_runs(m, [str(src)]) == 0
    assert (data / "runs" / src.name / "bags").is_dir()
    assert src.exists()  # copy keeps the archive's copy
    assert [r.run for r in runs.list_runs(m)] == [src.name]
    assert not (data / "current").exists()  # imported history never becomes the open run
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        assert runs.import_runs(m, [str(src)]) == 1  # collision refused
    assert "already in the registry" in err.getvalue()
    src2 = src_base / "20260901T000001Z_arch2"
    src2.mkdir()
    (src2 / "manifest.yaml").write_text("run: y\n")  # unsealed -> WARN, still imports
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        assert runs.import_runs(m, [str(src2)], move=True) == 0
    assert "not sealed" in err.getvalue()
    assert not src2.exists() and (data / "runs" / src2.name).is_dir()  # moved
    with contextlib.redirect_stderr(io.StringIO()):
        assert runs.import_runs(m, [str(src_base / "nope")]) == 1  # not a run dir


def test_provision_data_dir_absolute_only_and_shown():
    target = pathlib.Path(tempfile.mkdtemp()) / "vehicle.local.yaml"
    os.environ["RIG_VEHICLE_LOCAL"] = str(target)
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            rc = provision.provision(None, vehicle_id=None, name=None, set_vars=[],
                                     data_dir="/data/rig", force=False)
        assert rc == 0 and load_yaml(target)["data_dir"] == "/data/rig"
        try:
            provision.provision(None, vehicle_id=None, name=None, set_vars=[],
                                data_dir="relative/path", force=False)
            assert False, "relative data_dir must refuse (registry-fork trap)"
        except RigError as exc:
            assert "ABSOLUTE" in str(exc)
        err = io.StringIO()  # bare show mode includes it (LOCAL_KEYS intersection)
        with contextlib.redirect_stderr(err):
            provision.provision(None, vehicle_id=None, name=None, set_vars=[], force=False)
        assert "data_dir: /data/rig" in err.getvalue()
    finally:
        del os.environ["RIG_VEHICLE_LOCAL"]




def test_by_label_newest_and_rm_unlinks_linked_entries():
    data = pathlib.Path(tempfile.mkdtemp())
    _run(data, "20260901T000000Z_flight1")
    _run(data, "20260902T000000Z_flight1")
    m = _manifest(data)
    assert runs.by_label(m, "flight1") == "20260902T000000Z_flight1"  # newest wins
    assert runs.by_label(m, "nope") is None
    # a LINKED entry (reconstruct import): rm unlinks the link, never the target — no seal gate
    archive = pathlib.Path(tempfile.mkdtemp()) / "20260903T000000Z_arch"
    archive.mkdir()
    (archive / "manifest.yaml").write_text("run: x\n")  # unsealed on purpose
    (data / "runs" / archive.name).symlink_to(archive)
    assert runs.by_label(m, "arch") == archive.name  # links count for label resolution
    rows = {r.run: r for r in runs.list_runs(m)}
    assert rows[archive.name].linked and rows[archive.name].state == "interrupted"
    import contextlib as _ctx
    import io as _io
    with _ctx.redirect_stderr(_io.StringIO()):
        assert runs.remove_runs(m, [archive.name]) == 0  # no --force needed for a reference
    assert archive.exists()  # the archive target untouched
    assert not (data / "runs" / archive.name).exists()
    # dangling link (archive moved): listed as such, never silently vanished
    (data / "runs" / "20260904T000000Z_gone").symlink_to(archive.parent / "moved-away")
    rows = {r.run: r for r in runs.list_runs(m)}
    assert rows["20260904T000000Z_gone"].state == "dangling"


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
