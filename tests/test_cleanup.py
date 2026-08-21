"""cleanup — the decommission sweep (v0.2.15): image-ref collection (rendered composes + rig.lock,
composed platform tags included), the containers guard, volume targets, dry-run inertness, and the
baked cleanup.sh parity script. Run: python3 tests/test_cleanup.py"""
import contextlib
import io
import os
import pathlib
import subprocess
import sys
import tempfile
import textwrap

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from rig_cli import RigError  # noqa: E402
from rig_cli.cleanup import (cleanup, declared_volumes, lock_images,  # noqa: E402
                             rendered_images)
from rig_cli.descriptor import load_descriptor  # noqa: E402
from rig_cli.manifest import Manifest, RosSettings, Sensor  # noqa: E402

RIGGING = """\
service: fak
launcher: fak-up
build: { command: build.sh, images: [fak-core], platforms: [alpha, beta] }
platform: { override_env: FAK_PLATFORM }
external_volumes: ["fak_{name}_sock"]
"""

LAUNCHER = """\
#!/bin/sh
CONFIG="$1"; shift
case "$1" in
  config)
    cat <<EOF
name: whatever
services:
  core:
    image: reg:5000/fak-core:$RIG_IMAGE_TAG
  helper:
    image: docker.io/library/busybox:stable
EOF
    ;;
esac
"""


def _service() -> pathlib.Path:
    repo = pathlib.Path(tempfile.mkdtemp(prefix="cleanup-svc-"))
    (repo / "rigging.yaml").write_text(RIGGING)
    (repo / "fak-up").write_text(LAUNCHER)
    (repo / "fak-up").chmod(0o755)
    return repo


def _manifest(sensors) -> Manifest:
    return Manifest(vehicle="t", ros=RosSettings(domain_id=0, rmw="rmw_fastrtps_cpp", distro=None),
                    sensors=sensors, vehicle_id=1, image_tag="v1", platform="alpha")


def _fixture():
    repo = _service()
    desc = load_descriptor("fak", repo)
    cfg = repo / "instance.yaml"
    cfg.write_text("service: fak\n")
    sensors = [Sensor(name="cam_a", service="fak", config=cfg, enabled=True, order=10),
               Sensor(name="cam_b", service="fak", config=cfg, enabled=False, order=20)]
    return _manifest(sensors), {"fak": desc}


@contextlib.contextmanager
def _fake_docker(ps_output: str = "", fail_rm: bool = False):
    """A PATH-shimmed `docker` that logs every invocation and answers `ps`/`volume ls` with
    canned output — the sweep is exercised for real, the daemon is not. The residue volume is
    emitted ONLY when the compose-project label filter is on the `volume ls` command line:
    that filter is the label-scoping that keeps cleanup from deleting every volume on the
    host, so a shim answering an UNFILTERED ls would leave it untested. ``fail_rm`` makes
    `rmi`/`volume rm` refuse like the daemon's in-use errors (canned stderr, exit 1) to
    drive the kept/in-use reporting branches."""
    bin_dir = pathlib.Path(tempfile.mkdtemp(prefix="cleanup-bin-"))
    log = bin_dir / "calls.log"
    fail_branches = (
        '  "rmi "*) echo "Error response from daemon: conflict: image is being used'
        ' by a container" >&2; exit 1 ;;\n'
        '  "volume rm") echo "Error response from daemon: remove $3: volume is in use'
        ' - [cafe]" >&2; exit 1 ;;\n'
    ) if fail_rm else ""
    (bin_dir / "docker").write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> {log}\n'
        'case "$1 $2" in\n'
        f"""  "ps -aq") printf '%s' {repr(ps_output) if ps_output else "''"} ;;\n"""
        '  "volume ls")\n'
        '    case "$*" in\n'
        '      *"--filter label=com.docker.compose.project="*) printf \'residue_vol\\n\' ;;\n'
        "    esac ;;\n"
        f"{fail_branches}"
        "esac\n"
        "exit 0\n")
    (bin_dir / "docker").chmod(0o755)
    old = os.environ["PATH"]
    os.environ["PATH"] = f"{bin_dir}:{old}"
    try:
        yield log
    finally:
        os.environ["PATH"] = old


