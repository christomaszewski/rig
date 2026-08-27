"""The fleet-ros-msgs overlay — `msgs:` declarations, the union + its refusals, the overlay build
stage, RIG_MSGS_IMAGE export, and the doctor preflight. rosbag2 cannot record a topic whose message
package isn't in the recorder's image (it logs "unknown type" and keeps going), so a fleet with
custom types silently gets bags missing them; the overlay is base + the union of the riggings'
`msgs:` blocks, and the logger compose prefers RIG_MSGS_IMAGE over the bare base.
Run: `.venv/bin/python tests/test_msgs.py`."""
import contextlib
import io
import os
import pathlib
import sys
import tempfile
import textwrap

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import yaml

from rig_cli import RigError
from rig_cli.build import build, msgs_union, resolve_msgs_image
from rig_cli.descriptor import Descriptor, MsgsSource, load_descriptor
from rig_cli.manifest import Manifest, RosSettings, load_manifest


def _repo(rigging: str) -> pathlib.Path:
    d = pathlib.Path(tempfile.mkdtemp())
    (d / "rigging.yaml").write_text(textwrap.dedent(rigging))
    return d


def _load(rigging: str) -> Descriptor:
    return load_descriptor("s", _repo("service: s\nlauncher: s-up\n" + textwrap.dedent(rigging)))


def _expect_rigerror(rigging: str, needle: str):
    try:
        _load(rigging)
        raise AssertionError(f"expected RigError mentioning {needle!r}")
    except RigError as exc:
        assert needle in str(exc), f"{needle!r} not in {exc}"


PX4 = "https://github.com/PX4/px4_msgs.git"


# --- descriptor: the `msgs:` block and `build.msgs_overlay` --------------------------------------

def test_descriptor_parses_msgs_block():
    d = _load("msgs:\n"
              "  apt: [mavros_msgs, vision_msgs]\n"
              "  source:\n"
              f"    - {{repo: {PX4}, ref: v1.16.0, packages: [px4_msgs]}}\n")
    assert d.msgs_apt == ["mavros_msgs", "vision_msgs"]
    assert d.msgs_source == [MsgsSource(repo=PX4, ref="v1.16.0", packages=("px4_msgs",))]
    bare = _load("")  # no msgs block: empty lists, nothing to union
    assert bare.msgs_apt == [] and bare.msgs_source == []


def test_descriptor_msgs_validation_is_strict():
    # a typo'd sub-key would silently drop packages from the overlay — every one fails loudly
    _expect_rigerror("msgs:\n  atp: [mavros_msgs]\n", "unknown key")
    _expect_rigerror("msgs: [mavros_msgs]\n", "must be a mapping")
    # apt entries are ROS package names (underscores), never the mapped apt name
    _expect_rigerror("msgs:\n  apt: [ros-lyrical-mavros-msgs]\n", "not a ROS package name")
    # source entries: repo, ref, packages all mandatory; unknown keys refused
    _expect_rigerror(f"msgs:\n  source: [{{repo: {PX4}, packages: [px4_msgs]}}]\n", "`ref`")
    _expect_rigerror(f"msgs:\n  source: [{{repo: {PX4}, ref: v1}}]\n", "`packages`")
    _expect_rigerror("msgs:\n  source: [{ref: v1, packages: [x]}]\n", "`repo`")
    _expect_rigerror(f"msgs:\n  source: [{{repo: {PX4}, ref: v1, packages: [x], rev: v2}}]\n",
                     "unknown key")


def test_descriptor_parses_msgs_overlay():
    d = _load("build: {command: ../base/b.sh, images: [fleet-ros], provides: base,\n"
              "        msgs_overlay: {command: ../msgs/build-msgs.sh, image: fleet-ros-msgs}}\n")
    assert d.msgs_overlay_command == "../msgs/build-msgs.sh"
    assert d.msgs_overlay_image == "fleet-ros-msgs"
    # strict: unknown keys, missing halves, and non-base declarers all fail loudly
    _expect_rigerror("build: {command: b.sh, images: [fleet-ros], provides: base,\n"
                     "        msgs_overlay: {command: x, image: y, tag: z}}\n", "unknown key")
    _expect_rigerror("build: {command: b.sh, images: [fleet-ros], provides: base,\n"
                     "        msgs_overlay: {command: x}}\n", "both")
    _expect_rigerror("build: {command: b.sh, images: [fleet-ros], provides: base,\n"
                     "        msgs_overlay: {image: y}}\n", "both")
    # the overlay builds FROM the base — only a base provider may declare it
    _expect_rigerror("build: {command: b.sh,\n"
                     "        msgs_overlay: {command: x, image: y}}\n", "provides: base")


