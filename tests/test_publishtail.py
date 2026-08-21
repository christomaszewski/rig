"""registry pending/push/discard + pkg yank — the publish tail and its undo (F7/F10).
Run: python3 tests/test_publishtail.py"""
import contextlib
import io
import os
import pathlib
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from rig_cli.cli import main  # noqa: E402
from rig_cli.init import init  # noqa: E402
from rig_cli.publish import pending_count  # noqa: E402
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


def _run(*argv) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = main(list(argv))
    return rc, out.getvalue(), err.getvalue()


def _git(*args, cwd):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          cwd=cwd, capture_output=True, text=True)


def _registry_tree(ns: str) -> pathlib.Path:
    reg = pathlib.Path(tempfile.mkdtemp()) / ns
    with contextlib.redirect_stderr(io.StringIO()):
        registry_init(reg, namespace=ns)
    p = reg / "profiles" / "camish" / "cam"
    (p / "config").mkdir(parents=True)
    (p / "manifest.yaml").write_text(yaml.safe_dump({
        "kind": "profile", "name": "cam", "version": "1.0.0",
        "requires": {"service": "camish@1.0.0"},
        "config": {"payload": "config/payload.yaml"}}))
    (p / "config" / "payload.yaml").write_text("service: camish\nrate: 5\n")
    d = reg / "services" / "camish"
    d.mkdir(parents=True)
    return reg


def _code_repo() -> tuple[pathlib.Path, str]:
    repo = pathlib.Path(tempfile.mkdtemp()) / "camish"
    (repo / "config").mkdir(parents=True)
    (repo / "rigging.yaml").write_text(
        "service: camish\nlauncher: camish-up\ntier: sensor\n"
        "examples: [config/camish.example.yaml]\nlaunch_surface: [camish-up]\n")
    (repo / "camish-up").write_text("#!/bin/sh\n")
    (repo / "camish-up").chmod(0o755)
    (repo / "config" / "camish.example.yaml").write_text("service: camish\nrate: 1\n")
    _git("init", "-q", cwd=repo)
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "code", cwd=repo)
    return repo, _git("rev-parse", "HEAD", cwd=repo).stdout.strip()


def _git_world():
    """A git-TYPE registry with a bare origin, plus a deployment pinned to its profile."""
    repo, rev = _code_repo()
    reg = _registry_tree("gitty")
    (reg / "services" / "camish" / "manifest.yaml").write_text(yaml.safe_dump({
        "kind": "service", "name": "camish", "version": "1.0.0",
        "source": {"repo": str(repo), "rev": rev}}))
    _run("registry", "index", str(reg))
    _git("init", "-q", "-b", "main", cwd=reg)
    _git("add", "-A", cwd=reg)
    _git("commit", "-q", "-m", "seed", cwd=reg)
    origin = pathlib.Path(tempfile.mkdtemp()) / "origin.git"
    _git("clone", "--bare", "-q", str(reg), str(origin), cwd=reg.parent)
    _run("setup", "--no-default-registry")
    _run("registry", "add", "gitty", str(origin))
    assert _run("registry", "sync")[0] == 0
    veh = pathlib.Path(tempfile.mkdtemp()) / "veh"
    with contextlib.redirect_stderr(io.StringIO()):
        init(veh, no_git=True)
    assert _run("--root", str(veh), "pkg", "add", "gitty/camish:cam", "--as", "cam")[0] == 0
    cache = pathlib.Path(os.environ["RIG_HOME"]) / "cache" / "registries" / "gitty"
    return veh, cache, origin


def _dirty_and_save(veh) -> None:
    cfg = veh / "config" / "sensors" / "cam.yaml"
    doc = yaml.safe_load(cfg.read_text())
    doc["rate"] = 9
    cfg.write_text(yaml.safe_dump(doc))
    rc, _, err = _run("--root", str(veh), "pkg", "save", "cam")
    assert rc == 0, err


