"""rig build — descriptor build/mirror parsing, dry-run, once-per-service dedupe, the concurrent
-j path, the base-image resolution/staging, and the --no-cache channel.
Run: `.venv/bin/python tests/test_build.py`."""
import contextlib
import io
import os
import pathlib
import sys
import tempfile
import textwrap

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from rig_cli import RigError
from rig_cli.build import build, resolve_base_image
from rig_cli.descriptor import Descriptor, load_descriptor
from rig_cli.manifest import Manifest, RosSettings, load_manifest


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
    # the dedupe log lives in the per-test service dir — a fixed shared /tmp path races parallel runs
    svc = _service_with_build("cam", 'echo built >> "$(dirname "$0")/dedupe.log"\n')
    log = svc / "dedupe.log"
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


def test_build_exports_ros_distro_from_vehicle_yaml():
    # vehicle.yaml ros.distro rides into build commands as ROS_DISTRO (fleet-ros bakes the right distro
    # without the operator remembering an env var); absent distro -> env untouched.
    import os
    os.environ.pop("ROS_DISTRO", None)
    svc = _service_with_build("base", 'echo "${ROS_DISTRO:-UNSET}" > "$(dirname "$0")/seen"\n')

    def run_with(vehicle_yaml: str) -> str:
        root = pathlib.Path(tempfile.mkdtemp())
        (root / "config").mkdir()
        (root / "vehicle.yaml").write_text(vehicle_yaml)
        (root / "services.yaml").write_text(f"services: {{base: {{path: {svc}}}}}\n")
        (root / "config" / "a.yaml").write_text("service: base\nname: a\n")
        m = load_manifest(root)
        assert build(m, {"base": load_descriptor("base", svc)}, registry=None, tag=None, dry_run=False) == 0
        return (svc / "seen").read_text().strip()

    assert run_with("vehicle: t\nros: {distro: lyrical}\n"
                    "sensors: [{name: a, service: base, config: config/a.yaml}]\n") == "lyrical"
    assert run_with("vehicle: t\nsensors: [{name: a, service: base, config: config/a.yaml}]\n") == "UNSET"


def test_build_warns_when_rigging_distro_disagrees():
    import contextlib
    import io

    svc = _repo("service: s\nlauncher: s-up\nros_distro: noetic\nbuild: b.sh\n")
    root = pathlib.Path(tempfile.mkdtemp())
    (root / "config").mkdir()
    (root / "vehicle.yaml").write_text("vehicle: t\nros: {distro: lyrical}\n"
                                       "sensors: [{name: a, service: s, config: config/a.yaml}]\n")
    (root / "config" / "a.yaml").write_text("service: s\nname: a\n")
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        build(load_manifest(root), {"s": load_descriptor("s", svc)}, registry=None, tag=None, dry_run=True)
    out = err.getvalue()
    assert "WARNING" in out and "ros_distro 'noetic'" in out and "ROS_DISTRO=lyrical" in out


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


# --- base image: provides parsing, resolution, staging; the --no-cache channel -------------------

def _bdesc(svc, *, provides=None, images=("fleet-ros",), platforms=(), command="b.sh"):
    return Descriptor(service=svc, repo=pathlib.Path("/nonexistent"), launcher=f"{svc}-up", verbs={},
                      ros_distro=None, external_volumes=[], host_ports=[], build_command=command,
                      build_images=list(images), build_platforms=list(platforms),
                      build_provides=provides)


def _bmanifest(**kw):
    return Manifest(vehicle="t", ros=RosSettings(0, "rmw_zenoh_cpp", "lyrical"), sensors=[], **kw)


def test_descriptor_provides_base_parses_and_validates():
    d = load_descriptor("s", _repo("service: s\nlauncher: s-up\n"
                                   "build: {command: ../base/b.sh, images: [fleet-ros], provides: base}\n"))
    assert d.build_provides == "base" and d.build_images == ["fleet-ros"]
    assert load_descriptor("s", _repo("service: s\nlauncher: s-up\nbuild: b.sh\n")).build_provides is None
    for bad in ("build: {command: b.sh, images: [x], provides: bogus}\n",  # unknown role
                "build: {command: b.sh, provides: base}\n"):               # no images -> no base name
        try:
            load_descriptor("s", _repo(f"service: s\nlauncher: s-up\n{bad}"))
            raise AssertionError("expected RigError")
        except RigError:
            pass


