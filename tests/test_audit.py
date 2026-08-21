"""rig image audit — compose-ref collection, in-container inspection parsing, and the three checks
(distro match, rmw presence, cross-image version agreement). Docker is STUBBED (a fake `docker` on
PATH keyed on the image ref), so these run anywhere. Run: python3 tests/test_audit.py"""
import contextlib
import io
import os
import pathlib
import stat
import sys
import tempfile
import textwrap

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from rig_cli.audit import audit
from rig_cli.catalog import load_catalog
from rig_cli.descriptor import load_descriptor
from rig_cli.dispatch import fleet_env
from rig_cli.manifest import load_manifest

_MARK = "::rig-image-audit::"


def _service(svc: str, image: str) -> pathlib.Path:
    """A rig-compatible stub: its `config` verb emits a one-service compose pulling `image`."""
    d = pathlib.Path(tempfile.mkdtemp())
    (d / "rigging.yaml").write_text(f"service: {svc}\nlauncher: {svc}-up\n")
    (d / f"{svc}-up").write_text("#!/bin/sh\n"
                                 f"printf 'services:\\n  main:\\n    image: {image}\\n'\n")
    (d / f"{svc}-up").chmod(0o755)
    return d


def _fake_docker(cases: dict[str, str]) -> pathlib.Path:
    """A `docker` stub keyed on the image ref appearing in its argv. Values: inspection stdout
    (None-like 'FAIL' -> exit 126 like a shell-less image; 'SLEEP' -> hang past the timeout)."""
    bin_dir = pathlib.Path(tempfile.mkdtemp())
    lines = ["#!/bin/sh", 'case "$*" in']
    for ref, out in cases.items():
        if out == "FAIL":
            lines.append(f'  *"{ref}"*) echo "exec: /bin/sh: not found" >&2; exit 126 ;;')
        elif out == "SLEEP":
            lines.append(f'  *"{ref}"*) sleep 5 ;;')
        else:
            lines.append(f'  *"{ref}"*) printf %b "{out}" ;;')
    lines += ['  *) echo "unexpected: $*" >&2; exit 1 ;;', "esac"]
    docker = bin_dir / "docker"
    docker.write_text("\n".join(lines) + "\n")
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    return bin_dir


def _ros_out(distro: str, pkgs: dict[str, str]) -> str:
    body = f"{distro}\\n{_MARK}\\n" + "".join(f"{p} {v}\\n" for p, v in pkgs.items())
    return body


@contextlib.contextmanager
def _stubbed(cases: dict[str, str]):
    bin_dir = _fake_docker(cases)
    old = os.environ["PATH"]
    os.environ["PATH"] = f"{bin_dir}:{old}"
    try:
        yield
    finally:
        os.environ["PATH"] = old


def _run_audit(images: dict[str, str], cases: dict[str, str],
               ros="ros: {distro: lyrical, rmw: rmw_zenoh_cpp}", repos=None) -> tuple[int, str]:
    """Deployment with one stack per (service -> image ref); returns (rc, stderr text).
    `repos` overrides the generated launcher stubs (misbehaving `config` verbs)."""
    root = pathlib.Path(tempfile.mkdtemp())
    (root / "config").mkdir()
    repos = repos or {svc: _service(svc, ref) for svc, ref in images.items()}
    rows = "\n".join(f"  - {{name: {svc}_0, service: {svc}, config: config/{svc}.yaml}}"
                     for svc in images)
    (root / "vehicle.yaml").write_text(f"vehicle: t\nvehicle_id: 1\n{ros}\nsensors:\n{rows}\n")
    (root / "services.yaml").write_text(
        "services:\n" + "\n".join(f"  {svc}: {{path: {p}}}" for svc, p in repos.items()))
    for svc in images:
        (root / "config" / f"{svc}.yaml").write_text(f"service: {svc}\nname: {svc}_0\n")
    manifest = load_manifest(root)
    load_catalog(root)
    descriptors = {svc: load_descriptor(svc, p) for svc, p in repos.items()}
    err = io.StringIO()
    with _stubbed(cases):
        env = fleet_env(manifest, descriptors)
        with contextlib.redirect_stderr(err):
            rc = audit(manifest, descriptors, env)
    return rc, err.getvalue()


def test_version_skew_across_two_images_is_an_error():
    # THE motivating failure: one deployment, two images, two rmw_zenoh_cpp builds -> no sessions.
    rc, out = _run_audit(
        {"cam": "reg:5000/cam-core:v1", "logger": "reg:5000/fleet-ros:v1"},
        {"cam-core": _ros_out("lyrical", {"ros-lyrical-rmw-zenoh-cpp": "0.2.1",
                                          "ros-lyrical-rclcpp": "1.1.0"}),
         "fleet-ros": _ros_out("lyrical", {"ros-lyrical-rmw-zenoh-cpp": "0.3.2",
                                           "ros-lyrical-rclcpp": "1.1.0"})})
    assert rc == 1
    assert "version skew: ros-lyrical-rmw-zenoh-cpp" in out
    assert "0.2.1" in out and "0.3.2" in out
    assert "version skew: ros-lyrical-rclcpp" not in out  # agreeing shared packages don't fire