def test_rendered_images_cover_disabled_and_compose_platform_tags():
    manifest, descriptors = _fixture()
    env = {"RIG_IMAGE_TAG": "v1", "RIG_TARGET_PLATFORM": "alpha"}
    sensors = manifest.select([], enabled_only=False)
    assert len(sensors) == 2                                   # decommission sweeps disabled rows too
    refs, notes = rendered_images(sensors, descriptors, env)
    assert refs == {"reg:5000/fak-core:v1-alpha", "docker.io/library/busybox:stable"}
    assert not notes


def test_render_failure_is_a_note_not_an_abort():
    manifest, descriptors = _fixture()
    (descriptors["fak"].repo / "fak-up").write_text("#!/bin/sh\nexit 3\n")
    refs, notes = rendered_images(manifest.select([], enabled_only=False), descriptors, {})
    assert refs == set() and len(notes) == 2 and "rig.lock" in notes[0]


def test_lock_images_include_tags_and_digest_pins():
    root = pathlib.Path(tempfile.mkdtemp())
    (root / "rig.lock").write_text(textwrap.dedent("""\
        schema: 1
        images:
          reg:5000/fak-core:v1-alpha: reg:5000/fak-core@sha256:abc
          reg:5000/other:v1: null
        """))
    assert lock_images(root) == {"reg:5000/fak-core:v1-alpha",
                                 "reg:5000/fak-core@sha256:abc", "reg:5000/other:v1"}
    assert lock_images(pathlib.Path(tempfile.mkdtemp())) == set()   # no lock -> empty


def test_declared_volumes_format_instance_names():
    manifest, descriptors = _fixture()
    vols = declared_volumes(manifest.select([], enabled_only=False), descriptors)
    assert vols == {"fak_cam_a_sock", "fak_cam_b_sock"}


def test_dry_run_touches_nothing_and_prints_the_plan():
    manifest, descriptors = _fixture()
    root = pathlib.Path(tempfile.mkdtemp())
    err = io.StringIO()
    with _fake_docker() as log:
        with contextlib.redirect_stderr(err):
            rc = cleanup(root, manifest, descriptors, {"RIG_IMAGE_TAG": "v1"},
                         names=[], dry_run=True)
        assert rc == 0
        assert not log.exists(), "dry-run must not invoke docker at all"
    plan = err.getvalue()  # the plan must actually PRINT — images, declared volumes, and residue
    assert "would rmi reg:5000/fak-core:v1" in plan
    assert "would rmi docker.io/library/busybox:stable" in plan
    assert "would rm volume fak_cam_a_sock" in plan
    assert "would rm volumes labeled com.docker.compose.project=cam_a-vehicle-1" in plan


def test_guard_refuses_while_containers_exist():
    manifest, descriptors = _fixture()
    root = pathlib.Path(tempfile.mkdtemp())
    with _fake_docker(ps_output="c0ffee\\n"):
        try:
            cleanup(root, manifest, descriptors, {}, names=[])
            raise AssertionError("expected RigError")
        except RigError as exc:
            assert "rig down" in str(exc) and "cam_a-vehicle-1" in str(exc)


