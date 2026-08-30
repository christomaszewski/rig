"""doctor — config-path resolution (incl. the enabled-aware list selector) + the cross-service
checks (distros, ports, routers, launchers) and the non-ROS-safe instance-name warning.
Run: python3 tests/test_doctor.py"""
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from rig_cli.doctor import _get_path

# hermetic: no /etc/rig identity/vars leak — set at import, fixtures load manifests directly
os.environ["RIG_VEHICLE_LOCAL"] = str(pathlib.Path(tempfile.mkdtemp()) / "absent.yaml")


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


def test_errors_on_vehicle_vs_service_distro_mismatch():
    # ros.distro is load-bearing since `rig build` bakes it into built images (ROS_DISTRO) — a vehicle
    # that pins a distro the services don't target must be a prominent ERROR, not a shrug.
    import tempfile

    from rig_cli.catalog import load_catalog
    from rig_cli.descriptor import load_descriptor
    from rig_cli.doctor import collect
    from rig_cli.manifest import load_manifest

    svc = pathlib.Path(tempfile.mkdtemp())
    (svc / "rigging.yaml").write_text("service: s\nlauncher: s-up\nros_distro: lyrical\n")
    (svc / "s-up").write_text("#!/bin/sh\n")
    (svc / "s-up").chmod(0o755)

    def issues_with(ros_line: str):
        d = pathlib.Path(tempfile.mkdtemp())
        (d / "config").mkdir()
        (d / "vehicle.yaml").write_text(f"vehicle: t\n{ros_line}\n"
                                        "sensors: [{name: a, service: s, config: config/c.yaml}]\n")
        (d / "services.yaml").write_text(f"services: {{s: {{path: {svc}}}}}\n")
        (d / "config" / "c.yaml").write_text("service: s\nname: a\n")
        return collect(load_manifest(d), load_catalog(d), {"s": load_descriptor("s", svc)})

    mismatch = issues_with("ros: {distro: mismatched}")
    assert any(i.level == "ERROR" and "ros.distro=mismatched" in i.message and "ROS_DISTRO" in i.message
               for i in mismatch)
    agree = issues_with("ros: {distro: lyrical}")
    assert not any("ros.distro" in i.message and i.level == "ERROR" for i in agree)
    assert any(i.level == "OK" and "single ROS distro: lyrical" in i.message for i in agree)


def _three_tier_fixture(sensor_enabled: bool, autonomy_name: str = "planner"):
    import tempfile

    from rig_cli.catalog import load_catalog
    from rig_cli.descriptor import load_descriptor
    from rig_cli.doctor import collect
    from rig_cli.manifest import load_manifest

    repos = {}
    for svc in ("cam", "nav"):
        r = pathlib.Path(tempfile.mkdtemp())
        tier = "\ntier: autonomy" if svc == "nav" else ""
        (r / "rigging.yaml").write_text(f"service: {svc}\nlauncher: {svc}-up\nros_distro: lyrical{tier}\n")
        (r / f"{svc}-up").write_text("#!/bin/sh\n")
        (r / f"{svc}-up").chmod(0o755)
        repos[svc] = r
    d = pathlib.Path(tempfile.mkdtemp())
    (d / "config").mkdir()
    (d / "vehicle.yaml").write_text(
        f"vehicle: t\n"
        f"sensors: [{{name: cam0, service: cam, config: config/c.yaml, enabled: {str(sensor_enabled).lower()}}}]\n"
        f"autonomy: [{{name: {autonomy_name}, service: nav, config: config/n.yaml}}]\n")
    (d / "services.yaml").write_text(f"services: {{cam: {{path: {repos['cam']}}}, nav: {{path: {repos['nav']}}}}}\n")
    (d / "config" / "c.yaml").write_text("service: cam\nname: cam0\n")
    (d / "config" / "n.yaml").write_text(f"service: nav\nname: {autonomy_name}\n")
    m = load_manifest(d)
    descs = {svc: load_descriptor(svc, r) for svc, r in repos.items()}
    return collect(m, load_catalog(d), descs)


def _svc_repo(svc: str, rigging: str, *, launcher: bool = True, executable: bool = True) -> pathlib.Path:
    r = pathlib.Path(tempfile.mkdtemp())
    (r / "rigging.yaml").write_text(rigging)
    if launcher:
        (r / f"{svc}-up").write_text("#!/bin/sh\n")
        (r / f"{svc}-up").chmod(0o755 if executable else 0o644)
    return r


def _collect_from(vehicle_body: str, repos: dict, configs: dict):
    from rig_cli.catalog import load_catalog
    from rig_cli.descriptor import load_descriptor
    from rig_cli.doctor import collect
    from rig_cli.manifest import load_manifest

    d = pathlib.Path(tempfile.mkdtemp())
    (d / "config").mkdir()
    (d / "vehicle.yaml").write_text(vehicle_body)
    routes = ", ".join(f"{svc}: {{path: {repo}}}" for svc, repo in repos.items())
    (d / "services.yaml").write_text(f"services: {{{routes}}}\n")
    for rel, body in configs.items():
        (d / "config" / rel).write_text(body)
    descs = {svc: load_descriptor(svc, repo) for svc, repo in repos.items()}
    return collect(load_manifest(d), load_catalog(d), descs)


