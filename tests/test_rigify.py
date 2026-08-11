"""rig rigify — retrofit rig-compatibility onto an existing software dir. Run: python3 tests/test_rigify.py"""
import contextlib
import io
import pathlib
import shutil
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from rig_cli import RigError  # noqa: E402
from rig_cli.descriptor import load_descriptor  # noqa: E402
from rig_cli.rigify import rigify  # noqa: E402


def _bare() -> pathlib.Path:
    d = pathlib.Path(tempfile.mkdtemp()) / "my_thing"
    d.mkdir()
    (d / "main.py").write_text("print('hi')\n")
    return d


def test_bare_dir_generates_all_four_and_descriptor_loads():
    d = _bare()
    rigify(d)
    assert (d / "rigging.yaml").is_file()
    assert (d / "my_thing-up").is_file() and (d / "my_thing-up").stat().st_mode & 0o111
    assert (d / "config" / "my_thing.example.yaml").is_file()
    assert (d / "docker" / "compose.deploy.yaml").is_file()   # no compose found -> skeleton generated
    desc = load_descriptor("my_thing", d)                     # the generated descriptor is valid
    assert desc.launcher == "my_thing-up" and desc.examples == ["config/my_thing.example.yaml"]
    assert desc.ros_distro is None and desc.tier == "sensor"  # hints stay commented — no guesses
    example = (d / "config" / "my_thing.example.yaml").read_text()
    assert "service: my_thing" in example and "name: my_thing" in example


def test_found_compose_is_prewired_and_no_skeleton():
    d = _bare()
    (d / "deploy").mkdir()
    (d / "deploy" / "docker-compose.yaml").write_text(
        "services:\n"
        "  app:\n    image: acme/thing:1.2\n    build: .\n    ports: ['8080:80', '127.0.0.1:9090:90']\n"
        "volumes:\n  thing_data: {external: true}\n")
    (d / "Dockerfile").write_text("FROM busybox\n")
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rigify(d)
    out = err.getvalue()
    assert not (d / "docker" / "compose.deploy.yaml").exists()      # theirs is wired, not replaced
    launcher = (d / "my_thing-up").read_text()
    assert 'FILES=(-f "$REPO/deploy/docker-compose.yaml")' in launcher
    rigging = (d / "rigging.yaml").read_text()
    assert "deploy/docker-compose.yaml" in rigging                  # in launch_surface
    assert "# mirror: ['acme/thing:1.2']" in rigging                # seeded, commented
    assert "['thing_data']" in rigging                              # external volume hint
    assert "8080" in out and "9090" in out and "Dockerfile" in out  # analysis echoed


def test_refuses_already_rigged():
    d = _bare()
    (d / "rigging.yaml").write_text("service: my_thing\n")
    try:
        rigify(d)
        raise AssertionError("expected RigError")
    except RigError as exc:
        assert "already" in str(exc) and "certify" in str(exc)


def test_never_overwrites_existing_pieces():
    d = _bare()
    (d / "config").mkdir()
    (d / "config" / "my_thing.example.yaml").write_text("service: my_thing\nname: keepme\n")
    rigify(d)
    assert (d / "config" / "my_thing.example.yaml").read_text() == "service: my_thing\nname: keepme\n"
    assert (d / "rigging.yaml").is_file() and (d / "my_thing-up").is_file()  # the rest still generated


def test_service_name_override_and_sanitized_default():
    d = pathlib.Path(tempfile.mkdtemp()) / "My Thing!"
    d.mkdir()
    rigify(d)
    assert (d / "rigging.yaml").read_text().splitlines()[3] == "service: my-thing"  # sanitized default
    d2 = _bare()
    rigify(d2, service="widget")
    assert (d2 / "widget-up").is_file()
    assert "service: widget" in (d2 / "rigging.yaml").read_text()


def test_ros_launch_file_seeds_command_hint():
    d = _bare()
    (d / "launch").mkdir()
    (d / "launch" / "bringup.launch.py").write_text("# ros2 launch\n")
    rigify(d)
    skeleton = (d / "docker" / "compose.deploy.yaml").read_text()
    assert 'launch/bringup.launch.py' in skeleton                   # command hint points at the real file


def test_tier_flag_declares_in_generated_rigging():
    d = _bare()
    rigify(d, tier="autonomy")
    assert load_descriptor("my_thing", d).tier == "autonomy"          # declared, not a commented hint
    d2 = _bare()
    rigify(d2)                                                        # default stays a commented hint
    assert load_descriptor("my_thing", d2).tier == "sensor"
    assert "# tier: sensor" in (d2 / "rigging.yaml").read_text()
    try:
        rigify(_bare(), tier="bogus")
        raise AssertionError("expected RigError")
    except RigError as exc:
        assert "--tier" in str(exc)


def test_generated_skeleton_passes_certify_out_of_the_box():
    # THE acceptance: rigify -> certify green with ZERO hand edits (the contract mechanics come free;
    # certify then teaches only the service-specific 20%). Needs docker compose; skip cleanly without.
    if shutil.which("docker") is None:
        print("     (docker not on PATH — certify smoke skipped)")
        return
    import os

    from rig_cli import certify

    d = _bare()
    rigify(d)
    desc = load_descriptor("my_thing", d)
    env = dict(os.environ)
    env["MY_THING_PYTHON"] = sys.executable                         # launcher's yaml parse -> this venv
    errors, _checks = certify.report(
        "my_thing", certify.certify_target(desc, d / "config" / "my_thing.example.yaml", env))
    assert errors == 0


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
