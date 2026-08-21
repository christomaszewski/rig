"""CLI noun-group translation + permanent aliases. Run: python3 tests/test_cli_groups.py"""
import contextlib
import io
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from rig_cli.cli import main, translate_argv  # noqa: E402
from rig_cli.init import init  # noqa: E402


def _deployment() -> pathlib.Path:
    target = pathlib.Path(tempfile.mkdtemp()) / "veh"
    with contextlib.redirect_stderr(io.StringIO()):
        init(target, vehicle_id=1)  # single-vehicle tree: config show/render + pull consume identity
    data = target / "data"
    data.mkdir()
    v = target / "vehicle.yaml"
    body = v.read_text()
    assert 'data_dir: ""' in body, "init scaffold changed — fix this fixture"
    v.write_text(body.replace('data_dir: ""', f'data_dir: {data}'))  # `run list` needs a data_dir
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
    root = _deployment()
    rc, out, _ = _run("--root", str(root), "run", "list")
    assert rc == 0 and "no runs recorded" in out
    rc, out, _ = _run("--root", str(root), "artifact", "list")
    assert rc == 0 and "no artifacts" in out
    rc, _, _ = _run("--root", str(root), "config", "show")
    assert rc == 0
    rc, _, _ = _run("--root", str(root), "config", "render")
    assert rc == 0
    rc, _, _ = _run("--root", str(root), "image", "pull")
    assert rc == 0


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
