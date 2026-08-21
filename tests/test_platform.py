"""Platform targeting (v0.2.14): the per-host `platform:` field, the descriptor's build matrix +
override_env, RIG_TARGET_PLATFORM routing, composed <tag>-<platform> refs, the legacy
platform-valued images.tag path, and the doctor checks. Run: python3 tests/test_platform.py"""
import contextlib
import os
import pathlib
import sys
import tempfile
import textwrap

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from rig_cli import RigError  # noqa: E402
from rig_cli.build import _build_env, _service_tag  # noqa: E402
from rig_cli.descriptor import Descriptor, load_descriptor  # noqa: E402
from rig_cli.dispatch import fleet_env, service_env  # noqa: E402
from rig_cli.doctor import ERROR, OK, WARN, collect  # noqa: E402
from rig_cli.manifest import Manifest, RosSettings, Sensor, load_manifest  # noqa: E402


@contextlib.contextmanager
def _env(**over):
    old = {k: os.environ.get(k) for k in over}
    for k, v in over.items():
        os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, str(v))
    try:
        yield
    finally:
        for k, v in old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


_NO_MACHINE = str(pathlib.Path(tempfile.mkdtemp()) / "absent.yaml")  # hermetic: no /etc/rig leak


def _deployment(vehicle_yaml: str, local: str | None = None) -> pathlib.Path:
    root = pathlib.Path(tempfile.mkdtemp())
    (root / "vehicle.yaml").write_text(textwrap.dedent(vehicle_yaml))
    if local is not None:
        (root / "vehicle.local.yaml").write_text(textwrap.dedent(local))
    return root


def _write_rigging(body: str) -> pathlib.Path:
    repo = pathlib.Path(tempfile.mkdtemp())
    (repo / "rigging.yaml").write_text(textwrap.dedent(body))
    return repo


def _desc(svc: str = "cam", *, platforms=(), override=None, auto=None,
          images=("cam-core",)) -> Descriptor:
    repo = pathlib.Path(tempfile.mkdtemp())
    launcher = repo / f"{svc}-up"
    launcher.write_text("#!/bin/sh\n")
    launcher.chmod(0o755)
    return Descriptor(service=svc, repo=repo, launcher=f"{svc}-up", verbs={}, ros_distro=None,
                      external_volumes=[], host_ports=[], build_command="build.sh",
                      build_images=list(images), build_platforms=list(platforms),
                      platform_auto_detect=auto, platform_override_env=override)


def _manifest(*, tag=None, platform=None, services=("cam",)) -> Manifest:
    sensors = [Sensor(name=f"s{i}", service=svc, config=pathlib.Path("/dev/null"),
                      enabled=True, order=i) for i, svc in enumerate(services)]
    return Manifest(vehicle="t", ros=RosSettings(domain_id=0, rmw="rmw_fastrtps_cpp", distro=None),
                    sensors=sensors, image_tag=tag, platform=platform)


# --- descriptor parsing -------------------------------------------------------------------------

def test_descriptor_parses_matrix_and_platform_block():
    repo = _write_rigging("""\
        service: cam
        launcher: cam-up
        build: { command: tools/build-images.sh, images: [cam-core, ros2-bridge], platforms: [jp7, jp6] }
        platform: { auto_detect: /etc/nv_tegra_release, override_env: CAM_PLATFORM }
    """)
    desc = load_descriptor("cam", repo)
    assert desc.build_platforms == ["jp7", "jp6"]
    assert desc.platform_auto_detect == "/etc/nv_tegra_release"
    assert desc.platform_override_env == "CAM_PLATFORM"


def test_descriptor_without_matrix_is_platform_independent():
    repo = _write_rigging("service: gnss\nlauncher: gnss-up\nbuild: { command: b.sh, images: [g] }\n")
    desc = load_descriptor("gnss", repo)
    assert desc.build_platforms == [] and desc.platform_override_env is None


def test_descriptor_rejects_bad_platform_declarations():
    for body, needle in (
        ("service: c\nbuild: { command: b, platforms: ['-bad'] }\n", "tag fragment"),
        ("service: c\nplatform: { override_env: lower_case }\n", "UPPERCASE"),
        ("service: c\nplatform: { override_env: RIG_IMAGE_TAG }\n", "rig-owned"),
        ("service: c\nplatform: { override_env: RIG_TARGET_PLATFORM }\n", "rig-owned"),
        ("service: c\nplatform: { overide_env: X }\n", "unknown key"),
    ):
        try:
            load_descriptor("c", _write_rigging(body))
            raise AssertionError(f"expected RigError for {body!r}")
        except RigError as exc:
            assert needle in str(exc), f"{body!r} -> {exc}"


