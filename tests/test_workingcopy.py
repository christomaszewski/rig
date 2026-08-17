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

from test_install import _env, _git, _run, _world  # noqa: E402  (shared fixtures)

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


def _bump_service(reg, svc, version, example_text):
    """New commit in the pinned code repo + manifest version/rev bump + reindex."""
    mpath = reg / "services" / svc / "manifest.yaml"
    m = yaml.safe_load(mpath.read_text())
    repo = pathlib.Path(m["source"]["repo"])
    (repo / svc / "config" / f"{svc}.example.yaml").write_text(example_text)
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "bump", cwd=repo)
    m["version"] = version
    m["source"]["rev"] = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    mpath.write_text(yaml.safe_dump(m, sort_keys=False))
    _run("registry", "index", str(reg))
    return m["source"]["rev"]


def test_upgrade_bare_service_instance_clean():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, reg = _world()
        assert _run("--root", str(root), "add", "testns/routerish")[0] == 0
        rev = _bump_service(reg, "routerish", "1.3.0",
                            "service: routerish\nname: routerish\nrate: 9\n")
        rc, _, err = _run("--root", str(root), "pkg", "upgrade")
        assert rc == 0, err
        assert "testns/routerish@1.2.0 -> testns/routerish@1.3.0" in err
        assert "rate: 9" in (root / "config" / "infra" / "routerish.yaml").read_text()
        lock = load_lock(root)
        assert "testns/routerish@1.3.0" in lock["packages"]
        assert "testns/routerish@1.2.0" not in lock["packages"]
        vend = yaml.safe_load((root / "services" / "routerish" / ".vendored.yaml").read_text())
        assert vend["ref"] == rev                                       # re-vendored at the new pin


def test_upgrade_bare_service_instance_dirty_keeps_local():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, reg = _world()
        assert _run("--root", str(root), "add", "testns/routerish")[0] == 0
        working = root / "config" / "infra" / "routerish.yaml"
        working.write_text(working.read_text().replace("rate: 5", "rate: 7"))   # local edit
        _bump_service(reg, "routerish", "1.3.0",
                      "service: routerish\nname: routerish\nrate: 9\nburst: 2\n")
        rc, _, err = _run("--root", str(root), "pkg", "upgrade", "routerish")
        assert rc == 0, err
        assert "CONFLICT rate" in err and "keeping yours: 7" in err
        data = yaml.safe_load(working.read_text())
        assert data["rate"] == 7 and data["burst"] == 2                 # local wins; new key arrives


def test_upgrade_repins_profile_required_service():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, reg = _world()
        _install_acme(root)
        rev = _bump_service(reg, "camish", "1.3.0", "service: camish\ncamera: {type: usb}\n")
        rc, _, err = _run("--root", str(root), "pkg", "upgrade")
        assert rc == 0, err
        assert "testns/camish@1.2.0 -> testns/camish@1.3.0" in err
        lock = load_lock(root)
        assert lock["packages"]["testns/acme-cam@2.0.0"]["requires"] == "testns/camish@1.3.0"
        assert "testns/camish@1.3.0" in lock["packages"]
        vend = yaml.safe_load((root / "services" / "camish" / ".vendored.yaml").read_text())
        assert vend["ref"] == rev


def test_readd_of_installed_package_errors_early_without_mutation():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, reg = _world()
        assert _run("--root", str(root), "add", "testns/routerish")[0] == 0
        old_ref = yaml.safe_load((root / "services" / "routerish" / ".vendored.yaml").read_text())["ref"]
        _bump_service(reg, "routerish", "1.3.0",
                      "service: routerish\nname: routerish\nrate: 9\n")
        rc, _, err = _run("--root", str(root), "add", "testns/routerish")
        assert rc == 1 and "already installed" in err and "pkg upgrade routerish" in err
        now = yaml.safe_load((root / "services" / "routerish" / ".vendored.yaml").read_text())["ref"]
        assert now == old_ref                                           # nothing mutated
        rc, _, err = _run("--root", str(root), "pkg", "install", "sensor:acme")
        assert rc == 0, err
        rc, _, err = _run("--root", str(root), "pkg", "install", "testns/acme-cam")
        assert rc == 1 and "pkg upgrade acme_cam" in err                # profile re-add too


def test_relock_verifies_anchors():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        _install_acme(root)
        rc, out, _ = _run("--root", str(root), "pkg", "lock")
        assert rc == 0 and "all anchors verified" in out   # report verb -> stdout
        pin = root / "config" / ".pins" / "acme_cam.yaml"
        pin.write_text(pin.read_text() + "# tampered\n")
        rc, out, _ = _run("--root", str(root), "pkg", "lock")
        assert rc == 1 and "edited" in out


# --- upgrade correctness batch (v0.1.62): overlays, rollback, collisions, --locked -------------


def _bump_overlay(reg, name, version, delta_text):
    mpath = reg / "overlays" / name / "manifest.yaml"
    m = yaml.safe_load(mpath.read_text())
    m["version"] = version
    mpath.write_text(yaml.safe_dump(m, sort_keys=False))
    (reg / "overlays" / name / "config" / "delta.yaml").write_text(delta_text)
    _run("registry", "index", str(reg))


