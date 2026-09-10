"""`rig catalog` (rig ≥ v0.2.55): roots remembered as rig touches registries/harvest trees (plus
the machine registry, plus `catalog add`), a raw scan across every shape a run dir sits in
(registry, harvest tree, slim copies, a lone run dir, a linked entry seen once), and the
filters. Deliberately separate from `rig runs`/TAB. Run: python3 tests/test_runcatalog.py
"""
import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from rig_cli import RigError, runcatalog  # noqa: E402
from rig_cli.cli import main  # noqa: E402

for _stray in [k for k in os.environ
               if k in ("RIG_VEHICLE_ID", "RIG_VEHICLE_NAME") or k.startswith("RIG_VAR_")]:
    os.environ.pop(_stray)


def _run(d: pathlib.Path, *, vehicle="veh", vid=4, started="2026-09-01T10:00:00Z",
         sealed=True, tags=None, disk_kb=None, slim=None) -> pathlib.Path:
    name = d.name
    d.mkdir(parents=True, exist_ok=True)
    doc = {"run": name, "vehicle": vehicle, "vehicle_id": vid, "started": started}
    if "_" in name:
        doc["label"] = name.split("_", 1)[1]
    if sealed:
        doc["ended"] = started.replace("10:00", "11:00")
    if tags:
        doc["tags"] = tags
    if disk_kb is not None:
        doc["disk_kb"] = disk_kb
    (d / "manifest.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    (d / "bags" / "bag_logger").mkdir(parents=True)          # data dirs are never descended
    (d / "bags" / "bag_logger" / "manifest.yaml").write_text("not: a run\n")
    if slim:
        (d / ".rig").mkdir()
        (d / ".rig" / "export.yaml").write_text(yaml.safe_dump({"export": {"profile": slim}}))
    return d


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


def _cli(*argv, cwd=None) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    old = os.getcwd()
    os.chdir(cwd or tempfile.mkdtemp())   # OUTSIDE any deployment: the catalog needs none
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = main(list(argv))
            except SystemExit as exc:
                rc = int(exc.code or 0)
    finally:
        os.chdir(old)
    return rc, out.getvalue(), err.getvalue()


def _world():
    """machine registry + a harvest tree + a lone archived run + a workspace registry linking."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    home = tmp / "home"
    machine_reg = tmp / "machine"
    _run(machine_reg / "runs" / "20260801T100000Z_survey", tags=["site:mojave", "event:demo"],
         disk_kb=4_000_000, started="2026-08-01T10:00:00Z")
    _run(machine_reg / "runs" / "20260901T100000Z_bench", started="2026-09-01T10:00:00Z")
    _run(machine_reg / "runs" / "20260902T100000Z_open", started="2026-09-02T10:00:00Z", sealed=False)
    (machine_reg / "current").symlink_to(pathlib.Path("runs") / "20260902T100000Z_open")
    mfile = tmp / "vehicle.local.yaml"
    mfile.write_text(f"vehicle: box\nvehicle_id: 1\ndata_dir: {machine_reg}\n")
    harvest = tmp / "fleet-runs"
    _run(harvest / "survey" / "skiff-07" / "20260810T100000Z_survey", vehicle="skiff-07", vid=7,
         started="2026-08-10T10:00:00Z", tags=["site:coast"], slim="review")
    _run(harvest / "survey" / "skiff-09" / "20260810T100000Z_survey", vehicle="skiff-09", vid=9,
         started="2026-08-10T10:00:00Z")
    archive = tmp / "archive" / "20260701T100000Z_old"
    _run(archive, started="2026-07-01T10:00:00Z", tags=["site:mojave"])
    workspace = tmp / "ws-data"
    (workspace / "runs").mkdir(parents=True)
    (workspace / "runs" / "20260801T100000Z_survey").symlink_to(
        machine_reg / "runs" / "20260801T100000Z_survey")               # a LINKED entry
    _run(workspace / "runs" / "20260903T100000Z_replay-survey", started="2026-09-03T10:00:00Z")
    return home, mfile, machine_reg, harvest, archive, workspace


def test_roots_machine_first_remembered_deduped_and_missing_reported():
    home, mfile, machine_reg, harvest, archive, workspace = _world()
    with _env(RIG_HOME=str(home), RIG_VEHICLE_LOCAL=str(mfile)):
        assert runcatalog.roots() == [(machine_reg, "machine")]
        runcatalog.remember(harvest, kind="harvest")
        runcatalog.remember(harvest, kind="harvest")                    # idempotent
        runcatalog.remember(workspace)
        runcatalog.remember(machine_reg)                                # already the machine's
        assert [k for _, k in runcatalog.roots()] == ["machine", "harvest", "registry"]
        assert yaml.safe_load(runcatalog.roots_file().read_text())["roots"] == [
            {"path": str(harvest), "kind": "harvest"}, {"path": str(workspace), "kind": "registry"}]
        rc, out, err = _cli("catalog", "add", str(archive.parent), "--kind", "archive")
        assert rc == 0 and "added" in err
        rc, out, _ = _cli("catalog", "roots")
        assert rc == 0 and out.splitlines()[0].startswith("machine") and "archive" in out
        gone = pathlib.Path(tempfile.mkdtemp()) / "gone"
        gone.mkdir()
        runcatalog.remember(gone)
        gone.rmdir()
        rc, out, err = _cli("catalog")
        assert rc == 0 and "is missing" in err and str(gone) in err
        rc, _, err = _cli("catalog", "remove", str(gone))
        assert rc == 0 and str(gone) not in runcatalog.roots_file().read_text()
        rc, _, err = _cli("catalog", "remove", "/never/added")
        assert rc == 1 and "not a remembered root" in err
    with _env(RIG_HOME=str(pathlib.Path(tempfile.mkdtemp()) / "fresh"),
              RIG_VEHICLE_LOCAL=str(pathlib.Path(tempfile.mkdtemp()) / "absent.yaml")):
        assert runcatalog.roots() == []
        rc, out, _ = _cli("catalog")
        assert rc == 0 and "no runs cataloged" in out


def test_scan_every_shape_once_with_state_and_kind():
    home, mfile, machine_reg, harvest, archive, workspace = _world()
    with _env(RIG_HOME=str(home), RIG_VEHICLE_LOCAL=str(mfile)):
        runcatalog.remember(harvest, kind="harvest")
        runcatalog.remember(archive.parent, kind="archive")
        runcatalog.remember(workspace)
        entries, missing = runcatalog.scan()
        assert missing == []
        by_run = {(e.vehicle, e.run): e for e in entries}
        assert len(entries) == len(by_run) == 7                          # the linked twin: once
        assert [e.run for e in entries][:2] == ["20260903T100000Z_replay-survey",
                                                "20260902T100000Z_open"]   # newest first
        assert by_run[("veh", "20260902T100000Z_open")].state == "OPEN"  # the registry's current
        assert by_run[("veh", "20260801T100000Z_survey")].path == machine_reg / "runs" / "20260801T100000Z_survey"
        assert by_run[("veh", "20260801T100000Z_survey")].kind == "full"
        assert by_run[("skiff-07", "20260810T100000Z_survey")].kind == "slim:review"
        assert by_run[("skiff-07", "20260810T100000Z_survey")].root == harvest
        assert by_run[("veh", "20260701T100000Z_old")].state == "sealed"   # a lone run dir root
        assert by_run[("veh", "20260701T100000Z_old")].tags == ("site:mojave",)
        assert not any("bags" in str(e.path) for e in entries)            # data dirs not runs


def test_filters_and_outputs():
    home, mfile, machine_reg, harvest, archive, workspace = _world()
    with _env(RIG_HOME=str(home), RIG_VEHICLE_LOCAL=str(mfile)):
        runcatalog.remember(harvest, kind="harvest")
        runcatalog.remember(archive.parent, kind="archive")
        entries, _ = runcatalog.scan()

        def sel(**kw):
            base = dict(query=[], tags=[], label=None, vehicle=None, since=None, until=None, state=None)
            base.update(kw)
            return sorted(e.run for e in runcatalog.select(entries, **base))

        assert sel(tags=["site:mojave"]) == ["20260701T100000Z_old", "20260801T100000Z_survey"]
        assert sel(tags=["site:"]) == ["20260701T100000Z_old", "20260801T100000Z_survey",
                                       "20260810T100000Z_survey"]           # any site:*
        assert sel(tags=["site:mojave", "event:demo"]) == ["20260801T100000Z_survey"]  # all
        assert sel(label="survey") == ["20260801T100000Z_survey", "20260810T100000Z_survey",
                                       "20260810T100000Z_survey"]
        assert sel(vehicle="skiff-07") == ["20260810T100000Z_survey"]
        assert sel(vehicle="9") == ["20260810T100000Z_survey"]
        assert sel(since="2026-09") == ["20260901T100000Z_bench", "20260902T100000Z_open"]
        assert sel(until="2026-07-31") == ["20260701T100000Z_old"]
        assert sel(since="2026-08-05", until="2026-08") == ["20260810T100000Z_survey"] * 2
        assert sel(state="open") == ["20260902T100000Z_open"]
        assert sel(query=["skiff", "coast"]) == ["20260810T100000Z_survey"]  # all words
        assert sel(query=["mojave"]) == ["20260701T100000Z_old", "20260801T100000Z_survey"]
        try:
            sel(since="yesterday")
            assert False
        except RigError as exc:
            assert "--since" in str(exc)
        # the CLI: bare `rig catalog <query> [filters]` is a search; --json; --paths
        rc, out, err = _cli("catalog", "--tag", "site:mojave")
        assert rc == 0 and "20260801T100000Z_survey" in out and "TAGS" in out
        assert "2 of 6 run(s)" in err
        rc, out, _ = _cli("catalog", "survey", "--vehicle", "skiff-07")
        assert rc == 0 and "[slim:review]" in out and "20260810T100000Z_survey" in out
        rc, out, _ = _cli("catalog", "--json", "--label", "bench")
        assert rc == 0 and json.loads(out)[0]["run"] == "20260901T100000Z_bench"
        rc, out, _ = _cli("catalog", "--paths", "--since", "2026-09-02")
        assert rc == 0 and out.strip() == str(machine_reg / "runs" / "20260902T100000Z_open")
        rc, out, _ = _cli("catalog", "nothing-like-this")
        assert rc == 0 and "no match (6 run(s) cataloged)" in out
        rc, out, _ = _cli("catalog", "ls")
        assert rc == 0 and "6 of 6" not in out and "20260701T100000Z_old" in out


def test_rig_remembers_registries_it_writes_and_harvests():
    """new-run / import / fleet sync / reconstruct register their roots (best-effort)."""
    from rig_cli import runs
    from rig_cli.manifest import Manifest, RosSettings
    home = pathlib.Path(tempfile.mkdtemp()) / "home"
    data = pathlib.Path(tempfile.mkdtemp()) / "data"
    with _env(RIG_HOME=str(home), RIG_VEHICLE_LOCAL=str(pathlib.Path(tempfile.mkdtemp()) / "absent.yaml")):
        m = Manifest(vehicle="t", vehicle_id=1, sensors=[], data_dir=str(data), run_capture=False,
                     ros=RosSettings(domain_id=1, rmw="rmw_zenoh_cpp", distro=None))
        src = _run(pathlib.Path(tempfile.mkdtemp()) / "20260101T000000Z_x")
        with contextlib.redirect_stderr(io.StringIO()):
            assert runs.import_runs(m, [str(src)]) == 0
        assert runcatalog.roots() == [(data, "registry")]
        rc, out, _ = _cli("catalog")
        assert rc == 0 and "20260101T000000Z_x" in out


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except Exception as exc:  # noqa: BLE001
                failures += 1
                import traceback
                print("FAIL", name, "->", exc)
                traceback.print_exc()
    sys.exit(1 if failures else 0)