def test_descriptor_rejects_tier_typo():
    # a typo'd tier must not silently demote a service to sensor
    try:
        load_descriptor("c", _write_rigging("service: c\nlauncher: c-up\ntier: sensro\n"))
        raise AssertionError("expected RigError")
    except RigError as exc:
        assert "tier must be" in str(exc) and "sensro" in str(exc)


# --- manifest: the per-host field ---------------------------------------------------------------

def test_manifest_platform_from_vehicle_yaml_and_var_context():
    root = _deployment("vehicle: t\nvehicle_id: 1\nplatform: jp7\nimages: { tag: v1.3.0 }\n")
    with _env(RIG_VEHICLE_LOCAL=_NO_MACHINE):
        m = load_manifest(root)
    assert m.platform == "jp7" and m.image_tag == "v1.3.0"
    assert m.vars["platform"] == "jp7"          # configs may CONSUME {{platform}}


def test_manifest_platform_machine_local_beats_vehicle_yaml():
    machine = pathlib.Path(tempfile.mkdtemp()) / "vehicle.local.yaml"
    machine.write_text("platform: jp6\n")       # the HOST fact, per machine
    root = _deployment("vehicle: t\nvehicle_id: 1\nplatform: jp7\n")
    with _env(RIG_VEHICLE_LOCAL=str(machine)):
        assert load_manifest(root).platform == "jp6"


def test_manifest_platform_empty_is_none_and_marker_is_mandatory():
    with _env(RIG_VEHICLE_LOCAL=_NO_MACHINE):
        assert load_manifest(_deployment("vehicle: t\nvehicle_id: 1\nplatform: ''\n")).platform is None
        m = load_manifest(_deployment('vehicle: t\nvehicle_id: 1\nplatform: "{{platform}}"\n'))
        assert m.platform is None and "platform" in m.missing_identity  # fleet artifact: per-host
        local = "platform: jp7\n"
        m2 = load_manifest(_deployment('vehicle: t\nvehicle_id: 1\nplatform: "{{platform}}"\n',
                                       local=local))
        assert m2.platform == "jp7" and "platform" not in m2.missing_identity


def test_env_map_cannot_shadow_rig_target_platform():
    root = _deployment("vehicle: t\nvehicle_id: 1\nenv: { RIG_TARGET_PLATFORM: sneaky }\n")
    with _env(RIG_VEHICLE_LOCAL=_NO_MACHINE):
        try:
            load_manifest(root)
            raise AssertionError("expected RigError")
        except RigError as exc:
            assert "rig-owned" in str(exc)


# --- routing: fleet_env + service_env -----------------------------------------------------------

def test_fleet_env_exports_and_pops_target_platform():
    with _env(RIG_TARGET_PLATFORM="stale-from-shell"):
        env = fleet_env(_manifest(tag="v1", platform="jp7"))
        assert env["RIG_TARGET_PLATFORM"] == "jp7"
        env = fleet_env(_manifest(tag="v1", platform=None))
        assert "RIG_TARGET_PLATFORM" not in env   # rig owns it: inherited values must not leak


def test_fleet_env_pops_every_rig_owned_var_the_manifest_leaves_unset():
    # The same set-or-pop contract across the whole rig-owned channel: a leaked shell value must
    # never rename compose projects or redirect pulls/outputs; a manifest value always wins.
    leaked = {"VEHICLE_ID": "99", "RIG_IMAGE_REGISTRY": "leak:5000",
              "RIG_IMAGE_TAG": "leak-tag", "RIG_DATA_DIR": "/leak"}
    with _env(**leaked):
        env = fleet_env(_manifest(tag=None, platform=None))   # manifest defines none of them
        assert not any(key in env for key in leaked)
        declared = Manifest(vehicle="t", ros=RosSettings(domain_id=0, rmw="rmw_fastrtps_cpp",
                                                         distro=None),
                            sensors=[], vehicle_id=7, image_registry="fleet:5000",
                            image_tag="v2", data_dir="/data/rec")
        env2 = fleet_env(declared)
        assert env2["VEHICLE_ID"] == "7" and env2["RIG_IMAGE_REGISTRY"] == "fleet:5000"
        assert env2["RIG_IMAGE_TAG"] == "v2" and env2["RIG_DATA_DIR"] == "/data/rec"


def test_service_env_composes_tag_and_mirrors_override():
    base = {"RIG_TARGET_PLATFORM": "jp7", "RIG_IMAGE_TAG": "v1.3.0"}
    matrix = _desc(platforms=["jp7", "jp6"], override="CAM_PLATFORM")
    out = service_env(base, matrix)
    assert out["RIG_IMAGE_TAG"] == "v1.3.0-jp7"     # tag means VERSION; the ref carries the platform
    assert out["CAM_PLATFORM"] == "jp7"             # declared beats the launcher's auto-detect
    assert base["RIG_IMAGE_TAG"] == "v1.3.0"        # caller's env untouched (per-service view)

    no_tag = service_env({"RIG_TARGET_PLATFORM": "jp7"}, matrix)
    assert no_tag["RIG_IMAGE_TAG"] == "jp7"         # no version -> the platform's moving head

    plain = _desc(platforms=[], override=None)
    out2 = service_env(base, plain)
    assert out2["RIG_IMAGE_TAG"] == "v1.3.0"        # platform-independent: tag untouched

    sensitive = _desc(platforms=[], override="DASH_PLATFORM")  # override without a matrix
    assert service_env(base, sensitive)["DASH_PLATFORM"] == "jp7"