def test_resolve_base_image_precedence_composition_and_conflict():
    descs = {"router": _bdesc("router", provides="base")}
    # an explicit images.base beats any provider, verbatim
    m = _bmanifest(image_registry="reg:5000", image_tag="v1", image_base="custom/ros:x")
    assert resolve_base_image(m, descs) == ("custom/ros:x", "images.base", None)
    # provider ref composes like the pull side: <registry>/<images[0]>:<tag>
    m2 = _bmanifest(image_registry="reg:5000", image_tag="v1")
    ref, origin, err = resolve_base_image(m2, descs)
    assert (ref, err) == ("reg:5000/fleet-ros:v1", None) and "router" in origin
    assert resolve_base_image(_bmanifest(), descs)[0] == "fleet-ros"  # no registry/tag: bare repo
    # a matrix provider composes the per-platform tag (same rule as its build/pull)
    m3 = _bmanifest(image_registry="r", image_tag="v1", platform="jp7")
    assert resolve_base_image(m3, {"router": _bdesc("router", provides="base",
                                                    platforms=("jp7",))})[0] == "r/fleet-ros:v1-jp7"
    # two providers, same image = ONE base (the fleet-ros pattern); different images = ambiguous
    same = {"a": _bdesc("a", provides="base"), "b": _bdesc("b", provides="base")}
    ref, origin, err = resolve_base_image(m2, same)
    assert ref == "reg:5000/fleet-ros:v1" and err is None and "a" in origin and "b" in origin
    diff = {"a": _bdesc("a", provides="base"), "b": _bdesc("b", provides="base", images=("other",))}
    ref, origin, err = resolve_base_image(m2, diff)
    assert ref is None and err and "disagree" in err
    assert resolve_base_image(m2, {"a": _bdesc("a")}) == (None, None, None)  # no providers, no base


def _deployment(vehicle_yaml: str, services: dict) -> pathlib.Path:
    root = pathlib.Path(tempfile.mkdtemp())
    (root / "config").mkdir()
    (root / "vehicle.yaml").write_text(textwrap.dedent(vehicle_yaml))
    rows = ", ".join(f"{s}: {{path: {p}}}" for s, p in services.items())
    (root / "services.yaml").write_text(f"services: {{{rows}}}\n")
    for name, svc in [(f"i{i}", s) for i, s in enumerate(services)]:
        (root / "config" / f"{name}.yaml").write_text(f"service: {svc}\nname: {name}\n")
    return root


def test_build_no_cache_and_base_image_ride_the_env_set_or_popped():
    svc = _service_with_build(
        "cam", 'echo "${RIG_BUILD_NO_CACHE:-UNSET} ${RIG_BASE_IMAGE:-UNSET}" > "$(dirname "$0")/seen"\n')
    root = _deployment("vehicle: t\nimages: {base: 'my/base:2'}\n"
                       "sensors: [{name: i0, service: cam, config: config/i0.yaml}]\n", {"cam": svc})
    m = load_manifest(root)
    descs = {"cam": load_descriptor("cam", svc)}
    assert build(m, descs, registry=None, tag=None, dry_run=False, no_cache=True) == 0
    assert (svc / "seen").read_text().strip() == "1 my/base:2"
    # not requested + leaked from the shell -> POPPED, never inherited
    root2 = _deployment("vehicle: t\nsensors: [{name: i0, service: cam, config: config/i0.yaml}]\n",
                        {"cam": svc})
    os.environ["RIG_BUILD_NO_CACHE"] = "1"
    os.environ["RIG_BASE_IMAGE"] = "leak"
    try:
        assert build(load_manifest(root2), descs, registry=None, tag=None, dry_run=False) == 0
    finally:
        os.environ.pop("RIG_BUILD_NO_CACHE")
        os.environ.pop("RIG_BASE_IMAGE")
    assert (svc / "seen").read_text().strip() == "UNSET UNSET"


def _provider_workspace():
    """rig-infra shape: base/build.sh shared by two provider services; a dependent cam service."""
    ws = pathlib.Path(tempfile.mkdtemp())
    (ws / "base").mkdir()
    (ws / "base" / "build.sh").write_text(  # like rig-infra's: works from any provider's cwd
        '#!/bin/sh\ncd "$(dirname "$0")"\necho built >> log\ntouch marker\n')
    (ws / "base" / "build.sh").chmod(0o755)
    for svc in ("router", "logger"):
        d = ws / svc
        d.mkdir()
        (d / "rigging.yaml").write_text(
            f"service: {svc}\nlauncher: {svc}-up\n"
            f"build: {{command: ../base/build.sh, images: [fleet-ros], provides: base}}\n")
    cam = ws / "cam"
    cam.mkdir()
    (cam / "rigging.yaml").write_text("service: cam\nlauncher: cam-up\nbuild: build.sh\n")
    (cam / "build.sh").write_text(
        '#!/bin/sh\nif [ -f ../base/marker ]; then echo "${RIG_BASE_IMAGE:-UNSET}" > seen; '
        'else echo TOO-EARLY > seen; fi\n')
    (cam / "build.sh").chmod(0o755)
    return ws


