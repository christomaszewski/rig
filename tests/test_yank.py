"""pkg yank — the paths test_publishtail leaves dark: SERVICE-kind yank (+ the deployment's
lock repoint), the removal-only degrade on a non-git local-dir registry, and the no-deployment
root=None path. Run: python3 tests/test_yank.py"""
import contextlib
import io
import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import yaml  # noqa: E402

from test_install import _env, _git, _run  # noqa: E402  (shared fixtures)

from rig_cli.init import init  # noqa: E402
from rig_cli.registry_scaffold import registry_init  # noqa: E402


def _svc_repo() -> tuple[pathlib.Path, str]:
    """A single-service code repo (the publishable shape a service manifest pins)."""
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


def _write_service_manifest(reg: pathlib.Path, version: str, repo: pathlib.Path, rev: str) -> None:
    d = reg / "services" / "camish"
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.yaml").write_text(yaml.safe_dump({
        "kind": "service", "name": "camish", "version": version,
        "source": {"repo": str(repo), "rev": rev}}))


def _fresh_veh() -> pathlib.Path:
    veh = pathlib.Path(tempfile.mkdtemp()) / "veh"
    with contextlib.redirect_stderr(io.StringIO()):
        init(veh, no_git=True)
    return veh


def _service_world():
    """A git-backed LOCAL-DIR registry whose service moved 1.0.0 -> 1.1.0 (both committed), plus
    a deployment installed at 1.1.0. Returns (veh, reg, rev1)."""
    repo, rev1 = _svc_repo()
    reg = pathlib.Path(tempfile.mkdtemp()) / "loc"
    with contextlib.redirect_stderr(io.StringIO()):
        registry_init(reg, namespace="loc")
    _write_service_manifest(reg, "1.0.0", repo, rev1)
    _run("registry", "index", str(reg))
    _git("init", "-q", "-b", "main", cwd=reg)
    _git("add", "-A", cwd=reg)
    _git("commit", "-q", "-m", "camish 1.0.0", cwd=reg)
    (repo / "camish-up").write_text("#!/bin/sh\n# v2\n")        # a second release moves the pin
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "v2", cwd=repo)
    rev2 = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    _write_service_manifest(reg, "1.1.0", repo, rev2)
    _run("registry", "index", str(reg))
    _git("add", "-A", cwd=reg)
    _git("commit", "-q", "-m", "camish 1.1.0", cwd=reg)
    _run("setup", "--no-default-registry")
    _run("registry", "add", "loc", "--path", str(reg))
    veh = _fresh_veh()
    assert _run("--root", str(veh), "pkg", "add", "loc/camish")[0] == 0
    return veh, reg, rev1


def test_yank_service_kind_restores_previous_and_repoints_lock():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        veh, reg, rev1 = _service_world()
        lock = yaml.safe_load((veh / "rig.lock").read_text())
        assert "loc/camish@1.1.0" in lock["packages"]           # installed at the yanked-to-be pin
        rc, _, err = _run("--root", str(veh), "pkg", "yank", "camish", "--from", "loc")
        assert rc == 0, err
        assert "restore @1.0.0" in err and "current is @1.0.0 again" in err
        m = yaml.safe_load((reg / "services" / "camish" / "manifest.yaml").read_text())
        assert m["version"] == "1.0.0" and m["source"]["rev"] == rev1   # previous release restored
        index = json.loads((reg / "index.json").read_text())
        assert index["packages"]["camish"]["version"] == "1.0.0"        # index regenerated
        # the deployment half: the SERVICE lock row repoints; vendored surfaces stay put
        assert "lock repointed" in err and "vendored surfaces unchanged" in err
        lock = yaml.safe_load((veh / "rig.lock").read_text())
        assert "loc/camish@1.0.0" in lock["packages"]
        assert lock["packages"]["loc/camish@1.0.0"]["source"]["rev"] == rev1
        assert "loc/camish@1.1.0" not in lock["packages"]
        assert (veh / "services" / "camish" / ".vendored.yaml").is_file()


def test_yank_non_git_local_dir_is_removal_only():
    # No git history: nothing to restore — yank degrades to full removal and says so plainly.
    with _env(RIG_HOME=tempfile.mkdtemp()):
        repo, rev = _svc_repo()
        reg = pathlib.Path(tempfile.mkdtemp()) / "plain"
        with contextlib.redirect_stderr(io.StringIO()):
            registry_init(reg, namespace="plain")
        _write_service_manifest(reg, "1.0.0", repo, rev)
        p = reg / "profiles" / "camish" / "cam"
        (p / "config").mkdir(parents=True)
        (p / "manifest.yaml").write_text(yaml.safe_dump({
            "kind": "profile", "name": "cam", "version": "1.0.0",
            "requires": {"service": "camish@1.0.0"},
            "config": {"payload": "config/payload.yaml"}}))
        (p / "config" / "payload.yaml").write_text("service: camish\nrate: 5\n")
        assert _run("registry", "index", str(reg))[0] == 0      # a plain folder — never git init'd
        _run("setup", "--no-default-registry")
        _run("registry", "add", "plain", "--path", str(reg))
        veh = _fresh_veh()                                      # present but not referencing 'cam'
        rc, _, err = _run("--root", str(veh), "pkg", "yank", "cam", "--from", "plain", "--dry-run")
        assert rc == 0, err
        assert "REMOVE the package" in err and "no git history to restore from" in err
        assert (p / "manifest.yaml").is_file()                  # dry-run wrote nothing
        rc, _, err = _run("--root", str(veh), "pkg", "yank", "cam", "--from", "plain")
        assert rc == 0, err
        assert "REMOVE the package" in err and "removed" in err
        assert not (reg / "profiles" / "camish").exists()       # package + emptied profiles/<svc>/
        index = json.loads((reg / "index.json").read_text())
        assert "camish:cam" not in index["packages"]            # index regenerated without it
        assert "camish" in index["packages"]                    # the unrelated service survives


def test_yank_without_deployment_skips_the_fixup():
    # root=None: no deployment in reach — the registry half runs, the fix-up half must not.
    with _env(RIG_HOME=tempfile.mkdtemp()):
        veh, reg, rev1 = _service_world()
        nowhere = pathlib.Path(tempfile.mkdtemp())              # no vehicle.yaml anywhere up-tree
        rc, _, err = _run("--root", str(nowhere), "pkg", "yank", "camish", "--from", "loc")
        assert rc == 0, err
        assert "current is @1.0.0 again" in err
        m = yaml.safe_load((reg / "services" / "camish" / "manifest.yaml").read_text())
        assert m["version"] == "1.0.0"                          # registry half done
        assert "lock repointed" not in err and "WARNING" not in err   # fix-up skipped, not botched
        assert "deployments ELSEWHERE" in err                   # the caveat still prints
        lock = yaml.safe_load((veh / "rig.lock").read_text())
        assert "loc/camish@1.1.0" in lock["packages"]           # the far deployment stays untouched
        assert "loc/camish@1.0.0" not in lock["packages"]


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
