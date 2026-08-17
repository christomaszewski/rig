"""Profile identity = the (service, short) tuple, spelled `service:short` (schema 2): compound
refs through add/search, priority order, nested layout, foreign-service profiles, reserved
service names. Run: python3 tests/test_service_profile.py"""
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
from rig_cli.manifest import load_manifest  # noqa: E402
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
        try:
            rc = main(list(argv))
        except SystemExit as exc:  # argparse
            rc = int(exc.code or 0)
    return rc, out.getvalue(), err.getvalue()


def _git(*args, cwd):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          cwd=cwd, capture_output=True, text=True)


def _code_repo() -> tuple[pathlib.Path, str]:
    repo = pathlib.Path(tempfile.mkdtemp()) / "code"
    for svc, tier in (("camish", "sensor"), ("routerish", "infra")):
        d = repo / svc
        (d / "config").mkdir(parents=True)
        (d / "rigging.yaml").write_text(
            f"service: {svc}\nlauncher: {svc}-up\ntier: {tier}\n"
            f"examples: [config/{svc}.example.yaml]\nlaunch_surface: [{svc}-up]\n")
        (d / f"{svc}-up").write_text("#!/bin/sh\n")
        (d / f"{svc}-up").chmod(0o755)
        (d / "config" / f"{svc}.example.yaml").write_text(f"service: {svc}\nrate: 1\n")
    _git("init", "-q", cwd=repo)
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "code", cwd=repo)
    return repo, _git("rev-parse", "HEAD", cwd=repo).stdout.strip()


def _registry(ns: str, repo: pathlib.Path, rev: str, profiles: dict[str, str],
              services=("camish", "routerish")) -> pathlib.Path:
    """A registry carrying `services` + profiles as {short-name: requires-service-constraint};
    each profile nests under the unqualified service half of its constraint."""
    reg = pathlib.Path(tempfile.mkdtemp()) / ns
    with contextlib.redirect_stderr(io.StringIO()):
        registry_init(reg, namespace=ns)
    for svc in services:
        d = reg / "services" / svc
        d.mkdir(parents=True)
        (d / "manifest.yaml").write_text(yaml.safe_dump({
            "kind": "service", "name": svc, "version": "1.2.0",
            "source": {"repo": str(repo), "rev": rev, "path": svc}}))
    for short, constraint in profiles.items():
        svc = constraint.rpartition("/")[-1].partition("@")[0]
        p = reg / "profiles" / svc / short
        (p / "config").mkdir(parents=True)
        (p / "manifest.yaml").write_text(yaml.safe_dump({
            "kind": "profile", "name": short, "version": "1.0.0",
            "requires": {"service": constraint},
            "config": {"payload": "config/payload.yaml"}}))
        (p / "config" / "payload.yaml").write_text(f"service: {svc}\nrate: 9\n")
    assert _run("registry", "index", str(reg))[0] == 0
    return reg


def _world(*registries: pathlib.Path) -> pathlib.Path:
    _run("setup", "--no-default-registry")
    for reg in registries:
        assert _run("registry", "add", reg.name, "--path", str(reg))[0] == 0
    target = pathlib.Path(tempfile.mkdtemp()) / "veh"
    with contextlib.redirect_stderr(io.StringIO()):
        init(target, no_git=True)
    return target


def test_add_by_compound_ref_all_spellings():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        repo, rev = _code_repo()
        root = _world(_registry("regone", repo, rev,
                                {"tuned": "camish@^1.0", "routed": "routerish@^1.0"}))
        rc, _, err = _run("--root", str(root), "add", "camish:tuned")   # unqualified
        assert rc == 0, err
        row = next(s for s in load_manifest(root).sensors if s.name == "tuned")
        assert str(row.profile) == "regone/camish:tuned@1.0.0", row.profile
        assert (root / "config" / "sensors" / "tuned.yaml").is_file()   # instance = SHORT name
        # qualified + versioned spellings are ordinary refs now
        rc, _, err = _run("--root", str(root), "pkg", "add", "regone/routerish:routed@1.0.0",
                          "--as", "rt")
        assert rc == 0, err
        assert (root / "config" / "infra" / "rt.yaml").is_file()        # tier follows the service


