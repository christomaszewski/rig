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


def _service_with_build(name, script_body):
    s = pathlib.Path(tempfile.mkdtemp())
    (s / "rigging.yaml").write_text(f"service: {name}\nlauncher: {name}-up\nbuild: build.sh\n")
    (s / "build.sh").write_text("#!/bin/sh\n" + script_body)
    (s / "build.sh").chmod(0o755)
    return s


def test_build_runs_a_service_once_for_multiple_instances():
    svc = _service_with_build("cam", f'echo built >> "{tempfile.gettempdir()}/rig_dedupe.log"\n')
    log = pathlib.Path(tempfile.gettempdir(), "rig_dedupe.log")
    log.unlink(missing_ok=True)
    root = pathlib.Path(tempfile.mkdtemp())
    (root / "config").mkdir()
    (root / "vehicle.yaml").write_text(
        "vehicle: t\nimages: {registry: r:5000}\nsensors:\n"
        "  - {name: a, service: cam, config: config/a.yaml}\n"
        "  - {name: b, service: cam, config: config/b.yaml}\n")
    (root / "services.yaml").write_text(f"services: {{cam: {{path: {svc}}}}}\n")
    (root / "config" / "a.yaml").write_text("service: cam\nname: a\n")
    (root / "config" / "b.yaml").write_text("service: cam\nname: b\n")
    m = load_manifest(root)
    assert build(m, {"cam": load_descriptor("cam", svc)}, registry=None, tag=None, dry_run=False) == 0
    assert log.read_text().count("built") == 1  # 2 instances -> the service builds ONCE


def test_build_concurrent_runs_every_service():
    s1 = _service_with_build("svc1", 'touch "$(dirname "$0")/done"\n')
    s2 = _service_with_build("svc2", 'touch "$(dirname "$0")/done"\n')
    root = pathlib.Path(tempfile.mkdtemp())
    (root / "config").mkdir()
    (root / "vehicle.yaml").write_text(
        "vehicle: t\nimages: {registry: r:5000}\nsensors:\n"
        "  - {name: a, service: svc1, config: config/a.yaml}\n"
        "  - {name: b, service: svc2, config: config/b.yaml}\n")
    (root / "services.yaml").write_text(f"services: {{svc1: {{path: {s1}}}, svc2: {{path: {s2}}}}}\n")
    (root / "config" / "a.yaml").write_text("service: svc1\nname: a\n")
    (root / "config" / "b.yaml").write_text("service: svc2\nname: b\n")
    m = load_manifest(root)
    descs = {"svc1": load_descriptor("svc1", s1), "svc2": load_descriptor("svc2", s2)}
    assert build(m, descs, registry=None, tag=None, dry_run=False, jobs=2) == 0
    assert (s1 / "done").exists() and (s2 / "done").exists()  # both ran via the concurrent path


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