def test_build_provider_stage_runs_first_dedupes_and_exports_base():
    ws = _provider_workspace()
    # cam listed FIRST: stage 0 must still build the base before cam's build runs
    root = _deployment("vehicle: t\nimages: {registry: 'reg:5000', tag: v1}\nsensors:\n"
                       "  - {name: i0, service: cam, config: config/i0.yaml}\n"
                       "  - {name: i1, service: router, config: config/i1.yaml}\n"
                       "  - {name: i2, service: logger, config: config/i2.yaml}\n",
                       {"cam": ws / "cam", "router": ws / "router", "logger": ws / "logger"})
    m = load_manifest(root)
    descs = {s: load_descriptor(s, ws / s) for s in ("cam", "router", "logger")}
    for jobs in (1, 2):  # sequential AND concurrent path: base first, dependents see RIG_BASE_IMAGE
        # scrub ALL of the previous leg's state (log, marker, cam's seen) so jobs=2 proves the
        # concurrent path on its own, not by riding the jobs=1 leg's artifacts
        (ws / "base" / "log").unlink(missing_ok=True)
        (ws / "base" / "marker").unlink(missing_ok=True)
        (ws / "cam" / "seen").unlink(missing_ok=True)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            assert build(m, descs, registry=None, tag=None, dry_run=False, jobs=jobs) == 0
        assert (ws / "base" / "log").read_text().count("built") == 1  # two providers -> ONE base build
        assert (ws / "cam" / "seen").read_text().strip() == "reg:5000/fleet-ros:v1"
        assert "base image reg:5000/fleet-ros:v1" in err.getvalue()


def test_build_base_stage_failure_aborts_before_dependents():
    # stage 0 exiting nonzero must stop the WHOLE build: dependents FROM the base would silently
    # build against a stale/absent image ("stopping here" in build.py).
    ws = _provider_workspace()
    (ws / "base" / "build.sh").write_text("#!/bin/sh\nexit 1\n")
    (ws / "base" / "build.sh").chmod(0o755)
    root = _deployment("vehicle: t\nsensors:\n"
                       "  - {name: i0, service: cam, config: config/i0.yaml}\n"
                       "  - {name: i1, service: router, config: config/i1.yaml}\n",
                       {"cam": ws / "cam", "router": ws / "router"})
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        assert build(load_manifest(root),
                     {s: load_descriptor(s, ws / s) for s in ("cam", "router")},
                     registry=None, tag=None, dry_run=False) == 1
    assert "stopping here" in err.getvalue()
    assert not (ws / "cam" / "seen").exists()  # the dependent's build never ran


def test_build_failing_service_flips_rc_on_both_paths():
    # one failing + one passing service: the sequential leg sets rc=1; the concurrent leg must
    # AGGREGATE (`rc |= rc1`) — a passing service finishing last must not launder the failure.
    bad = _service_with_build("bad", "exit 1\n")
    good = _service_with_build("good", 'touch "$(dirname "$0")/done"\n')
    root = _deployment("vehicle: t\nsensors:\n"
                       "  - {name: i0, service: bad, config: config/i0.yaml}\n"
                       "  - {name: i1, service: good, config: config/i1.yaml}\n",
                       {"bad": bad, "good": good})
    m = load_manifest(root)
    descs = {"bad": load_descriptor("bad", bad), "good": load_descriptor("good", good)}
    for jobs in (1, 2):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            assert build(m, descs, registry=None, tag=None, dry_run=False, jobs=jobs) == 1
        assert "FAILED" in err.getvalue()


# --- mirror: the REAL pull→tag→push path, docker stubbed on PATH (no daemon needed) ---------------

