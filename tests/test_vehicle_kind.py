"""kind `vehicle` (v0.2.17) — the suite's instance PLAN: capture (template identity, unversioned
row refs), plan-driven install (names/order/enabled/tiers/per-row bindings), validation (identity
literals, versioned refs, closure both directions), and the guards.
Run: python3 tests/test_vehicle_kind.py"""
import contextlib
import io
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import yaml  # noqa: E402

from test_install import _env, _run, _world  # noqa: E402
from test_promote import _install_acme, _internal, _rendered  # noqa: E402

from rig_cli.init import init  # noqa: E402
from rig_cli.manifest import load_manifest  # noqa: E402


def _fresh() -> pathlib.Path:
    root2 = pathlib.Path(tempfile.mkdtemp()) / "veh2"
    with contextlib.redirect_stderr(io.StringIO()):
        init(root2, no_git=True)
    return root2


def _capture_world():
    """Origin: TWO instances of one profile (custom names), custom order, one disabled, a
    literal platform, and a dirty first instance (its delta becomes the plan's overlay)."""
    root, _ = _world()
    internal = _internal()
    assert _run("--root", str(root), "pkg", "install", "sensor:acme")[0] == 0
    assert _run("--root", str(root), "pkg", "add", "testns/camish:acme-cam", "--as", "cam_rear")[0] == 0
    veh = root / "vehicle.yaml"
    veh.write_text(veh.read_text().replace(
        "name: cam_rear, service: camish, config: config/sensors/cam_rear.yaml, "
        "profile: testns/camish:acme-cam@2.0.0, enabled: true, order: 20",
        "name: cam_rear, service: camish, config: config/sensors/cam_rear.yaml, "
        "profile: testns/camish:acme-cam@2.0.0, enabled: false, order: 77") + "platform: jp7\n")
    working = root / "config" / "sensors" / "acme_cam.yaml"
    working.write_text(working.read_text().replace("width: 1280", "width: 640"))
    return root, internal


def test_capture_emits_template_and_plan_install_reproduces():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, internal = _capture_world()
        rc, _, err = _run("--root", str(root), "pkg", "promote", "--all", "--suite", "boat",
                          "--vehicle", "gideon", "--to", "internal")
        assert rc == 0, err
        vp = yaml.safe_load((internal / "vehicles" / "gideon" / "config" / "vehicle.yaml").read_text())
        assert vp["vehicle"] == "{{vehicle}}" and vp["vehicle_id"] == "{{vehicle_id}}"
        assert vp["platform"] == "jp7"                       # fleet DEFAULT: literal allowed
        rows = {r["name"]: r for r in vp["sensors"]}
        assert rows["cam_rear"]["enabled"] is False and rows["cam_rear"]["order"] == 77
        assert rows["acme_cam"]["profile"] == "testns/camish:acme-cam"      # UNVERSIONED
        assert rows["acme_cam"]["overlays"] == ["internal/acme-cam"]        # fresh delta folded in
        s = yaml.safe_load((internal / "suites" / "boat" / "manifest.yaml").read_text())
        assert s["members"]["vehicles"] == ["internal/gideon@1.0.0"]
        assert _run("registry", "validate", str(internal))[0] == 0
        # origin: bind the new overlay so origin render == plan render
        assert _run("--root", str(root), "overlay", "apply", "acme_cam", "internal/acme-cam",
                    "--clear-local")[0] == 0
        root2 = _fresh()
        rc, _, err = _run("--root", str(root2), "pkg", "install", "internal/boat")
        assert rc == 0, err
        m2 = load_manifest(root2)
        got = {s.name: (s.enabled, s.order, list(s.overlays)) for s in m2.sensors}
        assert got == {"acme_cam": (True, 10, ["internal/acme-cam@1.0.0"]),
                       "cam_rear": (False, 77, [])}          # names/order/enabled/bindings travel
        assert m2.platform == "jp7"
        assert m2.vehicle == "veh2" and m2.vehicle_id == 1   # TARGET identity survives the plan
        assert _rendered(root2, "acme_cam") == _rendered(root, "acme_cam")
        assert _rendered(root2, "cam_rear") == _rendered(root, "cam_rear")


