"""Working-copy layer: config diff attribution, pkg upgrade three-way, pkg lock verification.
Run: python3 tests/test_workingcopy.py"""
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
from rig_cli.resolve import deep_merge, structural_diff  # noqa: E402


def _install_acme(root):
    rc, _, err = _run("--root", str(root), "pkg", "install", "sensor:acme")
    assert rc == 0, err
    return root / "config" / "sensors" / "acme_cam.yaml"


def test_structural_diff_roundtrip_law():
    base = {"a": 1, "b": {"c": 2, "d": [1, 2]}, "e": "x"}
    cur = {"a": 1, "b": {"c": 5, "d": [9]}, "f": {"g": True}}   # e deleted, c changed, d replaced, f added
    patch = structural_diff(base, cur)
    assert patch == {"b": {"c": 5, "d": [9]}, "e": None, "f": {"g": True}}
    assert deep_merge(base, patch) == cur                        # THE law promote/apply relies on


def test_diff_clean_then_local_edit_then_override():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        working = _install_acme(root)
        rc, out, _ = _run("--root", str(root), "config", "diff")
        assert rc == 0 and "all pinned instances clean" in out
        working.write_text(working.read_text().replace("width: 1280", "width: 640")
                           + "extra: {knob: 7}\n")
        rc, out, _ = _run("--root", str(root), "config", "diff")
        assert "acme_cam: dirty" in out and "(base: testns/acme-cam@2.0.0)" in out
        assert "~ usb.width: 1280 -> 640  [local edit]" in out
        assert "+ extra.knob: 7  [local edit]" in out
        veh = root / "vehicle.yaml"
        veh.write_text(veh.read_text().replace(
            "profile: testns/acme-cam@2.0.0, enabled",
            "profile: testns/acme-cam@2.0.0, overrides: {camera: {frame_rate: 15}}, enabled"))
        rc, out, _ = _run("--root", str(root), "config", "diff", "acme_cam")
        assert "~ camera.frame_rate: -> 15  [override]" in out


def test_diff_reports_deletions():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        working = _install_acme(root)
        data = yaml.safe_load(working.read_text())
        del data["usb"]
        working.write_text(yaml.safe_dump(data, sort_keys=False))
        rc, out, _ = _run("--root", str(root), "config", "diff")
        assert "[deleted]" in out and "usb" in out


def test_upgrade_clean_takes_new_payload_verbatim():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, reg = _world()
        working = _install_acme(root)
        rc, _, err = _run("--root", str(root), "pkg", "upgrade")
        assert rc == 0 and "up to date" in err
        # registry moves: new version, new payload with a NEW comment
        mpath = reg / "profiles" / "acme-cam" / "manifest.yaml"
        m = yaml.safe_load(mpath.read_text())
        m["version"] = "2.1.0"
        mpath.write_text(yaml.safe_dump(m, sort_keys=False))
        (reg / "profiles" / "acme-cam" / "config" / "payload.yaml").write_text(
            "# v2.1 default\nservice: camish\ncamera: {type: usb}\nusb: {width: 1920}\n")
        _run("registry", "index", str(reg))
        rc, _, err = _run("--root", str(root), "pkg", "upgrade")
        assert rc == 0 and "-> testns/acme-cam@2.1.0" in err
        assert "# v2.1 default" in working.read_text()            # clean upgrade: comments survive
        assert "profile: testns/acme-cam@2.1.0" in (root / "vehicle.yaml").read_text()
        lock = load_lock(root)
        assert "testns/acme-cam@2.1.0" in lock["packages"]
        assert "testns/acme-cam@2.0.0" not in lock["packages"]


def test_upgrade_dirty_keeps_local_and_surfaces_conflicts():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, reg = _world()
        working = _install_acme(root)
        working.write_text(working.read_text().replace("width: 1280", "width: 640"))  # local edit
        mpath = reg / "profiles" / "acme-cam" / "manifest.yaml"
        m = yaml.safe_load(mpath.read_text())
        m["version"] = "3.0.0"
        mpath.write_text(yaml.safe_dump(m, sort_keys=False))
        (reg / "profiles" / "acme-cam" / "config" / "payload.yaml").write_text(
            "service: camish\ncamera: {type: usb}\nusb: {width: 3840, fps: 30}\n")  # base ALSO moves width
        _run("registry", "index", str(reg))
        rc, _, err = _run("--root", str(root), "pkg", "upgrade", "acme_cam")
        assert rc == 0, err
        assert "CONFLICT usb.width" in err and "keeping yours: 640" in err
        data = yaml.safe_load(working.read_text())
        assert data["usb"]["width"] == 640                        # local wins
        assert data["usb"]["fps"] == 30                           # new base key arrives
        rc, out, _ = _run("--root", str(root), "config", "diff", "acme_cam")
        assert "~ usb.width: 3840 -> 640  [local edit]" in out    # delta now vs the NEW pin


def test_relock_verifies_anchors():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        _install_acme(root)
        rc, _, err = _run("--root", str(root), "pkg", "lock")
        assert rc == 0 and "all anchors verified" in err
        pin = root / "config" / ".pins" / "acme_cam.yaml"
        pin.write_text(pin.read_text() + "# tampered\n")
        rc, _, err = _run("--root", str(root), "pkg", "lock")
        assert rc == 1 and "edited" in err


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