def _hand_row(root, name="handy", service="camish"):
    """A hand-authored instance row (no pin, no provenance) next to the installed acme_cam."""
    (root / "config" / "sensors" / f"{name}.yaml").write_text(
        "camera: {type: usb}\nusb: {width: 111}\n")
    veh = root / "vehicle.yaml"
    body = veh.read_text()
    anchor = next(line for line in body.splitlines() if "name: acme_cam" in line)
    indent = anchor[:len(anchor) - len(anchor.lstrip())]
    veh.write_text(body.replace(anchor, anchor + "\n" + indent +
                                f"- {{ name: {name}, service: {service}, "
                                f"config: config/sensors/{name}.yaml, enabled: true, order: 30 }}"))


def test_upgrade_rebinds_bound_overlays_in_place():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, reg = _world()
        _install_acme(root)
        _run("--root", str(root), "overlay", "apply", "acme_cam", "testns/cam-tune")
        _run("--root", str(root), "overlay", "apply", "acme_cam", "testns/cam-tune-b")
        _bump_overlay(reg, "cam-tune", "1.1.0", "usb: {width: 1600, fps: 99}\n")
        rc, _, err = _run("--root", str(root), "pkg", "upgrade")
        assert rc == 0, err
        assert "overlay testns/cam-tune@1.0.0 -> testns/cam-tune@1.1.0" in err
        from rig_cli.manifest import load_manifest
        sensor = next(s for s in load_manifest(root).sensors if s.name == "acme_cam")
        assert list(sensor.overlays) == ["testns/cam-tune@1.1.0", "testns/cam-tune-b@1.0.0"]
        lock = load_lock(root)                                     # POSITION preserved ^
        assert "testns/cam-tune@1.1.0" in lock["packages"]
        assert "testns/cam-tune@1.0.0" not in lock["packages"]
        assert lock["instances"]["acme_cam"]["overlays"] == list(sensor.overlays)
        assert (root / "config" / ".overlays" / "testns--cam-tune--1.1.0.yaml").is_file()
        assert not (root / "config" / ".overlays" / "testns--cam-tune--1.0.0.yaml").exists()


def test_upgrade_profile_keeps_lock_binding_record():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, reg = _world()
        _install_acme(root)
        _run("--root", str(root), "overlay", "apply", "acme_cam", "testns/cam-tune")
        mpath = reg / "profiles" / "acme-cam" / "manifest.yaml"
        m = yaml.safe_load(mpath.read_text())
        m["version"] = "2.1.0"
        mpath.write_text(yaml.safe_dump(m, sort_keys=False))
        (reg / "profiles" / "acme-cam" / "config" / "payload.yaml").write_text(
            "service: camish\ncamera: {type: usb}\nusb: {width: 1920}\n")
        _run("registry", "index", str(reg))
        rc, _, err = _run("--root", str(root), "pkg", "upgrade")
        assert rc == 0, err
        lock = load_lock(root)
        assert lock["instances"]["acme_cam"]["profile"] == "testns/acme-cam@2.1.0"
        assert lock["instances"]["acme_cam"]["overlays"] == ["testns/cam-tune@1.0.0"]  # SURVIVES


def test_upgrade_failure_rolls_back_everything():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, reg = _world()
        assert _run("--root", str(root), "add", "testns/routerish")[0] == 0
        _install_acme(root)
        _bump_service(reg, "routerish", "1.3.0", "service: routerish\nname: routerish\nrate: 9\n")
        mpath = reg / "profiles" / "acme-cam" / "manifest.yaml"   # sabotage AFTER routerish mutates:
        m = yaml.safe_load(mpath.read_text())                     # profile bumps but payload vanishes
        m["version"] = "3.0.0"
        m["config"]["payload"] = "config/nope.yaml"
        mpath.write_text(yaml.safe_dump(m, sort_keys=False))
        _run("registry", "index", str(reg))
        before = {p.name: (root / p.name).read_text()
                  for p in (root / "vehicle.yaml", root / "rig.lock")}
        router_cfg = (root / "config" / "infra" / "routerish.yaml").read_text()
        vend = (root / "services" / "routerish" / ".vendored.yaml").read_text()
        rc, _, err = _run("--root", str(root), "pkg", "upgrade")
        assert rc == 1 and "rolled back" in err
        assert (root / "vehicle.yaml").read_text() == before["vehicle.yaml"]
        assert (root / "rig.lock").read_text() == before["rig.lock"]
        assert (root / "config" / "infra" / "routerish.yaml").read_text() == router_cfg
        assert (root / "services" / "routerish" / ".vendored.yaml").read_text() == vend


def test_upgrade_names_hand_wired_says_no_provenance():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        _install_acme(root)
        _hand_row(root)
        rc, _, err = _run("--root", str(root), "pkg", "upgrade", "handy")
        assert rc == 0 and "no registry provenance" in err


