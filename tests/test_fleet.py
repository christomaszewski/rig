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
from rig_cli.common import safe_extract  # noqa: E402
from rig_cli.manifest import load_manifest  # noqa: E402
from rig_cli.resolve import materialize_manifest  # noqa: E402
# Hermetic user state: rig remembers registries it touches in $RIG_HOME/catalog.yaml (rig catalog)
# — never the developer's real ~/.rig.
os.environ.setdefault("RIG_HOME", str(pathlib.Path(tempfile.mkdtemp()) / "home"))

# Hermetic on ANY host, provisioned included: a fleet artifact's whole point is that identity comes
# from the VEHICLE, so ambient shell identity must not stand in for it here (tests set the machine
# tier themselves via _env(RIG_VEHICLE_LOCAL=...)).
for _stray in [k for k in os.environ
               if k in ("RIG_VEHICLE_ID", "RIG_VEHICLE_NAME") or k.startswith("RIG_VAR_")]:
    os.environ.pop(_stray)


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
        shim = staging / "rig"
        assert shim.stat().st_mode & 0o111                             # bundled rig: GENERATED shim
        assert "bundled entry point" in shim.read_text()               # (installed rigs have no ./rig
        assert (staging / "rig_cli" / "cli.py").is_file()              #  to copy — regression, v0.1.58)
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


def test_fleet_bake_default_tag_needs_no_vehicle_identity():
    root = _fleet_deployment()
    vehicle = root / "vehicle.yaml"
    vehicle.write_text(vehicle.read_text() + "images: {tag: fleet-release}\n")
    (root / "vehicle.local.yaml").write_text("images: {tag: local-only}\n")
    with _env(RIG_VAR_image_tag="shell-only"):
        rc, _, err = _run("--root", str(root), "artifact", "bake")
    assert rc == 0, err
    assert (root / "var" / "artifacts" / "fleet-release.tar.gz").is_file()
    staged = yaml.safe_load((root / "var" / "bake" / "fleet-release" / "vehicle.yaml").read_text())
    assert staged["vehicle_id"] == "{{vehicle_id}}"
    assert staged["images"]["tag"] == "fleet-release"


def test_fleet_bake_templated_tag_requires_explicit_artifact_name():
    root = _fleet_deployment()
    vehicle = root / "vehicle.yaml"
    vehicle.write_text(vehicle.read_text() + 'images: {tag: "{{image_tag}}"}\n')
    with _env(RIG_VAR_image_tag="shell-only"):
        rc, _, err = _run("--root", str(root), "bake")
        assert rc == 1 and "templated" in err and "--tag" in err, err
        assert not (root / "var" / "bake").exists()
        rc, _, err = _run("--root", str(root), "bake", "--tag", "snapshot")
    assert rc == 0, err
    staged = root / "var" / "bake" / "snapshot" / "vehicle.yaml"
    assert yaml.safe_load(staged.read_text())["images"]["tag"] == "{{image_tag}}"


def _map_deployment() -> pathlib.Path:
    """A deployment whose ONLY templating is the {{map …}} form — the fleet-detection blind spot
    (MARKER alone does not match it)."""
    root = pathlib.Path(tempfile.mkdtemp()) / "veh"
    (root / "config" / "infra").mkdir(parents=True)
    (root / "vehicle.yaml").write_text(textwrap.dedent("""\
        vehicle: swarm
        vehicle_id: 3
        vars:
          fleet_ids: [3, 9]
          peer_endpoint: tcp/10.0.0.{}:7447
        infra:
          - {name: router, service: routersvc, config: config/infra/router.yaml}
        """))
    (root / "config" / "infra" / "router.yaml").write_text(
        'service: routersvc\nname: router\nconnect: "{{map fleet_peer_ids peer_endpoint}}"\n')
    svc = pathlib.Path(tempfile.mkdtemp()) / "routersvc"
    svc.mkdir(parents=True)
    (svc / "rigging.yaml").write_text("service: routersvc\nlauncher: router-up\ntier: infra\n"
                                      "launch_surface: [router-up]\n")
    (svc / "router-up").write_text("#!/bin/sh\n")
    (svc / "router-up").chmod(0o755)
    (root / "services.yaml").write_text(f"services:\n  routersvc: {{ path: {svc} }}\n")
    return root


