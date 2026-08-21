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

from test_install import _run, _world  # noqa: E402  (shared fixtures)
from test_platform import _env  # noqa: E402  (None-aware: value None UNSETS the variable)
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
        with _env(RIG_VEHICLE_ID=None, RIG_VEHICLE_NAME=None):  # ambient per-host identity (the
            m2 = load_manifest(root2)                           # documented env) must not mask markers
        got = {s.name: (s.enabled, s.order, list(s.overlays)) for s in m2.sensors}
        assert got == {"acme_cam": (True, 10, ["internal/acme-cam@1.0.0"]),
                       "cam_rear": (False, 77, [])}          # names/order/enabled/bindings travel
        assert m2.platform == "jp7"
        # Identity: the init'd target carried MARKERS (v0.2.20 default) and the plan carries markers,
        # so the fresh tree stays per-host/mandatory-from-local — fleet-style reproduction.
        assert m2.vehicle_id is None and "vehicle_id" in m2.missing_identity
        assert _rendered(root2, "acme_cam") == _rendered(root, "acme_cam")
        assert _rendered(root2, "cam_rear") == _rendered(root, "cam_rear")
        # A DELIBERATE target identity (rig init --vehicle-id 7) survives the plan verbatim.
        root3 = pathlib.Path(tempfile.mkdtemp()) / "veh3"
        with contextlib.redirect_stderr(io.StringIO()):
            init(root3, no_git=True, vehicle_id=7)
        rc, _, err = _run("--root", str(root3), "pkg", "install", "internal/boat")
        assert rc == 0, err
        with _env(RIG_VEHICLE_ID=None, RIG_VEHICLE_NAME=None):  # shell wins by design — clear it
            m3 = load_manifest(root3)
        assert m3.vehicle == "veh3" and m3.vehicle_id == 7


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


def test_capture_preserves_overlay_order_fresh_delta_last():
    # ORDER IS MERGE ORDER: the suite's overlays member carries the existing bindings in binding
    # order with the just-emitted delta overlay appended LAST, and the plan row mirrors the same
    # order unversioned (apply-order parity — anything else changes last-wins precedence).
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        internal = _internal()
        _install_acme(root)
        assert _run("--root", str(root), "overlay", "apply", "acme_cam", "testns/cam-tune")[0] == 0
        assert _run("--root", str(root), "overlay", "apply", "acme_cam", "testns/cam-tune-b")[0] == 0
        working = root / "config" / "sensors" / "acme_cam.yaml"
        working.write_text(working.read_text().replace("width: 1280", "width: 640"))  # dirty delta
        rc, _, err = _run("--root", str(root), "pkg", "promote", "--all", "--suite", "boat",
                          "--vehicle", "gideon", "--to", "internal")
        assert rc == 0, err
        s = yaml.safe_load((internal / "suites" / "boat" / "manifest.yaml").read_text())
        assert s["members"]["overlays"] == ["testns/cam-tune@1.0.0", "testns/cam-tune-b@1.0.0",
                                            "internal/acme-cam@1.0.0"]   # bindings first, fresh LAST
        vp = yaml.safe_load((internal / "vehicles" / "gideon" / "config" / "vehicle.yaml").read_text())
        row = next(r for r in vp["sensors"] if r["name"] == "acme_cam")
        assert row["overlays"] == ["testns/cam-tune", "testns/cam-tune-b", "internal/acme-cam"]