# --- the union: dedupe, merge, and the pin-conflict refusal --------------------------------------

def _mdesc(svc, *, apt=(), source=(), overlay_image=None, overlay_command=None,
           provides=None, images=("fleet-ros",), platforms=(), command="b.sh"):
    return Descriptor(service=svc, repo=pathlib.Path("/nonexistent"), launcher=f"{svc}-up",
                      verbs={}, ros_distro=None, external_volumes=[], host_ports=[],
                      build_command=command, build_images=list(images),
                      build_platforms=list(platforms), build_provides=provides,
                      msgs_apt=list(apt), msgs_source=list(source),
                      msgs_overlay_command=overlay_command, msgs_overlay_image=overlay_image)


def _src(repo=PX4, ref="v1.16.0", packages=("px4_msgs",)):
    return MsgsSource(repo=repo, ref=ref, packages=tuple(packages))


def test_msgs_union_dedupes_and_merges():
    descs = {"mav": _mdesc("mav", apt=["mavros_msgs"], source=[_src()]),
             "px4": _mdesc("px4", apt=["mavros_msgs", "ackermann_msgs"],
                           source=[_src(packages=("px4_msgs", "px4_extra"))])}
    union, err = msgs_union(descs)
    assert err is None
    assert union == {"apt": ["ackermann_msgs", "mavros_msgs"],
                     "source": [{"repo": PX4, "ref": "v1.16.0",
                                 "packages": ["px4_extra", "px4_msgs"]}]}
    assert msgs_union({"a": _mdesc("a")}) == (None, None)  # nothing declared -> no overlay


def test_msgs_union_refuses_one_repo_at_two_refs():
    # a drifted pin is a SILENT schema mismatch in the recorded bags — refuse naming the services,
    # never a manifest-order guess (the base-provider-disagreement doctrine)
    descs = {"mav": _mdesc("mav", source=[_src(ref="v1.16.0")]),
             "px4": _mdesc("px4", source=[_src(ref="v1.17.0")])}
    union, err = msgs_union(descs)
    assert union is None and err
    assert "different refs" in err and "mav" in err and "px4" in err
    assert "v1.16.0" in err and "v1.17.0" in err


# --- resolve_msgs_image: composition, gating, provider agreement ---------------------------------

def _mmanifest(**kw):
    return Manifest(vehicle="t", ros=RosSettings(0, "rmw_zenoh_cpp", "lyrical"), sensors=[], **kw)


def test_resolve_msgs_image_composes_like_the_pull_side():
    m = _mmanifest(image_registry="reg:5000", image_tag="v1")
    descs = {"logger": _mdesc("logger", provides="base", overlay_image="fleet-ros-msgs",
                              overlay_command="../msgs/build-msgs.sh"),
             "mav": _mdesc("mav", apt=["mavros_msgs"])}
    ref, origin, err = resolve_msgs_image(m, descs)
    assert (ref, err) == ("reg:5000/fleet-ros-msgs:v1", None) and "logger" in origin
    # the overlay is FROM the base, so it inherits the provider's matrix: <tag>-<platform>
    m2 = _mmanifest(image_registry="r", image_tag="v1", platform="jp7")
    descs2 = {"logger": _mdesc("logger", provides="base", overlay_image="fleet-ros-msgs",
                               overlay_command="x", platforms=("jp7",)),
              "mav": _mdesc("mav", apt=["mavros_msgs"])}
    assert resolve_msgs_image(m2, descs2)[0] == "r/fleet-ros-msgs:v1-jp7"


