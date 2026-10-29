"""Offline builds: `rig src sync` fills the local git mirrors, `rig build --offline` checks every
source up front and builds from the mirrors without fetching, and every build receives its
declared git sources (`build.sources`, the msgs overlay's `source:` repos) as RIG_BUILD_SOURCES.
Upstreams are local repos deleted before the offline steps, so any fetch would fail. Build
commands are stub scripts that record their env. Run: python3 tests/test_build_offline.py"""
import contextlib
import io
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from rig_cli import RigError, gitmirror  # noqa: E402
from rig_cli.cli import main  # noqa: E402
from rig_cli.descriptor import load_descriptor  # noqa: E402

GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
os.environ["RIG_VEHICLE_LOCAL"] = str(pathlib.Path(tempfile.mkdtemp()) / "absent.yaml")

STUB = """#!/bin/sh
out="$(dirname "$0")/seen.$(basename "$0")"
{
  for v in RIG_OFFLINE RIG_BUILD_PULL; do eval "echo $v=\\${$v:-UNSET}"; done
  if [ -n "${RIG_BUILD_SOURCES:-}" ]; then
    cat "$RIG_BUILD_SOURCES/sources.tsv"
    (cd "$RIG_BUILD_SOURCES" && find . -type f ! -name sources.tsv | sort)
  else
    echo NO_SOURCES
  fi
} > "$out"
"""


@contextlib.contextmanager
def _env(**over):
    old = {k: os.environ.get(k) for k in over}
    os.environ.update({k: str(v) for k, v in over.items()})
    gitmirror.reset()
    try:
        yield
    finally:
        gitmirror.reset()
        for k, v in old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def _git(*args, cwd):
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _upstream(name, files, tag="v1") -> tuple[pathlib.Path, str]:
    repo = pathlib.Path(tempfile.mkdtemp()) / name
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "c", cwd=repo)
    _git("tag", tag, cwd=repo)
    return repo, _git("rev-parse", "HEAD", cwd=repo)


def _deployment(dep_url, msgs_url, dep_ref="v1"):
    """A base provider with an msgs overlay (logger) and a service declaring a build source and
    an msgs source (drv), both with stub build scripts; legacy (non-matrix) build path."""
    root = pathlib.Path(tempfile.mkdtemp())
    (root / "config").mkdir()
    logger = pathlib.Path(tempfile.mkdtemp())
    (logger / "rigging.yaml").write_text(
        "service: logger\nlauncher: logger-up\n"
        "build: {command: base.sh, images: [fleet-ros], provides: base,\n"
        "        msgs_overlay: {command: msgs.sh, image: fleet-ros-msgs}}\n")
    drv = pathlib.Path(tempfile.mkdtemp())
    (drv / "rigging.yaml").write_text(
        "service: drv\nlauncher: drv-up\n"
        f"build: {{command: build.sh, sources: [{{name: dep, repo: '{dep_url}', ref: {dep_ref}}}]}}\n"
        f"msgs: {{source: [{{repo: '{msgs_url}', ref: v1, packages: [drv_msgs]}}]}}\n")
    for repo, scripts in ((logger, ("base.sh", "msgs.sh")), (drv, ("build.sh",))):
        for script in scripts:
            (repo / script).write_text(STUB)
            (repo / script).chmod(0o755)
    (root / "vehicle.yaml").write_text(
        "vehicle: t\nimages: {registry: reg.test}\nsensors:\n"
        "  - {name: lg, service: logger, config: config/lg.yaml}\n"
        "  - {name: d, service: drv, config: config/d.yaml}\n")
    (root / "services.yaml").write_text(f"services: {{logger: {{path: {logger}}}, drv: {{path: {drv}}}}}\n")
    for name, svc in (("lg", "logger"), ("d", "drv")):
        (root / "config" / f"{name}.yaml").write_text(f"service: {svc}\nname: {name}\n")
    return root, logger, drv


def _rig(*argv) -> tuple[int, str]:
    err = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
        try:
            rc = main(list(argv))
        except RigError as exc:
            return 2, str(exc)
    return rc, err.getvalue()


def _seen(repo, script) -> list[str]:
    return (repo / f"seen.{script}").read_text().splitlines()


def _world():
    dep, dep_sha = _upstream("dep", {"lib/dep.c": "int dep;\n"})
    msgs, msgs_sha = _upstream("drv_msgs", {"drv_msgs/msg/M.msg": "uint8 m\n"})
    return dep, dep_sha, msgs, msgs_sha