def test_priority_order_decides_across_registries():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        repo, rev = _code_repo()
        first = _registry("regone", repo, rev, {"tuned": "camish@^1.0"})
        second = _registry("regtwo", repo, rev, {"tuned": "camish@^1.0"})
        root = _world(second, first)  # regtwo at higher priority
        assert _run("--root", str(root), "add", "camish:tuned")[0] == 0
        row = next(s for s in load_manifest(root).sensors if s.name == "tuned")
        assert str(row.profile).startswith("regtwo/"), row.profile


def test_miss_lists_profiles_for_the_service():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        repo, rev = _code_repo()
        root = _world(_registry("regone", repo, rev, {"tuned": "camish@^1.0"}))
        rc, _, err = _run("--root", str(root), "add", "camish:nope")
        assert rc != 0 and "regone/camish:tuned" in err, err            # what DOES exist, listed
        rc, _, err = _run("--root", str(root), "add", "ghost:tuned")
        assert rc != 0 and "not found" in err, err                      # nothing to list: plain miss


def test_search_service_axis_scans_keys():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        repo, rev = _code_repo()
        first = _registry("regone", repo, rev, {"tuned": "camish@^1.0", "tuned-b": "camish@^1.0",
                                                "routed": "routerish@^1.0"})
        second = _registry("regtwo", repo, rev, {"tuned": "camish@^1.0"})
        _world(first, second)
        rc, out, _ = _run("pkg", "search", "camish:")
        assert rc == 0
        assert "regone/camish:tuned" in out and "regone/camish:tuned-b" in out
        assert "regtwo/camish:tuned" in out and "routed" not in out
        rc, out, _ = _run("pkg", "search", "camish:*-b")                # glob on the SHORT half
        assert rc == 0 and "camish:tuned-b" in out and "camish:tuned " not in out
        rc, out, _ = _run("pkg", "search", "routerish:")
        assert rc == 0 and "regone/routerish:routed" in out and "tuned" not in out
        assert _run("pkg", "search", "camish:zzz")[0] == 1


def test_freetext_search_finds_keys_and_requires():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        repo, rev = _code_repo()
        _world(_registry("regone", repo, rev, {"tuned": "camish@^1.0"}))
        rc, out, _ = _run("pkg", "search", "camish")     # key substring covers profiles now
        assert rc == 0 and "regone/camish:tuned" in out, out
        rc, out, _ = _run("pkg", "search", "@^1.0")      # requires axis: needle only in constraint
        assert rc == 0 and "requires: camish@^1.0" in out, out


def test_foreign_service_profile_validates_and_installs():
    # A profile hosted in one registry targeting a service DEFINED in another: placement uses the
    # unqualified service name; requires stays fully qualified; install resolves cross-registry.
    with _env(RIG_HOME=tempfile.mkdtemp()):
        repo, rev = _code_repo()
        base = _registry("testns", repo, rev, {})
        org = _registry("orgreg", repo, rev, {"tuned": "testns/camish@^1.2"}, services=())
        assert _run("registry", "validate", str(org))[0] == 0
        root = _world(org, base)
        rc, _, err = _run("--root", str(root), "add", "camish:tuned")
        assert rc == 0, err
        row = next(s for s in load_manifest(root).sensors if s.name == "tuned")
        assert str(row.profile) == "orgreg/camish:tuned@1.0.0", row.profile
        lock = yaml.safe_load((root / "rig.lock").read_text())
        assert any(r.startswith("testns/camish@") for r in lock["packages"])  # service from testns


def test_service_names_sensor_and_project_are_reserved():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        reg = pathlib.Path(tempfile.mkdtemp()) / "reg"
        with contextlib.redirect_stderr(io.StringIO()):
            registry_init(reg, namespace="reg")
        d = reg / "services" / "sensor"
        d.mkdir(parents=True)
        (d / "manifest.yaml").write_text(yaml.safe_dump({
            "kind": "service", "name": "sensor", "version": "1.0.0",
            "source": {"repo": "https://example.com/x", "rev": "0" * 40, "path": "."}}))
        rc, _, err = _run("registry", "validate", str(reg))
        assert rc != 0 and "reserved" in err, err


def test_promote_short_name_guard():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        repo, rev = _code_repo()
        root = _world(_registry("regone", repo, rev, {"tuned": "camish@^1.0"}))
        assert _run("--root", str(root), "add", "camish:tuned")[0] == 0
        for bad in ("camish:extra", "a/b", "x@1.0.0"):
            rc, _, err = _run("--root", str(root), "pkg", "promote", "tuned",
                              "--kind", "profile", "--name", bad, "--to", "regone")
            assert rc != 0 and "SHORT half" in err, (bad, err)


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