def test_sweep_removes_images_declared_and_labeled_volumes():
    manifest, descriptors = _fixture()
    root = pathlib.Path(tempfile.mkdtemp())
    (root / "rig.lock").write_text("schema: 1\nimages: {'reg:5000/fak-core:v1-alpha': "
                                   "'reg:5000/fak-core@sha256:abc'}\n")
    with _fake_docker() as log:
        rc = cleanup(root, manifest, descriptors,
                     {"RIG_IMAGE_TAG": "v1", "RIG_TARGET_PLATFORM": "alpha"}, names=[])
        assert rc == 0
        calls = log.read_text()
    assert "rmi reg:5000/fak-core:v1-alpha" in calls          # rendered (composed) ref
    assert "rmi reg:5000/fak-core@sha256:abc" in calls        # lock digest pin untagged too
    assert "rmi docker.io/library/busybox:stable" in calls    # upstream refs are the deployment's pulls
    assert "volume rm fak_cam_a_sock" in calls and "volume rm fak_cam_b_sock" in calls
    assert "volume rm residue_vol" in calls                   # project-labeled residue swept
    assert "label=com.docker.compose.project=cam_a-vehicle-1" in calls
    # The residue listing must be label-SCOPED (an unfiltered `volume ls` would sweep every
    # volume on the host) — assert the exact filtered invocation, per project:
    assert "volume ls -q --filter label=com.docker.compose.project=cam_a-vehicle-1" in calls
    assert "volume ls -q --filter label=com.docker.compose.project=cam_b-vehicle-1" in calls


def test_keep_volumes_removes_images_only():
    manifest, descriptors = _fixture()
    root = pathlib.Path(tempfile.mkdtemp())
    with _fake_docker() as log:
        cleanup(root, manifest, descriptors, {"RIG_IMAGE_TAG": "v1"}, names=[],
                keep_volumes=True)
        calls = log.read_text()
    assert "rmi " in calls and "volume rm" not in calls and "volume ls" not in calls


def test_bake_emits_cleanup_script():
    from rig_cli.bake import _write_bootstrap, _write_scripts

    staging = pathlib.Path(tempfile.mkdtemp())
    entries = [{"sensor": "cam", "project": "cam-vehicle-1",
                "compose": "compose/cam/docker-compose.yaml",
                "external_volumes": ["fak_cam_sock"]}]
    _write_scripts(staging, entries,
                   cleanup_refs=["reg:5000/fak-core:v1-alpha", "reg:5000/fak-core@sha256:abc"])
    _write_bootstrap(staging)
    body = (staging / "cleanup.sh").read_text()
    assert (staging / "cleanup.sh").stat().st_mode & 0o111
    proc = subprocess.run(["sh", "-n", str(staging / "cleanup.sh")], capture_output=True, text=True)
    assert proc.returncode == 0, f"cleanup.sh has a sh syntax error: {proc.stderr}"
    assert "docker rmi reg:5000/fak-core:v1-alpha" in body
    assert "docker rmi reg:5000/fak-core@sha256:abc" in body
    assert "fak_cam_sock" in body
    assert "com.docker.compose.project=$p" in body
    assert body.index("ps -aq") < body.index("rmi")           # guard runs BEFORE any removal
    assert "cleanup)" in (staging / "run.sh").read_text()      # run.sh routes the verb


def test_in_use_refusals_are_reported_kept_with_counts():
    # The daemon refusing `rmi`/`volume rm` (image/volume still in use by ANOTHER deployment)
    # is cleanup's safety net — it must surface as per-item "kept" lines plus accurate summary
    # counts, and the sweep still exits 0 (partial is expected, not an error).
    manifest, descriptors = _fixture()
    root = pathlib.Path(tempfile.mkdtemp())
    err = io.StringIO()
    with _fake_docker(fail_rm=True):
        with contextlib.redirect_stderr(err):
            rc = cleanup(root, manifest, descriptors, {"RIG_IMAGE_TAG": "v1"}, names=[])
    assert rc == 0
    out = err.getvalue()
    # per-item reporting: the image line carries the daemon's reason, the volume line the fixed hint
    assert "kept image reg:5000/fak-core:v1 (Error response from daemon: conflict" in out
    assert "kept image docker.io/library/busybox:stable (" in out
    assert "kept volume fak_cam_a_sock (in use — a consumer may still be attached)" in out
    assert "kept volume residue_vol (in use" in out            # labeled residue reported too
    assert "removed image" not in out and "removed volume" not in out
    # summary: 2 rendered images kept, 2 declared volumes + 1 residue kept, nothing removed
    assert "0 image(s) removed, 2 kept (in use), 0 volume(s) removed, 3 kept" in out


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
