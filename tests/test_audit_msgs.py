"""rig image audit — the msgs checks. v0.2.29: the stale-overlay check (the overlay in the
registry is whatever the LAST `rig build` baked; a declaration added or a pin bumped since then
leaves `up` pulling the old image under the same tag, and the new types silently vanish from
bags) + declared-apt-installed. Docker is STUBBED (test_audit's technique, extended: the msgs file
probe is keyed on its script marker, the dpkg inspection on the ref alone).
Run: python3 tests/test_audit_msgs.py"""
import contextlib
import io
import os
import pathlib
import stat
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from rig_cli.audit import _norm_repo, audit
from rig_cli.catalog import load_catalog
from rig_cli.descriptor import load_descriptor
from rig_cli.dispatch import fleet_env
from rig_cli.manifest import load_manifest

_MARK = "::rig-image-audit::"
_FMARK = "::rig-msgs-file::"
_ABSENT = "::rig-msgs-absent::"

OVERLAY = "reg:5000/fleet-ros-msgs:v1"
PX4 = "https://github.com/PX4/px4_msgs.git"

# The union rig renders for the fixture riggings below (mav declares mavros_msgs + px4@v1.16.0).
FRESH_MANIFEST = (f"apt:\\n- mavros_msgs\\nsource:\\n- repo: {PX4}\\n"
                  "  ref: v1.16.0\\n  packages:\\n  - px4_msgs\\n")
OVERLAY_PKGS = {"ros-lyrical-rmw-zenoh-cpp": "0.3.2", "ros-lyrical-mavros-msgs": "2.6.0"}


def _ros_out(distro: str, pkgs: dict[str, str]) -> str:
    return f"{distro}\\n{_MARK}\\n" + "".join(f"{p} {v}\\n" for p, v in pkgs.items())


def _files_out(manifest_yaml: str | None, provenance_yaml: str | None = None) -> str:
    """The msgs file-probe stdout: a marker line per path, then the file body or the absent tag."""
    out = ""
    for path, body in (("/opt/fleet-msgs/manifest.yaml", manifest_yaml),
                       ("/opt/fleet-msgs/provenance.yaml", provenance_yaml)):
        out += f"{_FMARK}{path}\\n" + (f"{body}\\n" if body is not None else f"{_ABSENT}\\n")
    return out


def _fake_docker(inspect: dict[str, str], probes: dict[str, str]) -> pathlib.Path:
    """`docker` stub: msgs file-probe calls (script carries the file marker) are keyed FIRST so
    the plain ref patterns of the inspection cases don't swallow them. 'FAIL' -> exit 126."""
    bin_dir = pathlib.Path(tempfile.mkdtemp())
    lines = ["#!/bin/sh", 'case "$*" in']
    for ref, out in probes.items():
        if out == "FAIL":
            lines.append(f'  *"{ref}"*"rig-msgs-file"*) echo "no shell" >&2; exit 126 ;;')
        else:
            lines.append(f'  *"{ref}"*"rig-msgs-file"*) printf %b "{out}" ;;')
    for ref, out in inspect.items():
        if out == "FAIL":
            lines.append(f'  *"{ref}"*) echo "exec: /bin/sh: not found" >&2; exit 126 ;;')
        else:
            lines.append(f'  *"{ref}"*) printf %b "{out}" ;;')
    lines += ['  *) echo "unexpected: $*" >&2; exit 1 ;;', "esac"]
    docker = bin_dir / "docker"
    docker.write_text("\n".join(lines) + "\n")
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    return bin_dir