def test_plan_refuses_populated_deployment_and_standalone_add():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, internal = _capture_world()
        rc, _, err = _run("--root", str(root), "pkg", "promote", "--all", "--suite", "boat",
                          "--vehicle", "gideon", "--to", "internal")
        assert rc == 0, err
        root2 = _fresh()
        assert _run("--root", str(root2), "pkg", "add", "testns/camish")[0] == 0  # populate
        rc, _, err = _run("--root", str(root2), "pkg", "add", "internal/boat")
        assert rc == 1 and "EMPTY" in err                    # plan installs need a fresh tree
        rc, _, err = _run("--root", str(_fresh()), "pkg", "add", "internal/gideon")
        assert rc == 1 and "suite" in err                    # a vehicle never installs standalone


def test_vehicle_requires_suite_flag():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        _install_acme(root)
        _internal()
        rc, _, err = _run("--root", str(root), "pkg", "promote", "--all", "--vehicle", "g",
                          "--to", "internal")
        assert rc == 1 and "--suite" in err


def test_validate_flags_identity_literals_versioned_refs_and_closure():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, internal = _capture_world()
        rc, _, err = _run("--root", str(root), "pkg", "promote", "--all", "--suite", "boat",
                          "--vehicle", "gideon", "--to", "internal")
        assert rc == 0, err
        payload = internal / "vehicles" / "gideon" / "config" / "vehicle.yaml"
        pristine = payload.read_text()

        vp = yaml.safe_load(pristine)
        vp["vehicle_id"] = 7                                  # literal identity in a shared package
        payload.write_text(yaml.safe_dump(vp, sort_keys=False))
        rc, out, err = _run("registry", "validate", str(internal))
        assert rc == 1 and "self-marker" in (out + err)

        vp = yaml.safe_load(pristine)
        vp["sensors"][0]["profile"] = "testns/camish:acme-cam@2.0.0"   # versioned row ref
        payload.write_text(yaml.safe_dump(vp, sort_keys=False))
        rc, out, err = _run("registry", "validate", str(internal))
        assert rc == 1 and "VERSIONED" in (out + err)

        vp = yaml.safe_load(pristine)
        vp["sensors"][0]["overlays"] = ["internal/nonexistent"]        # row ref with no member
        payload.write_text(yaml.safe_dump(vp, sort_keys=False))
        rc, out, err = _run("registry", "validate", str(internal))
        assert rc == 1 and "not a member" in (out + err)

        payload.write_text(pristine)
        assert _run("registry", "validate", str(internal))[0] == 0


def test_repromote_recaptures_plan_and_repin_refreshes_member():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, internal = _capture_world()
        rc, _, err = _run("--root", str(root), "pkg", "promote", "--all", "--suite", "boat",
                          "--vehicle", "gideon", "--to", "internal")
        assert rc == 0, err
        assert _run("--root", str(root), "overlay", "apply", "acme_cam", "internal/acme-cam",
                    "--clear-local")[0] == 0
        # Layout-only change; re-promote WITHOUT --vehicle: the plan re-captures under its name.
        veh = root / "vehicle.yaml"
        veh.write_text(veh.read_text().replace("order: 77", "order: 88"))
        rc, _, err = _run("--root", str(root), "pkg", "promote", "--all", "--suite", "boat",
                          "--to", "internal", "--bump")
        assert rc == 0, err
        assert "re-capturing" in err
        vp = yaml.safe_load((internal / "vehicles" / "gideon" / "config" / "vehicle.yaml").read_text())
        assert {r["name"]: r.get("order") for r in vp["sensors"]}["cam_rear"] == 88
        s = yaml.safe_load((internal / "suites" / "boat" / "manifest.yaml").read_text())
        assert s["members"]["vehicles"] == ["internal/gideon@1.0.1"]   # suite follows the new plan
        # repin: a stale suite (hand-rolled back) refreshes its vehicle member to head
        s["members"]["vehicles"] = ["internal/gideon@1.0.0"]
        s["version"] = "1.0.1"
        (internal / "suites" / "boat" / "manifest.yaml").write_text(yaml.safe_dump(s, sort_keys=False))
        rc, _, err = _run("pkg", "repin", "boat", "--to", "internal")
        assert rc == 0, err
        s2 = yaml.safe_load((internal / "suites" / "boat" / "manifest.yaml").read_text())
        assert s2["members"]["vehicles"] == ["internal/gideon@1.0.1"]
        # direct repin of the vehicle kind is refused with the re-capture pointer
        rc, _, err = _run("pkg", "repin", "gideon", "--to", "internal")
        assert rc == 1 and "UNVERSIONED" in err


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