def test_pending_push_and_merged_lifecycle():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        veh, cache, origin = _git_world()
        rc, out, _ = _run("registry", "pending")
        assert rc == 0 and "no pending" in out
        _dirty_and_save(veh)
        assert pending_count() == 1                                      # doctor's advisory count
        rc, out, _ = _run("registry", "pending")
        assert rc == 0 and "promote/cam" in out and "unpushed" in out
        assert "registry push gitty" in out                              # copy-paste hint
        branch = next(w for w in out.split() if w.startswith("promote/"))
        rc, _, err = _run("registry", "push", "gitty", "main")           # default branch guard
        assert rc == 1 and "not a promote/* branch" in err
        rc, _, err = _run("registry", "push", "gitty", branch, "--pr")
        assert rc == 0, err
        assert "pushed to origin" in err
        assert pending_count() == 0                                      # pushed = off the worklist
        assert "open the PR from the URL above" in err                   # --pr fallback (no forge)
        assert branch in _git("branch", "--list", "--all", cwd=origin).stdout
        rc, out, _ = _run("registry", "pending")
        assert "pushed (PR/merge pending)" in out
        # merge upstream, sync, and the branch reports merged
        scratch = pathlib.Path(tempfile.mkdtemp()) / "scratch"
        _git("clone", "-q", str(origin), str(scratch), cwd=origin.parent)
        _git("merge", "-q", f"origin/{branch}", cwd=scratch)
        _git("push", "-q", "origin", "main", cwd=scratch)
        assert _run("registry", "sync")[0] == 0
        rc, out, _ = _run("registry", "pending")
        assert "merged — safe to delete" in out and "registry discard gitty" in out
        rc, _, err = _run("--root", str(veh), "registry", "discard", "gitty", branch)
        assert rc == 0, err                                              # merged: plain cleanup
        assert branch not in _git("branch", "--list", cwd=cache).stdout
        assert pending_count() == 0                                      # nothing left pending


def test_discard_unsaves_the_deployment():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        veh, cache, origin = _git_world()
        render_before = yaml.safe_load(
            (veh / "config" / "sensors" / "cam.yaml").read_text())
        render_before["rate"] = 9                                        # the edit's effect
        _dirty_and_save(veh)
        lock = yaml.safe_load((veh / "rig.lock").read_text())
        assert "gitty/camish:cam@1.0.1" in lock["packages"]              # save re-pinned
        rc, out, _ = _run("registry", "pending")
        branch = next(w for w in out.split() if w.startswith("promote/"))
        rc, _, err = _run("--root", str(veh), "registry", "discard", "gitty", branch)
        assert rc == 0, err
        assert branch not in _git("branch", "--list", cwd=cache).stdout  # branch gone
        assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=cache).stdout.strip() == "main"
        lock = yaml.safe_load((veh / "rig.lock").read_text())
        assert "gitty/camish:cam@1.0.0" in lock["packages"]              # back on the old pin
        assert "gitty/camish:cam@1.0.1" not in lock["packages"]
        rc, out, _ = _run("--root", str(veh), "config", "diff")
        assert "1 instance(s) dirty" in out                              # the delta is LOCAL again
        working = yaml.safe_load((veh / "config" / "sensors" / "cam.yaml").read_text())
        assert working == render_before                                  # WHOLE render preserved
        pin = yaml.safe_load((veh / "config" / ".pins" / "cam.yaml").read_text())
        assert pin["rate"] == 5                                          # pin = pre-save payload


def _local_git_world():
    """A local-dir registry that IS a git checkout — save writes in place; yank restores from
    its history."""
    repo, rev = _code_repo()
    reg = _registry_tree("loc")
    (reg / "services" / "camish" / "manifest.yaml").write_text(yaml.safe_dump({
        "kind": "service", "name": "camish", "version": "1.0.0",
        "source": {"repo": str(repo), "rev": rev}}))
    _run("registry", "index", str(reg))
    _git("init", "-q", "-b", "main", cwd=reg)
    _git("add", "-A", cwd=reg)
    _git("commit", "-q", "-m", "seed", cwd=reg)
    _run("setup", "--no-default-registry")
    _run("registry", "add", "loc", "--path", str(reg))
    veh = pathlib.Path(tempfile.mkdtemp()) / "veh"
    with contextlib.redirect_stderr(io.StringIO()):
        init(veh, no_git=True)
    assert _run("--root", str(veh), "pkg", "add", "loc/camish:cam", "--as", "cam")[0] == 0
    return veh, reg


