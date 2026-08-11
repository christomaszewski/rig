"""rig fetch — materialize example configs for hand-authored manifests. Run: python3 tests/test_fetch.py"""
import contextlib
import io
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from rig_cli import RigError  # noqa: E402
from rig_cli.init import fetch  # noqa: E402
from rig_cli.manifest import load_manifest  # noqa: E402


def _repo(ws, name, svc, tier=None, example=("config", "{svc}.example.yaml", "service: {svc}\nname: ex_{svc}\n")):
    d = ws / name
    (d / example[0]).mkdir(parents=True, exist_ok=True) if example else d.mkdir(parents=True)
    tier_line = f"tier: {tier}\n" if tier else ""
    (d / "rigging.yaml").write_text(f"service: {svc}\nlauncher: {svc}-up\n{tier_line}")
    (d / f"{svc}-up").write_text("#!/bin/sh\n")
    (d / f"{svc}-up").chmod(0o755)
    if example:
        (d / example[0] / example[1].format(svc=svc)).write_text(example[2].format(svc=svc))
    return d


def _deployment(ws, vehicle_yaml, services_yaml) -> pathlib.Path:
    veh = ws / "veh"
    (veh / "config" / "sensors").mkdir(parents=True)
    (veh / "config" / "infra").mkdir(parents=True)
    (veh / "vehicle.yaml").write_text(vehicle_yaml)
    (veh / "services.yaml").write_text(services_yaml)
    return veh


def test_fetch_fills_missing_row_configs_and_manifest_loads():
    ws = pathlib.Path(tempfile.mkdtemp())
    _repo(ws, "nova", "nova")
    _repo(ws, "router", "router", tier="infra")
    veh = _deployment(
        ws,
        "vehicle: t\n"
        "infra: [{name: rtr, service: router, config: config/infra/rtr.yaml}]\n"
        "sensors: [{name: gnss, service: nova, config: config/sensors/gnss.yaml}]\n",
        "services:\n  nova: { path: ../nova }\n  router: { path: ../router }\n")
    try:
        load_manifest(veh)
        raise AssertionError("fixture should be unloadable before fetch")
    except RigError:
        pass
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        assert fetch(veh) == 0
    gnss = (veh / "config" / "sensors" / "gnss.yaml").read_text()
    assert "# name: ex_nova" in gnss                       # nameless profile: the row stamps the name
    assert (veh / "config" / "infra" / "rtr.yaml").is_file()
    m = load_manifest(veh)                                 # THE point: doctor is now unblocked
    assert sorted(s.name for s in m.sensors) == ["gnss", "rtr"]
    assert "manifest loads" in err.getvalue()


def test_fetch_never_overwrites_and_is_idempotent():
    ws = pathlib.Path(tempfile.mkdtemp())
    _repo(ws, "nova", "nova")
    veh = _deployment(ws, "vehicle: t\nsensors: [{name: gnss, service: nova, config: config/sensors/gnss.yaml}]\n",
                      "services:\n  nova: { path: ../nova }\n")
    (veh / "config" / "sensors" / "gnss.yaml").write_text("service: nova\nname: gnss\nmine: true\n")
    before_veh = (veh / "vehicle.yaml").read_text()
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        fetch(veh)
    assert (veh / "config" / "sensors" / "gnss.yaml").read_text() == "service: nova\nname: gnss\nmine: true\n"
    assert "0 config(s) fetched, 1 already present" in err.getvalue()
    assert (veh / "vehicle.yaml").read_text() == before_veh   # manifests are never touched


def test_fetch_reports_unrouted_and_exampleless_rows():
    ws = pathlib.Path(tempfile.mkdtemp())
    _repo(ws, "bare", "bare", example=None)
    veh = _deployment(
        ws,
        "vehicle: t\nsensors:\n"
        "  - {name: ghost, service: missing, config: config/sensors/ghost.yaml}\n"
        "  - {name: nocfg, service: bare, config: config/sensors/nocfg.yaml}\n",
        "services:\n  bare: { path: ../bare }\n")
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        assert fetch(veh) == 0                             # reports, never aborts
    out = err.getvalue()
    assert "ghost" in out and "doesn't route" in out
    assert "nocfg" in out and "no example config" in out
    assert not (veh / "config" / "sensors" / "ghost.yaml").exists()
    assert "still doesn't load" in out                     # honest about the remaining work


def test_fetch_copies_material_for_unreferenced_services():
    ws = pathlib.Path(tempfile.mkdtemp())
    _repo(ws, "extra", "extra", tier="autonomy")
    veh = _deployment(ws, "vehicle: t\nsensors: []\n", "services:\n  extra: { path: ../extra }\n")
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        fetch(veh)
    assert (veh / "config" / "autonomy" / "extra.yaml").is_file()   # tier-routed material
    assert "suggested row (under `autonomy:`)" in err.getvalue()    # echoed, never written
    assert "extra" not in (veh / "vehicle.yaml").read_text()


def test_fetch_warns_when_example_name_survives_and_differs():
    ws = pathlib.Path(tempfile.mkdtemp())
    _repo(ws, "odd", "odd", example=("sensors", "odd.example.yaml", "service: odd\nname:\n  front\n"))
    veh = _deployment(ws, "vehicle: t\nsensors: [{name: gnss, service: odd, config: config/sensors/g.yaml}]\n",
                      "services:\n  odd: { path: ../odd }\n")
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        fetch(veh)
    out = err.getvalue()
    assert "WARNING" in out and "'front'" in out and "'gnss'" in out  # both names, actionable
    assert (veh / "config" / "sensors" / "g.yaml").is_file()          # material kept anyway


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