def test_map_only_deployment_is_fleet():
    root = _map_deployment()
    assert is_fleet(root)                                     # MAP args harvested — never a
    assert {"fleet_peer_ids", "peer_endpoint"} <= fleet_refs(root)  # silently-resolved bake
    with _env(RIG_VEHICLE_LOCAL=str(pathlib.Path(tempfile.mkdtemp()) / "none.yaml")):
        rc, _, err = _run("--root", str(root), "artifact", "bake", "--tag", "m1")
        assert rc == 0, err
        example = (root / "var" / "bake" / "m1" / "vehicle.local.example.yaml").read_text()
        # the DERIVED var must never be demanded from the operator; the template var shows as
        # optional with its fleet default
        assert not any("fleet_peer_ids" in line and "REQUIRED" in line
                       for line in example.splitlines())
        assert "peer_endpoint" in example and "optional" in example


def test_one_artifact_two_vehicles_peer_exclusion():
    root = _map_deployment()
    with _env(RIG_VEHICLE_LOCAL=str(pathlib.Path(tempfile.mkdtemp()) / "none.yaml")):
        rc, _, err = _run("--root", str(root), "artifact", "bake", "--tag", "m2")
        assert rc == 0, err
    artifact = root / "var" / "artifacts" / "m2.tar.gz"
    peers = {}
    for vid in (3, 9):
        vehicle_home = pathlib.Path(tempfile.mkdtemp())
        with tarfile.open(artifact) as tf:
            safe_extract(tf, vehicle_home)
        tree = vehicle_home / "m2"
        machine = vehicle_home / "machine.yaml"
        machine.write_text(f"vehicle: skiff-{vid:02d}\nvehicle_id: {vid}\n")
        with _env(RIG_VEHICLE_LOCAL=str(machine)):
            mm = materialize_manifest(load_manifest(tree), tree)
            cfg = yaml.safe_load(pathlib.Path(mm.sensors[0].config).read_text())
            peers[vid] = cfg["connect"]
    assert peers == {3: ["tcp/10.0.0.9:7447"], 9: ["tcp/10.0.0.3:7447"]}  # ONE artifact,
    #                                                             each vehicle excludes ITSELF


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
            safe_extract(tf, vehicle_home)
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
        assert rc == 0  # inventory renders unprovisioned: local rows or the empty message
        assert "no packages in this deployment" in out or "unpublished" in out
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
        assert yaml.safe_load(machine.read_text())["vars"] == \
            {"camera_ip": "10.160.7.25", "extra": 1}                     # MERGED; digit -> int
        rc, _, err = _run("provision", "--id", "8")                      # re-identify without --force
        assert rc == 1 and "ORPHANING" in err
        rc, _, err = _run("provision", "--id", "8", "--force")
        assert rc == 0 and yaml.safe_load(machine.read_text())["vehicle_id"] == 8
        rc, _, err = _run("provision", "--id", "x")
        assert rc == 1 and "numeric" in err
        rc, _, err = _run("provision", "--platform", "jp7")              # a HOST fact, not identity:
        assert rc == 0, err                                              # no --force gate
        data = yaml.safe_load(machine.read_text())
        assert data["platform"] == "jp7" and data["vehicle_id"] == 8     # identity untouched
        rc, _, err = _run("provision", "--platform", "jp8")              # CHANGING it: still no gate
        assert rc == 0 and yaml.safe_load(machine.read_text())["platform"] == "jp8"
        rc, _, err = _run("provision", "--platform=-bad")                # it suffixes image tags
        assert rc == 1 and "image-tag fragment" in err


def test_provision_show_checks_deployment():
    root = _fleet_deployment()
    machine = pathlib.Path(tempfile.mkdtemp()) / "id.yaml"
    with _env(RIG_VEHICLE_LOCAL=str(machine)):
        rc, _, err = _run("--root", str(root), "provision")
        assert rc == 1 and "NOT satisfied" in err
        _run("provision", "--id", "4", "--name", "skiff")
        rc, _, err = _run("--root", str(root), "provision")
        assert rc == 0 and "all vars satisfied" in err and "vehicle_id=4" in err

# --- provisioning records: export from one vehicle, apply to another -----------------------------

