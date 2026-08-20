"""pkg outdated + the sync delta digest — registry-authoring currency (QoL plan F2).
Run: python3 tests/test_outdated.py"""
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


def _mk_service(reg: pathlib.Path, name: str, version: str) -> None:
    d = reg / "services" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.yaml").write_text(yaml.safe_dump({
        "kind": "service", "name": name, "version": version,
        "source": {"repo": "https://example.com/x", "rev": "a" * 40}}))


def _mk_profile(reg: pathlib.Path, svc: str, short: str, version: str, requires: str,
                based_on: str | None = None) -> None:
    p = reg / "profiles" / svc / short
    (p / "config").mkdir(parents=True, exist_ok=True)
    m = {"kind": "profile", "name": short, "version": version,
         "requires": {"service": requires}, "config": {"payload": "config/payload.yaml"}}
    if based_on:
        m["based_on"] = based_on
    (p / "manifest.yaml").write_text(yaml.safe_dump(m))
    (p / "config" / "payload.yaml").write_text(f"service: {svc}\nrate: 1\n")


def _mk_overlay(reg: pathlib.Path, name: str, version: str, target_svc: str,
                authored: dict | None = None) -> None:
    o = reg / "overlays" / name
    (o / "config").mkdir(parents=True, exist_ok=True)
    m = {"kind": "overlay", "name": name, "version": version,
         "targets": [{"service": target_svc}], "config": {"payload": "config/delta.yaml"}}
    if authored:
        m["authored_against"] = authored
    (o / "manifest.yaml").write_text(yaml.safe_dump(m))
    (o / "config" / "delta.yaml").write_text("rate: 2\n")


def _mk_suite(reg: pathlib.Path, name: str, version: str, members: dict) -> None:
    s = reg / "suites" / name
    s.mkdir(parents=True, exist_ok=True)
    (s / "manifest.yaml").write_text(yaml.safe_dump(
        {"kind": "suite", "name": name, "version": version, "members": members}))


def _registry(ns: str) -> pathlib.Path:
    reg = pathlib.Path(tempfile.mkdtemp()) / ns
    with contextlib.redirect_stderr(io.StringIO()):
        registry_init(reg, namespace=ns)
    return reg


def test_all_four_drift_kinds_and_exit_codes():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        reg = _registry("main")
        _mk_service(reg, "camish", "1.5.0")
        _mk_profile(reg, "camish", "stale-exact", "1.0.0", "camish@1.4.2")   # exact pin behind
        _mk_profile(reg, "camish", "caret-ok", "1.0.0", "camish@^1.4")       # covers -> INFO
        _mk_overlay(reg, "old-stamp", "1.0.0", "camish",
                    authored={"service": "main/camish@1.4.2"})               # stamp behind
        _mk_suite(reg, "kit", "1.0.0",
                  {"profiles": ["main/camish:stale-exact@1.0.0"],            # current -> clean
                   "overlays": ["main/old-stamp@0.9.0"]})                    # member behind
        _run("registry", "index", str(reg))
        _run("setup", "--no-default-registry")
        _run("registry", "add", "main", "--path", str(reg))

        rc, out, _ = _run("pkg", "outdated")
        assert rc == 1, out
        assert "stale-exact" in out and "1.4.2" in out and "1.5.0" in out and "pkg repin" in out
        assert "old-stamp" in out                                   # overlay stamp row
        assert "main/kit" in out and "0.9.0" in out                 # suite member row
        assert "caret covers" in out                                # INFO row visible by default
        rc_q, out_q, _ = _run("pkg", "outdated", "--quiet")
        assert rc_q == 1 and "caret covers" not in out_q            # --quiet hides INFO only

        rc, out, _ = _run("pkg", "outdated", "kit")                 # REF narrowing
        assert rc == 1 and "kit" in out and "stale-exact" not in out
        rc, _, err = _run("pkg", "outdated", "no-such-pkg")
        assert rc == 1 and "not found" in err