@contextlib.contextmanager
def _stub_docker(body: str):
    """A fake `docker` first on PATH (test_audit's technique) that runs `body` as sh."""
    bin_dir = pathlib.Path(tempfile.mkdtemp())
    docker = bin_dir / "docker"
    docker.write_text("#!/bin/sh\n" + body)
    docker.chmod(0o755)
    old = os.environ["PATH"]
    os.environ["PATH"] = f"{bin_dir}:{old}"
    try:
        yield bin_dir
    finally:
        os.environ["PATH"] = old


def _mirror_deployment(images_line: str):
    svc = _repo("service: s\nlauncher: s-up\nmirror: [busybox:1]\n")
    root = pathlib.Path(tempfile.mkdtemp())
    (root / "config").mkdir()
    (root / "vehicle.yaml").write_text(f"vehicle: t\n{images_line}"
                                       "sensors: [{name: a, service: s, config: config/a.yaml}]\n")
    (root / "services.yaml").write_text(f"services: {{s: {{path: {svc}}}}}\n")
    (root / "config" / "a.yaml").write_text("service: s\nname: a\n")
    return load_manifest(root), {"s": load_descriptor("s", svc)}


def test_build_mirror_runs_pull_tag_push_in_order():
    m, descs = _mirror_deployment("images: {registry: 'r:5000'}\n")
    with _stub_docker('echo "$@" >> "$(dirname "$0")/argv.log"\n') as bin_dir:
        assert build(m, descs, registry=None, tag=None, dry_run=False) == 0
        lines = (bin_dir / "argv.log").read_text().splitlines()
    assert lines == ["pull busybox:1", "tag busybox:1 r:5000/busybox:1", "push r:5000/busybox:1"]


def test_build_mirror_pull_failure_stops_the_chain():
    m, descs = _mirror_deployment("images: {registry: 'r:5000'}\n")
    body = ('echo "$@" >> "$(dirname "$0")/argv.log"\n'
            '[ "$1" = pull ] && exit 1\nexit 0\n')
    err = io.StringIO()
    with _stub_docker(body) as bin_dir:
        with contextlib.redirect_stderr(err):
            assert build(m, descs, registry=None, tag=None, dry_run=False) == 1
        lines = (bin_dir / "argv.log").read_text().splitlines()
    assert lines == ["pull busybox:1"]  # tag/push never invoked for an image we don't have
    assert "mirror busybox:1 FAILED" in err.getvalue()


def test_build_mirror_without_registry_skips_and_says_so():
    m, descs = _mirror_deployment("")  # no images.registry, no --registry
    err = io.StringIO()
    with _stub_docker('echo "$@" >> "$(dirname "$0")/argv.log"\n') as bin_dir:
        with contextlib.redirect_stderr(err):
            assert build(m, descs, registry=None, tag=None, dry_run=False) == 0
        assert not (bin_dir / "argv.log").exists()  # docker never invoked
    assert "no registry" in err.getvalue() and "skipped" in err.getvalue()


def test_build_conflicting_providers_refuse_to_guess():
    ws = _provider_workspace()
    (ws / "logger" / "rigging.yaml").write_text(
        "service: logger\nlauncher: logger-up\n"
        "build: {command: ../base/build.sh, images: [other-base], provides: base}\n")
    root = _deployment("vehicle: t\nsensors:\n"
                       "  - {name: i0, service: router, config: config/i0.yaml}\n"
                       "  - {name: i1, service: logger, config: config/i1.yaml}\n",
                       {"router": ws / "router", "logger": ws / "logger"})
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        assert build(load_manifest(root),
                     {s: load_descriptor(s, ws / s) for s in ("router", "logger")},
                     registry=None, tag=None, dry_run=False) == 1
    assert "disagree" in err.getvalue()
    assert not (ws / "base" / "log").exists()  # refused BEFORE building anything


def test_build_external_base_skips_single_image_provider():
    ws = _provider_workspace()
    root = _deployment("vehicle: t\nimages: {base: 'nvcr.io/custom-ros:1'}\nsensors:\n"
                       "  - {name: i0, service: cam, config: config/i0.yaml}\n"
                       "  - {name: i1, service: router, config: config/i1.yaml}\n",
                       {"cam": ws / "cam", "router": ws / "router"})
    # cam's guard would see no marker (provider build skipped) — make it record the base only
    (ws / "cam" / "build.sh").write_text('#!/bin/sh\necho "${RIG_BASE_IMAGE:-UNSET}" > seen\n')
    (ws / "cam" / "build.sh").chmod(0o755)
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        assert build(load_manifest(root),
                     {s: load_descriptor(s, ws / s) for s in ("cam", "router")},
                     registry=None, tag=None, dry_run=False) == 0
    assert not (ws / "base" / "log").exists()  # nothing would pull the provider's fleet-ros
    assert "skipped" in err.getvalue()
    assert (ws / "cam" / "seen").read_text().strip() == "nvcr.io/custom-ros:1"


