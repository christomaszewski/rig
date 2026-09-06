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
    (s / "rigging.yaml").write_text("service: demo\nlauncher: demo-up\n" + surface)
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
    assert (t / "rigging.yaml").exists()       # descriptor always included
    assert not (t / "src").exists()            # source tree is NOT vendored
    stamp = load_yaml(t / ".vendored.yaml")
    assert stamp["service"] == "demo"
    assert "demo-up" in stamp["files"] and "rigging.yaml" in stamp["files"]


def test_vendor_copies_a_directory_in_the_surface():
    s = pathlib.Path(tempfile.mkdtemp())
    (s / "rigging.yaml").write_text("service: demo\nlauncher: demo-up\nlaunch_surface:\n  - demo-up\n  - www\n")
    (s / "demo-up").write_text("#!/usr/bin/env bash\n")
    (s / "www").mkdir()
    (s / "www" / "index.html").write_text("<html></html>")
    root = pathlib.Path(tempfile.mkdtemp())
    vendor("demo", s, root)
    assert (root / "services" / "demo" / "www" / "index.html").is_file()  # whole directory vendored


def test_legacy_deploy_yaml_still_vendors():
    s = pathlib.Path(tempfile.mkdtemp())
    (s / "deploy.yaml").write_text("service: demo\nlauncher: demo-up\nlaunch_surface:\n  - demo-up\n")
    (s / "demo-up").write_text("#!/usr/bin/env bash\n")
    root = pathlib.Path(tempfile.mkdtemp())
    vendor("demo", s, root)
    assert (root / "services" / "demo" / "deploy.yaml").exists()  # legacy descriptor vendored under its name


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
    # The guard exists to PREVENT data loss, not just to phrase an error: the hand-written
    # tree must survive the refusal untouched (no rmtree, no partial copy over it).
    assert (t / "handwritten.txt").read_text() == "do not delete me"
    assert not (t / "demo-up").exists() and not (t / ".vendored.yaml").exists()


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

# --- v0.2.51: the vendored copy is never deleted before the new one is whole (finding 2) -------

def test_vendor_refuses_its_own_vendored_copy_as_source():
    """After a registry install services.yaml routes the service at services/<service> — the
    vendored copy itself — and `rig vendor <service>` defaults --from to that route. vendor()
    deleted the target first and then read the surface from it: the launcher was gone and the
    error came after. Refuse up front, with nothing touched."""
    root = pathlib.Path(tempfile.mkdtemp())
    target = vendor("demo", _make_source(), root)
    before = sorted(p.name for p in target.rglob("*"))
    try:
        vendor("demo", target, root)
    except RigError as exc:
        assert "IS the source" in str(exc) and "--from" in str(exc)
    else:
        raise AssertionError("expected RigError")
    assert (target / "demo-up").exists()
    assert sorted(p.name for p in target.rglob("*")) == before   # byte-for-byte the same tree


def test_revendor_from_an_incomplete_source_keeps_the_old_copy():
    """The same ordering bug from the other side: a source missing ONE declared surface file
    raised the "entry missing" error only after the old copy was rmtree'd. The copy is now built
    beside the target and swapped in last, so a bad refresh leaves the working vendored dir as
    it was."""
    root = pathlib.Path(tempfile.mkdtemp())
    good = _make_source()
    target = vendor("demo", good, root)
    stamp_before = (target / ".vendored.yaml").read_text()
    broken = _make_source()
    (broken / "docker" / "compose" / "compose.deploy.yaml").unlink()   # declared, absent
    try:
        vendor("demo", broken, root)
    except RigError as exc:
        assert "missing" in str(exc)
    else:
        raise AssertionError("expected RigError")
    assert (target / "demo-up").exists()
    assert (target / "docker" / "compose" / "compose.deploy.yaml").exists()
    assert (target / ".vendored.yaml").read_text() == stamp_before       # the old stamp, not a new one
    assert not [p for p in target.parent.iterdir() if p.name.startswith(".demo.vendoring-")]  # no litter

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
