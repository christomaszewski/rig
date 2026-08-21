"""pkg repin — registry-side dependency-pin advance (QoL plan F3).
Run: python3 tests/test_repin.py"""
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


def _registry(ns: str) -> pathlib.Path:
    reg = pathlib.Path(tempfile.mkdtemp()) / ns
    with contextlib.redirect_stderr(io.StringIO()):
        registry_init(reg, namespace=ns)
    return reg


def _mk_service(reg, name, version):
    d = reg / "services" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.yaml").write_text(yaml.safe_dump({
        "kind": "service", "name": name, "version": version,
        "source": {"repo": "https://example.com/x", "rev": "a" * 40}}))


def _mk_profile(reg, svc, short, version, requires, payload="service: camish\nrate: 1\n"):
    p = reg / "profiles" / svc / short
    (p / "config").mkdir(parents=True, exist_ok=True)
    (p / "manifest.yaml").write_text(yaml.safe_dump({
        "kind": "profile", "name": short, "version": version,
        "provides": {"sensor": [{"model": short, "match": [short]}]},
        "requires": {"service": requires}, "config": {"payload": "config/payload.yaml"}}))
    (p / "config" / "payload.yaml").write_text(payload)


def _mk_overlay(reg, name, version, target, authored=None, delta="rate: 2\n"):
    o = reg / "overlays" / name
    (o / "config").mkdir(parents=True, exist_ok=True)
    m = {"kind": "overlay", "name": name, "version": version,
         "targets": [{"service": target}], "config": {"payload": "config/delta.yaml"}}
    if authored:
        m["authored_against"] = authored
    (o / "manifest.yaml").write_text(yaml.safe_dump(m))
    (o / "config" / "delta.yaml").write_text(delta)


def _mk_suite(reg, name, version, members):
    s = reg / "suites" / name
    s.mkdir(parents=True, exist_ok=True)
    (s / "manifest.yaml").write_text(yaml.safe_dump(
        {"kind": "suite", "name": name, "version": version, "members": members}))


def _manifest(reg, kind_dir, key) -> dict:
    return yaml.safe_load((reg / kind_dir / key.replace(":", "/") / "manifest.yaml").read_text())


def test_profile_repin_exact_and_caret():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        reg = _registry("main")
        _mk_service(reg, "camish", "1.5.0")
        _mk_profile(reg, "camish", "exactly", "1.0.0", "camish@1.4.2")
        _mk_profile(reg, "camish", "carety", "1.0.0", "camish@^1.4")
        _run("registry", "index", str(reg))
        _run("setup", "--no-default-registry")
        _run("registry", "add", "main", "--path", str(reg))
        rc, _, err = _run("pkg", "repin", "exactly", "--to", "main")
        assert rc == 0, err
        m = _manifest(reg, "profiles", "camish:exactly")
        assert m["requires"]["service"] == "camish@1.5.0" and m["version"] == "1.0.1"
        assert m["provides"]["sensor"][0]["match"] == ["exactly"]      # carry-forward held
        payload = reg / "profiles" / "camish" / "exactly" / "config" / "payload.yaml"
        assert payload.read_text() == "service: camish\nrate: 1\n"     # payload untouched
        rc, _, err = _run("pkg", "repin", "carety", "--to", "main")    # caret keeps its caret
        assert rc == 0, err
        assert _manifest(reg, "profiles", "camish:carety")["requires"]["service"] == "camish@^1.5.0"
        rc, _, err = _run("pkg", "repin", "exactly", "--to", "main")   # already current
        assert rc == 0 and "already current" in err
        # --dep to a NON-head in-registry version: an explicit exact pin is a legal SNAPSHOT
        # (v0.2.19 — validate warns "stale", install serves it from git history); the publish
        # goes through and the manifest carries the asked-for pin.
        rc, _, err = _run("pkg", "repin", "exactly", "--to", "main", "--dep", "camish@1.4.0")
        assert rc == 0, err
        assert _manifest(reg, "profiles", "camish:exactly")["requires"]["service"] == "camish@1.4.0"