def test_distro_mismatch_and_missing_rmw_are_errors():
    rc, out = _run_audit(
        {"cam": "cam-core:v1"},
        {"cam-core": _ros_out("humble", {"ros-humble-rclcpp": "1.0.0"})})
    assert rc == 1 and "ros.distro is 'lyrical'" in out
    rc, out = _run_audit(
        {"cam": "cam-core:v1"},
        {"cam-core": _ros_out("lyrical", {"ros-lyrical-rclcpp": "1.0.0"})})
    assert rc == 1 and "rmw_zenoh_cpp" in out and "not installed" in out


def test_consistent_deployment_is_green():
    pkgs = {"ros-lyrical-rmw-zenoh-cpp": "0.3.2", "ros-lyrical-rclcpp": "1.1.0"}
    rc, out = _run_audit(
        {"cam": "cam-core:v1", "logger": "fleet-ros:v1"},
        {"cam-core": _ros_out("lyrical", pkgs), "fleet-ros": _ros_out("lyrical", pkgs)})
    assert rc == 0
    assert "versions agree" in out and "/opt/ros/lyrical" in out and "0 error(s)" in out


def test_non_ros_excluded_and_uninspectable_skipped_without_failing():
    pkgs = {"ros-lyrical-rmw-zenoh-cpp": "0.3.2"}
    rc, out = _run_audit(
        {"cam": "cam-core:v1", "zen": "eclipse/zenoh:1.0.0", "weird": "distroless-thing:1"},
        {"cam-core": _ros_out("lyrical", pkgs),
         "eclipse/zenoh": f"{_MARK}\\n",       # no /opt/ros, no ros-* packages -> non-ROS
         "distroless-thing": "FAIL"})          # no shell -> uninspectable, reported, not fatal
    assert rc == 0
    assert "non-ROS" in out and "uninspectable" in out


def _scripted_service(svc: str, script: str) -> pathlib.Path:
    """A launcher whose `config` verb misbehaves in a controlled way (exit 1, imageless compose)."""
    d = pathlib.Path(tempfile.mkdtemp())
    (d / "rigging.yaml").write_text(f"service: {svc}\nlauncher: {svc}-up\n")
    (d / f"{svc}-up").write_text("#!/bin/sh\n" + script)
    (d / f"{svc}-up").chmod(0o755)
    return d


def test_inspect_timeout_is_a_warn_not_fatal():
    # a hung daemon/cold pull must degrade to `uninspectable` (WARN), never hang audit or flip
    # the exit — pin the timeout down to 1s so the SLEEP stub trips it fast
    from rig_cli import audit as audit_mod
    old = audit_mod._DOCKER_TIMEOUT
    audit_mod._DOCKER_TIMEOUT = 1
    try:
        rc, out = _run_audit({"cam": "cam-core:v1"}, {"cam-core": "SLEEP"})
    finally:
        audit_mod._DOCKER_TIMEOUT = old
    assert rc == 0
    assert "timed out" in out and "uninspectable" in out


def test_config_failure_is_a_per_stack_warn_not_fatal():
    # a stack whose launcher can't render is REPORTED per stack, but a config failure alone must
    # not flip the exit — audit's errors are about image CONTENT; doctor owns launcher health
    rc, out = _run_audit({"cam": "unused"}, {},
                         repos={"cam": _scripted_service("cam", "exit 1\n")})
    assert rc == 0 and "produced no compose" in out


def test_docker_missing_from_path_is_rc_1():
    # audit inspects with `docker run` — without the CLI it must say so and fail. PATH is emptied
    # around the audit call ONLY (fixture building above still needs the real PATH).
    root = pathlib.Path(tempfile.mkdtemp())
    (root / "config").mkdir()
    repo = _service("cam", "cam-core:v1")
    (root / "vehicle.yaml").write_text("vehicle: t\nvehicle_id: 1\n"
                                      "ros: {distro: lyrical, rmw: rmw_zenoh_cpp}\nsensors:\n"
                                      "  - {name: cam_0, service: cam, config: config/cam.yaml}\n")
    (root / "services.yaml").write_text(f"services:\n  cam: {{path: {repo}}}\n")
    (root / "config" / "cam.yaml").write_text("service: cam\nname: cam_0\n")
    manifest = load_manifest(root)
    descriptors = {"cam": load_descriptor("cam", repo)}
    env = fleet_env(manifest, descriptors)
    err = io.StringIO()
    old = os.environ["PATH"]
    os.environ["PATH"] = tempfile.mkdtemp()  # a dir with nothing in it
    try:
        with contextlib.redirect_stderr(err):
            rc = audit(manifest, descriptors, env)
    finally:
        os.environ["PATH"] = old
    assert rc == 1 and "docker not found" in err.getvalue()


def test_no_resolved_images_is_nothing_to_audit():
    # enabled stacks rendering composes WITHOUT image: keys leave nothing to inspect — a clean
    # rc 0, not an error and not a per-stack failure
    launcher = "printf 'services:\\n  main:\\n    command: sleep\\n'\n"
    rc, out = _run_audit({"cam": "unused"}, {},
                         repos={"cam": _scripted_service("cam", launcher)})
    assert rc == 0 and "nothing to audit" in out


def test_source_built_ros_image_warns_thin_packages():
    rc, out = _run_audit(
        {"cam": "cam-core:v1"},
        {"cam-core": f"lyrical\\n{_MARK}\\n"})  # /opt/ros/lyrical but zero ros-* dpkg packages
    assert rc == 0 and "source-built" in out


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