_DONOR = {
    "vehicle": "uav-03", "vehicle_id": 3, "platform": "jp7", "arch": "arm64",
    "data_dir": "/home/uxv/logs", "images": {"registry": "depot.lan:5000"},
    "ros": {"domain_id": 42}, "run_capture": False,
    "replay": {"services": {"camsvc": {"start": False}}},
    "vars": {"altitude_band": 3, "camera_ip": "10.160.3.25"},
    "env": {"SIYI_IP": "10.160.3.25"},
}
_SPARE = {
    "vehicle": "spare-1", "vehicle_id": 9, "platform": "jp8", "arch": "amd64",
    "data_dir": "/data/spare", "images": {"registry": "bench.lan:5000"},
    "ros": {"domain_id": 9},
    "vars": {"camera_ip": "10.160.9.25", "gimbal_trim": 12},
    "env": {"KEEP": "x"},
}


def _machine_file(data=None) -> pathlib.Path:
    path = pathlib.Path(tempfile.mkdtemp()) / "vehicle.local.yaml"
    if data is not None:
        path.write_text(yaml.safe_dump(data, sort_keys=False))
    return path


def _export(data: dict) -> tuple[pathlib.Path, dict]:
    """Export a machine file holding `data`; (record path, parsed record)."""
    record = pathlib.Path(tempfile.mkdtemp()) / "record.yaml"
    with _env(RIG_VEHICLE_LOCAL=str(_machine_file(data))):
        rc, _, err = _run("provision", "--export", str(record))
    assert rc == 0, err
    return record, yaml.safe_load(record.read_text())


def test_export_then_apply_with_identity_rebuilds_the_machine_file():
    import datetime
    from rig_cli import __version__
    donor = _machine_file(_DONOR)
    with _env(RIG_VEHICLE_LOCAL=str(donor)):
        rc, out, err = _run("provision", "--export")                    # standard output by default
        assert rc == 0, err
        record = yaml.safe_load(out)
        assert record["rig_provision"] == 1 and record["rig"] == __version__
        datetime.datetime.strptime(record["exported"], "%Y-%m-%dT%H:%M:%SZ")
        assert record["source"] == {"vehicle": "uav-03", "vehicle_id": 3, "file": str(donor)}
        assert record["values"] == {"vars": _DONOR["vars"], "env": _DONOR["env"]}
        assert record["machine"] == {k: v for k, v in _DONOR.items() if k not in ("vars", "env")}
        assert yaml.safe_load(donor.read_text()) == _DONOR              # export only reads
        path = pathlib.Path(tempfile.mkdtemp()) / "uav-03.yaml"
        rc, _, err = _run("provision", "--export", str(path))           # or into a file
        assert rc == 0, err
        written = yaml.safe_load(path.read_text())
        assert {k: written[k] for k in ("rig_provision", "source", "values", "machine")} == \
            {k: record[k] for k in ("rig_provision", "source", "values", "machine")}
    with _env(RIG_VEHICLE_LOCAL=str(_machine_file())):                  # nothing to export
        rc, _, err = _run("provision", "--export")
        assert rc == 1 and "NOT PROVISIONED" in err
    fresh = _machine_file()
    with _env(RIG_VEHICLE_LOCAL=str(fresh)):
        rc, _, err = _run("provision", "--apply", str(path), "--with-identity")
        assert rc == 0, err
    assert yaml.safe_load(fresh.read_text()) == _DONOR                  # nothing was dropped


def test_apply_by_default_moves_values_and_leaves_the_machine_alone():
    record, _ = _export(_DONOR)
    spare = _machine_file(_SPARE)
    with _env(RIG_VEHICLE_LOCAL=str(spare)):
        rc, _, err = _run("provision", "--apply", str(record))
    assert rc == 0, err
    got = yaml.safe_load(spare.read_text())
    assert got["vars"] == {"camera_ip": "10.160.3.25", "gimbal_trim": 12,
                           "altitude_band": 3}                          # merged, as --var merges
    assert got["env"] == {"KEEP": "x", "SIYI_IP": "10.160.3.25"}
    for key in ("vehicle", "vehicle_id", "platform", "arch", "data_dir", "images", "ros"):
        assert got[key] == _SPARE[key], key
    assert "run_capture" not in got and "replay" not in got             # the donor's own


