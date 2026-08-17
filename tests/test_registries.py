"""Client-side registries: setup, add/remove/list, sync (git + local-dir), degrade, pkg search/info,
init git default. Run: python3 tests/test_registries.py"""
import contextlib
import io
import os
import pathlib
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from rig_cli import RigError  # noqa: E402
from rig_cli.cli import main  # noqa: E402
from rig_cli.registries import load_entries, rig_home  # noqa: E402
from rig_cli.registry_scaffold import registry_init  # noqa: E402


@contextlib.contextmanager
def _env(**over):
    old = {k: os.environ.get(k) for k in over}
    os.environ.update({k: str(v) for k, v in over.items()})
    try:
        yield
    finally:
        for k, v in old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def _home() -> str:
    return tempfile.mkdtemp()


def _run(*argv) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = main(list(argv))
        except RigError as exc:  # main normally catches; direct module raises bubble in some paths
            rc, err = 1, io.StringIO(err.getvalue() + str(exc))
    return rc, out.getvalue(), err.getvalue()


def _seed_registry(ns="testns", pkgs=True) -> pathlib.Path:
    root = pathlib.Path(tempfile.mkdtemp()) / "reg"
    with contextlib.redirect_stderr(io.StringIO()):
        registry_init(root, namespace=ns)
    if pkgs:
        d = root / "services" / "camera-service"
        d.mkdir(parents=True)
        (d / "manifest.yaml").write_text(yaml.safe_dump({
            "kind": "service", "name": "camera-service", "version": "1.4.2",
            "source": {"repo": "https://example.com/x.git", "rev": "a" * 40}}))
        p = root / "profiles" / "siyi-zr30"
        (p / "config").mkdir(parents=True)
        (p / "manifest.yaml").write_text(yaml.safe_dump({
            "kind": "profile", "name": "siyi-zr30", "version": "1.0.0",
            "provides": {"sensor": [{"model": "SIYI ZR30", "match": ["zr30", "usb:1234:*"]}]},
            "requires": {"service": "camera-service@^1.4"},
            "config": {"payload": "config/payload.yaml"}}))
        (p / "config" / "payload.yaml").write_text("service: camera-service\nrtsp: {url: x}\n")
        _run("registry", "index", str(root))
    return root


def _git(*args, cwd):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          cwd=cwd, capture_output=True, text=True)


def test_setup_creates_state_and_is_idempotent():
    with _env(RIG_HOME=_home()):
        rc, _, err = _run("setup")
        assert rc == 0 and "created" in err
        entries = load_entries()
        assert [e.name for e in entries] == ["public"] and entries[0].type == "git"
        rc, _, err = _run("setup")  # second run: no duplicates, reports existing
        assert rc == 0 and "left untouched" in err
        assert len(load_entries()) == 1
        assert (rig_home() / "cache" / "registries").is_dir()


def test_setup_no_default_registry():
    with _env(RIG_HOME=_home()):
        rc, _, _ = _run("setup", "--no-default-registry")
        assert rc == 0 and load_entries() == []


def test_setup_purge_removes_state():
    with _env(RIG_HOME=_home()):
        _run("setup")
        assert rig_home().is_dir()
        rc, _, _ = _run("setup", "--purge", "--yes")
        assert rc == 0 and not rig_home().exists()


def test_registry_add_remove_list_and_priority():
    with _env(RIG_HOME=_home()):
        _run("setup", "--no-default-registry")
        reg = _seed_registry()
        rc, _, _ = _run("registry", "add", "testns", "--path", str(reg))
        assert rc == 0
        rc, _, err = _run("registry", "add", "testns", "--path", str(reg))
        assert rc == 1 and "already exists" in err
        reg2 = _seed_registry(ns="dev", pkgs=False)
        rc, _, _ = _run("registry", "add", "dev", "--path", str(reg2), "--front")
        assert rc == 0
        assert [e.name for e in load_entries()] == ["dev", "testns"]  # --front wins priority
        rc, out, _ = _run("registry", "list")
        assert rc == 0 and "in place" in out and "testns" in out
        rc, _, _ = _run("registry", "remove", "dev")
        assert rc == 0 and [e.name for e in load_entries()] == ["testns"]


def test_registry_add_rejects_non_registry_path():
    with _env(RIG_HOME=_home()):
        _run("setup", "--no-default-registry")
        rc, _, err = _run("registry", "add", "x", "--path", tempfile.mkdtemp())
        assert rc == 1 and "not a registry" in err


def test_sync_git_clone_pull_and_divergence():
    with _env(RIG_HOME=_home()):
        _run("setup", "--no-default-registry")
        remote = _seed_registry()
        _git("init", "-q", cwd=remote)
        _git("add", "-A", cwd=remote)
        _git("commit", "-q", "-m", "seed", cwd=remote)
        rc, _, _ = _run("registry", "add", "testns", str(remote))
        assert rc == 0
        rc, _, err = _run("registry", "sync")
        assert rc == 0 and "cloned" in err and "2 packages" in err
        # remote grows a package -> sync ff-pulls it
        d = remote / "services" / "extra-svc"
        d.mkdir(parents=True)
        (d / "manifest.yaml").write_text(yaml.safe_dump({
            "kind": "service", "name": "extra-svc", "version": "1.0.0",
            "source": {"repo": "r", "rev": "b" * 40}}))
        _run("registry", "index", str(remote))
        _git("add", "-A", cwd=remote)
        _git("commit", "-q", "-m", "extra", cwd=remote)
        rc, _, err = _run("registry", "sync")
        assert rc == 0 and "updated" in err and "3 packages" in err
        # divergence: remote moves AND cache moves -> ff-only fails with guidance
        cache = rig_home() / "cache" / "registries" / "testns"
        (cache / "junk.txt").write_text("local\n")
        _git("add", "-A", cwd=cache)
        _git("commit", "-q", "-m", "local", cwd=cache)
        (remote / "README2.md").write_text("remote\n")
        _git("add", "-A", cwd=remote)
        _git("commit", "-q", "-m", "remote", cwd=remote)
        rc, _, err = _run("registry", "sync")
        assert rc == 1 and "FAILED" in err and "ff-only" in err


