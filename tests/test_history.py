"""Git history as a read-only registry version archive: historical `pkg add ns/name@old` and
--locked reproduction from the locked registry commit. Run: python3 tests/test_history.py"""
import contextlib
import io
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import yaml  # noqa: E402

from test_install import _code_repo, _env, _git, _registry_with, _run  # noqa: E402

from rig_cli.init import init  # noqa: E402
from rig_cli.lock import load_lock  # noqa: E402


def _git_world():
    """A GIT-type registry (cloned into the cache by sync — full history) + a fresh deployment."""
    repo, rev = _code_repo()
    reg = _registry_with(repo, rev)                       # acme-cam@2.0.0, usb width 1280
    _git("init", "-q", cwd=reg)
    _git("add", "-A", cwd=reg)
    _git("commit", "-q", "-m", "seed", cwd=reg)
    _run("setup", "--no-default-registry")
    _run("registry", "add", "testns", str(reg))           # positional location => git type
    assert _run("registry", "sync")[0] == 0
    target = pathlib.Path(tempfile.mkdtemp()) / "veh"
    with contextlib.redirect_stderr(io.StringIO()):
        init(target, no_git=True)
    return target, reg


def _bump_profile(reg, version, payload):
    mpath = reg / "profiles" / "camish" / "acme-cam" / "manifest.yaml"
    m = yaml.safe_load(mpath.read_text())
    m["version"] = version
    mpath.write_text(yaml.safe_dump(m, sort_keys=False))
    (reg / "profiles" / "camish" / "acme-cam" / "config" / "payload.yaml").write_text(payload)
    _run("registry", "index", str(reg))
    _git("add", "-A", cwd=reg)
    _git("commit", "-q", "-m", f"bump {version}", cwd=reg)
    assert _run("registry", "sync")[0] == 0               # the cache now holds BOTH states


def test_add_historical_version_from_git_history():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, reg = _git_world()
        _bump_profile(reg, "2.1.0", "service: camish\ncamera: {type: usb}\nusb: {width: 4096}\n")
        rc, _, err = _run("--root", str(root), "pkg", "install", "testns/camish:acme-cam@2.0.0")
        assert rc == 0, err
        assert "HISTORICAL" in err                        # loud: this is not the current version
        working = (root / "config" / "sensors" / "acme_cam.yaml").read_text()
        assert "width: 1280" in working                   # the OLD payload, byte-faithful
        lock = load_lock(root)
        assert "testns/camish:acme-cam@2.0.0" in lock["packages"]
        assert "testns/camish:acme-cam@2.1.0" not in lock["packages"]


def test_add_version_absent_from_history_errors():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _git_world()
        rc, _, err = _run("--root", str(root), "pkg", "install", "testns/camish:acme-cam@9.9.9")
        assert rc == 1 and "not in the registry's git history" in err


def test_add_historical_needs_git_backed_registry():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        from test_install import _world
        root, _ = _world()                                # plain local-dir folder, no .git
        rc, _, err = _run("--root", str(root), "pkg", "install", "testns/camish:acme-cam@1.9.9")
        assert rc == 1 and "git-backed registry" in err


def test_locked_reproduces_from_locked_registry_commit():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, reg = _git_world()
        rc, _, err = _run("--root", str(root), "pkg", "install", "sensor:acme")
        assert rc == 0, err                               # locks acme-cam@2.0.0 @ commit C1
        _bump_profile(reg, "2.1.0", "service: camish\ncamera: {type: usb}\nusb: {width: 4096}\n")
        root2 = pathlib.Path(tempfile.mkdtemp()) / "veh2"
        with contextlib.redirect_stderr(io.StringIO()):
            init(root2, no_git=True)
        (root2 / "rig.lock").write_text((root / "rig.lock").read_text())
        rc, _, err = _run("--root", str(root2), "pkg", "install", "sensor:acme", "--locked")
        assert rc == 0, err
        assert "locked registry commit" in err            # reproduced, not verified-and-failed
        working = (root2 / "config" / "sensors" / "acme_cam.yaml").read_text()
        assert "width: 1280" in working                   # byte-identical to the origin install
        lock = load_lock(root2)
        assert "testns/camish:acme-cam@2.0.0" in lock["packages"]
        assert (lock["registries"]["testns"]["commit"]    # the anchor commit was NOT clobbered
                == load_lock(root)["registries"]["testns"]["commit"])


def test_locked_fails_when_history_rewritten():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, reg = _git_world()
        assert _run("--root", str(root), "pkg", "install", "sensor:acme")[0] == 0
        # same version, different payload upstream; the locked commit no longer exists (rewrite)
        _bump_profile(reg, "2.0.0", "service: camish\ncamera: {type: usb}\nusb: {width: 666}\n")
        root2 = pathlib.Path(tempfile.mkdtemp()) / "veh2"
        with contextlib.redirect_stderr(io.StringIO()):
            init(root2, no_git=True)
        lock_text = (root / "rig.lock").read_text()
        bogus = "0" * 40                                  # simulate an unreachable/rewritten commit
        real = load_lock(root)["registries"]["testns"]["commit"]
        (root2 / "rig.lock").write_text(lock_text.replace(real, bogus))
        rc, _, err = _run("--root", str(root2), "pkg", "install", "sensor:acme", "--locked")
        assert rc == 1 and "registry content changed under the pin" in err


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
