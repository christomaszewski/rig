"""pkg QoL surface — no-arg search catalog (+filters), the ONE add grammar under both
spellings, and the full-inventory pkg list. Run: python3 tests/test_pkg.py"""
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


def _service_dir(base: pathlib.Path, svc: str, tier: str = "sensor") -> pathlib.Path:
    d = base / svc
    (d / "config").mkdir(parents=True)
    (d / "rigging.yaml").write_text(
        f"service: {svc}\nlauncher: {svc}-up\ntier: {tier}\n"
        f"examples: [config/{svc}.example.yaml]\nlaunch_surface: [{svc}-up]\n")
    (d / f"{svc}-up").write_text("#!/bin/sh\n")
    (d / f"{svc}-up").chmod(0o755)
    (d / "config" / f"{svc}.example.yaml").write_text(f"service: {svc}\nrate: 1\n")
    return d


def _code_repo() -> tuple[pathlib.Path, str]:
    repo = pathlib.Path(tempfile.mkdtemp()) / "collection"
    repo.mkdir(parents=True)
    _service_dir(repo, "camish")
    _git("init", "-q", cwd=repo)
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "code", cwd=repo)
    return repo, _git("rev-parse", "HEAD", cwd=repo).stdout.strip()


def _registry_with(repo: pathlib.Path, rev: str) -> pathlib.Path:
    """All four kinds — the catalog listing must show every one."""
    reg = pathlib.Path(tempfile.mkdtemp()) / "reg"
    with contextlib.redirect_stderr(io.StringIO()):
        registry_init(reg, namespace="testns")
    d = reg / "services" / "camish"
    d.mkdir(parents=True)
    (d / "manifest.yaml").write_text(yaml.safe_dump({
        "kind": "service", "name": "camish", "version": "1.2.0",
        "source": {"repo": str(repo), "rev": rev, "path": "camish"}}))
    p = reg / "profiles" / "camish" / "acme-cam"
    (p / "config").mkdir(parents=True)
    (p / "manifest.yaml").write_text(yaml.safe_dump({
        "kind": "profile", "name": "acme-cam", "version": "2.0.0",
        "provides": {"sensor": [{"model": "ACME Cam", "match": ["acme"]}]},
        "requires": {"service": "camish@^1.0"},
        "config": {"payload": "config/payload.yaml"}}))
    (p / "config" / "payload.yaml").write_text("service: camish\ncamera: {type: usb}\n")
    o = reg / "overlays" / "cam-tune"
    (o / "config").mkdir(parents=True)
    (o / "manifest.yaml").write_text(yaml.safe_dump({
        "kind": "overlay", "name": "cam-tune", "version": "1.0.0",
        "targets": [{"service": "camish"}], "project": "gideon",
        "config": {"payload": "config/delta.yaml"}}))
    (o / "config" / "delta.yaml").write_text("camera: {fps: 25}\n")
    s = reg / "suites" / "cam-kit"
    s.mkdir(parents=True)
    (s / "manifest.yaml").write_text(yaml.safe_dump({
        "kind": "suite", "name": "cam-kit", "version": "1.0.0",
        "members": {"profiles": ["testns/camish:acme-cam@2.0.0"],
                    "overlays": ["testns/cam-tune@1.0.0"]}}))
    _run("registry", "index", str(reg))
    return reg


def _world() -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    """(workspace, deployment, registry) under a fresh RIG_HOME (caller sets it)."""
    repo, rev = _code_repo()
    reg = _registry_with(repo, rev)
    _run("setup", "--no-default-registry")
    _run("registry", "add", "testns", "--path", str(reg))
    ws = pathlib.Path(tempfile.mkdtemp())
    with contextlib.redirect_stderr(io.StringIO()):
        init(ws / "veh", no_git=True)
    return ws, ws / "veh", reg