def test_resolve_msgs_image_gates_on_declarations_and_mechanism():
    m = _mmanifest(image_registry="reg:5000", image_tag="v1")
    provider = _mdesc("logger", provides="base", overlay_image="fleet-ros-msgs", overlay_command="x")
    # mechanism declared but EMPTY union -> nothing (bare base is correct)
    assert resolve_msgs_image(m, {"logger": provider, "cam": _mdesc("cam")}) == (None, None, None)
    # declarations but NO mechanism -> nothing here (doctor WARNs)
    assert resolve_msgs_image(m, {"mav": _mdesc("mav", apt=["mavros_msgs"])}) == (None, None, None)
    # pin conflict surfaces as the error
    clash = {"logger": provider,
             "a": _mdesc("a", source=[_src(ref="v1")]), "b": _mdesc("b", source=[_src(ref="v2")])}
    ref, _, err = resolve_msgs_image(m, clash)
    assert ref is None and err and "different refs" in err


def test_resolve_msgs_image_refuses_provider_disagreement():
    m = _mmanifest(image_registry="reg:5000", image_tag="v1")
    mav = _mdesc("mav", apt=["mavros_msgs"])
    # two providers, different overlay IMAGES -> refuse
    diff = {"a": _mdesc("a", provides="base", overlay_image="fleet-ros-msgs", overlay_command="x"),
            "b": _mdesc("b", provides="base", overlay_image="other-msgs", overlay_command="x"),
            "mav": mav}
    ref, _, err = resolve_msgs_image(m, diff)
    assert ref is None and err and "disagree" in err
    # same image, different MATRICES -> the composed tag would depend on manifest order: refuse
    m2 = _mmanifest(image_registry="r", image_tag="v1", platform="jp7")
    for order in ((("a", ("jp7",)), ("b", ())), (("b", ()), ("a", ("jp7",)))):
        descs = {svc: _mdesc(svc, provides="base", overlay_image="fleet-ros-msgs",
                             overlay_command="x", platforms=plats) for svc, plats in order}
        descs["mav"] = mav
        ref, _, err = resolve_msgs_image(m2, descs)
        assert ref is None and err and "different build.platforms" in err
    # agreeing providers resolve (the fleet-ros two-provider pattern)
    same = {"a": _mdesc("a", provides="base", overlay_image="fleet-ros-msgs", overlay_command="x"),
            "b": _mdesc("b", provides="base", overlay_image="fleet-ros-msgs", overlay_command="x"),
            "mav": mav}
    ref, origin, err = resolve_msgs_image(m, same)
    assert ref == "reg:5000/fleet-ros-msgs:v1" and err is None and "a" in origin and "b" in origin


# --- the build stage: after the base, union manifest + env, refusals before anything builds ------

def _overlay_workspace():
    """rig-infra shape: base/build.sh + msgs/build-msgs.sh, a provider declaring both, and a
    msgs-declaring dependent."""
    ws = pathlib.Path(tempfile.mkdtemp())
    (ws / "base").mkdir()
    (ws / "base" / "build.sh").write_text('#!/bin/sh\ncd "$(dirname "$0")"\necho built >> log\n')
    (ws / "base" / "build.sh").chmod(0o755)
    (ws / "msgs").mkdir()
    (ws / "msgs" / "build-msgs.sh").write_text(  # record argv + the env channel + the manifest
        '#!/bin/sh\ncd "$(dirname "$0")"\necho "$@" > argv\n'
        'echo "${RIG_BASE_IMAGE:-UNSET} ${RIG_MSGS_MANIFEST:-UNSET}" > env\n'
        'if [ -f "$(dirname "$0")/../base/log" ] || [ -n "${EXTERNAL_BASE_OK:-}" ]; '
        'then echo after-base > order; else echo TOO-EARLY > order; fi\n'
        'cat "${RIG_MSGS_MANIFEST}" > manifest\n')
    (ws / "msgs" / "build-msgs.sh").chmod(0o755)
    logger = ws / "logger"
    logger.mkdir()
    (logger / "rigging.yaml").write_text(
        "service: logger\nlauncher: logger-up\n"
        "build: {command: ../base/build.sh, images: [fleet-ros], provides: base,\n"
        "        msgs_overlay: {command: ../msgs/build-msgs.sh, image: fleet-ros-msgs}}\n")
    mav = ws / "mav"
    mav.mkdir()
    (mav / "rigging.yaml").write_text(
        "service: mav\nlauncher: mav-up\n"
        "msgs:\n  apt: [mavros_msgs]\n"
        f"  source: [{{repo: {PX4}, ref: v1.16.0, packages: [px4_msgs]}}]\n")
    return ws


