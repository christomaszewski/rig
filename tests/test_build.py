"""rig build — descriptor build/mirror parsing + dry-run. Run: `.venv/bin/python tests/test_build.py`."""
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from rig_cli.build import build
from rig_cli.descriptor import load_descriptor
from rig_cli.manifest import load_manifest


def _repo(rigging: str) -> pathlib.Path:
    d = pathlib.Path(tempfile.mkdtemp())
    (d / "rigging.yaml").write_text(rigging)
    return d


def test_descriptor_parses_build_string_and_mapping_and_mirror():
    d = load_descriptor("s", _repo("service: s\nlauncher: s-up\nbuild: tools/b.sh\nmirror: [eclipse/zenoh:latest]\n"))
    assert d.build_command == "tools/b.sh" and d.mirror == ["eclipse/zenoh:latest"]
    d2 = load_descriptor("s", _repo("service: s\nlauncher: s-up\nbuild: {command: tools/b.sh}\n"))
    assert d2.build_command == "tools/b.sh" and d2.mirror == []
    d3 = load_descriptor("s", _repo("service: s\nlauncher: s-up\n"))  # neither declared
    assert d3.build_command is None and d3.mirror == []


def test_build_dry_run_does_not_run_anything():
    root = pathlib.Path(tempfile.mkdtemp())
    (root / "config").mkdir()
    svc = _repo("service: s\nlauncher: s-up\nmirror: [busybox:latest]\n")
    (root / "vehicle.yaml").write_text(
        "vehicle: t\nimages: {registry: reg:5000}\nsensors: [{name: a, service: s, config: config/a.yaml}]\n")
    (root / "config" / "a.yaml").write_text("service: s\nname: a\n")
    manifest = load_manifest(root)
    descriptors = {"s": load_descriptor("s", svc)}
    assert build(manifest, descriptors, registry=None, tag=None, dry_run=True) == 0  # prints, runs nothing


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