def test_service_env_legacy_passthrough_without_platform():
    # images.tag: jp7 with no platform: — exactly the pre-0.2.14 behavior, byte for byte.
    base = {"RIG_IMAGE_TAG": "jp7"}
    out = service_env(base, _desc(platforms=["jp7", "jp6"], override="CAM_PLATFORM"))
    assert out == base and "CAM_PLATFORM" not in out


# --- build: composed tag + env in the build contract --------------------------------------------

def test_build_tag_composition_and_env():
    matrix = _desc(platforms=["jp7", "jp6"], override="CAM_PLATFORM")
    plain = _desc(platforms=[])
    assert _service_tag(matrix, "v1.3.0", "jp7") == "v1.3.0-jp7"
    assert _service_tag(matrix, None, "jp7") == "jp7"
    assert _service_tag(matrix, "jp7", None) == "jp7"       # legacy: tag passes through
    assert _service_tag(plain, "v1.3.0", "jp7") == "v1.3.0"
    env = _build_env("lyrical", matrix, "jp7")
    assert env["ROS_DISTRO"] == "lyrical" and env["RIG_TARGET_PLATFORM"] == "jp7"
    assert env["CAM_PLATFORM"] == "jp7"
    with _env(ROS_DISTRO="from-shell", RIG_BASE_IMAGE="leaked", RIG_BUILD_NO_CACHE="leaked",
              RIG_TARGET_PLATFORM="leaked"):
        env2 = _build_env(None, plain, "jp7")               # nothing to add for a plain service …
        assert "RIG_TARGET_PLATFORM" not in env2 and env2["ROS_DISTRO"] == "from-shell"
        # … but the rig-owned build channel is set-or-POPPED, never inherited from the shell
        assert "RIG_BASE_IMAGE" not in env2 and "RIG_BUILD_NO_CACHE" not in env2
        env3 = _build_env(None, plain, None, base_image="reg:5000/fleet-ros:v1", no_cache=True)
        assert env3["RIG_BASE_IMAGE"] == "reg:5000/fleet-ros:v1"
        assert env3["RIG_BUILD_NO_CACHE"] == "1"
        assert "RIG_TARGET_PLATFORM" not in env3               # popped here too, not inherited


# --- doctor -------------------------------------------------------------------------------------

def _issues(manifest, descriptors):
    return collect(manifest, {}, descriptors)


def test_doctor_platform_not_in_matrix_is_error():
    issues = _issues(_manifest(tag="v1", platform="jp8"),
                     {"cam": _desc(platforms=["jp7", "jp6"])})
    assert any(i.level == ERROR and "build matrix" in i.message for i in issues)


def test_doctor_consistent_platform_is_ok():
    issues = _issues(_manifest(tag="v1", platform="jp7"),
                     {"cam": _desc(platforms=["jp7", "jp6"])})
    assert any(i.level == OK and "RIG_TARGET_PLATFORM" in i.message for i in issues)
    assert not any(i.level == ERROR and "build matrix" in i.message for i in issues)


def test_doctor_legacy_platform_tag_warns_deprecated():
    issues = _issues(_manifest(tag="jp7", platform=None),
                     {"cam": _desc(platforms=["jp7", "jp6"])})
    assert any(i.level == WARN and "DEPRECATED" in i.message for i in issues)


def test_doctor_matrix_without_platform_warns():
    issues = _issues(_manifest(tag="v1.3.0", platform=None),
                     {"cam": _desc(platforms=["jp7", "jp6"], auto="/etc/nv_tegra_release")})
    assert any(i.level == WARN and "auto-detection" in i.message for i in issues)


def test_doctor_version_tag_with_platform_declared_is_clean():
    issues = _issues(_manifest(tag="v1.3.0", platform="jp7"),
                     {"cam": _desc(platforms=["jp7", "jp6"])})
    assert not any("DEPRECATED" in i.message or "platform name" in i.message for i in issues)


def test_doctor_platform_tag_plus_platform_field_warns():
    issues = _issues(_manifest(tag="jp7", platform="jp7"),
                     {"cam": _desc(platforms=["jp7", "jp6"])})
    assert any(i.level == WARN and "jp7-jp7" in i.message for i in issues)


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