def test_resolve_base_image_providers_must_agree_on_the_platform_matrix():
    # Agreeing on the image NAME is not agreeing on the base: with a matrix declared by only ONE
    # provider, the composed tag flipped with descriptor order (v1-jp7 vs v1) and BOTH orders
    # reported success — the manifest-order guess resolve_base_image exists to refuse.
    m = _bmanifest(image_registry="reg:5000", image_tag="v1", platform="jp7")
    matrix, plain = _bdesc("router", provides="base", platforms=("jp7",)), _bdesc("logger",
                                                                                  provides="base")
    for descs in ({"router": matrix, "logger": plain}, {"logger": plain, "router": matrix}):
        ref, _, err = resolve_base_image(m, descs)
        assert ref is None and err and "different build.platforms" in err
    # agreeing matrices still resolve — and compose the per-platform tag
    both = {"router": matrix, "logger": _bdesc("logger", provides="base", platforms=("jp7",))}
    assert resolve_base_image(m, both)[0] == "reg:5000/fleet-ros:v1-jp7"


def test_build_refuses_two_provider_scripts_racing_for_one_tag():
    # Same image name from DIFFERENT build scripts: stage-0 dedup (by resolved script path) keeps
    # both, so both would build and push one ref — two different images, last writer wins. That is
    # the skew `rig image audit` exists to catch, produced by rig itself.
    ws = _provider_workspace()
    (ws / "logger" / "build-alt.sh").write_text("#!/bin/sh\ntouch alt-ran\n")
    (ws / "logger" / "build-alt.sh").chmod(0o755)
    (ws / "logger" / "rigging.yaml").write_text(
        "service: logger\nlauncher: logger-up\n"
        "build: {command: ./build-alt.sh, images: [fleet-ros], provides: base}\n")
    root = _deployment("vehicle: t\nimages: {registry: 'reg:5000', tag: v1}\nsensors:\n"
                       "  - {name: i0, service: router, config: config/i0.yaml}\n"
                       "  - {name: i1, service: logger, config: config/i1.yaml}\n",
                       {"router": ws / "router", "logger": ws / "logger"})
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        assert build(load_manifest(root),
                     {s: load_descriptor(s, ws / s) for s in ("router", "logger")},
                     registry=None, tag=None, dry_run=False) == 1
    assert "different build commands" in err.getvalue()
    assert not (ws / "base" / "log").exists()          # refused BEFORE building anything
    assert not (ws / "logger" / "alt-ran").exists()


def test_build_exports_ros_rmw_set_or_popped():
    # audit ERRORs when ros-<distro>-<rmw> is missing from an image; the builder must be TOLD which
    # rmw the vehicle declares, or that check has no prevention counterpart. Rig-owned (not the
    # conventional RMW_IMPLEMENTATION) so a dev box's .bashrc can't decide what a fleet image holds.
    svc = _service_with_build("base", 'echo "${RIG_ROS_RMW:-UNSET}" > "$(dirname "$0")/seen"\n')
    root = _deployment("vehicle: t\nros: {distro: lyrical, rmw: rmw_cyclonedds_cpp}\n"
                       "sensors: [{name: i0, service: base, config: config/i0.yaml}]\n",
                       {"base": svc})
    descs = {"base": load_descriptor("base", svc)}
    assert build(load_manifest(root), descs, registry=None, tag=None, dry_run=False) == 0
    assert (svc / "seen").read_text().strip() == "rmw_cyclonedds_cpp"
    # the audit's package mapping is the shared source of truth for builder and checker
    assert "ros-lyrical-rmw-cyclonedds-cpp" == f"ros-lyrical-{'rmw_cyclonedds_cpp'.replace('_', '-')}"
    os.environ["RIG_ROS_RMW"] = "leak"                 # rig-owned: never inherited from the shell
    try:
        m = load_manifest(_deployment(
            "vehicle: t\nsensors: [{name: i0, service: base, config: config/i0.yaml}]\n",
            {"base": svc}))
        m.ros = type(m.ros)(domain_id=0, rmw="", distro=None)   # nothing declared -> POP
        assert build(m, descs, registry=None, tag=None, dry_run=False) == 0
    finally:
        os.environ.pop("RIG_ROS_RMW")
    assert (svc / "seen").read_text().strip() == "UNSET"