def test_relock_self_heals_legacy_empty_anchor():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        _install_acme(root)
        _hand_row(root)
        from rig_cli.lock import save_lock
        lock = load_lock(root)
        lock.setdefault("instances", {})["handy"] = {"base_sha256": ""}  # pre-v0.1.62 poison
        save_lock(root, lock)
        rc, out, _ = _run("--root", str(root), "pkg", "lock")
        assert rc == 0, out
        assert "dropping it" in out                        # report verb -> stdout
        assert "handy" not in (load_lock(root).get("instances") or {})


def test_install_errors_on_service_pin_collision():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, reg = _world()
        _install_acme(root)                                        # locks testns/camish@1.2.0
        old_vend = (root / "services" / "camish" / ".vendored.yaml").read_text()
        _bump_service(reg, "camish", "1.3.0", "service: camish\ncamera: {type: usb}\n")
        rc, _, err = _run("--root", str(root), "pkg", "install", "testns/camish", "--as", "cam2")
        assert rc == 1, "collision must refuse"
        assert "locked at testns/camish@1.2.0" in err and "pkg upgrade" in err
        assert (root / "services" / "camish" / ".vendored.yaml").read_text() == old_vend
        lock = load_lock(root)
        assert "testns/camish@1.2.0" in lock["packages"]
        assert "testns/camish@1.3.0" not in lock["packages"]       # no duplicate service rows


def test_install_locked_catches_rewritten_rev():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, reg = _world()
        _install_acme(root)
        mpath = reg / "services" / "camish" / "manifest.yaml"      # registry rewrites the rev
        m = yaml.safe_load(mpath.read_text())                      # UNDER the same version
        repo = pathlib.Path(m["source"]["repo"])
        (repo / "camish" / "tweak.txt").write_text("drift\n")
        _git("add", "-A", cwd=repo)
        _git("commit", "-q", "-m", "sneaky", cwd=repo)
        m["source"]["rev"] = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
        mpath.write_text(yaml.safe_dump(m, sort_keys=False))
        _run("registry", "index", str(reg))
        root2 = pathlib.Path(tempfile.mkdtemp()) / "veh2"
        from rig_cli.init import init
        with contextlib.redirect_stderr(io.StringIO()):
            init(root2, no_git=True)
        (root2 / "rig.lock").write_text((root / "rig.lock").read_text())  # reproduce from the lock
        rc, _, err = _run("--root", str(root2), "pkg", "install", "sensor:acme", "--locked")
        assert rc == 1 and "source.rev" in err and "provenance" in err


def test_install_failure_rolls_back_single_package():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        (root / "config" / "sensors").mkdir(parents=True, exist_ok=True)
        (root / "config" / "sensors" / "acme_cam.yaml").write_text("squatter: true\n")
        rc, _, err = _run("--root", str(root), "pkg", "install", "sensor:acme")
        assert rc == 1 and "rolled back" in err
        assert not (root / "services" / "camish").exists()         # vendor undone
        assert not (root / "rig.lock").exists()                    # no half-locked state
        assert "camish" not in ((root / "services.yaml").read_text()
                                if (root / "services.yaml").exists() else "")


# --- UX batch (v0.1.64): dirty markers, upgrade hints ------------------------------------------


def test_relock_verifies_overlay_payload_copies():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        _install_acme(root)
        _run("--root", str(root), "overlay", "apply", "acme_cam", "testns/cam-tune")
        assert _run("--root", str(root), "pkg", "lock")[0] == 0
        copy = root / "config" / ".overlays" / "testns--cam-tune--1.0.0.yaml"
        copy.write_text(copy.read_text() + "# tampered\n")
        rc, out, _ = _run("--root", str(root), "pkg", "lock")
        assert rc == 1 and "payload copy no longer matches" in out


def test_pkg_list_marks_dirty_and_diff_hints_upgrade():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, reg = _world()
        working = _install_acme(root)
        working.write_text(working.read_text().replace("width: 1280", "width: 640"))  # dirty
        mpath = reg / "profiles" / "acme-cam" / "manifest.yaml"    # registry moves, NO upgrade run
        m = yaml.safe_load(mpath.read_text())
        m["version"] = "2.1.0"
        mpath.write_text(yaml.safe_dump(m, sort_keys=False))
        _run("registry", "index", str(reg))
        rc, out, _ = _run("--root", str(root), "pkg", "list")
        assert rc == 0
        assert "acme_cam*" in out                                  # * = local edits
        assert "2.1.0 available" in out and "local edits" in out   # legend included
        rc, out, _ = _run("--root", str(root), "config", "diff", "acme_cam")
        assert "(base: testns/acme-cam@2.0.0 — 2.1.0 available)" in out
        pin = root / "config" / ".pins" / "acme_cam.yaml"          # back to clean: pin shown too
        working.write_bytes(pin.read_bytes())
        rc, out, _ = _run("--root", str(root), "config", "diff", "acme_cam")
        assert "clean (base: testns/acme-cam@2.0.0 — 2.1.0 available)" in out


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