def _deployment(vehicle_yaml: str, services: dict) -> pathlib.Path:
    root = pathlib.Path(tempfile.mkdtemp())
    (root / "config").mkdir()
    (root / "vehicle.yaml").write_text(textwrap.dedent(vehicle_yaml))
    rows = ", ".join(f"{s}: {{path: {p}}}" for s, p in services.items())
    (root / "services.yaml").write_text(f"services: {{{rows}}}\n")
    for name, svc in [(f"i{i}", s) for i, s in enumerate(services)]:
        (root / "config" / f"{name}.yaml").write_text(f"service: {svc}\nname: {name}\n")
    return root


def _build_ws(ws, vehicle_yaml, services=("logger", "mav")):
    root = _deployment(vehicle_yaml, {s: ws / s for s in services})
    descs = {s: load_descriptor(s, ws / s) for s in services}
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = build(load_manifest(root), descs, registry=None, tag=None, dry_run=False)
    return rc, err.getvalue()


def test_build_runs_overlay_after_base_with_union_manifest():
    ws = _overlay_workspace()
    rc, err = _build_ws(ws, "vehicle: t\nimages: {registry: 'reg:5000', tag: v1}\n"
                            "ros: {distro: lyrical}\nsensors:\n"
                            "  - {name: i0, service: logger, config: config/i0.yaml}\n"
                            "  - {name: i1, service: mav, config: config/i1.yaml}\n")
    assert rc == 0, err
    assert "msgs overlay reg:5000/fleet-ros-msgs:v1" in err and "RIG_MSGS_IMAGE" in err
    assert (ws / "msgs" / "order").read_text().strip() == "after-base"  # stage 0 built first
    assert (ws / "msgs" / "argv").read_text().split() == ["reg:5000", "v1"]
    base_seen, manifest_path = (ws / "msgs" / "env").read_text().split()
    assert base_seen == "reg:5000/fleet-ros:v1"  # the overlay builds FROM the deployment's base
    rendered = yaml.safe_load((ws / "msgs" / "manifest").read_text())
    assert rendered == {"apt": ["mavros_msgs"],
                        "source": [{"repo": PX4, "ref": "v1.16.0", "packages": ["px4_msgs"]}]}


def test_build_skips_overlay_when_nothing_declares_msgs():
    ws = _overlay_workspace()
    (ws / "mav" / "rigging.yaml").write_text("service: mav\nlauncher: mav-up\n")  # no msgs block
    rc, err = _build_ws(ws, "vehicle: t\nimages: {registry: 'reg:5000', tag: v1}\nsensors:\n"
                            "  - {name: i0, service: logger, config: config/i0.yaml}\n"
                            "  - {name: i1, service: mav, config: config/i1.yaml}\n")
    assert rc == 0, err
    assert not (ws / "msgs" / "argv").exists()  # empty union -> no overlay build at all
    assert "RIG_MSGS_IMAGE" not in err
    assert (ws / "base" / "log").exists()       # the base still builds


def test_build_refuses_pin_conflict_before_building_anything():
    ws = _overlay_workspace()
    other = ws / "px4"
    other.mkdir()
    (other / "rigging.yaml").write_text(
        "service: px4\nlauncher: px4-up\n"
        f"msgs: {{source: [{{repo: {PX4}, ref: v1.17.0, packages: [px4_msgs]}}]}}\n")
    rc, err = _build_ws(ws, "vehicle: t\nimages: {registry: 'reg:5000', tag: v1}\nsensors:\n"
                            "  - {name: i0, service: logger, config: config/i0.yaml}\n"
                            "  - {name: i1, service: mav, config: config/i1.yaml}\n"
                            "  - {name: i2, service: px4, config: config/i2.yaml}\n",
                        services=("logger", "mav", "px4"))
    assert rc == 1 and "different refs" in err
    assert not (ws / "base" / "log").exists()   # refused BEFORE building anything
    assert not (ws / "msgs" / "argv").exists()