def test_caret_unsatisfiable_is_drift():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        reg = _registry("major")
        _mk_service(reg, "camish", "2.0.0")                         # major moved
        _mk_profile(reg, "camish", "left-behind", "1.0.0", "camish@^1.4")
        _run("registry", "index", str(reg))
        _run("setup", "--no-default-registry")
        _run("registry", "add", "major", "--path", str(reg))
        rc, out, _ = _run("pkg", "outdated")
        assert rc == 1 and "UNSATISFIABLE" in out and "2.0.0" in out


def test_cross_registry_resolution_and_fail_soft():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        base = _registry("base")
        _mk_service(base, "camish", "1.6.0")
        _run("registry", "index", str(base))
        prog = _registry("prog")
        _mk_profile(prog, "camish", "fork", "1.0.0", "base/camish@1.4.0",
                    based_on="base/camish:parent@1.0.0")            # parent ns unresolvable too?
        _mk_profile(base, "camish", "parent", "2.0.0", "camish@^1.0")
        _run("registry", "index", str(base))
        _run("registry", "index", str(prog))
        _run("setup", "--no-default-registry")
        _run("registry", "add", "base", "--path", str(base))
        _run("registry", "add", "prog", "--path", str(prog))
        rc, out, _ = _run("pkg", "outdated", "--registry", "prog")
        assert rc == 1, out
        assert "base/camish" in out and "1.6.0" in out              # requires resolved cross-registry
        assert "pkg rebase fork" in out.replace("camish:fork", "fork") or "rebase" in out
        # an unconfigured namespace degrades to INFO, never fails the sweep
        _mk_profile(prog, "camish", "orphan", "1.0.0", "ghostns/camish@1.0.0")
        _run("registry", "index", str(prog))
        rc, out, _ = _run("pkg", "outdated", "orphan", "--registry", "prog")
        assert "unresolvable" in out
        rc, out, _ = _run("pkg", "outdated", "orphan", "--registry", "prog", "--quiet")
        assert rc == 0 and "everything current" in out              # INFO-only -> exit 0


def test_outdated_registry_dir_form():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        reg = _registry("adhoc")
        _mk_service(reg, "camish", "1.5.0")
        _mk_profile(reg, "camish", "p", "1.0.0", "camish@1.4.0")
        _run("registry", "index", str(reg))
        _run("setup", "--no-default-registry")                      # NOT configured as an entry
        rc, out, _ = _run("pkg", "outdated", "--registry", str(reg))
        assert rc == 1 and "1.5.0" in out


def test_sync_digest_reports_version_delta():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        upstream = _registry("up")
        _mk_service(upstream, "camish", "1.2.0")
        _mk_service(upstream, "gone-soon", "1.0.0")
        _run("registry", "index", str(upstream))
        _git("init", "-q", cwd=upstream)
        _git("add", "-A", cwd=upstream)
        _git("commit", "-q", "-m", "seed", cwd=upstream)
        _run("setup", "--no-default-registry")
        _run("registry", "add", "up", str(upstream))                # git-type (url = local path)
        rc, _, err = _run("registry", "sync")
        assert rc == 0 and "cloned" in err and "->" not in err      # first clone: no digest
        # upstream moves: bump one, add one, drop one
        _mk_service(upstream, "camish", "1.3.0")
        _mk_service(upstream, "fresh", "1.0.0")
        import shutil
        shutil.rmtree(upstream / "services" / "gone-soon")
        _run("registry", "index", str(upstream))
        _git("add", "-A", cwd=upstream)
        _git("commit", "-q", "-m", "move", cwd=upstream)
        rc, _, err = _run("registry", "sync")
        assert rc == 0, err
        assert "camish 1.2.0 -> 1.3.0" in err
        assert "+ fresh (service) 1.0.0" in err
        assert "- gone-soon" in err
        rc, _, err = _run("registry", "sync")                       # no change -> quiet
        assert rc == 0 and "->" not in err.replace("- gone-soon", "")


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