def test_apply_writes_a_value_that_differs_only_in_its_yaml_type():
    # true is not 1 and 1.0 is not 1: a whole-marker reference keeps the type, and a typed
    # consumer (a ROS parameter) refuses the wrong one.
    from rig_cli.interpolate import substitute_scalar
    from rig_cli.manifest import load_manifest
    record, _ = _export({"vehicle": "uav-03", "vehicle_id": 3,
                         "vars": {"use_sensor": True, "scale": 1.0, "nested": {"on": True}},
                         "env": {"FEATURE": True}})
    spare = _machine_file({"vehicle": "uav-09", "vehicle_id": 9,
                           "vars": {"use_sensor": 1, "scale": 1, "nested": {"on": 1}},
                           "env": {"FEATURE": 1}})
    before = spare.read_text()
    with _env(RIG_VEHICLE_LOCAL=str(spare)):
        rc, _, err = _run("provision", "--apply", str(record), "--dry-run")
        assert rc == 0 and spare.read_text() == before, err
        for line in ("vars.use_sensor: 1 -> True", "vars.scale: 1 -> 1.0", "env.FEATURE: 1 -> True"):
            assert line in err, err
        assert "unchanged" not in err, err
        rc, _, err = _run("provision", "--apply", str(record))
        assert rc == 0 and "written" in err, err
        stored = yaml.safe_load(spare.read_text())
        assert stored["vars"]["use_sensor"] is True and stored["env"]["FEATURE"] is True
        assert type(stored["vars"]["scale"]) is float and stored["vars"]["nested"] == {"on": True}
        assert stored["vars"]["nested"]["on"] is True
        root = pathlib.Path(tempfile.mkdtemp())
        (root / "vehicle.yaml").write_text("vehicle: t\nvehicle_id: 9\nsensors: []\n")
        ctx = load_manifest(root).vars
        assert substitute_scalar("{{use_sensor}}", ctx, where="t") is True       # as rendered
        assert type(substitute_scalar("{{scale}}", ctx, where="t")) is float
        rc, _, err = _run("provision", "--apply", str(record))      # and now there is nothing to do
        assert rc == 0 and "nothing to change" in err and "1 ->" not in err, err


def test_apply_only_and_except_select_by_name():
    record, _ = _export(_DONOR)
    spare = _machine_file(_SPARE)
    with _env(RIG_VEHICLE_LOCAL=str(spare)):
        rc, _, err = _run("provision", "--apply", str(record), "--only", "altitude_band")
        assert rc == 0, err
        got = yaml.safe_load(spare.read_text())
        assert got["vars"] == {"camera_ip": "10.160.9.25", "gimbal_trim": 12, "altitude_band": 3}
        assert got["env"] == _SPARE["env"]                              # not named, not applied
    spare = _machine_file(_SPARE)
    with _env(RIG_VEHICLE_LOCAL=str(spare)):
        rc, _, err = _run("provision", "--apply", str(record), "--except", "camera_ip")
        assert rc == 0, err
        got = yaml.safe_load(spare.read_text())
        assert got["vars"] == {"camera_ip": "10.160.9.25", "gimbal_trim": 12, "altitude_band": 3}
        assert got["env"] == {"KEEP": "x", "SIYI_IP": "10.160.3.25"}
        before = spare.read_bytes()
        for flag in ("--only", "--except"):                             # a typo applies nothing
            rc, _, err = _run("provision", "--apply", str(record), flag,
                              "altitude_band,altitude_bnad")
            assert rc == 1 and "altitude_bnad" in err, err
            assert spare.read_bytes() == before


def test_apply_dry_run_prints_every_change_and_writes_nothing():
    donor = dict(_DONOR, vars={**_DONOR["vars"], "gimbal_trim": 12})    # one value both hold
    record, _ = _export(donor)
    spare = _machine_file(_SPARE)
    before = spare.read_bytes()

    def line(text: str, key: str) -> str:
        return next((ln for ln in text.splitlines() if key in ln), "")

    with _env(RIG_VEHICLE_LOCAL=str(spare)):
        rc, out, err = _run("provision", "--apply", str(record), "--dry-run")
        assert rc == 0, err
        assert spare.read_bytes() == before
        said = out + err
        assert "10.160.9.25" in line(said, "camera_ip") and "10.160.3.25" in line(said, "camera_ip")
        assert "3" in line(said, "altitude_band")                       # new here
        assert "unchanged" in line(said, "gimbal_trim")
        assert "10.160.3.25" in line(said, "SIYI_IP")
        rc, out, err = _run("provision", "--apply", str(record))        # the real one says it too
        assert rc == 0, err
        assert "10.160.9.25" in line(out + err, "camera_ip")
        assert spare.read_bytes() != before


