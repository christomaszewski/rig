"""Git history as a read-only registry version archive: historical `pkg add ns/name@old` and
--locked reproduction from the locked registry commit. Run: python3 tests/test_history.py"""
import contextlib
import io
import pathlib
import shutil
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import yaml  # noqa: E402

from test_install import _code_repo, _env, _git, _registry_with, _run, _world  # noqa: E402

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


def _checkout_world():
    """A LOCAL-DIR registry that is a git checkout with an upstream — a GitHub/GitLab clone used
    in place — plus a fresh deployment; returns (deployment, checkout)."""
    repo, rev = _code_repo()
    seed = _registry_with(repo, rev)
    _git("init", "-q", "-b", "main", cwd=seed)
    _git("add", "-A", cwd=seed)
    _git("commit", "-q", "-m", "seed", cwd=seed)
    upstream = pathlib.Path(tempfile.mkdtemp()) / "upstream.git"
    _git("clone", "-q", "--bare", str(seed), str(upstream), cwd=seed.parent)
    checkout = pathlib.Path(tempfile.mkdtemp()) / "checkout"
    _git("clone", "-q", str(upstream), str(checkout), cwd=upstream.parent)
    _run("setup", "--no-default-registry")
    _run("registry", "add", "testns", "--path", str(checkout))
    return _fresh(), checkout


def _fresh():
    target = pathlib.Path(tempfile.mkdtemp()) / "veh"
    with contextlib.redirect_stderr(io.StringIO()):
        init(target, no_git=True)
    return target


def test_local_checkout_registry_records_commits_and_warns_about_unshared_bytes():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, checkout = _checkout_world()
        head = _git("rev-parse", "HEAD", cwd=checkout).stdout.strip()
        rc, _, err = _run("--root", str(root), "pkg", "install", "testns/camish:acme-cam")
        assert rc == 0 and "warning" not in err, err
        rows = load_lock(root)["packages"]
        assert rows["testns/camish:acme-cam@2.0.0"]["commit"] == head  # was never recorded
        assert load_lock(root)["registries"]["testns"]["commit"] == head
        # an uncommitted edit is installed loudly, and the lock records no commit for it
        payload = checkout / "profiles" / "camish" / "acme-cam" / "config" / "payload.yaml"
        payload.write_text(payload.read_text().replace("1280", "1281"))
        root2 = _fresh()
        rc, _, err = _run("--root", str(root2), "pkg", "install", "testns/camish:acme-cam")
        assert rc == 0 and "UNCOMMITTED changes" in err and "config/payload.yaml" in err, err
        assert "commit" not in load_lock(root2)["packages"]["testns/camish:acme-cam@2.0.0"]
        # committed but not pushed: recorded, and warned that no other machine can fetch it
        _git("commit", "-q", "-am", "tune", cwd=checkout)
        tuned = _git("rev-parse", "HEAD", cwd=checkout).stdout.strip()
        root3 = _fresh()
        rc, _, err = _run("--root", str(root3), "pkg", "install", "testns/camish:acme-cam")
        assert rc == 0 and f"commit {tuned[:7]}" in err and "on no remote branch" in err, err
        assert load_lock(root3)["packages"]["testns/camish:acme-cam@2.0.0"]["commit"] == tuned
        # the service row records the last commit that touched IT, which is pushed: no warning
        assert load_lock(root3)["packages"]["testns/camish@1.2.0"]["commit"] == head
        assert not [line for line in err.splitlines() if "warning" in line and "camish@1.2.0" in line]
        # pushed: silent
        _git("push", "-q", "origin", "main", cwd=checkout)
        rc, _, err = _run("--root", str(_fresh()), "pkg", "install", "testns/camish:acme-cam")
        assert rc == 0 and "warning" not in err, err


def test_a_plain_folder_registry_records_no_commit_and_says_nothing():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, reg = _world()                              # local-dir, no git anywhere
        rc, _, err = _run("--root", str(root), "pkg", "install", "testns/camish:acme-cam")
        assert rc == 0 and "warning" not in err, err
        assert "commit" not in load_lock(root)["packages"]["testns/camish:acme-cam@2.0.0"]
        # inside an UNRELATED repository (a $HOME kept in git): still a plain folder
        _git("init", "-q", cwd=reg.parent)
        (reg.parent / "dotfile").write_text("x\n")
        _git("add", "dotfile", cwd=reg.parent)
        _git("commit", "-q", "-m", "home", cwd=reg.parent)
        rc, _, err = _run("--root", str(_fresh()), "pkg", "install", "testns/camish:acme-cam")
        assert rc == 0 and "warning" not in err, err
        rc, _, err = _run("registry", "sync", "testns")
        assert "git checkout" not in err, err


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


