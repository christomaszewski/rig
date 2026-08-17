"""Overlay bindings: apply/remove/reorder/list, four-layer render precedence, diff attribution.
Run: python3 tests/test_overlay.py"""
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
from rig_cli.resolve import materialize_manifest  # noqa: E402


def _install_acme(root):
    rc, _, err = _run("--root", str(root), "pkg", "install", "sensor:acme")
    assert rc == 0, err
    return root / "config" / "sensors" / "acme_cam.yaml"


def _rendered(root) -> dict:
    manifest = materialize_manifest(load_manifest(root), root)
    sensor = next(s for s in manifest.sensors if s.name == "acme_cam")
    return yaml.safe_load(pathlib.Path(sensor.config).read_text())


def test_apply_binds_and_renders_layer2():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        _install_acme(root)
        rc, _, err = _run("--root", str(root), "overlay", "apply", "acme_cam", "testns/cam-tune")
        assert rc == 0, err
        assert "overlays: [testns/cam-tune@1.0.0]" in (root / "vehicle.yaml").read_text()
        assert (root / "config" / ".overlays" / "testns--cam-tune--1.0.0.yaml").is_file()
        cfg = _rendered(root)
        assert cfg["usb"]["width"] == 1600 and cfg["usb"]["fps"] == 25   # overlay beats base
        lock = load_lock(root)
        assert lock["instances"]["acme_cam"]["overlays"] == ["testns/cam-tune@1.0.0"]
        assert "testns/cam-tune@1.0.0" in lock["packages"]


def test_local_beats_overlay_and_override_beats_all():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        working = _install_acme(root)
        _run("--root", str(root), "overlay", "apply", "acme_cam", "testns/cam-tune")
        working.write_text(working.read_text().replace("width: 1280", "width: 640"))  # local edit
        cfg = _rendered(root)
        assert cfg["usb"]["width"] == 640                                # LOCAL beats overlay
        assert cfg["usb"]["fps"] == 25                                   # untouched overlay key stays
        veh = root / "vehicle.yaml"
        veh.write_text(veh.read_text().replace(
            "enabled: true, order: 10 }", "overrides: {usb: {width: 99}}, enabled: true, order: 10 }"))
        assert _rendered(root)["usb"]["width"] == 99                     # override beats everything


def test_reorder_changes_last_wins():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        _install_acme(root)
        _run("--root", str(root), "overlay", "apply", "acme_cam", "testns/cam-tune")
        _run("--root", str(root), "overlay", "apply", "acme_cam", "testns/cam-tune-b")
        assert _rendered(root)["usb"]["width"] == 2222                   # b bound last -> wins
        rc, _, err = _run("--root", str(root), "overlay", "reorder", "acme_cam",
                          "cam-tune-b", "cam-tune")
        assert rc == 0, err
        assert _rendered(root)["usb"]["width"] == 1600                   # deterministic flip
        assert _rendered(root)["usb"]["fps"] == 25
        rc, _, err = _run("--root", str(root), "overlay", "reorder", "acme_cam", "cam-tune")
        assert rc == 1 and "COMPLETE" in err


def test_apply_guards():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        _install_acme(root)
        rc, _, err = _run("--root", str(root), "overlay", "apply", "acme_cam", "testns/acme-cam")
        assert rc == 1 and "not an overlay" in err
        rc, _, err = _run("--root", str(root), "add", "testns/routerish") and (0, "", "")
        rc, _, err = _run("--root", str(root), "overlay", "apply", "routerish", "testns/cam-tune")
        assert rc == 1 and "does not target" in err
        _run("--root", str(root), "overlay", "apply", "acme_cam", "testns/cam-tune")
        rc, _, err = _run("--root", str(root), "overlay", "apply", "acme_cam", "testns/cam-tune")
        assert rc == 1 and "already bound" in err


def test_remove_unbinds_and_drops_last_copy():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        _install_acme(root)
        _run("--root", str(root), "overlay", "apply", "acme_cam", "testns/cam-tune")
        copy = root / "config" / ".overlays" / "testns--cam-tune--1.0.0.yaml"
        assert copy.is_file()
        rc, _, err = _run("--root", str(root), "overlay", "remove", "acme_cam", "cam-tune")
        assert rc == 0, err
        assert "overlays:" not in (root / "vehicle.yaml").read_text()
        assert not copy.exists()                                          # last binding -> copy dropped
        assert "testns/cam-tune@1.0.0" not in (load_lock(root).get("packages") or {})


def test_diff_attributes_overlay_keys_and_delta_is_vs_layers12():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        working = _install_acme(root)
        _run("--root", str(root), "overlay", "apply", "acme_cam", "testns/cam-tune")
        rc, out, _ = _run("--root", str(root), "config", "diff", "acme_cam")
        assert rc == 0 and "clean" in out
        assert "= usb.width: 1600  [overlay testns/cam-tune@1.0.0]" in out
        working.write_text(working.read_text().replace("width: 1280", "width: 640"))
        rc, out, _ = _run("--root", str(root), "config", "diff", "acme_cam")
        assert "~ usb.width: 1280 -> 640  [local edit]" in out            # vs the PIN (your edit surface)
        assert "= usb.width: 1600  [overlay testns/cam-tune@1.0.0]  (masked by local)" in out


def test_apply_clear_local_resets_working_copy():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        working = _install_acme(root)
        pristine = working.read_text()
        working.write_text(pristine.replace("width: 1280", "width: 640"))
        rc, _, err = _run("--root", str(root), "overlay", "apply", "acme_cam", "testns/cam-tune",
                          "--clear-local")
        assert rc == 0, err
        assert working.read_text() == pristine                            # reset to the pin
        rc, out, _ = _run("--root", str(root), "config", "diff", "acme_cam")
        assert "clean" in out