def _runs_under_two_provisionings() -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    """(root, data, run dir): one run, two `up`s — the band was re-provisioned in between."""
    from rig_cli import runs
    root = _fleet_deployment()
    data = pathlib.Path(tempfile.mkdtemp()) / "data"
    machine = _machine_file()
    with _env(RIG_VEHICLE_LOCAL=str(machine)):
        for band in (3, 4):
            machine.write_text(yaml.safe_dump(
                {"vehicle": "uav-03", "vehicle_id": 3, "data_dir": str(data), "run_capture": False,
                 "vars": {"altitude_band": band}}, sort_keys=False))
            m = materialize_manifest(load_manifest(root), root)
            rid = runs.ensure(m, root)
            assert runs.snapshot(m, root, stacks=["cam"])
    return root, data, data / "runs" / rid


def test_apply_from_a_run_reads_the_machine_file_it_captured():
    root, data, run_dir = _runs_under_two_provisionings()
    spare = _machine_file({"vehicle": "spare-1", "vehicle_id": 9, "data_dir": str(data)})
    with _env(RIG_VEHICLE_LOCAL=str(spare)):
        rc, _, err = _run("provision", "--apply", str(run_dir))         # a run directory
        assert rc == 0, err
        got = yaml.safe_load(spare.read_text())
        assert got["vars"] == {"altitude_band": 4}                      # the LAST up's file
        assert (got["vehicle"], got["vehicle_id"]) == ("spare-1", 9)    # values only, as ever
        spare.write_text(yaml.safe_dump({"vehicle": "spare-1", "vehicle_id": 9,
                                         "data_dir": str(data)}, sort_keys=False))
        rc, _, err = _run("--root", str(root), "provision", "--apply", run_dir.name)  # or its id
        assert rc == 0, err
        assert yaml.safe_load(spare.read_text())["vars"] == {"altitude_band": 4}
        # a run made by an older rig captured no machine file; vars.yaml mixes every tier and
        # is no substitute
        old = next((pathlib.Path(__file__).resolve().parent / "fixtures" / "run-v0.2.84"
                    / "data" / "runs").iterdir())
        before = spare.read_bytes()
        rc, _, err = _run("provision", "--apply", str(old))
        assert rc == 1 and "machine file" in err and "0.2.84" in err, err
        assert spare.read_bytes() == before


def test_apply_with_identity_keeps_the_force_gate():
    record, _ = _export(_DONOR)
    spare = _machine_file(_SPARE)
    before = spare.read_bytes()
    with _env(RIG_VEHICLE_LOCAL=str(spare)):
        rc, _, err = _run("provision", "--apply", str(record), "--with-identity")
        assert rc == 1 and "ORPHANING" in err, err
        assert spare.read_bytes() == before
        rc, _, err = _run("provision", "--apply", str(record), "--with-identity", "--force")
        assert rc == 0, err
    got = yaml.safe_load(spare.read_text())
    assert (got["vehicle"], got["vehicle_id"], got["platform"]) == ("uav-03", 3, "jp7")
    assert got["data_dir"] == "/home/uxv/logs" and got["ros"] == {"domain_id": 42}


def test_export_needs_no_root_and_apply_names_sudo_when_it_cannot_write():
    if os.geteuid() == 0:
        return  # root writes anywhere: there is no refusal to observe
    record, _ = _export(dict(_DONOR, vars={"altitude_band": 8}))
    machine = _machine_file(_DONOR)
    before = machine.read_bytes()
    machine.chmod(0o444)
    machine.parent.chmod(0o555)
    try:
        with _env(RIG_VEHICLE_LOCAL=str(machine)):
            rc, out, err = _run("provision", "--export")
            assert rc == 0, err
            assert yaml.safe_load(out)["source"]["vehicle"] == "uav-03"
            rc, _, err = _run("provision", "--apply", str(record), "--dry-run")
            assert rc == 0, err                                         # looking needs no root
            rc, _, err = _run("provision", "--apply", str(record))
            assert rc == 1 and "sudo" in err, err
            assert machine.read_bytes() == before
    finally:
        machine.parent.chmod(0o755)
        machine.chmod(0o644)


