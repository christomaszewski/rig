"""CLI noun-group translation + permanent aliases. Run: python3 tests/test_cli_groups.py"""
import contextlib
import io
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from rig_cli.cli import main, translate_argv  # noqa: E402
from rig_cli.init import init  # noqa: E402

# hermetic: no /etc/rig identity leak — the CLI loads the manifest on every deployment verb
os.environ["RIG_VEHICLE_LOCAL"] = str(pathlib.Path(tempfile.mkdtemp()) / "absent.yaml")
for _key in ("RIG_VEHICLE_ID", "RIG_VEHICLE_NAME"):
    os.environ.pop(_key, None)


def _deployment(with_stack: bool = False) -> pathlib.Path:
    target = pathlib.Path(tempfile.mkdtemp()) / "veh"
    with contextlib.redirect_stderr(io.StringIO()):
        init(target, vehicle_id=1)  # single-vehicle tree: config show/render + pull consume identity
    data = target / "data"
    data.mkdir()
    v = target / "vehicle.yaml"
    body = v.read_text()
    assert 'data_dir: ""' in body, "init scaffold changed — fix this fixture"
    body = body.replace('data_dir: ""', f'data_dir: {data}')  # `run list` needs a data_dir
    if with_stack:  # ONE enabled stack on a stub sh launcher, so grouped verbs act on something real
        svc = pathlib.Path(tempfile.mkdtemp())
        (svc / "rigging.yaml").write_text("service: cam\nlauncher: cam-up\n")
        (svc / "cam-up").write_text("#!/bin/sh\nexit 0\n")
        (svc / "cam-up").chmod(0o755)
        assert "\nsensors:\n" in body, "init scaffold changed — fix this fixture"
        body = body.replace("\nsensors:\n", "\nsensors:\n  - { name: cam0, service: cam, "
                                            "config: config/sensors/cam0.yaml, enabled: true, order: 10 }\n")
        (target / "config" / "sensors" / "cam0.yaml").write_text("service: cam\nname: cam0\n")
        services = target / "services.yaml"
        services.write_text(services.read_text().replace(
            "services: {}", f"services: {{ cam: {{ path: {svc} }} }}"))
    v.write_text(body)
    return target


def _run(*argv) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = main(list(argv))
    return rc, out.getvalue(), err.getvalue()


def test_translation_table():
    assert translate_argv(["run", "list"]) == ["runs"]
    assert translate_argv(["run", "new", "dock-test"]) == ["new-run", "dock-test"]
    assert translate_argv(["run", "end", "--force"]) == ["end-run", "--force"]
    assert translate_argv(["artifact", "bake", "--tag", "v1"]) == ["bake", "--tag", "v1"]
    assert translate_argv(["artifact", "unbake", "x.tar.gz"]) == ["unbake", "x.tar.gz"]
    assert translate_argv(["artifact", "list"]) == ["artifact-list"]
    assert translate_argv(["image", "build", "-j", "3"]) == ["build", "-j", "3"]
    assert translate_argv(["image", "pull"]) == ["pull"]
    assert translate_argv(["image", "audit"]) == ["image-audit"]
    assert translate_argv(["service", "rigify", "d"]) == ["rigify", "d"]
    assert translate_argv(["service", "vendor", "s"]) == ["vendor", "s"]
    assert translate_argv(["service", "certify", "--repo", "."]) == ["certify", "--repo", "."]
    assert translate_argv(["config", "show"]) == ["config"]
    assert translate_argv(["config", "show", "cam"]) == ["config", "cam"]
    assert translate_argv(["config", "render"]) == ["config-render"]


def test_translation_preserves_global_flags():
    assert translate_argv(["--root", "/x", "run", "list"]) == ["--root", "/x", "runs"]
    assert translate_argv(["--root=/x", "artifact", "list"]) == ["--root=/x", "artifact-list"]


def test_legacy_spellings_untouched():
    for argv in (["runs"], ["new-run"], ["bake", "--tag", "v1"], ["build"], ["rigify", "d"],
                 ["config"], ["config", "cam_front"], ["up", "--dry-run"]):
        assert translate_argv(list(argv)) == argv


def test_group_help_synthesized():
    rc, out, _ = _run("run")
    assert rc == 0 and "new" in out and "end" in out and "list" in out
    rc, out, _ = _run("artifact", "--help")
    assert rc == 0 and "bake" in out
    rc, _, err = _run("image", "wrongverb")
    assert rc == 1 and "unknown verb" in err and "build" in err


def test_config_diff_translates_and_runs():
    root = _deployment()
    rc, out, _ = _run("--root", str(root), "config", "diff")
    assert rc == 0 and "clean" in out  # no pinned instances yet -> trivially clean


def test_grouped_commands_run_end_to_end():
    # a wired stack, so config show/render and pull demonstrably ACT on it (an empty loop proves nothing)
    root = _deployment(with_stack=True)
    rc, out, _ = _run("--root", str(root), "run", "list")
    assert rc == 0 and "no runs recorded" in out
    rc, out, _ = _run("--root", str(root), "artifact", "list")
    assert rc == 0 and "no artifacts" in out
    rc, _, err = _run("--root", str(root), "config", "show")
    assert rc == 0 and "cam0" in err                      # the stub launcher really ran for the stack
    rc, out, _ = _run("--root", str(root), "config", "render")
    assert rc == 0 and "cam0" in out                      # per-instance effective config path printed
    rc, _, err = _run("--root", str(root), "image", "pull")
    assert rc == 0 and "cam0" in err                      # per-stack pull line names the stack


def test_unrouted_service_is_a_pointed_error():
    # a manifest row routing to a service the catalog lacks -> _load's pointed RigError, no KeyError
    root = pathlib.Path(tempfile.mkdtemp())
    (root / "config").mkdir()
    (root / "vehicle.yaml").write_text(
        "vehicle: t\nvehicle_id: 1\nsensors: [{name: gnss, service: ghost, config: config/g.yaml}]\n")
    (root / "config" / "g.yaml").write_text("service: ghost\nname: gnss\n")
    (root / "services.yaml").write_text("services: {}\n")
    rc, _, err = _run("--root", str(root), "doctor")
    assert rc == 1 and "sensor 'gnss': service 'ghost' not in services.yaml" in err


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
