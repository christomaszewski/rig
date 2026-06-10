"""init — deployment scaffold. Run: python3 tests/test_init.py"""
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from rig_cli import RigError  # noqa: E402
from rig_cli.init import init  # noqa: E402


def test_init_scaffolds_infra_and_sensors_config_dirs():
    target = pathlib.Path(tempfile.mkdtemp()) / "veh"
    init(target)
    assert (target / "config" / "sensors" / ".gitkeep").is_file()
    assert (target / "config" / "infra" / ".gitkeep").is_file()  # infra tier scaffolded alongside sensors
    assert (target / "services").is_dir()
    assert (target / "vehicle.yaml").is_file() and (target / "services.yaml").is_file()


def test_init_refuses_an_existing_deployment():
    target = pathlib.Path(tempfile.mkdtemp()) / "veh"
    init(target)
    try:
        init(target)
        raise AssertionError("expected RigError on re-init")
    except RigError:
        pass


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