def test_hand_authored_row_captured_as_adopted_profile():
    # A service with NO examples: install can't anchor an instance, so the config is
    # hand-authored. The suite capture must not skip it (an overlay is impossible — no base):
    # the FULL config becomes a PROFILE, the origin row is ADOPTED, the plan row references it,
    # and a fresh install reconstructs the row with render equality (v0.2.18).
    from test_install import _git  # the shared helper carries committer identity (CI has none)
    from test_promote import _dev_service

    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        internal = _internal()
        _install_acme(root)                        # a normal profile instance alongside
        route, _, _ = _dev_service(name="lidarish")
        (route / "rigging.yaml").write_text("service: lidarish\nlauncher: lidarish-up\n"
                                            "tier: sensor\nlaunch_surface: [lidarish-up]\n")
        (route / "config" / "lidarish.example.yaml").unlink()
        _git("add", "-A", cwd=route)
        _git("commit", "-q", "-m", "no examples", cwd=route)
        _git("push", "-q", "origin", "HEAD", cwd=route)
        assert _run("--root", str(root), "add", str(route))[0] == 0
        cfg = root / "config" / "sensors" / "lidar_main.yaml"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text("service: lidarish\nrate: 42\nrange_m: 120\n")
        veh = root / "vehicle.yaml"
        veh.write_text(veh.read_text().replace(
            "sensors:",
            "sensors:\n  - { name: lidar_main, service: lidarish, "
            "config: config/sensors/lidar_main.yaml, enabled: true, order: 30 }", 1))
        assert _run("--root", str(root), "pkg", "promote", "lidarish", "--kind", "service",
                    "--to", "internal", "--version", "0.0.1", "--adopt")[0] == 0
        # Without --adopt: adoption mutates the origin + auto-derives a package name, so the
        # capture refuses to do it silently — loud skip, row omitted, both fixes printed.
        rc, _, err = _run("--root", str(root), "pkg", "promote", "--all", "--suite", "boat",
                          "--vehicle", "gideon", "--to", "internal")
        assert rc == 0, err
        assert "NOT captured" in err and "--adopt" in err and "--name <short>" in err
        s = yaml.safe_load((internal / "suites" / "boat" / "manifest.yaml").read_text())
        assert s["members"]["profiles"] == ["testns/camish:acme-cam@2.0.0"]  # lidar absent
        assert "services" not in s["members"]
        vp = yaml.safe_load((internal / "vehicles" / "gideon" / "config" / "vehicle.yaml").read_text())
        assert not any(r["name"] == "lidar_main" for r in vp.get("sensors") or [])
        assert _run("registry", "validate", str(internal))[0] == 0    # consistent, just smaller
        row = next(x for x in load_manifest(root).sensors if x.name == "lidar_main")
        assert row.profile is None                                    # origin untouched
        # With --adopt: consent given — profile + adoption + full capture.
        rc, _, err = _run("--root", str(root), "pkg", "promote", "--all", "--suite", "boat",
                          "--vehicle", "gideon", "--to", "internal", "--adopt", "--bump")
        assert rc == 0, err
        assert "capturing as PROFILE and adopting" in err
        s = yaml.safe_load((internal / "suites" / "boat" / "manifest.yaml").read_text())
        assert s["members"]["profiles"] == ["internal/lidarish:lidar-main@1.0.0",
                                            "testns/camish:acme-cam@2.0.0"]
        assert "services" not in s["members"]                # the profile IS the row's base now
        vp = yaml.safe_load((internal / "vehicles" / "gideon" / "config" / "vehicle.yaml").read_text())
        assert vp["sensors"][0]["profile"] == "internal/lidarish:lidar-main"   # plan row adopted
        origin_row = next(x for x in load_manifest(root).sensors if x.name == "lidar_main")
        assert origin_row.profile == "internal/lidarish:lidar-main@1.0.0"      # origin adopted too
        assert _run("registry", "validate", str(internal))[0] == 0
        root2 = _fresh()
        rc, _, err = _run("--root", str(root2), "pkg", "install", "internal/boat")
        assert rc == 0, err
        assert _rendered(root2, "lidar_main") == _rendered(root, "lidar_main")
        # Second capture: the instance is a normal pinned row now — no re-profile, no churn.
        rc, _, err = _run("--root", str(root), "pkg", "promote", "--all", "--suite", "boat",
                          "--to", "internal", "--bump")
        assert rc == 0, err
        assert "capturing as PROFILE" not in err


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