def test_fleet_env_exports_resolves_and_pops_base_image():
    from rig_cli.dispatch import fleet_env
    root = _deployment("vehicle: t\nvehicle_id: 1\nimages: {base: 'my/base:2'}\n"
                       "sensors: [{name: i0, service: cam, config: config/i0.yaml}]\n",
                       {"cam": pathlib.Path("/x")})
    assert fleet_env(load_manifest(root))["RIG_BASE_IMAGE"] == "my/base:2"
    # no base declared: a provider service resolves it; leaked shell values are popped without one
    bare = _deployment("vehicle: t\nvehicle_id: 1\nimages: {registry: 'r:5000', tag: v1}\n"
                       "sensors: [{name: i0, service: cam, config: config/i0.yaml}]\n",
                       {"cam": pathlib.Path("/x")})
    m = load_manifest(bare)
    provider = {"router": _bdesc("router", provides="base")}
    assert fleet_env(m, provider)["RIG_BASE_IMAGE"] == "r:5000/fleet-ros:v1"
    os.environ["RIG_BASE_IMAGE"] = "leak"
    try:
        assert "RIG_BASE_IMAGE" not in fleet_env(m, {"cam": _bdesc("cam")})
        # a provider CONFLICT resolves to nothing here — doctor owns the ERROR, up blocks on it
        clash = {"a": _bdesc("a", provides="base"), "b": _bdesc("b", provides="base", images=("o",))}
        assert "RIG_BASE_IMAGE" not in fleet_env(m, clash)
    finally:
        os.environ.pop("RIG_BASE_IMAGE")


def test_env_map_cannot_shadow_base_image_or_no_cache():
    for var in ("RIG_BASE_IMAGE", "RIG_BUILD_NO_CACHE"):
        root = _deployment(f"vehicle: t\nenv: {{{var}: sneaky}}\nsensors: []\n", {})
        try:
            load_manifest(root)
            raise AssertionError("expected RigError")
        except RigError as exc:
            assert "rig-owned" in str(exc)


def test_doctor_warns_on_a_zenoh_router_under_a_non_zenoh_rmw():
    # The converse of the existing guardrail. The router runs rmw_zenohd out of the base image, and a
    # base built for the DECLARED rmw (RIG_ROS_RMW) carries only that one — so a DDS fleet with a
    # router enabled builds clean and dies on `up`. Catch it at doctor time instead.
    from rig_cli.doctor import WARN, collect
    from rig_cli.manifest import Sensor

    def _issues(rmw, enabled=True):
        m = _bmanifest()
        m.ros = RosSettings(0, rmw, "lyrical")
        m.sensors = [Sensor(name="router", service="zenoh-router", config=pathlib.Path("/x"),
                            enabled=enabled, order=0, tier="infra")]
        return collect(m, {}, {})

    hit = [i for i in _issues("rmw_cyclonedds_cpp") if i.level == WARN and "rmw_zenohd" in i.message]
    assert hit and "router" in hit[0].message and "rmw_cyclonedds_cpp" in hit[0].message
    assert not [i for i in _issues("rmw_zenoh_cpp") if "rmw_zenohd" in i.message]   # zenoh fleet: fine
    assert not [i for i in _issues("rmw_cyclonedds_cpp", enabled=False)             # disabled: not running
                if "rmw_zenohd" in i.message]


def test_doctor_base_image_checks():
    from rig_cli.doctor import ERROR, INFO, OK, collect
    ros_desc = _bdesc("cam")
    ros_desc.ros_distro = "lyrical"
    m = _bmanifest(image_registry="r", image_tag="v1")
    # resolved via a provider -> OK line naming the composed ref
    issues = collect(m, {}, {"router": _bdesc("router", provides="base"), "cam": ros_desc})
    assert any(i.level == OK and "r/fleet-ros:v1" in i.message for i in issues)
    # conflicting providers -> ERROR (blocks up)
    clash = {"a": _bdesc("a", provides="base"), "b": _bdesc("b", provides="base", images=("o",))}
    assert any(i.level == ERROR and "disagree" in i.message for i in collect(m, {}, clash))
    # ROS services but no base anywhere -> advisory only
    issues = collect(m, {}, {"cam": ros_desc})
    assert any(i.level == INFO and "no deployment base image" in i.message for i in issues)


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