def test_dry_run_writes_nothing():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        reg = _registry("dry")
        _mk_service(reg, "camish", "1.5.0")
        _mk_profile(reg, "camish", "p", "1.0.0", "camish@1.4.0")
        _run("registry", "index", str(reg))
        _run("setup", "--no-default-registry")
        _run("registry", "add", "dry", "--path", str(reg))
        before = sorted((str(p.relative_to(reg)), p.read_bytes()) for p in reg.rglob("*") if p.is_file())
        rc, _, err = _run("pkg", "repin", "p", "--to", "dry", "--dry-run")
        assert rc == 0 and "1.4.0 -> " not in err.replace("requires: camish@1.4.0 -> camish@1.5.0", "X")
        assert "camish@1.5.0" in err and "dry-run" in err
        after = sorted((str(p.relative_to(reg)), p.read_bytes()) for p in reg.rglob("*") if p.is_file())
        assert before == after                                         # byte-identical tree


def test_overlay_restamp_fresh_and_vanished_warning():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        reg = _registry("ovl")
        _mk_service(reg, "camish", "1.5.0")
        _mk_profile(reg, "camish", "base", "2.0.0", "camish@^1.0",
                    payload="service: camish\ncamera: {type: usb}\n")   # rate: GONE in 2.0.0
        _mk_overlay(reg, "tune", "1.0.0", "camish",
                    authored={"service": "ovl/camish@1.4.0",
                              "profile": "ovl/camish:base@1.0.0"},
                    delta="rate: 9\n")
        _mk_overlay(reg, "unstamped", "1.0.0", "camish")                # pre-v0.1.59 package
        _run("registry", "index", str(reg))
        _run("setup", "--no-default-registry")
        _run("registry", "add", "ovl", "--path", str(reg))
        rc, _, err = _run("pkg", "repin", "tune", "--to", "ovl")
        assert rc == 0, err
        m = _manifest(reg, "overlays", "tune")
        assert m["authored_against"]["service"] == "ovl/camish@1.5.0"
        assert m["authored_against"]["profile"] == "ovl/camish:base@2.0.0"
        assert m["version"] == "1.0.1"
        assert "not on the new profile surface" in err or "VANISHED" in err  # rate is gone
        rc, _, err = _run("pkg", "repin", "unstamped", "--to", "ovl")   # OQ-D: fresh stamp
        assert rc == 0, err
        m = _manifest(reg, "overlays", "unstamped")
        assert m["authored_against"] == {"service": "ovl/camish@1.5.0"}
        assert "fresh stamp" in err


def test_suite_refresh_all_and_dep_narrowing():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        reg = _registry("kit")
        _mk_service(reg, "camish", "1.5.0")
        _mk_profile(reg, "camish", "p", "3.0.0", "camish@^1.0")
        _mk_overlay(reg, "o", "2.0.0", "camish")
        _mk_suite(reg, "boat", "1.0.0",
                  {"services": ["kit/camish@1.4.0"],
                   "profiles": ["kit/camish:p@2.0.0"],
                   "overlays": ["kit/o@1.0.0"]})
        _run("registry", "index", str(reg))
        _run("setup", "--no-default-registry")
        _run("registry", "add", "kit", "--path", str(reg))
        rc, _, err = _run("pkg", "repin", "boat", "--to", "kit")       # EVERY member refreshes
        assert rc == 0, err                                            # (registry law: in-registry
        m = _manifest(reg, "suites", "boat")                           #  members sit at head)
        assert m["members"]["overlays"] == ["kit/o@2.0.0"]
        assert m["members"]["services"] == ["kit/camish@1.5.0"]
        assert m["members"]["profiles"] == ["kit/camish:p@3.0.0"]
        assert m["version"] == "1.0.1"
        # unresolvable member = hard error, nothing written
        _mk_suite(reg, "broken", "1.0.0", {"profiles": ["ghost/none:p@1.0.0"]})
        _run("registry", "index", str(reg))
        rc, _, err = _run("pkg", "repin", "broken", "--to", "kit")
        assert rc == 1 and "does not resolve" in err