def test_apply_refuses_a_record_it_cannot_validate():
    import copy
    _, good = _export(_DONOR)
    spare = _machine_file(_SPARE)
    before = spare.read_bytes()

    def newer(r): r["rig_provision"] = 2

    def stray_key(r): r["machine"]["sensors"] = []

    def bad_name(r): r["values"]["vars"]["Altitude-Band"] = 3

    def relative_dir(r): r["machine"]["data_dir"] = "logs"

    with _env(RIG_VEHICLE_LOCAL=str(spare)):
        for edit, said in ((newer, "rig_provision"), (stray_key, "sensors"),
                           (bad_name, "Altitude-Band"), (relative_dir, "absolute")):
            record = copy.deepcopy(good)
            edit(record)
            path = pathlib.Path(tempfile.mkdtemp()) / "record.yaml"
            path.write_text(yaml.safe_dump(record, sort_keys=False))
            rc, _, err = _run("provision", "--apply", str(path), "--with-identity", "--force")
            assert rc == 1 and said in err.replace("ABSOLUTE", "absolute"), (said, err)
            assert spare.read_bytes() == before


def test_provision_show_and_doctor_name_where_each_value_comes_from():
    root = _fleet_deployment()
    machine = _machine_file({"vehicle": "skiff", "vehicle_id": 4})

    def names(text: str, key: str, source: str) -> bool:
        return any(key in ln and source in ln for ln in text.splitlines())

    with _env(RIG_VEHICLE_LOCAL=str(machine)):
        rc, _, err = _run("--root", str(root), "provision")
        assert rc == 0 and "all vars satisfied" in err, err
        assert names(err, "rtsp_port", "vehicle.yaml"), err
        assert names(err, "vehicle_id", str(machine)), err
        rc, _, err = _run("--root", str(root), "doctor")
        assert names(err, "rtsp_port", "vehicle.yaml"), err
        assert names(err, "vehicle_id", str(machine)), err


# --- v0.2.51: fleet-ness is decided by VALUES, not text (finding 8) ------------------------------

def test_fleet_detection_ignores_comments_and_unreferenced_configs():
    """rig init's own scaffold documents the fleet vocabulary in COMMENTS ({{gcs_ip}},
    {{fleet_ids}}, {{map fleet_peer_ids peer_endpoint}}); the text scan read a literal
    `rig init --vehicle-id 7` tree as a fleet and refused --bundle-images / --registry on it.
    A stray config nobody's row references doesn't vote either."""
    root = pathlib.Path(tempfile.mkdtemp())
    (root / "config" / "sensors").mkdir(parents=True)
    (root / "vehicle.yaml").write_text(
        "vehicle: t\nvehicle_id: 7\n"
        "# vars:\n#   gcs_ip: 10.0.0.10   # {{fleet_ids}}, {{gcs_ip}}, {{fleet_mode}}\n"
        '#   connect: "{{map fleet_peer_ids peer_endpoint}}"\n'
        "sensors:\n  - {name: cam, service: camsvc, config: config/sensors/cam.yaml}  # {{vehicle}}\n")
    (root / "config" / "sensors" / "cam.yaml").write_text("service: camsvc\nname: cam\n# {{rtsp_port}}\n")
    (root / "config" / "sensors" / "unused.yaml").write_text("service: camsvc\nurl: '{{gcs_ip}}'\n")
    assert fleet_refs(root) == set()
    assert not is_fleet(root)
    # ...and a REAL reference in a referenced file still counts, map form included
    (root / "config" / "sensors" / "cam.yaml").write_text(
        "service: camsvc\nname: cam\npeers: '{{map fleet_peer_ids peer_endpoint}}'\n")
    assert {"fleet_peer_ids", "peer_endpoint"} <= fleet_refs(root)


def test_fleet_detection_counts_pins_overlays_and_unparseable_files():
    """The layers rig composes under a working config vote too, and a file YAML can't parse (a
    marker STARTING an unquoted value is the classic) falls back to the text scan rather than
    silently reading as literal."""
    root = pathlib.Path(tempfile.mkdtemp())
    (root / "config" / "sensors").mkdir(parents=True)
    (root / "config" / ".overlays").mkdir()
    (root / "vehicle.yaml").write_text(
        "vehicle: t\nvehicle_id: 7\nsensors:\n  - {name: cam, service: camsvc, config: config/sensors/cam.yaml}\n")
    (root / "config" / "sensors" / "cam.yaml").write_text("service: camsvc\nname: cam\n")
    assert not is_fleet(root)
    (root / "config" / ".overlays" / "proj.yaml").write_text("gcs: {ip: '{{gcs_ip}}'}\n")
    assert fleet_refs(root) == {"gcs_ip"}
    (root / "config" / "sensors" / "cam.yaml").write_text("service: camsvc\nname: cam\nid: {{vehicle_id}}\n")
    assert "vehicle_id" in fleet_refs(root)    # unquoted marker -> load error -> text fallback

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