def test_errors_on_host_port_clash_across_instances():
    # two instances of a host_ports-declaring service claiming ONE port -> ERROR naming both
    svc = _svc_repo("dash", "service: dash\nlauncher: dash-up\nhost_ports: [server.port]\n")

    def issues_with(port_b: int):
        return _collect_from(
            "vehicle: t\n"
            "sensors: [{name: dash_a, service: dash, config: config/a.yaml},\n"
            "          {name: dash_b, service: dash, config: config/b.yaml}]\n",
            {"dash": svc},
            {"a.yaml": "service: dash\nname: dash_a\nserver: {port: 8443}\n",
             "b.yaml": f"service: dash\nname: dash_b\nserver: {{port: {port_b}}}\n"})

    clash = issues_with(8443)
    assert any(i.level == "ERROR" and "host port 8443" in i.message
               and "dash_a" in i.message and "dash_b" in i.message for i in clash)
    assert not any("host port" in i.message for i in issues_with(8444))  # distinct ports: clean


def test_errors_on_mixed_distros_across_services():
    cam = _svc_repo("cam", "service: cam\nlauncher: cam-up\nros_distro: lyrical\n")
    gnss = _svc_repo("gnss", "service: gnss\nlauncher: gnss-up\nros_distro: jazzy\n")
    issues = _collect_from(
        "vehicle: t\n"
        "sensors: [{name: cam0, service: cam, config: config/c.yaml},\n"
        "          {name: gnss0, service: gnss, config: config/g.yaml}]\n",
        {"cam": cam, "gnss": gnss},
        {"c.yaml": "service: cam\nname: cam0\n", "g.yaml": "service: gnss\nname: gnss0\n"})
    assert any(i.level == "ERROR" and "mixed ROS distros" in i.message for i in issues)


def test_zenoh_rmw_without_infra_router_warns():
    cam = _svc_repo("cam", "service: cam\nlauncher: cam-up\nros_distro: lyrical\n")
    router = _svc_repo("zenoh-router", "service: zenoh-router\nlauncher: zenoh-router-up\n")
    without = _collect_from(
        "vehicle: t\nros: {rmw: rmw_zenoh_cpp}\n"
        "sensors: [{name: cam0, service: cam, config: config/c.yaml}]\n",
        {"cam": cam}, {"c.yaml": "service: cam\nname: cam0\n"})
    assert any(i.level == "WARN" and "no zenoh router" in i.message for i in without)
    with_router = _collect_from(
        "vehicle: t\nros: {rmw: rmw_zenoh_cpp}\n"
        "infra: [{name: router, service: zenoh-router, config: config/r.yaml}]\n"
        "sensors: [{name: cam0, service: cam, config: config/c.yaml}]\n",
        {"cam": cam, "zenoh-router": router},
        {"c.yaml": "service: cam\nname: cam0\n",
         "r.yaml": "service: zenoh-router\nname: router\n"})
    assert not any("no zenoh router" in i.message for i in with_router)


def test_launcher_missing_errors_and_non_executable_warns():
    fixture = ("vehicle: t\nsensors: [{name: cam0, service: cam, config: config/c.yaml}]\n",
               {"c.yaml": "service: cam\nname: cam0\n"})
    missing = _svc_repo("cam", "service: cam\nlauncher: cam-up\n", launcher=False)
    issues = _collect_from(fixture[0], {"cam": missing}, fixture[1])
    assert any(i.level == "ERROR" and "launcher missing" in i.message for i in issues)
    non_exec = _svc_repo("cam", "service: cam\nlauncher: cam-up\n", executable=False)
    issues2 = _collect_from(fixture[0], {"cam": non_exec}, fixture[1])
    assert any(i.level == "WARN" and "not executable" in i.message for i in issues2)
    assert not any("launcher missing" in i.message for i in issues2)


def test_partial_state_trio_warns_full_trio_and_none_stay_silent():
    # Operational-state verbs are declared all-three-or-none: a partial trio is a broken support
    # claim (certify ERRORs service-side; doctor is the non-blocking deployment-side echo). A
    # complete trio and no declaration at all are both silent.
    fixture = ("vehicle: t\nsensors: [{name: cam0, service: cam, config: config/c.yaml}]\n",
               {"c.yaml": "service: cam\nname: cam0\n"})
    partial = _svc_repo("cam", "service: cam\nlauncher: cam-up\nverbs: {standby: standby}\n")
    issues = _collect_from(fixture[0], {"cam": partial}, fixture[1])
    assert any(i.level == "WARN" and "partial operational-state verbs" in i.message for i in issues)
    trio = _svc_repo("cam", "service: cam\nlauncher: cam-up\n"
                            "verbs: {standby: standby, activate: activate, state: state}\n")
    issues2 = _collect_from(fixture[0], {"cam": trio}, fixture[1])
    assert not any("operational-state" in i.message for i in issues2)
    none = _svc_repo("cam", "service: cam\nlauncher: cam-up\n")
    issues3 = _collect_from(fixture[0], {"cam": none}, fixture[1])
    assert not any("operational-state" in i.message for i in issues3)


def test_warns_autonomy_with_no_enabled_sensors():
    # a brain with no eyes: enabled autonomy + zero enabled sensors -> WARN; with a sensor on, silent
    issues = _three_tier_fixture(sensor_enabled=False)
    assert any(i.level == "WARN" and "no eyes" in i.message and "planner" in i.message for i in issues)
    assert not any("no eyes" in i.message for i in _three_tier_fixture(sensor_enabled=True))


def test_ros_name_warning_covers_autonomy_tier():
    # autonomy stacks namespace ROS nodes too — a hyphenated instance name gets the same WARN as a sensor
    issues = _three_tier_fixture(sensor_enabled=True, autonomy_name="nav-stack")
    assert any(i.level == "WARN" and "nav-stack" in i.message and "ROS 2 name" in i.message for i in issues)


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
