"""Stale exact pins are SNAPSHOTS, not errors (v0.2.19): a member's later release must not block
validation of (or installs from) the suites/profiles that pin it. validate warns; install serves
the pinned version from git history; a non-git registry fails with the pointed hint; `pkg
outdated` owns currency. Run: python3 tests/test_stale_pins.py"""
import contextlib
import io
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import yaml  # noqa: E402

from test_install import _env, _git, _run, _world  # noqa: E402
from test_promote import _commit, _dev_service, _gitify, _internal, _rendered  # noqa: E402

from rig_cli.init import init  # noqa: E402


def _fresh() -> pathlib.Path:
    root2 = pathlib.Path(tempfile.mkdtemp()) / "veh2"
    with contextlib.redirect_stderr(io.StringIO()):
        init(root2, no_git=True)
    return root2


def _publish_service(route, version: str, root) -> None:
    (route / f"rel-{version}.txt").write_text(version + "\n")
    _git("add", "-A", cwd=route)
    _git("commit", "-q", "-m", f"rel {version}", cwd=route)
    _git("push", "-q", "origin", "HEAD", cwd=route)
    rc, _, err = _run("--root", str(root), "pkg", "promote", "lidarish", "--kind", "service",
                      "--to", "internal", "--version", version)
    assert rc == 0, err


def _world_with_pinned_suite(git_backed: bool):
    """Origin: a hand-authored lidarish instance adopted as a profile (requires lidarish@0.0.1)
    inside suite `boat` with a vehicle plan. Returns (origin_root, internal_root, route)."""
    root, _ = _world()
    internal = _internal()
    if git_backed:
        _gitify(internal)
    route, _, _ = _dev_service(name="lidarish")
    (route / "rigging.yaml").write_text("service: lidarish\nlauncher: lidarish-up\ntier: sensor\n"
                                        "launch_surface: [lidarish-up]\n")
    (route / "config" / "lidarish.example.yaml").unlink()
    _git("add", "-A", cwd=route)
    _git("commit", "-q", "-m", "v1", cwd=route)
    _git("push", "-q", "origin", "HEAD", cwd=route)
    assert _run("--root", str(root), "add", str(route))[0] == 0
    cfg = root / "config" / "sensors" / "lidar_main.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("service: lidarish\nrate: 42\n")
    veh = root / "vehicle.yaml"
    veh.write_text(veh.read_text().replace(
        "sensors:", "sensors:\n  - { name: lidar_main, service: lidarish, "
                    "config: config/sensors/lidar_main.yaml, enabled: true, order: 30 }", 1))
    assert _run("--root", str(root), "pkg", "promote", "lidarish", "--kind", "service",
                "--to", "internal", "--version", "0.0.1", "--adopt")[0] == 0
    rc, _, err = _run("--root", str(root), "pkg", "promote", "--all", "--suite", "boat",
                      "--vehicle", "gideon", "--to", "internal", "--adopt")
    assert rc == 0, err
    if git_backed:
        _commit(internal, "suite at service 0.0.1")
    return root, internal, route


def test_service_release_does_not_block_validation_and_suite_installs_from_history():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, internal, route = _world_with_pinned_suite(git_backed=True)
        before = _rendered(root, "lidar_main")
        _publish_service(route, "0.0.2", root)          # the service repo's CI fires
        _commit(internal, "service 0.0.2")
        rc, out, err = _run("registry", "validate", str(internal))
        assert rc == 0, (out + err)                      # NOT an error any more
        assert "behind registry-current" in (out + err)  # ...but loudly stale
        root2 = _fresh()
        rc, out, err = _run("--root", str(root2), "pkg", "install", "internal/boat")
        assert rc == 0, err
        assert "HISTORICAL version" in err               # served from git history
        lock = yaml.safe_load((root2 / "rig.lock").read_text())
        assert "internal/lidarish@0.0.1" in lock["packages"]   # the PINNED version, not head
        assert _rendered(root2, "lidar_main") == before
        rc, out, _ = _run("pkg", "outdated")             # currency lives here, exit 1 on drift
        assert rc == 1 and "lidar-main" in out


def test_stale_suite_member_validates_with_warning_and_installs_from_history():
    # The suite's OWN member pin goes stale (a services: member), not just a profile's requires.
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        internal = _internal()
        _gitify(internal)
        rc, _, err = _run("--root", str(root), "pkg", "add", "testns/camish")   # bare, anchored
        assert rc == 0, err
        row_cfg = root / "config" / "sensors" / "camish.yaml"
        row_cfg.write_text(row_cfg.read_text() + "usb: {width: 640}\n")
        rc, _, err = _run("--root", str(root), "pkg", "promote", "--all", "--suite", "boat",
                          "--to", "internal")
        assert rc == 0, err
        _commit(internal, "boat")
        s = yaml.safe_load((internal / "suites" / "boat" / "manifest.yaml").read_text())
        assert s["members"]["overlays"] == ["internal/camish@1.0.0"]
        # the overlay republishes (head moves to 1.0.1) without touching the suite
        row_cfg.write_text(row_cfg.read_text() + "fps: 15\n")
        rc, _, err = _run("--root", str(root), "pkg", "promote", "camish", "--name", "camish",
                          "--to", "internal", "--bump")
        assert rc == 0, err
        _commit(internal, "overlay 1.0.1")
        rc, out, err = _run("registry", "validate", str(internal))
        assert rc == 0 and "stale" in (out + err)
        root2 = _fresh()
        rc, out, err = _run("--root", str(root2), "pkg", "install", "internal/boat")
        assert rc == 0, err
        assert "behind registry-current" in err and "HISTORICAL version" in err
        from rig_cli.manifest import load_manifest
        fresh = next(x for x in load_manifest(root2).sensors)
        assert list(fresh.overlays) == ["internal/camish@1.0.0"]     # bound at the PIN


def test_stale_pin_without_git_history_fails_pointedly():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, internal, route = _world_with_pinned_suite(git_backed=False)
        _publish_service(route, "0.0.2", root)
        rc, out, err = _run("registry", "validate", str(internal))
        assert rc == 0                                   # still only a warning registry-side
        root2 = _fresh()
        texts = {n: (root2 / n).read_bytes() if (root2 / n).exists() else None
                 for n in ("vehicle.yaml", "services.yaml", "rig.lock")}
        cfg = {p.relative_to(root2): p.read_bytes()
               for p in (root2 / "config").rglob("*") if p.is_file()}
        rc, out, err = _run("--root", str(root2), "pkg", "install", "internal/boat")
        assert rc == 1
        assert "git history" in err and "rolled back" in err   # pointed, atomic
        # ...and PROVABLY atomic: the claim must hold byte-for-byte, not just print
        for name, blob in texts.items():
            now = (root2 / name).read_bytes() if (root2 / name).exists() else None
            assert now == blob, f"{name} not restored by the rollback"
        assert {p.relative_to(root2): p.read_bytes()
                for p in (root2 / "config").rglob("*") if p.is_file()} == cfg


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