def test_build_warns_on_declarations_without_a_mechanism():
    ws = _overlay_workspace()
    (ws / "logger" / "rigging.yaml").write_text(  # provider without msgs_overlay
        "service: logger\nlauncher: logger-up\n"
        "build: {command: ../base/build.sh, images: [fleet-ros], provides: base}\n")
    rc, err = _build_ws(ws, "vehicle: t\nimages: {registry: 'reg:5000', tag: v1}\nsensors:\n"
                            "  - {name: i0, service: logger, config: config/i0.yaml}\n"
                            "  - {name: i1, service: mav, config: config/i1.yaml}\n")
    assert rc == 0, err
    assert "WARNING" in err and "mav" in err and "msgs_overlay" in err
    assert not (ws / "msgs" / "argv").exists()


def test_build_external_base_still_builds_the_overlay_from_it():
    # images.base skips the provider's own base build, NOT the overlay — an external base still
    # needs its overlay, built FROM the external ref
    ws = _overlay_workspace()
    os.environ["EXTERNAL_BASE_OK"] = "1"  # the order-guard has no base log to see
    try:
        rc, err = _build_ws(ws, "vehicle: t\n"
                                "images: {registry: 'reg:5000', tag: v1, base: 'nvcr.io/custom:1'}\n"
                                "sensors:\n"
                                "  - {name: i0, service: logger, config: config/i0.yaml}\n"
                                "  - {name: i1, service: mav, config: config/i1.yaml}\n")
    finally:
        os.environ.pop("EXTERNAL_BASE_OK")
    assert rc == 0, err
    assert not (ws / "base" / "log").exists()   # provider's base build skipped (external base)
    assert (ws / "msgs" / "env").read_text().split()[0] == "nvcr.io/custom:1"  # FROM the external


def test_build_overlay_failure_flips_rc_but_does_not_stop_dependents():
    ws = _overlay_workspace()
    (ws / "msgs" / "build-msgs.sh").write_text("#!/bin/sh\nexit 1\n")
    (ws / "msgs" / "build-msgs.sh").chmod(0o755)
    cam = ws / "cam"
    cam.mkdir()
    (cam / "rigging.yaml").write_text("service: cam\nlauncher: cam-up\nbuild: build.sh\n")
    (cam / "build.sh").write_text("#!/bin/sh\ntouch ran\n")
    (cam / "build.sh").chmod(0o755)
    rc, err = _build_ws(ws, "vehicle: t\nimages: {registry: 'reg:5000', tag: v1}\nsensors:\n"
                            "  - {name: i0, service: logger, config: config/i0.yaml}\n"
                            "  - {name: i1, service: mav, config: config/i1.yaml}\n"
                            "  - {name: i2, service: cam, config: config/i2.yaml}\n",
                        services=("logger", "mav", "cam"))
    assert rc == 1 and "[msgs] FAILED" in err
    assert (ws / "cam" / "ran").exists()  # nothing builds FROM the overlay — dependents continue


def test_build_overlay_without_registry_skips_and_says_so():
    ws = _overlay_workspace()
    rc, err = _build_ws(ws, "vehicle: t\nsensors:\n"
                            "  - {name: i0, service: logger, config: config/i0.yaml}\n"
                            "  - {name: i1, service: mav, config: config/i1.yaml}\n")
    assert rc == 0, err
    assert "no registry" in err and "skipped" in err
    assert not (ws / "msgs" / "argv").exists()  # build-msgs.sh pushes; nowhere to push to


# --- the export: fleet_env, ownership, certify -----------------------------------------------------

