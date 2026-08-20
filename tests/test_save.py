"""pkg save — publish local edits into the package they came from (QoL plan F6).
Run: python3 tests/test_save.py"""
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


def _code_repo(svc="camish") -> tuple[pathlib.Path, str]:
    repo = pathlib.Path(tempfile.mkdtemp()) / svc
    (repo / "config").mkdir(parents=True)
    (repo / "rigging.yaml").write_text(
        f"service: {svc}\nlauncher: {svc}-up\ntier: sensor\n"
        f"examples: [config/{svc}.example.yaml]\nlaunch_surface: [{svc}-up]\n")
    (repo / f"{svc}-up").write_text("#!/bin/sh\n")
    (repo / f"{svc}-up").chmod(0o755)
    (repo / "config" / f"{svc}.example.yaml").write_text(f"service: {svc}\nrate: 1\n")
    _git("init", "-q", cwd=repo)
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "code", cwd=repo)
    return repo, _git("rev-parse", "HEAD", cwd=repo).stdout.strip()


def _registry(ns: str, repo, rev) -> pathlib.Path:
    reg = pathlib.Path(tempfile.mkdtemp()) / ns
    with contextlib.redirect_stderr(io.StringIO()):
        registry_init(reg, namespace=ns)
    d = reg / "services" / "camish"
    d.mkdir(parents=True)
    (d / "manifest.yaml").write_text(yaml.safe_dump({
        "kind": "service", "name": "camish", "version": "1.0.0",
        "source": {"repo": str(repo), "rev": rev}}))
    p = reg / "profiles" / "camish" / "cam"
    (p / "config").mkdir(parents=True)
    (p / "manifest.yaml").write_text(yaml.safe_dump({
        "kind": "profile", "name": "cam", "version": "1.0.0",
        "provides": {"sensor": [{"model": "cam", "match": ["cam"]}]},
        "requires": {"service": "camish@1.0.0"},
        "config": {"payload": "config/payload.yaml"}}))
    (p / "config" / "payload.yaml").write_text(
        "service: camish\nrate: 5\ncamera: {type: usb, gain: 1}\n")
    o = reg / "overlays" / "cam-tune"
    (o / "config").mkdir(parents=True)
    (o / "manifest.yaml").write_text(yaml.safe_dump({
        "kind": "overlay", "name": "cam-tune", "version": "1.0.0",
        "targets": [{"service": "camish"}], "project": "gideon",
        "config": {"payload": "config/delta.yaml"}}))
    (o / "config" / "delta.yaml").write_text("camera: {gain: 3}\n")
    _run("registry", "index", str(reg))
    return reg


def _world():
    repo, rev = _code_repo()
    reg = _registry("sv", repo, rev)
    _run("setup", "--no-default-registry")
    _run("registry", "add", "sv", "--path", str(reg))
    veh = pathlib.Path(tempfile.mkdtemp()) / "veh"
    with contextlib.redirect_stderr(io.StringIO()):
        init(veh, no_git=True)
    return veh, reg, repo


def _render(root, name="cam") -> dict:
    """The effective config surface (identity stripped) — the invariant both save directions
    must preserve."""
    from rig_cli.manifest import load_manifest
    from rig_cli.resolve import deep_merge
    from rig_cli.workingcopy import _strip_identity, expected_base, local_delta
    from rig_cli.common import load_yaml as _ly
    sensor = next(s for s in load_manifest(root).sensors if s.name == name)
    base = expected_base(root, sensor)
    if base is None:
        return _strip_identity(_ly(pathlib.Path(sensor.config)))
    state = local_delta(root, sensor)
    fd, ov = state if state else ({}, {})
    return deep_merge(deep_merge(base, fd), ov)


def test_profile_save_round_trip():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        veh, reg, _ = _world()
        assert _run("--root", str(veh), "pkg", "add", "sv/camish:cam", "--as", "cam")[0] == 0
        cfg = veh / "config" / "sensors" / "cam.yaml"
        doc = yaml.safe_load(cfg.read_text())
        doc["rate"] = 9
        cfg.write_text(yaml.safe_dump(doc))
        before = _render(veh)
        rc, _, err = _run("--root", str(veh), "pkg", "save", "cam")
        assert rc == 0, err
        m = yaml.safe_load((reg / "profiles" / "camish" / "cam" / "manifest.yaml").read_text())
        assert m["version"] == "1.0.1"                                  # auto-bump fired
        payload = yaml.safe_load(
            (reg / "profiles" / "camish" / "cam" / "config" / "payload.yaml").read_text())
        assert payload["rate"] == 9
        assert _render(veh) == before                                   # render identical
        rc, out, _ = _run("--root", str(veh), "config", "diff")
        assert "all pinned instances clean" in out                      # instance clean
        lock = yaml.safe_load((veh / "rig.lock").read_text())
        assert "sv/camish:cam@1.0.1" in lock["packages"]                # re-pinned
        rc, _, err = _run("--root", str(veh), "pkg", "save", "cam")     # clean -> nothing
        assert rc == 0 and "nothing to save" in err