def test_list_bindings():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        _install_acme(root)
        rc, out, _ = _run("--root", str(root), "overlay", "list")
        assert rc == 0 and "no overlay bindings" in out
        _run("--root", str(root), "overlay", "apply", "acme_cam", "testns/cam-tune")
        rc, out, _ = _run("--root", str(root), "overlay", "list")
        assert "acme_cam:" in out and "1. testns/cam-tune@1.0.0" in out


# --- binding hygiene (v0.1.62) ------------------------------------------------------------------


def _hand_row(root, name="handy", service="camish"):
    (root / "config" / "sensors" / f"{name}.yaml").write_text(
        "camera: {type: usb}\nusb: {width: 111}\n")
    veh = root / "vehicle.yaml"
    body = veh.read_text()
    anchor = next(line for line in body.splitlines() if "name: acme_cam" in line)
    indent = anchor[:len(anchor) - len(anchor.lstrip())]
    veh.write_text(body.replace(anchor, anchor + "\n" + indent +
                                f"- {{ name: {name}, service: {service}, "
                                f"config: config/sensors/{name}.yaml, enabled: true, order: 30 }}"))


def test_apply_rebinds_same_package_at_new_version():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, reg = _world()
        _install_acme(root)
        _run("--root", str(root), "overlay", "apply", "acme_cam", "testns/cam-tune")
        mpath = reg / "overlays" / "cam-tune" / "manifest.yaml"
        m = yaml.safe_load(mpath.read_text())
        m["version"] = "1.1.0"
        mpath.write_text(yaml.safe_dump(m, sort_keys=False))
        (reg / "overlays" / "cam-tune" / "config" / "delta.yaml").write_text(
            "usb: {width: 1700, fps: 25}\n")
        _run("registry", "index", str(reg))
        rc, _, err = _run("--root", str(root), "overlay", "apply", "acme_cam", "testns/cam-tune")
        assert rc == 0, err
        assert "rebound" in err                       # two versions of one overlay never coexist
        assert "overlays: [testns/cam-tune@1.1.0]" in (root / "vehicle.yaml").read_text()
        assert not (root / "config" / ".overlays" / "testns--cam-tune--1.0.0.yaml").exists()
        lock = load_lock(root)
        assert "testns/cam-tune@1.0.0" not in lock["packages"]
        assert _rendered(root)["usb"]["width"] == 1700


def test_apply_pinless_records_no_anchor_and_lock_stays_green():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        _install_acme(root)
        _hand_row(root)
        rc, _, err = _run("--root", str(root), "overlay", "apply", "handy", "testns/cam-tune")
        assert rc == 0, err
        assert "handy" not in (load_lock(root).get("instances") or {})  # no synthesized anchor
        assert _run("--root", str(root), "pkg", "lock")[0] == 0         # pkg lock stays green


def test_clear_local_refuses_without_pin():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        _install_acme(root)
        _hand_row(root)
        veh_before = (root / "vehicle.yaml").read_text()
        rc, _, err = _run("--root", str(root), "overlay", "apply", "handy", "testns/cam-tune",
                          "--clear-local")
        assert rc == 1 and "no pinned base" in err and "irreversible" in err
        assert (root / "vehicle.yaml").read_text() == veh_before        # refused BEFORE mutating


def test_apply_warns_on_authored_against_drift():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, reg = _world()
        _install_acme(root)                                             # pins testns/camish@1.2.0
        mpath = reg / "overlays" / "cam-tune" / "manifest.yaml"
        m = yaml.safe_load(mpath.read_text())
        m["authored_against"] = {"service": "testns/camish@1.1.0"}      # older than the pin
        mpath.write_text(yaml.safe_dump(m, sort_keys=False))
        _run("registry", "index", str(reg))
        rc, _, err = _run("--root", str(root), "overlay", "apply", "acme_cam", "testns/cam-tune")
        assert rc == 0, err
        assert "authored against testns/camish@1.1.0" in err and "re-verify" in err


def test_remove_ambiguous_versions_errors():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        _install_acme(root)
        _run("--root", str(root), "overlay", "apply", "acme_cam", "testns/cam-tune")
        veh = root / "vehicle.yaml"                   # hand-edited row with two versions bound
        veh.write_text(veh.read_text().replace(
            "overlays: [testns/cam-tune@1.0.0]",
            "overlays: [testns/cam-tune@1.0.0, testns/cam-tune@1.1.0]"))
        rc, _, err = _run("--root", str(root), "overlay", "remove", "acme_cam", "cam-tune")
        assert rc == 1 and "ambiguous" in err and "fully-qualified" in err


def test_overlay_list_is_a_status_view():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, reg = _world()
        working = _install_acme(root)
        _run("--root", str(root), "overlay", "apply", "acme_cam", "testns/cam-tune")
        working.write_text(working.read_text().replace("width: 1280", "width: 640"))  # masks usb.width
        mpath = reg / "overlays" / "cam-tune" / "manifest.yaml"    # registry moves, NO upgrade run
        m = yaml.safe_load(mpath.read_text())
        m["version"] = "1.1.0"
        mpath.write_text(yaml.safe_dump(m, sort_keys=False))
        _run("registry", "index", str(reg))
        rc, out, _ = _run("--root", str(root), "overlay", "list", "acme_cam")
        assert rc == 0, out
        assert "1.1.0 available" in out                            # upgrade signal
        assert "1 key(s) masked by local" in out                   # usb.width: local beats overlay
        (root / "config" / ".overlays" / "testns--cam-tune--1.0.0.yaml").unlink()
        rc, out, _ = _run("--root", str(root), "overlay", "list", "acme_cam")
        assert "payload copy MISSING" in out


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