def test_service_repin_refused_and_git_branch_session():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        reg = _registry("gitty")
        _mk_service(reg, "camish", "1.5.0")
        _mk_profile(reg, "camish", "p", "1.0.0", "camish@1.4.0")
        _run("registry", "index", str(reg))
        _git("init", "-q", cwd=reg)
        _git("add", "-A", cwd=reg)
        _git("commit", "-q", "-m", "seed", cwd=reg)
        _run("setup", "--no-default-registry")
        _run("registry", "add", "gitty", str(reg))                     # git-type
        assert _run("registry", "sync")[0] == 0
        rc, _, err = _run("pkg", "repin", "camish", "--to", "gitty")
        assert rc == 1 and "promote --kind service" in err             # services refused
        rc, _, err = _run("pkg", "repin", "p", "--to", "gitty")
        assert rc == 0, err
        cache = pathlib.Path(os.environ["RIG_HOME"]) / "cache" / "registries" / "gitty"
        branches = _git("branch", "--list", cwd=cache).stdout
        assert "promote/repin-camish-p" in branches                    # local branch, no push
        assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=cache).stdout.strip() != \
               "promote/repin-camish-p"                                # cache back on default
        # the SOURCE local-dir tree is untouched (the cache clone carries the branch)
        assert yaml.safe_load((reg / "profiles" / "camish" / "p" / "manifest.yaml")
                              .read_text())["requires"]["service"] == "camish@1.4.0"


def test_repin_then_upgrade_e2e():
    """The staleness triangle round-trips: outdated -> repin -> sync -> deployment upgrade."""
    from rig_cli.init import init
    with _env(RIG_HOME=tempfile.mkdtemp()):
        # a real code repo so install can vendor the service
        code = pathlib.Path(tempfile.mkdtemp()) / "camish"
        (code / "config").mkdir(parents=True)
        (code / "rigging.yaml").write_text(
            "service: camish\nlauncher: camish-up\ntier: sensor\n"
            "examples: [config/camish.example.yaml]\nlaunch_surface: [camish-up]\n")
        (code / "camish-up").write_text("#!/bin/sh\n")
        (code / "camish-up").chmod(0o755)
        (code / "config" / "camish.example.yaml").write_text("service: camish\nrate: 1\n")
        _git("init", "-q", cwd=code)
        _git("add", "-A", cwd=code)
        _git("commit", "-q", "-m", "c", cwd=code)
        rev = _git("rev-parse", "HEAD", cwd=code).stdout.strip()
        reg = _registry("e2e")
        d = reg / "services" / "camish"
        d.mkdir(parents=True)
        (d / "manifest.yaml").write_text(yaml.safe_dump({
            "kind": "service", "name": "camish", "version": "1.0.0",
            "source": {"repo": str(code), "rev": rev}}))
        _mk_profile(reg, "camish", "cam", "1.0.0", "camish@1.0.0",
                    payload="service: camish\nrate: 5\n")
        _run("registry", "index", str(reg))
        _run("setup", "--no-default-registry")
        _run("registry", "add", "e2e", "--path", str(reg))
        veh = pathlib.Path(tempfile.mkdtemp()) / "veh"
        with contextlib.redirect_stderr(io.StringIO()):
            init(veh, no_git=True)
        assert _run("--root", str(veh), "pkg", "add", "e2e/camish:cam")[0] == 0
        # service moves; the profile's pin is now stale -> outdated flags it -> repin repairs
        m = yaml.safe_load((d / "manifest.yaml").read_text())
        m["version"] = "1.1.0"
        (d / "manifest.yaml").write_text(yaml.safe_dump(m))
        _run("registry", "index", str(reg))
        assert _run("pkg", "outdated", "--registry", "e2e")[0] == 1
        assert _run("pkg", "repin", "cam", "--to", "e2e")[0] == 0
        assert _run("pkg", "outdated", "--registry", "e2e")[0] == 0    # clean after repair
        rc, _, err = _run("--root", str(veh), "pkg", "upgrade")        # consumer follows
        assert rc == 0, err
        lock = yaml.safe_load((veh / "rig.lock").read_text())
        assert "e2e/camish:cam@1.0.1" in lock["packages"]              # repinned profile version
        assert lock["packages"]["e2e/camish:cam@1.0.1"]["requires"] == "e2e/camish@1.1.0"


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