def test_sync_then_offline_build_exports_sources_without_fetching():
    with _env(RIG_HOME=tempfile.mkdtemp(), **GIT_ENV):
        dep, dep_sha, msgs, msgs_sha = _world()
        root, logger, drv = _deployment(dep, msgs)
        rc, out = _rig("--root", str(root), "src", "sync")
        assert rc == 0, out
        assert f"drv: build source 'dep' {dep} @ v1 -> {dep_sha[:12]}" in out
        assert f"msgs overlay: msgs source {msgs} @ v1 -> {msgs_sha[:12]}" in out
        for repo in (dep, msgs):
            shutil.rmtree(repo.parent)  # offline from here on
        gitmirror.reset()
        rc, out = _rig("--root", str(root), "build", "--offline", "--check")
        assert rc == 0, out
        assert not list(drv.glob("seen.*")) and not list(logger.glob("seen.*"))  # nothing built
        rc, out = _rig("--root", str(root), "build", "--offline")
        assert rc == 0, out
        assert _seen(drv, "build.sh") == [
            "RIG_OFFLINE=1", "RIG_BUILD_PULL=0",
            f"dep\t{dep}\tv1\t{dep_sha}", "./dep/lib/dep.c"]
        assert _seen(logger, "msgs.sh")[2:] == [f"m0\t{msgs}\tv1\t{msgs_sha}",
                                                "./m0/drv_msgs/msg/M.msg"]
        # the base declares no sources of its own
        assert _seen(logger, "base.sh") == ["RIG_OFFLINE=1", "RIG_BUILD_PULL=0", "NO_SOURCES"]


def test_offline_preflight_lists_every_missing_source_and_builds_nothing():
    with _env(RIG_HOME=tempfile.mkdtemp(), **GIT_ENV):
        dep, _, msgs, _ = _world()
        root, logger, drv = _deployment(dep, msgs, dep_ref="v2")  # v2 never existed
        rc, out = _rig("--root", str(root), "build", "--offline")
        assert rc == 1
        assert "2 thing(s) an offline build would need are missing" in out, out
        assert f"build source 'dep' {dep} @ v2" in out and f"msgs source {msgs} @ v1" in out
        assert "rig src sync" in out
        assert not list(drv.glob("seen.*")) and not list(logger.glob("seen.*"))


def test_online_build_exports_sources_and_never_inherits_offline_channels():
    with _env(RIG_HOME=tempfile.mkdtemp(), RIG_OFFLINE="1", RIG_BUILD_SOURCES="/leaked",
              **GIT_ENV):
        dep, dep_sha, msgs, _ = _world()
        root, logger, drv = _deployment(dep, msgs)
        rc, out = _rig("--root", str(root), "build")  # no sync first: online fetches as needed
        assert rc == 0, out
        seen = _seen(drv, "build.sh")
        assert seen[0] == "RIG_OFFLINE=UNSET"
        assert seen[2] == f"dep\t{dep}\tv1\t{dep_sha}"
        assert _seen(logger, "base.sh")[2] == "NO_SOURCES"


def test_flag_combinations_are_refused():
    with _env(RIG_HOME=tempfile.mkdtemp(), **GIT_ENV):
        dep, _, msgs, _ = _world()
        root, _, _ = _deployment(dep, msgs)
        rc, out = _rig("--root", str(root), "build", "--check")
        assert rc == 1 and "use it with --offline" in out, out
        rc, out = _rig("--root", str(root), "build", "--offline", "--no-cache")
        assert rc == 1 and "cannot use --no-cache" in out, out


def test_offline_mirror_images_must_already_be_in_the_registry():
    from rig_cli import build as build_mod
    from rig_cli.docker_registry import Access
    from rig_cli.manifest import load_manifest
    with _env(RIG_HOME=tempfile.mkdtemp(), **GIT_ENV):
        svc = pathlib.Path(tempfile.mkdtemp())
        (svc / "rigging.yaml").write_text("service: z\nlauncher: z-up\nmirror: [eclipse/zenoh:1.2.1, busybox:1]\n")
        root = pathlib.Path(tempfile.mkdtemp())
        (root / "config").mkdir()
        (root / "vehicle.yaml").write_text(
            "vehicle: t\nsensors: [{name: z, service: z, config: config/z.yaml}]\n")
        (root / "config" / "z.yaml").write_text("service: z\nname: z\n")
        descs = {"z": load_descriptor("z", svc)}
        m = load_manifest(root)
        assert "need a registry" in build_mod.offline_preflight(m, descs, None, root)[0]
        present = {"reg.test/busybox:1"}
        with patch("rig_cli.docker_registry.prepare", return_value=Access()), \
                patch("rig_cli.matrix_build.inspect_raw",
                      side_effect=lambda ref, access=None: {} if ref in present else None):
            problems = build_mod.offline_preflight(m, descs, "reg.test", root)
        assert problems == ["mirror image eclipse/zenoh:1.2.1 is not in reg.test — run `rig build` "
                            "online once to copy it in"]


def test_build_sources_are_validated():
    bad = {"sources: x": "must be a list",
           "sources: [{name: dep, repo: r}]": "needs `name`",
           "sources: [{name: Dep, repo: r, ref: v1}]": "must match",
           "sources: [{name: dep, repo: r, ref: v1, path: x}]": "only name, repo, ref",
           "sources: [{name: d, repo: r, ref: v1}, {name: d, repo: s, ref: v2}]": "declared twice"}
    for snippet, needle in bad.items():
        repo = pathlib.Path(tempfile.mkdtemp())
        (repo / "rigging.yaml").write_text(f"service: s\nlauncher: s-up\nbuild: {{command: b.sh, {snippet}}}\n")
        try:
            load_descriptor("s", repo)
            raise AssertionError(f"accepted {snippet}")
        except RigError as exc:
            assert needle in str(exc), (snippet, exc)


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