def test_nested_registry_resolves_history_through_parent_repo():
    """A registry checked out BELOW a larger repo's root (monorepo layout): _git_prefix must
    address blobs as <prefix>/<kind_dir>/… — historical versions still resolve."""
    with _env(RIG_HOME=tempfile.mkdtemp()):
        repo, rev = _code_repo()
        parent = pathlib.Path(tempfile.mkdtemp()) / "monorepo"
        parent.mkdir(parents=True)
        (parent / "README.md").write_text("the registry is just one corner of this repo\n")
        reg = parent / "deploy" / "registry"
        reg.parent.mkdir(parents=True)
        shutil.copytree(_registry_with(repo, rev), reg)       # acme-cam@2.0.0, usb width 1280
        _git("init", "-q", cwd=parent)
        _git("add", "-A", cwd=parent)
        _git("commit", "-q", "-m", "seed", cwd=parent)
        _run("setup", "--no-default-registry")
        _run("registry", "add", "testns", "--path", str(reg))  # local-dir INSIDE the parent repo
        root = pathlib.Path(tempfile.mkdtemp()) / "veh"
        with contextlib.redirect_stderr(io.StringIO()):
            init(root, no_git=True)
        _bump_profile(reg, "2.1.0", "service: camish\ncamera: {type: usb}\nusb: {width: 4096}\n")
        rc, _, err = _run("--root", str(root), "pkg", "install", "testns/camish:acme-cam@2.0.0")
        assert rc == 0, err
        assert "HISTORICAL" in err                            # found through the nested prefix
        working = (root / "config" / "sensors" / "acme_cam.yaml").read_text()
        assert "width: 1280" in working                       # the OLD payload, byte-faithful


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


def _fresh_with_lock(src):
    root2 = pathlib.Path(tempfile.mkdtemp()) / "veh2"
    with contextlib.redirect_stderr(io.StringIO()):
        init(root2, no_git=True)
    (root2 / "rig.lock").write_text((src / "rig.lock").read_text())
    return root2


def test_locked_reproduces_a_historical_install():
    """`pkg add …@2.0.0` while the registry is at 2.1.0: the lock row records the commit the
    2.0.0 bytes came from, so `--locked` reproduces 2.0.0 (it used to resolve at HEAD and fail)."""
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, reg = _git_world()
        _bump_profile(reg, "2.1.0", "service: camish\ncamera: {type: usb}\nusb: {width: 4096}\n")
        rc, _, err = _run("--root", str(root), "pkg", "install", "testns/camish:acme-cam@2.0.0")
        assert rc == 0, err
        row = load_lock(root)["packages"]["testns/camish:acme-cam@2.0.0"]
        assert row["commit"] != load_lock(root)["registries"]["testns"]["commit"]  # its own commit
        root2 = _fresh_with_lock(root)
        rc, _, err = _run("--root", str(root2), "pkg", "install",
                          "testns/camish:acme-cam@2.0.0", "--locked")
        assert rc == 0, err
        assert "width: 1280" in (root2 / "config" / "sensors" / "acme_cam.yaml").read_text()


def test_locked_survives_a_later_install_moving_the_registry_commit():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, reg = _git_world()
        assert _run("--root", str(root), "pkg", "install", "sensor:acme")[0] == 0  # 2.0.0 @ C1
        _bump_profile(reg, "2.1.0", "service: camish\ncamera: {type: usb}\nusb: {width: 4096}\n")
        rc, _, err = _run("--root", str(root), "pkg", "install", "testns/routerish")  # lock @ C2
        assert rc == 0, err
        root2 = _fresh_with_lock(root)
        for spec in ("sensor:acme", "testns/routerish"):
            rc, _, err = _run("--root", str(root2), "pkg", "install", spec, "--locked")
            assert rc == 0, err
        assert "width: 1280" in (root2 / "config" / "sensors" / "acme_cam.yaml").read_text()


def test_locked_checks_the_required_service_at_its_locked_version():
    """camish moves to 2.0.0 upstream; the profile requires camish@^1.0 and the lock pins
    1.2.0 — `--locked` judges the constraint against the LOCKED service, not registry HEAD."""
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, reg = _git_world()
        assert _run("--root", str(root), "pkg", "install", "sensor:acme")[0] == 0
        mpath = reg / "services" / "camish" / "manifest.yaml"
        m = yaml.safe_load(mpath.read_text())
        m["version"] = "2.0.0"
        mpath.write_text(yaml.safe_dump(m, sort_keys=False))
        _run("registry", "index", str(reg))
        _git("add", "-A", cwd=reg)
        _git("commit", "-q", "-m", "camish 2", cwd=reg)
        assert _run("registry", "sync")[0] == 0
        root2 = _fresh_with_lock(root)
        rc, _, err = _run("--root", str(root2), "pkg", "install", "sensor:acme", "--locked")
        assert rc == 0, err
        assert "testns/camish@1.2.0" in load_lock(root2)["packages"]


def test_upgrade_never_moves_a_saved_pin_backwards():
    """A save on a git registry publishes 2.0.1 on an unmerged promote branch and re-pins the
    instance; the registry's main still carries 2.0.0. That is not an upgrade."""
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _git_world()
        assert _run("--root", str(root), "pkg", "install", "sensor:acme")[0] == 0
        working = root / "config" / "sensors" / "acme_cam.yaml"
        working.write_text(working.read_text().replace("width: 1280", "width: 640"))
        rc, _, err = _run("--root", str(root), "pkg", "save", "acme_cam")
        assert rc == 0, err
        rc, out, _ = _run("--root", str(root), "pkg", "list")
        assert "registry carries older 2.0.0" in out and "2.0.0 available" not in out
        rc, _, err = _run("--root", str(root), "pkg", "upgrade")
        assert rc == 0, err
        assert "kept" in err
        from rig_cli.manifest import load_manifest
        sensor = next(s for s in load_manifest(root).sensors if s.name == "acme_cam")
        assert sensor.profile == "testns/camish:acme-cam@2.0.1"


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
