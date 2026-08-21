"""pkg remove — the inverse of pkg add: rows, configs, anchors, binding refcounts, service GC.
Run: python3 tests/test_remove.py"""
import contextlib
import io
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import yaml  # noqa: E402

from test_install import _env, _run, _world  # noqa: E402  (shared fixtures)

from rig_cli.lock import load_lock  # noqa: E402
from rig_cli.manifest import load_manifest  # noqa: E402


def test_remove_profile_instance_gcs_everything():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        assert _run("--root", str(root), "pkg", "add", "sensor:acme")[0] == 0
        rc, _, err = _run("--root", str(root), "pkg", "remove", "acme_cam")
        assert rc == 0, err
        assert "DOWN first" in err                                     # the orphan warning
        assert not any(s.name == "acme_cam" for s in load_manifest(root).sensors)
        assert not (root / "config" / "sensors" / "acme_cam.yaml").exists()   # clean -> deleted
        assert not (root / "config" / ".pins" / "acme_cam.yaml").exists()
        assert not (root / "services" / "camish").exists()             # service GC'd (vendored)
        assert "camish:" not in (root / "services.yaml").read_text()   # route dropped
        lock = load_lock(root)
        assert not lock.get("packages") and not lock.get("instances")


def test_remove_keeps_dirty_config_unless_purged():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        assert _run("--root", str(root), "pkg", "add", "sensor:acme")[0] == 0
        working = root / "config" / "sensors" / "acme_cam.yaml"
        working.write_text(working.read_text() + "tuned: yes\n")       # local edits
        rc, _, err = _run("--root", str(root), "pkg", "remove", "acme_cam")
        assert rc == 0 and "kept" in err and "local edits" in err
        assert working.exists()
        working.unlink()
        # purge variant on a fresh install
        assert _run("--root", str(root), "pkg", "add", "sensor:acme")[0] == 0
        working.write_text(working.read_text() + "tuned: yes\n")
        rc, _, err = _run("--root", str(root), "pkg", "remove", "acme_cam", "--purge-config")
        assert rc == 0 and not working.exists()


def test_service_survives_while_second_instance_remains():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        assert _run("--root", str(root), "pkg", "add", "sensor:acme")[0] == 0
        assert _run("--root", str(root), "pkg", "add", "sensor:acme", "--as", "cam_b")[0] == 0
        assert _run("--root", str(root), "pkg", "remove", "acme_cam")[0] == 0
        assert (root / "services" / "camish").exists()                 # cam_b still needs it
        lock = load_lock(root)
        assert "testns/camish:acme-cam@2.0.0" in lock["packages"]             # profile still used by cam_b
        assert _run("--root", str(root), "pkg", "remove", "cam_b")[0] == 0
        assert not (root / "services" / "camish").exists()             # last user gone -> GC


def test_remove_unbinds_overlays_with_refcount():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        assert _run("--root", str(root), "pkg", "add", "sensor:acme")[0] == 0
        assert _run("--root", str(root), "overlay", "apply", "acme_cam", "testns/cam-tune")[0] == 0
        copy = root / "config" / ".overlays" / "testns--cam-tune--1.0.0.yaml"
        assert copy.exists()
        rc, _, err = _run("--root", str(root), "pkg", "remove", "acme_cam")
        assert rc == 0, err
        assert not copy.exists() and "last binding" in err
        assert "testns/cam-tune@1.0.0" not in (load_lock(root).get("packages") or {})


def test_overlay_payload_survives_while_second_binding_remains():
    # the KEEP side of the refcount: a payload copy bound to TWO instances outlives the first remove
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        assert _run("--root", str(root), "pkg", "add", "sensor:acme")[0] == 0
        assert _run("--root", str(root), "pkg", "add", "sensor:acme", "--as", "cam_b")[0] == 0
        assert _run("--root", str(root), "overlay", "apply", "acme_cam", "testns/cam-tune")[0] == 0
        assert _run("--root", str(root), "overlay", "apply", "cam_b", "testns/cam-tune")[0] == 0
        copy = root / "config" / ".overlays" / "testns--cam-tune--1.0.0.yaml"
        assert copy.exists()
        rc, _, err = _run("--root", str(root), "pkg", "remove", "acme_cam")
        assert rc == 0, err
        assert "last binding" not in err
        assert copy.exists()                                           # cam_b still binds it
        assert "testns/cam-tune@1.0.0" in (load_lock(root).get("packages") or {})
        rc, _, err = _run("--root", str(root), "pkg", "remove", "cam_b")
        assert rc == 0, err
        assert "last binding" in err and not copy.exists()             # last binding -> both gone
        assert "testns/cam-tune@1.0.0" not in (load_lock(root).get("packages") or {})