def _msgs_deployment(mav_image="mav-core:v1", logger_image=OVERLAY, mav_msgs=None):
    """A deployment whose fleet_env exports RIG_MSGS_IMAGE: a base provider declaring
    msgs_overlay (logger) + a msgs-declaring service (mav)."""
    root = pathlib.Path(tempfile.mkdtemp())
    (root / "config").mkdir()
    repos = {}
    logger = pathlib.Path(tempfile.mkdtemp())
    (logger / "rigging.yaml").write_text(
        "service: logger\nlauncher: logger-up\n"
        "build: {command: ../base/build.sh, images: [fleet-ros], provides: base,\n"
        "        msgs_overlay: {command: ../msgs/build-msgs.sh, image: fleet-ros-msgs}}\n")
    (logger / "logger-up").write_text(
        "#!/bin/sh\n" f"printf 'services:\\n  main:\\n    image: {logger_image}\\n'\n")
    (logger / "logger-up").chmod(0o755)
    repos["logger"] = logger
    mav = pathlib.Path(tempfile.mkdtemp())
    (mav / "rigging.yaml").write_text(
        "service: mav\nlauncher: mav-up\n" + (mav_msgs if mav_msgs is not None else (
            "msgs:\n  apt: [mavros_msgs]\n"
            f"  source: [{{repo: {PX4}, ref: v1.16.0, packages: [px4_msgs]}}]\n")))
    (mav / "mav-up").write_text(
        "#!/bin/sh\n" f"printf 'services:\\n  main:\\n    image: {mav_image}\\n'\n")
    (mav / "mav-up").chmod(0o755)
    repos["mav"] = mav
    (root / "vehicle.yaml").write_text(
        "vehicle: t\nvehicle_id: 1\nros: {distro: lyrical, rmw: rmw_zenoh_cpp}\n"
        "images: {registry: 'reg:5000', tag: v1}\nsensors:\n"
        "  - {name: logger_0, service: logger, config: config/logger.yaml}\n"
        "  - {name: mav_0, service: mav, config: config/mav.yaml}\n")
    (root / "services.yaml").write_text(
        "services:\n" + "\n".join(f"  {s}: {{path: {p}}}" for s, p in repos.items()))
    for s in repos:
        (root / "config" / f"{s}.yaml").write_text(f"service: {s}\nname: {s}_0\n")
    manifest = load_manifest(root)
    load_catalog(root)
    descriptors = {s: load_descriptor(s, p) for s, p in repos.items()}
    return manifest, descriptors


def _run(manifest, descriptors, inspect, probes) -> tuple[int, str]:
    bin_dir = _fake_docker(inspect, probes)
    old = os.environ["PATH"]
    os.environ["PATH"] = f"{bin_dir}:{old}"
    err = io.StringIO()
    try:
        env = fleet_env(manifest, descriptors)
        with contextlib.redirect_stderr(err):
            rc = audit(manifest, descriptors, env)
    finally:
        os.environ["PATH"] = old
    return rc, err.getvalue()


_INSPECT_OK = {"fleet-ros-msgs": _ros_out("lyrical", OVERLAY_PKGS),
               "mav-core": _ros_out("lyrical", {"ros-lyrical-rmw-zenoh-cpp": "0.3.2"})}


def test_fresh_overlay_is_green():
    m, d = _msgs_deployment()
    rc, out = _run(m, d, _INSPECT_OK, {"fleet-ros-msgs": _files_out(FRESH_MANIFEST)})
    assert rc == 0, out
    assert "baked manifest matches the current" in out
    assert "declared apt interface package(s) installed" in out


def test_stale_pin_is_an_error_naming_both_refs():
    stale = FRESH_MANIFEST.replace("v1.16.0", "v1.15.0")
    m, d = _msgs_deployment()
    rc, out = _run(m, d, _INSPECT_OK, {"fleet-ros-msgs": _files_out(stale)})
    assert rc == 1
    assert "STALE" in out and "v1.15.0" in out and "v1.16.0" in out and "rig build" in out


def test_stale_apt_addition_is_an_error():
    # declaration grew (vision_msgs) after the last build — the exact silent-missing-topics case
    m, d = _msgs_deployment(mav_msgs=(
        "msgs:\n  apt: [mavros_msgs, vision_msgs]\n"
        f"  source: [{{repo: {PX4}, ref: v1.16.0, packages: [px4_msgs]}}]\n"))
    rc, out = _run(m, d, _INSPECT_OK, {"fleet-ros-msgs": _files_out(FRESH_MANIFEST)})
    assert rc == 1 and "STALE" in out and "apt +vision_msgs" in out