def test_yank_restores_previous_and_unsaves():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        veh, reg = _local_git_world()
        cfg = veh / "config" / "sensors" / "cam.yaml"
        doc = yaml.safe_load(cfg.read_text())
        doc["rate"] = 9
        cfg.write_text(yaml.safe_dump(doc))
        rc, _, err = _run("--root", str(veh), "pkg", "save", "cam")      # in place: now current
        assert rc == 0, err
        m = yaml.safe_load((reg / "profiles" / "camish" / "cam" / "manifest.yaml").read_text())
        assert m["version"] == "1.0.1"
        rc, _, err = _run("--root", str(veh), "pkg", "yank", "cam", "--from", "loc", "--dry-run")
        assert rc == 0 and "restore @1.0.0" in err
        assert yaml.safe_load((reg / "profiles" / "camish" / "cam" / "manifest.yaml")
                              .read_text())["version"] == "1.0.1"        # dry-run wrote nothing
        rc, _, err = _run("--root", str(veh), "pkg", "yank", "cam", "--from", "loc")
        assert rc == 0, err
        m = yaml.safe_load((reg / "profiles" / "camish" / "cam" / "manifest.yaml").read_text())
        assert m["version"] == "1.0.0"                                   # previous restored
        payload = yaml.safe_load(
            (reg / "profiles" / "camish" / "cam" / "config" / "payload.yaml").read_text())
        assert payload["rate"] == 5
        lock = yaml.safe_load((veh / "rig.lock").read_text())
        assert "loc/camish:cam@1.0.0" in lock["packages"]                # un-saved
        working = yaml.safe_load(cfg.read_text())
        assert working["rate"] == 9                                      # changes LOCAL again
        rc, out, _ = _run("--root", str(veh), "config", "diff")
        assert "1 instance(s) dirty" in out


def test_yank_first_publish_removes_and_falls_back():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        veh, reg = _local_git_world()
        # hand-author an instance, promote --adopt (first publish), then yank it away
        (veh / "config" / "sensors" / "solo.yaml").write_text("service: camish\nmode: x\n")
        v = veh / "vehicle.yaml"
        v.write_text(v.read_text().replace(
            "sensors:", "sensors:\n  - { name: solo, service: camish, "
                        "config: config/sensors/solo.yaml, enabled: true, order: 20 }"))
        rc, _, err = _run("--root", str(veh), "pkg", "promote", "solo", "--kind", "profile",
                          "--adopt", "--to", "loc", "--requires", "loc/camish@1.0.0")
        assert rc == 0, err
        lock = yaml.safe_load((veh / "rig.lock").read_text())
        assert "loc/camish:solo@1.0.0" in lock["packages"]
        rc, _, err = _run("--root", str(veh), "pkg", "yank", "solo", "--from", "loc")
        assert rc == 0, err
        assert "removed" in err
        assert not (reg / "profiles" / "camish" / "solo").exists()       # package gone
        lock = yaml.safe_load((veh / "rig.lock").read_text())
        assert "loc/camish:solo@1.0.0" not in lock.get("packages", {})
        assert "solo" not in lock.get("instances", {})                   # back to hand-authored
        assert "profile:" not in next(
            l for l in (veh / "vehicle.yaml").read_text().splitlines() if "solo" in l)
        assert yaml.safe_load((veh / "config" / "sensors" / "solo.yaml")
                              .read_text())["mode"] == "x"               # working kept


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except AssertionError as exc:
                failed += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if failed else 0)