def test_fleet_env_exports_and_pops_msgs_image():
    from rig_cli.dispatch import fleet_env
    root = _deployment("vehicle: t\nvehicle_id: 1\nimages: {registry: 'r:5000', tag: v1}\n"
                       "sensors: [{name: i0, service: cam, config: config/i0.yaml}]\n",
                       {"cam": pathlib.Path("/x")})
    m = load_manifest(root)
    descs = {"logger": _mdesc("logger", provides="base", overlay_image="fleet-ros-msgs",
                              overlay_command="x"),
             "mav": _mdesc("mav", apt=["mavros_msgs"])}
    env = fleet_env(m, descs)
    assert env["RIG_MSGS_IMAGE"] == "r:5000/fleet-ros-msgs:v1"
    assert env["RIG_BASE_IMAGE"] == "r:5000/fleet-ros:v1"  # both channels compose side by side
    os.environ["RIG_MSGS_IMAGE"] = "leak"
    try:
        # no declarations -> popped, never inherited; a conflict resolves to nothing here too
        assert "RIG_MSGS_IMAGE" not in fleet_env(m, {"cam": _mdesc("cam")})
        clash = {**descs, "px4": _mdesc("px4", source=[_src(ref="v9")]),
                 "mav2": _mdesc("mav2", source=[_src(ref="v8")])}
        assert "RIG_MSGS_IMAGE" not in fleet_env(m, clash)
    finally:
        os.environ.pop("RIG_MSGS_IMAGE")


def test_env_map_cannot_shadow_msgs_vars():
    for var in ("RIG_MSGS_IMAGE", "RIG_MSGS_MANIFEST"):
        root = _deployment(f"vehicle: t\nenv: {{{var}: sneaky}}\nsensors: []\n", {})
        try:
            load_manifest(root)
            raise AssertionError("expected RigError")
        except RigError as exc:
            assert "rig-owned" in str(exc)


def test_certify_poison_env_keeps_msgs_image_unset():
    # certify's doctrine: RIG_MSGS_IMAGE and RIG_BASE_IMAGE stay unset so the launcher compose's
    # own fallback chain (BAG_LOGGER_IMAGE -> RIG_MSGS_IMAGE -> RIG_BASE_IMAGE -> composed ref)
    # is what gets certified
    from rig_cli.certify import _poison_env
    env = _poison_env({"RIG_MSGS_IMAGE": "leak", "RIG_BASE_IMAGE": "leak"}, "certifyname0")
    assert "RIG_MSGS_IMAGE" not in env and "RIG_BASE_IMAGE" not in env


def test_regular_builds_pop_a_leaked_msgs_manifest():
    from rig_cli.build import _build_env
    os.environ["RIG_MSGS_MANIFEST"] = "leak"
    try:
        assert "RIG_MSGS_MANIFEST" not in _build_env("lyrical")  # rig-owned: set-or-POPPED
    finally:
        os.environ.pop("RIG_MSGS_MANIFEST")


# --- doctor: the preflight triad -------------------------------------------------------------------

def test_doctor_msgs_overlay_checks():
    from rig_cli.doctor import ERROR, OK, WARN, collect
    m = _mmanifest(image_registry="r", image_tag="v1")
    provider = _mdesc("logger", provides="base", overlay_image="fleet-ros-msgs", overlay_command="x")
    mav = _mdesc("mav", apt=["mavros_msgs"])
    # declarations + mechanism -> OK naming the composed ref
    issues = collect(m, {}, {"logger": provider, "mav": mav})
    assert any(i.level == OK and "r/fleet-ros-msgs:v1" in i.message for i in issues)
    # declarations WITHOUT the mechanism -> the bags-silently-missing-topics preflight WARN
    issues = collect(m, {}, {"mav": mav})
    hits = [i for i in issues if i.level == WARN and "msgs_overlay" in i.message]
    assert hits and "mav" in hits[0].message and "silently" in hits[0].message
    # a pin conflict -> ERROR (blocks up)
    clash = {"logger": provider, "a": _mdesc("a", source=[_src(ref="v1")]),
             "b": _mdesc("b", source=[_src(ref="v2")])}
    assert any(i.level == ERROR and "different refs" in i.message for i in collect(m, {}, clash))
    # nothing declared -> silence (no overlay is the correct state, not a warnable one)
    issues = collect(m, {}, {"logger": provider, "cam": _mdesc("cam")})
    assert not any("msgs" in i.message for i in issues)


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
