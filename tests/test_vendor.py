"""rig vendor — copy a service's launch surface. Run: `.venv/bin/python tests/test_vendor.py`."""
import pathlib
import sys
import tempfile
import textwrap

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from rig_cli import RigError
from rig_cli.common import load_yaml
from rig_cli.vendor import vendor


def _make_source(with_surface: bool = True) -> pathlib.Path:
    s = pathlib.Path(tempfile.mkdtemp())
    surface = (
        "launch_surface:\n  - demo-up\n  - docker/compose/compose.deploy.yaml\n" if with_surface else ""
    )
    (s / "deploy.yaml").write_text("service: demo\nlauncher: demo-up\n" + surface)
    (s / "demo-up").write_text("#!/usr/bin/env bash\necho demo\n")
    (s / "docker" / "compose").mkdir(parents=True)
    (s / "docker" / "compose" / "compose.deploy.yaml").write_text("services: {driver: {image: x}}\n")
    (s / "src").mkdir()
    (s / "src" / "big.cpp").write_text("// driver source — must NOT be vendored\n")
    return s


def test_vendor_copies_only_the_surface_and_stamps():
    root = pathlib.Path(tempfile.mkdtemp())
    vendor("demo", _make_source(), root)
    t = root / "services" / "demo"
    assert (t / "demo-up").exists()
    assert (t / "docker" / "compose" / "compose.deploy.yaml").exists()
    assert (t / "deploy.yaml").exists()        # descriptor always included
    assert not (t / "src").exists()            # source tree is NOT vendored
    stamp = load_yaml(t / ".vendored.yaml")
    assert stamp["service"] == "demo"
    assert "demo-up" in stamp["files"] and "deploy.yaml" in stamp["files"]


def test_revendor_succeeds_on_unchanged_source():
    root = pathlib.Path(tempfile.mkdtemp())
    s = _make_source()
    vendor("demo", s, root)
    vendor("demo", s, root)  # target now has .vendored.yaml -> refreshed, no error
    assert (root / "services" / "demo" / "demo-up").exists()


def test_refuses_to_clobber_a_nonvendored_dir():
    root = pathlib.Path(tempfile.mkdtemp())
    t = root / "services" / "demo"
    t.mkdir(parents=True)
    (t / "handwritten.txt").write_text("do not delete me")
    try:
        vendor("demo", _make_source(), root)
    except RigError as exc:
        assert "isn't a vendored dir" in str(exc)
    else:
        raise AssertionError("expected RigError")


def test_requires_launch_surface_and_errors_on_missing_file():
    root = pathlib.Path(tempfile.mkdtemp())
    try:
        vendor("demo", _make_source(with_surface=False), root)
    except RigError as exc:
        assert "launch_surface" in str(exc)
    else:
        raise AssertionError("expected RigError for missing launch_surface")

    s = _make_source()
    (s / "demo-up").unlink()  # declared but absent
    try:
        vendor("demo", s, root)
    except RigError as exc:
        assert "missing" in str(exc)
    else:
        raise AssertionError("expected RigError for missing surface file")


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