def test_overlay_save_folds_into_the_bound_overlay():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        veh, reg, _ = _world()
        assert _run("--root", str(veh), "pkg", "add", "sv/camish:cam", "--as", "cam")[0] == 0
        assert _run("--root", str(veh), "overlay", "apply", "cam", "sv/cam-tune")[0] == 0
        cfg = veh / "config" / "sensors" / "cam.yaml"
        doc = yaml.safe_load(cfg.read_text())
        doc["camera"]["gain"] = 7          # edits an OVERLAY-owned key (local beats overlays)
        doc["rate"] = None                 # null-delete marker via a real edit
        del doc["rate"]                    # deleting a pin key entirely
        cfg.write_text(yaml.safe_dump(doc))
        before = _render(veh)
        rc, _, err = _run("--root", str(veh), "pkg", "save", "cam")
        assert rc == 0, err
        m = yaml.safe_load((reg / "overlays" / "cam-tune" / "manifest.yaml").read_text())
        assert m["version"] == "1.0.1"                                  # the BOUND overlay moved
        assert m["project"] == "gideon"                                 # carry-forward held
        assert m["authored_against"]["profile"].startswith("sv/camish:cam@")
        delta = yaml.safe_load(
            (reg / "overlays" / "cam-tune" / "config" / "delta.yaml").read_text())
        assert delta["camera"]["gain"] == 7
        assert delta["rate"] is None                                    # delete marker folded
        pm = yaml.safe_load((reg / "profiles" / "camish" / "cam" / "manifest.yaml").read_text())
        assert pm["version"] == "1.0.0"                                 # profile UNTOUCHED
        assert _render(veh) == before                                   # render identical
        from rig_cli.manifest import load_manifest
        sensor = next(s for s in load_manifest(veh).sensors if s.name == "cam")
        assert list(sensor.overlays) == ["sv/cam-tune@1.0.1"]           # rebound in place
        assert yaml.safe_load(cfg.read_text())["camera"]["gain"] == 1   # working = pristine pin


def test_save_refusals():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        veh, reg, _ = _world()
        # hand-authored instance: no provenance
        (veh / "config" / "sensors").mkdir(parents=True, exist_ok=True)
        (veh / "config" / "sensors" / "hand.yaml").write_text("service: camish\nrate: 2\n")
        v = veh / "vehicle.yaml"
        v.write_text(v.read_text().replace(
            "sensors:", "sensors:\n  - { name: hand, service: camish, "
                        "config: config/sensors/hand.yaml, enabled: true, order: 10 }"))
        (veh / "services.yaml").write_text("services:\n  camish: { path: ../nowhere }\n")
        rc, _, err = _run("--root", str(veh), "pkg", "save", "hand")
        assert rc == 1 and "hand-authored" in err and "promote" in err
        rc, _, err = _run("--root", str(veh), "pkg", "save", "ghost")
        assert rc == 1 and "no instance or routed service" in err and "repin" in err


def test_service_save_advances_the_code_pointer_with_config_note():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        veh, reg, repo = _world()
        assert _run("--root", str(veh), "pkg", "add", "sv/camish:cam", "--as", "cam")[0] == 0
        # route camish at the DEV CHECKOUT (the vendored route would be refused)
        sv = veh / "services.yaml"
        sv.write_text(sv.read_text().replace(
            "camish: { path: services/camish }",
            f"camish: {{ path: {os.path.relpath(repo, veh)} }}"))
        rc, _, err = _run("--root", str(veh), "pkg", "save", "camish")  # HEAD == pinned rev
        assert rc == 0 and "nothing to save" in err
        (repo / "NEW").write_text("x\n")
        _git("add", "-A", cwd=repo)
        _git("commit", "-q", "-m", "more", cwd=repo)
        rc, _, err = _run("--root", str(veh), "pkg", "save", "camish")
        assert rc == 1 and "origin" in err                              # unpushed/unremoted HEAD
        origin = pathlib.Path(tempfile.mkdtemp()) / "origin.git"
        _git("clone", "--bare", "-q", str(repo), str(origin), cwd=repo.parent)
        _git("remote", "add", "origin", str(origin), cwd=repo)
        _git("fetch", "-q", "origin", cwd=repo)
        # dirty instance config -> the code-only note names the follow-up
        cfg = veh / "config" / "sensors" / "cam.yaml"
        doc = yaml.safe_load(cfg.read_text())
        doc["rate"] = 42
        cfg.write_text(yaml.safe_dump(doc))
        rc, _, err = _run("--root", str(veh), "pkg", "save", "camish")
        assert rc == 0, err
        head = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
        m = yaml.safe_load((reg / "services" / "camish" / "manifest.yaml").read_text())
        assert m["version"] == "1.0.1" and m["source"]["rev"] == head   # pointer advanced
        assert "published CODE only" in err and "rig pkg save cam" in err


def test_dry_run_writes_nothing():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        veh, reg, _ = _world()
        assert _run("--root", str(veh), "pkg", "add", "sv/camish:cam", "--as", "cam")[0] == 0
        cfg = veh / "config" / "sensors" / "cam.yaml"
        doc = yaml.safe_load(cfg.read_text())
        doc["rate"] = 9
        cfg.write_text(yaml.safe_dump(doc))
        snap_reg = sorted((str(p), p.read_bytes()) for p in reg.rglob("*") if p.is_file())
        snap_veh = sorted((str(p), p.read_bytes()) for p in veh.rglob("*") if p.is_file())
        rc, _, err = _run("--root", str(veh), "pkg", "save", "cam", "--dry-run")
        assert rc == 0 and "dry-run" in err
        assert snap_reg == sorted((str(p), p.read_bytes()) for p in reg.rglob("*") if p.is_file())
        assert snap_veh == sorted((str(p), p.read_bytes()) for p in veh.rglob("*") if p.is_file())


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