def test_sync_warns_on_namespace_mismatch_and_degrades_broken():
    with _env(RIG_HOME=_home()):
        _run("setup", "--no-default-registry")
        reg = _seed_registry(ns="testns")
        _run("registry", "add", "othername", "--path", str(reg))
        rc, _, err = _run("registry", "sync")
        assert rc == 0 and "namespace 'testns'" in err and "othername" in err
        broken = _seed_registry(ns="broke", pkgs=False)
        (broken / "registry.yaml").write_text("schema: 99\nname: broke\nnamespace: broke\n")
        _run("registry", "add", "broke", "--path", str(broken))
        rc, _, err = _run("registry", "sync")
        assert rc == 1 and "DEGRADED" in err and "upgrade rig" in err


def test_sync_warns_on_stale_index():
    with _env(RIG_HOME=_home()):
        _run("setup", "--no-default-registry")
        reg = _seed_registry(ns="testns")
        _run("registry", "add", "testns", "--path", str(reg))
        rc, _, err = _run("registry", "sync")
        assert rc == 0 and "STALE" not in err
        mpath = reg / "profiles" / "siyi-zr30" / "manifest.yaml"    # a merge whose CI forgot the
        mpath.write_text(mpath.read_text().replace("1.0.0", "1.0.1"))  # index regen
        rc, _, err = _run("registry", "sync")
        assert rc == 0 and "STALE" in err                           # split-world surfaced at sync


def test_pkg_search_and_info_across_registries():
    with _env(RIG_HOME=_home()):
        _run("setup", "--no-default-registry")
        reg = _seed_registry(ns="testns")
        _run("registry", "add", "testns", "--path", str(reg))
        rc, out, _ = _run("pkg", "search", "zr30")
        assert rc == 0 and "testns/siyi-zr30" in out and "profile" in out
        rc, out, _ = _run("pkg", "search", "sensor:zr30")
        assert rc == 0 and "match: exact" in out
        rc, out, _ = _run("pkg", "search", "sensor:usb:1234:5678")  # glob identifier covers it
        assert rc == 0 and "match: glob" in out
        rc, out, _ = _run("pkg", "search", "nope-nothing")
        assert rc == 0 and "no matches" in out
        rc, out, _ = _run("pkg", "info", "testns/siyi-zr30")
        assert rc == 0 and "requires: camera-service@^1.4" in out and "SIYI ZR30" in out
        rc, out, _ = _run("pkg", "info", "camera-service")  # unqualified resolves priority order
        assert rc == 0 and "source: https://example.com/x.git" in out
        rc, _, err = _run("pkg", "info", "ghost-pkg")
        assert rc == 1 and "not found" in err


def test_pkg_search_priority_order_and_shadowing():
    with _env(RIG_HOME=_home()):
        _run("setup", "--no-default-registry")
        low = _seed_registry(ns="low")
        high = _seed_registry(ns="high")
        _run("registry", "add", "low", "--path", str(low))
        _run("registry", "add", "high", "--path", str(high), "--front")
        rc, out, _ = _run("pkg", "search", "siyi")
        assert rc == 0
        assert out.index("high/siyi-zr30") < out.index("low/siyi-zr30")  # priority order printed first
        rc, out, _ = _run("pkg", "info", "siyi-zr30")
        assert rc == 0 and out.splitlines()[0].startswith("high/")


def test_setup_shell_block_idempotent_and_purged():
    fake_home = _home()
    with _env(RIG_HOME=_home(), HOME=fake_home, SHELL="/bin/zsh", PATH="/usr/bin:/bin"):
        rc, _, err = _run("setup", "--shell", "--no-default-registry")
        rc2, _, _ = _run("setup", "--shell", "--no-default-registry")  # idempotent
        assert rc == 0 and rc2 == 0
        rcfile = pathlib.Path(fake_home) / ".zshrc"
        body = rcfile.read_text()
        assert body.count("# >>> rig >>>") == 1 and 'export PATH="' in body
        rc, _, _ = _run("setup", "--purge", "--yes")
        assert rc == 0 and "# >>> rig >>>" not in rcfile.read_text()


def test_init_git_default_and_no_git():
    ws = pathlib.Path(tempfile.mkdtemp())
    rc, _, err = _run("init", str(ws / "veh"))
    assert rc == 0 and (ws / "veh" / ".git").is_dir() and "scaffold committed" in err
    log = subprocess.run(["git", "-C", str(ws / "veh"), "log", "--oneline"],
                         capture_output=True, text=True).stdout
    assert "rig init" in log
    rc, _, _ = _run("init", str(ws / "veh2"), "--no-git")
    assert rc == 0 and not (ws / "veh2" / ".git").exists()
    # inside an existing worktree: never nest
    inner = ws / "veh" / "sub" / "veh3"
    rc, _, err = _run("init", str(inner))
    assert rc == 0 and not (inner / ".git").exists() and "not nesting" in err


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
