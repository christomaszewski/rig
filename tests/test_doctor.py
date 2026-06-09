"""doctor — config-path resolution incl. the enabled-aware list selector. Run: python3 tests/test_doctor.py"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from rig_cli.doctor import _get_path


def test_dotted_and_list_selector():
    cfg = {"camera": {"frame_rate": 20}, "plugins": [
        {"name": "ros2-bridge", "enabled": True, "params": {"topic": "x"}},
        {"name": "webrtc-bridge", "enabled": True, "params": {"port": 8443}},
    ]}
    assert _get_path(cfg, "camera.frame_rate") == 20
    assert _get_path(cfg, "plugins[name=webrtc-bridge].params.port") == 8443
    assert _get_path(cfg, "plugins[name=absent].params.port") is None       # no match -> None
    assert _get_path(cfg, "camera.nope") is None                            # missing key -> None


def test_selector_is_enabled_aware():
    cfg = {"plugins": [{"name": "webrtc-bridge", "enabled": False, "params": {"port": 8443}}]}
    # enabled=true condition + a DISABLED plugin -> no match -> None (so a disabled port isn't a false clash)
    assert _get_path(cfg, "plugins[name=webrtc-bridge,enabled=true].params.port") is None
    # bool matches case-insensitively
    on = {"plugins": [{"name": "webrtc-bridge", "enabled": True, "params": {"port": 8443}}]}
    assert _get_path(on, "plugins[name=webrtc-bridge,enabled=true].params.port") == 8443


def test_warns_on_non_ros_safe_sensor_name():
    import tempfile

    from rig_cli.catalog import load_catalog
    from rig_cli.descriptor import load_descriptor
    from rig_cli.doctor import collect
    from rig_cli.manifest import load_manifest

    svc = pathlib.Path(tempfile.mkdtemp())
    (svc / "rigging.yaml").write_text("service: cam\nlauncher: cam-up\nros_distro: lyrical\n")
    (svc / "cam-up").write_text("#!/bin/sh\n")
    (svc / "cam-up").chmod(0o755)

    def manifest_with(name):
        d = pathlib.Path(tempfile.mkdtemp())
        (d / "config").mkdir()
        (d / "vehicle.yaml").write_text(f"vehicle: t\nsensors: [{{name: {name}, service: cam, config: config/c.yaml}}]\n")
        (d / "services.yaml").write_text(f"services: {{cam: {{path: {svc}}}}}\n")
        (d / "config" / "c.yaml").write_text(f"service: cam\nname: {name}\n")
        return load_manifest(d), load_catalog(d), {"cam": load_descriptor("cam", svc)}

    m, cat, descs = manifest_with("cam-usb")     # hyphen -> invalid ROS name -> WARN
    assert any(i.level == "WARN" and "cam-usb" in i.message and "ROS 2 name" in i.message for i in collect(m, cat, descs))
    m2, cat2, descs2 = manifest_with("cam_usb")  # underscore -> no such warning
    assert not any("ROS 2 name" in i.message for i in collect(m2, cat2, descs2))


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