def test_hand_authored_manifest_compares_by_content_not_spelling():
    # scp-form repo spelling + reordered lists (a FLEET_MSGS_MANIFEST hand-build) still MATCH —
    # the comparison normalizes repos (contract §A3) and sorts lists
    hand = ("source:\\n- packages:\\n  - px4_msgs\\n  ref: v1.16.0\\n"
            "  repo: git@github.com:PX4/px4_msgs.git\\napt:\\n- mavros_msgs\\n")
    m, d = _msgs_deployment()
    rc, out = _run(m, d, _INSPECT_OK, {"fleet-ros-msgs": _files_out(hand)})
    assert rc == 0, out
    assert "baked manifest matches" in out


def test_missing_baked_manifest_is_a_warn_not_error():
    m, d = _msgs_deployment()
    rc, out = _run(m, d, _INSPECT_OK, {"fleet-ros-msgs": _files_out(None)})
    assert rc == 0, out
    assert "staleness unverifiable" in out
    assert "declared apt interface package(s) installed" in out  # the apt check still ran


def test_declared_apt_missing_from_overlay_is_an_error():
    thin = {"fleet-ros-msgs": _ros_out("lyrical", {"ros-lyrical-rmw-zenoh-cpp": "0.3.2"}),
            "mav-core": _INSPECT_OK["mav-core"]}
    m, d = _msgs_deployment()
    rc, out = _run(m, d, thin, {"fleet-ros-msgs": _files_out(FRESH_MANIFEST)})
    assert rc == 1
    assert "not installed: mavros_msgs" in out and "cannot record" in out


def test_overlay_not_pulled_by_any_compose_is_still_audited():
    # BAG_LOGGER_IMAGE-override shape: no rendered compose pulls the overlay, but the export makes
    # it the deployment's overlay — audit probes it anyway (falling back to a direct inspect for
    # the apt check, since the main loop never saw it)
    m, d = _msgs_deployment(logger_image="other-logger:v1")
    inspect = {**_INSPECT_OK, "other-logger": _ros_out("lyrical",
                                                       {"ros-lyrical-rmw-zenoh-cpp": "0.3.2"})}
    stale = FRESH_MANIFEST.replace("v1.16.0", "v1.15.0")
    rc, out = _run(m, d, inspect, {"fleet-ros-msgs": _files_out(stale)})
    assert rc == 1 and "STALE" in out


def test_unpullable_overlay_is_a_warn():
    m, d = _msgs_deployment(logger_image="other-logger:v1")
    inspect = {**_INSPECT_OK, "other-logger": _ros_out("lyrical",
                                                       {"ros-lyrical-rmw-zenoh-cpp": "0.3.2"})}
    rc, out = _run(m, d, inspect, {"fleet-ros-msgs": "FAIL"})
    assert rc == 0, out
    assert "msgs overlay" in out and "uninspectable" in out


def test_no_msgs_export_no_msgs_lines():
    m, d = _msgs_deployment(mav_msgs="")  # nothing declared -> fleet_env exports no RIG_MSGS_IMAGE
    rc, out = _run(m, d, _INSPECT_OK, {})
    assert rc == 0, out
    assert "msgs overlay" not in out


def test_norm_repo_contract_a3():
    https = _norm_repo("https://github.com/PX4/px4_msgs.git")
    assert https == "github.com/PX4/px4_msgs"
    assert _norm_repo("git@GitHub.com:PX4/px4_msgs.git") == https      # scp form + host case
    assert _norm_repo("ssh://git@github.com/PX4/px4_msgs") == https    # scheme + userinfo
    assert _norm_repo("https://github.com/PX4/px4_msgs.git.git").endswith("px4_msgs.git")  # ONE .git
    assert _norm_repo("https://gitlab.com/PX4/px4_msgs.git") != https  # different remotes differ
    assert _norm_repo("https://github.com/PX4/PX4_msgs.git") != https  # path case is preserved


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
