"""bake compose-transforms. Run: `.venv/bin/python tests/test_bake.py`."""
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from rig_cli.bake import (
    _external_volume_names,
    _pin_images,
    _relocate_file_binds,
    _service_images,
    _strip_build,
)


def test_strip_build_keeps_image():
    c = {"services": {"driver": {"image": "x:tag", "build": {"context": "."}}}}
    _strip_build(c)
    assert "build" not in c["services"]["driver"]
    assert _service_images(c) == {"driver": "x:tag"}


def test_external_volume_names():
    c = {"volumes": {"sock": {"external": True, "name": "gige_cam_sock"}, "data": {}}}
    assert _external_volume_names(c) == ["gige_cam_sock"]


def test_pin_images_resolved_and_unresolved():
    c = {"services": {"d": {"image": "x:tag"}, "e": {"image": "y:tag"}}}
    _pin_images(c, {"x:tag": "x@sha256:abc", "y:tag": None})
    assert c["services"]["d"]["image"] == "x@sha256:abc"
    assert c["services"]["e"]["image"] == "y:tag"  # unresolved -> left as a tag


def test_relocate_file_binds_relativizes_files_not_devices():
    src = pathlib.Path(tempfile.mkdtemp()) / "params.yaml"
    src.write_text("a: 1\n")
    dest = pathlib.Path(tempfile.mkdtemp())
    c = {"services": {"driver": {"volumes": [
        {"type": "bind", "source": str(src), "target": "/etc/p.yaml"},
        {"type": "bind", "source": "/dev/sbg_imu", "target": "/dev/sbg_imu"},
    ]}}}
    _relocate_file_binds(c, dest)
    vols = c["services"]["driver"]["volumes"]
    assert vols[0]["source"] == "./driver__params.yaml" and (dest / "driver__params.yaml").exists()
    assert vols[1]["source"] == "/dev/sbg_imu"  # device left literal


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
