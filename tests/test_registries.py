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
from rig_cli.registries import load_entries, resolve_namespace, rig_home  # noqa: E402
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
        p = root / "profiles" / "camera-service" / "siyi-zr30"
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
    with _env(RIG_HOME=_home(), HOME=_home()):  # fresh HOME: purge scans rc files under it
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
        assert "+ extra-svc (service) 1.0.0" in err          # the delta digest names the change
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
        entry = resolve_namespace("testns")                  # declared namespace beats the alias
        assert entry is not None and entry.name == "othername"
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
        mpath = reg / "profiles" / "camera-service" / "siyi-zr30" / "manifest.yaml"    # a merge whose CI forgot the
        mpath.write_text(mpath.read_text().replace("1.0.0", "1.0.1"))  # index regen
        rc, _, err = _run("registry", "sync")
        assert rc == 0 and "STALE" in err                           # split-world surfaced at sync


def _add_overlay(reg, name="zr30-gideon", project="gideon"):
    o = reg / "overlays" / name
    (o / "config").mkdir(parents=True)
    (o / "manifest.yaml").write_text(yaml.safe_dump({
        "kind": "overlay", "name": name, "version": "1.1.0",
        "targets": [{"service": "camera-service"}], "project": project,
        "authored_against": {"service": "camera-service@1.4.2"},
        "config": {"payload": "config/delta.yaml"}}))
    (o / "config" / "delta.yaml").write_text("rtsp: {url: y}\n")
    _run("registry", "index", str(reg))


def test_parse_ref_shapes():
    from rig_cli.refs import parse_ref, unqualified
    assert parse_ref("public/cam@1.2.3") == ("public", "cam", "1.2.3")
    assert parse_ref("cam@1.2.3") == (None, "cam", "1.2.3")
    assert parse_ref("public/cam") == ("public", "cam", None)
    assert parse_ref("cam") == (None, "cam", None)
    assert unqualified("public/cam@1.2.3") == "cam"


def test_search_covers_project_tags_and_targets_with_header():
    with _env(RIG_HOME=_home()):
        _run("setup", "--no-default-registry")
        reg = _seed_registry(ns="testns")
        _add_overlay(reg)
        _run("registry", "add", "testns", "--path", str(reg))
        rc, out, _ = _run("pkg", "search", "gideon")        # free text hits the PROJECT tag
        assert rc == 0 and "testns/zr30-gideon" in out and "PACKAGE" in out  # header row
        rc, out, _ = _run("pkg", "search", "camera-service")
        assert rc == 0 and "testns/zr30-gideon" in out      # overlays found by TARGET too


def test_info_versioned_ref_and_authored_against():
    with _env(RIG_HOME=_home()):
        _run("setup", "--no-default-registry")
        reg = _seed_registry(ns="testns")
        _add_overlay(reg)
        _run("registry", "add", "testns", "--path", str(reg))
        rc, out, _ = _run("pkg", "info", "testns/camera-service:siyi-zr30@0.9.9")   # @version now PARSES
        assert rc == 0 and "you asked about @0.9.9" in out and "1.0.0" in out
        rc, out, _ = _run("pkg", "info", "testns/zr30-gideon")
        assert rc == 0 and "authored_against: service: camera-service@1.4.2" in out
        rc, out, _ = _run("pkg", "info", "testns/camera-service:siyi-zr30")
        assert rc == 0 and "based_on" not in out             # absent fields stay silent


def test_pkg_search_and_info_across_registries():
    with _env(RIG_HOME=_home()):
        _run("setup", "--no-default-registry")
        reg = _seed_registry(ns="testns")
        _run("registry", "add", "testns", "--path", str(reg))
        rc, out, _ = _run("pkg", "search", "zr30")
        assert rc == 0 and "testns/camera-service:siyi-zr30" in out and "profile" in out
        rc, out, _ = _run("pkg", "search", "sensor:zr30")
        assert rc == 0 and "match: exact" in out
        rc, out, _ = _run("pkg", "search", "sensor:usb:1234:5678")  # glob identifier covers it
        assert rc == 0 and "match: glob" in out
        rc, out, _ = _run("pkg", "search", "nope-nothing")
        assert rc == 1 and "no matches" in out          # scriptable: no hits = nonzero
        rc, out, _ = _run("pkg", "info", "testns/camera-service:siyi-zr30")
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
        assert out.index("high/camera-service:siyi-zr30") < out.index("low/camera-service:siyi-zr30")  # priority order printed first
        rc, out, _ = _run("pkg", "info", "camera-service:siyi-zr30")
        assert rc == 0 and out.splitlines()[0].startswith("high/")


def test_pkg_search_survives_corrupt_index():
    with _env(RIG_HOME=_home()):
        _run("setup", "--no-default-registry")
        reg = _seed_registry(ns="testns")
        _run("registry", "add", "testns", "--path", str(reg))
        (reg / "index.json").write_text("{ not json !")      # corrupt on-disk index
        rc, out, _ = _run("pkg", "search", "zr30")           # open_registry regenerates in memory
        assert rc == 0 and "testns/camera-service:siyi-zr30" in out


def test_setup_shell_block_idempotent_and_purged():
    fake_home = _home()
    with _env(RIG_HOME=_home(), HOME=fake_home, SHELL="/bin/zsh", PATH="/usr/bin:/bin"):
        rc, _, err = _run("setup", "--shell", "--no-default-registry")
        rc2, _, _ = _run("setup", "--shell", "--no-default-registry")  # idempotent
        assert rc == 0 and rc2 == 0
        rcfile = pathlib.Path(fake_home) / ".zshrc"
        body = rcfile.read_text()
        assert body.count("# >>> rig >>>") == 1 and 'export PATH="' in body
        assert 'eval "$(rig completion zsh' in body  # TAB completion rides the same block
        rc, _, _ = _run("setup", "--purge", "--yes")
        assert rc == 0 and "# >>> rig >>>" not in rcfile.read_text()


def test_setup_shell_completion_line_even_when_on_path():
    # rig already resolving on PATH (pipx/deb/brew) still gets the completion eval — only
    # the PATH line is skipped.
    fake_home = _home()
    bindir = pathlib.Path(fake_home) / "bin"
    bindir.mkdir(parents=True)
    (bindir / "rig").write_text("#!/bin/sh\nexit 0\n")
    (bindir / "rig").chmod(0o755)
    with _env(RIG_HOME=_home(), HOME=fake_home, SHELL="/bin/bash",
              PATH=f"{bindir}:/usr/bin:/bin"):
        rc, _, err = _run("setup", "--shell", "--no-default-registry")
        assert rc == 0 and "already resolves on PATH" in err
        body = (pathlib.Path(fake_home) / ".bashrc").read_text()
        assert 'eval "$(rig completion bash' in body and "export PATH" not in body


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