def test_package_form_guards_and_dependency_removal():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        assert _run("--root", str(root), "pkg", "add", "sensor:acme")[0] == 0
        rc, _, err = _run("--root", str(root), "pkg", "remove", "testns/camish")
        assert rc == 1 and "used by" in err and "acme_cam" in err      # in-use package refused
        rc, _, err = _run("--root", str(root), "pkg", "remove", "ghost-pkg")
        assert rc == 1 and "neither an instance" in err
        # a hand-wired instance removes too (v0.2.11+): row dropped, config KEPT (no pin)
        (root / "config" / "sensors").mkdir(parents=True, exist_ok=True)
        (root / "config" / "sensors" / "handmade.yaml").write_text(
            "service: camish\nname: handmade\n")
        veh = root / "vehicle.yaml"
        veh.write_text(veh.read_text().replace(
            "sensors:",
            "sensors:\n  - { name: handmade, service: camish, config: config/sensors/handmade.yaml, "
            "enabled: true, order: 99 }", 1))
        rc, _, err = _run("--root", str(root), "pkg", "remove", "handmade")
        assert rc == 0, err
        assert "hand-wired" in err
        assert not any(s.name == "handmade" for s in load_manifest(root).sensors)
        assert (root / "config" / "sensors" / "handmade.yaml").is_file()   # kept: no pin to prove clean


def test_remove_unlinks_stale_render():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        rc, _, err = _run("--root", str(root), "pkg", "install", "sensor:acme")
        assert rc == 0, err
        rendered = root / "var" / "rendered" / "acme_cam.yaml"
        rendered.parent.mkdir(parents=True, exist_ok=True)
        rendered.write_text("name: acme_cam\n")                   # a past render
        assert _run("--root", str(root), "pkg", "remove", "acme_cam")[0] == 0
        assert not rendered.exists()                              # no stale file for a gone instance


def test_removed_instance_is_readdable():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        assert _run("--root", str(root), "pkg", "add", "sensor:acme")[0] == 0
        assert _run("--root", str(root), "pkg", "remove", "acme_cam")[0] == 0
        rc, _, err = _run("--root", str(root), "pkg", "add", "sensor:acme")   # full round trip
        assert rc == 0, err
        assert any(s.name == "acme_cam" for s in load_manifest(root).sensors)


def _local_service(name: str, tier: str = "sensor") -> pathlib.Path:
    base = pathlib.Path(tempfile.mkdtemp())
    d = base / name
    (d / "config").mkdir(parents=True)
    (d / "rigging.yaml").write_text(
        f"service: {name}\nlauncher: {name}-up\ntier: {tier}\n"
        f"examples: [config/{name}.example.yaml]\nlaunch_surface: [{name}-up]\n")
    (d / f"{name}-up").write_text("#!/bin/sh\n")
    (d / f"{name}-up").chmod(0o755)
    (d / "config" / f"{name}.example.yaml").write_text(
        f"service: {name}\nname: {name.replace('-', '_')}\nrate: 1\n")
    return d


def test_remove_local_menu_service_by_name():
    # the F8 inverse: `pkg add <path>` (sensor tier -> menu row only) undone by `pkg rm <svc>`
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        d = _local_service("barish")
        assert _run("--root", str(root), "pkg", "add", str(d))[0] == 0
        assert "barish" in (root / "services.yaml").read_text()
        assert "service: barish" in (root / "vehicle.yaml").read_text()   # the menu comment
        rc, _, err = _run("--root", str(root), "pkg", "rm", "barish")
        assert rc == 0, err
        assert "barish" not in (root / "services.yaml").read_text()       # route gone
        assert "service: barish" not in (root / "vehicle.yaml").read_text()  # menu comment gone
        assert (root / "config" / "sensors" / "barish.yaml").is_file()    # config KEPT by default
        assert "kept" in err and "--purge-config" in err
        d2 = _local_service("purgish")
        assert _run("--root", str(root), "pkg", "add", str(d2))[0] == 0
        rc, _, err = _run("--root", str(root), "pkg", "rm", "purgish", "--purge-config")
        assert rc == 0, err
        assert not (root / "config" / "sensors" / "purgish.yaml").exists()
        assert d.is_dir() and d2.is_dir()                                 # checkouts NEVER touched


def test_remove_local_enabled_instance_gcs_the_route():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        d = _local_service("routerish-loc", tier="infra")                 # infra -> ENABLED row
        assert _run("--root", str(root), "pkg", "add", str(d))[0] == 0
        assert any(s.service == "routerish-loc" for s in load_manifest(root).sensors)
        # service-form refuses while the instance uses it
        rc, _, err = _run("--root", str(root), "pkg", "rm", "routerish-loc")
        assert rc == 1 and "used by instance" in err
        instance = next(s.name for s in load_manifest(root).sensors
                        if s.service == "routerish-loc")
        rc, _, err = _run("--root", str(root), "pkg", "rm", instance)     # hand-wired row removes
        assert rc == 0, err
        assert "hand-wired" in err
        assert not any(s.service == "routerish-loc" for s in load_manifest(root).sensors)
        assert "routerish-loc" not in (root / "services.yaml").read_text()  # route GC'd with it
        assert d.is_dir()


def test_rm_is_a_permanent_alias():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        assert _run("--root", str(root), "pkg", "add", "sensor:acme")[0] == 0
        rc, _, err = _run("--root", str(root), "pkg", "rm", "acme_cam")
        assert rc == 0, err
        assert not any(s.name == "acme_cam" for s in load_manifest(root).sensors)


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
