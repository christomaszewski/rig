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
# Hermetic user state: rig remembers registries it touches in $RIG_HOME/catalog.yaml (rig catalog)
# — never the developer's real ~/.rig.
os.environ.setdefault("RIG_HOME", str(pathlib.Path(tempfile.mkdtemp()) / "home"))


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


def _someone_elses(run_dir):
    """A recording as the stock logger leaves it: a directory this user cannot change, with a
    bag in it. The logger writes as root; a read-only directory refuses removal the same way."""
    bag = run_dir / "bags" / "bag_logger" / "session"
    bag.mkdir(parents=True)
    (bag / "data.mcap").write_bytes(b"recorded")
    (run_dir / ".rig").mkdir()
    (run_dir / ".rig" / "images.yaml").write_text("{}\n")
    bag.chmod(0o555)
    return bag


def test_rm_refuses_before_removing_anything_when_part_of_the_run_is_not_the_users():
    if os.geteuid() == 0:
        print("     (skipped: root can remove anything)")
        return
    data = pathlib.Path(tempfile.mkdtemp())
    run = _run(data, "a_rooted")
    _run(data, "b_plain")
    bag = _someone_elses(run)
    m = _manifest(data)
    try:
        before = sorted(str(p.relative_to(run)) for p in run.rglob("*"))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = runs.remove_runs(m, ["a_rooted", "b_plain"])  # v0.2.85: PermissionError, half removed
        said = err.getvalue()
        assert rc == 1
        # nothing of the run was removed, so it still lists as what it is
        assert sorted(str(p.relative_to(run)) for p in run.rglob("*")) == before
        assert [r.state for r in runs.registry_rows(data) if r.run == "a_rooted"] == ["sealed"]
        assert "a_rooted" in said and "nothing was removed" in said, said
        assert str(bag) in said, said                       # where the trouble is
        assert f"chown -R {os.getuid()}:{os.getgid()} {run}" in said, said  # and what to do
        assert "Traceback" not in said
        # the other run named in the same command is still removed
        assert not (data / "runs" / "b_plain").exists() and "removed b_plain" in said
        # --force is for unsealed runs: it does not get past ownership either
        with contextlib.redirect_stderr(io.StringIO()):
            assert runs.remove_runs(m, ["a_rooted"], force=True) == 1
        assert (run / "manifest.yaml").exists()
    finally:
        bag.chmod(0o755)
    with contextlib.redirect_stderr(io.StringIO()):
        assert runs.remove_runs(m, ["a_rooted"]) == 0      # once it is the user's, it removes
    assert not run.exists()


def test_rm_reports_a_removal_that_stops_part_way_without_a_traceback():
    """Whatever the check before removal could not foresee is still reported, not raised."""
    from unittest.mock import patch
    data = pathlib.Path(tempfile.mkdtemp())
    _run(data, "a_sealed")
    _run(data, "b_sealed")
    m = _manifest(data)
    real = runs.shutil.rmtree

    def stops(path, *args, **kwargs):
        if pathlib.Path(path).name == "a_sealed":
            raise PermissionError(13, "Permission denied", str(pathlib.Path(path) / "bags"))
        return real(path, *args, **kwargs)
    err = io.StringIO()
    with patch.object(runs.shutil, "rmtree", side_effect=stops), contextlib.redirect_stderr(err):
        rc = runs.remove_runs(m, ["a_sealed", "b_sealed"])
    assert rc == 1
    assert "a_sealed" in err.getvalue() and "stopped part-way" in err.getvalue(), err.getvalue()
    assert not (data / "runs" / "b_sealed").exists()        # the next run is still attempted


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




def test_provision_registry_host_merges_images_and_refuses_urls():
    target = pathlib.Path(tempfile.mkdtemp()) / "vehicle.local.yaml"
    target.write_text("images: {tag: v1.2.0}\n")  # a pre-existing subkey must survive the merge
    os.environ["RIG_VEHICLE_LOCAL"] = str(target)
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            rc = provision.provision(None, vehicle_id=None, name=None, set_vars=[],
                                     registry="localhost:5000/", force=False)
        assert rc == 0
        assert load_yaml(target)["images"] == {"tag": "v1.2.0", "registry": "localhost:5000"}
        for bad in ("http://localhost:5000", "not a host"):
            try:
                provision.provision(None, vehicle_id=None, name=None, set_vars=[],
                                    registry=bad, force=False)
                assert False, f"{bad!r} must refuse"
            except RigError as exc:
                assert "registry" in str(exc)
        err = io.StringIO()  # bare show mode lists it (images is a LOCAL_KEY)
        with contextlib.redirect_stderr(err):
            provision.provision(None, vehicle_id=None, name=None, set_vars=[], force=False)
        assert "images:" in err.getvalue() and "localhost:5000" in err.getvalue()
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