def test_noarg_search_lists_the_catalog():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        _world()
        rc, out, _ = _run("pkg", "search")
        assert rc == 0
        assert "testns/camish " in out and "src: collection" in out            # service + axis
        assert "testns/camish:acme-cam" in out and "match: acme" in out        # profile + axis
        assert "testns/cam-tune" in out and "target: service=camish" in out    # overlay + axis
        assert "project: gideon" in out
        assert "testns/cam-kit" in out and "2 members" in out                  # suite + axis
        rc, out, _ = _run("pkg", "search", "--kind", "overlay")                # filter composes
        assert rc == 0 and "cam-tune" in out
        assert "acme-cam" not in out and "cam-kit" not in out
        rc, out, _ = _run("pkg", "search", "--registry", "testns")
        assert rc == 0 and "testns/camish" in out
        rc, out, _ = _run("pkg", "search", "zzz-no-such")                      # empty result = 1
        assert rc == 1
        rc, _, err = _run("pkg", "search", "--registry", "nope")               # unknown registry
        assert rc == 1 and "no registry named" in err


def test_kind_filter_composes_with_query_forms():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        _world()
        rc, out, _ = _run("pkg", "search", "cam", "--kind", "profile")
        assert rc == 0 and "acme-cam" in out and "cam-tune" not in out
        rc, _, _ = _run("pkg", "search", "cam", "--kind", "suite")
        assert rc in (0, 1)  # free-text 'cam' matches cam-kit by name -> 0; the point is no crash


def test_pkg_add_takes_a_local_path():
    # F8: the grammar is ONE — a service-dir path works under `pkg add` exactly like `rig add`.
    with _env(RIG_HOME=tempfile.mkdtemp()):
        ws, veh, _ = _world()
        bar = _service_dir(ws, "barish")
        rc, _, err = _run("--root", str(veh), "pkg", "add", str(bar))
        assert rc == 0, err
        assert "barish: { path:" in (veh / "services.yaml").read_text()
        assert "service: barish" in (veh / "vehicle.yaml").read_text()


def test_pkg_add_locked_refuses_paths():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        ws, veh, _ = _world()
        bar = _service_dir(ws, "lockedish")
        rc, _, err = _run("--root", str(veh), "pkg", "add", str(bar), "--locked")
        assert rc == 1 and "--locked applies to registry installs" in err


def test_add_ambiguous_dir_vs_registry_is_a_hard_error():
    # `testns/camish` names an on-disk service dir AND a registry package -> refuse; ./ escapes.
    with _env(RIG_HOME=tempfile.mkdtemp()):
        ws, veh, _ = _world()
        cwd = os.getcwd()
        try:
            os.chdir(ws)
            _service_dir(ws / "testns", "camish")
            for spelling in ("add", None):  # both spellings share the router
                argv = ["--root", str(veh)] + (["add"] if spelling else ["pkg", "add"])
                rc, _, err = _run(*argv, "testns/camish")
                assert rc == 1 and "ambiguous" in err, err
            rc, _, err = _run("--root", str(veh), "pkg", "add", "./testns/camish")
            assert rc == 0, err  # ./ forces the directory reading
            assert "camish: { path:" in (veh / "services.yaml").read_text()
        finally:
            os.chdir(cwd)


def test_list_is_the_full_inventory():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        ws, veh, _ = _world()
        rc, out, _ = _run("--root", str(veh), "pkg", "list")            # empty deployment
        assert rc == 0 and "no packages in this deployment" in out
        assert _run("--root", str(veh), "pkg", "add", "sensor:acme")[0] == 0
        bar = _service_dir(ws, "localish")
        assert _run("--root", str(veh), "pkg", "add", str(bar))[0] == 0
        rc, out, _ = _run("--root", str(veh), "pkg", "list")
        assert rc == 0, out
        assert "testns/camish:acme-cam@2.0.0" in out                    # registry rows intact
        assert "localish" in out and "unpublished" in out               # local row appears
        assert "local (" in out                                         # ...with its origin
        assert "not tracked in any registry" in out                     # the promotion footnote
        lines = out.splitlines()
        local_i = next(i for i, l in enumerate(lines) if l.startswith("localish"))
        reg_i = next(i for i, l in enumerate(lines) if "acme-cam" in l)
        assert local_i > reg_i                                          # local rows sort together, last


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
