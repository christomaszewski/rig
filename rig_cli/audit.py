"""``rig image audit`` — deployment-wide image *content* consistency checks.

`doctor` checks the manifest, `certify` checks each launcher; audit checks what actually RUNS: the
images the deployment's stacks resolve to. It renders each enabled stack's compose (the launcher's
`config` verb under the same fleet env `up` exports — so per-instance ``${<SVC>_IMAGE}`` overrides
and composed platform tags are honored), collects the `image:` refs, inspects each unique image with
docker (`ls /opt/ros` + the dpkg ``ros-*`` package list), and cross-checks:

  - distro     every ROS image carries vehicle.yaml ``ros.distro`` (a stray humble image on a
               lyrical vehicle breaks the shared graph at runtime, silently)
  - rmw        the declared ``ros.rmw`` package (``ros-<distro>-rmw-…``) is installed in every
               image matching the declared distro — nodes fall back to another RMW otherwise
  - versions   every ``ros-*`` package present in two or more images has ONE version across them.
               Independent images apt-install at different times against a moving ros repo, and a
               skewed rmw package between two images means sessions that can't talk. A shared base
               (RIG_BASE_IMAGE) prevents the skew by construction for base packages; ``rig build
               --no-cache`` re-converges a fleet that already drifted.

Non-ROS images (no /opt/ros, no ros-* packages) are excluded from the checks; an image without a
shell/dpkg is reported and skipped (uninspectable this way); a ROS image with zero ros-* dpkg
packages (source-built workspace) gets a WARN — dpkg cannot see inside it. Needs docker and the
images local or pullable: run right after ``rig build`` (or ``rig pull``).
"""
from __future__ import annotations

import shutil
import subprocess

import yaml

from .common import eprint
from .descriptor import Descriptor
from .dispatch import launcher_cmd, run as run_launcher
from .manifest import Manifest

ERROR, WARN, INFO, OK = "ERROR", "WARN", "INFO", "OK"
_SYMBOL = {ERROR: "✗", WARN: "!", INFO: "·", OK: "✓"}

_MARK = "::rig-image-audit::"
# One in-container probe: the /opt/ros distro dirs, a marker, then `pkg version` lines. Runs under
# /bin/sh (every debian/ubuntu ROS image has it); dpkg-query errors (no matches, no dpkg) are
# normalized away — absence is DATA here, not failure.
_INSPECT_SH = (f"ls /opt/ros 2>/dev/null; echo '{_MARK}'; "
               "dpkg-query -W -f '${Package} ${Version}\\n' 'ros-*' 2>/dev/null; true")
_DOCKER_TIMEOUT = 600  # seconds; covers a cold pull of a multi-GB image, not a hung daemon forever


def _inspect(ref: str) -> tuple[list[str], dict[str, str], str | None]:
    """(distro dirs under /opt/ros, ros-* package -> version, error). error set = uninspectable."""
    cmd = ["docker", "run", "--rm", "--entrypoint", "/bin/sh", ref, "-c", _INSPECT_SH]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=_DOCKER_TIMEOUT)
    except subprocess.TimeoutExpired:
        return [], {}, f"inspect timed out after {_DOCKER_TIMEOUT}s"
    if proc.returncode != 0 or _MARK not in proc.stdout:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        return [], {}, (detail[-1][:160] if detail else f"docker run exited {proc.returncode}")
    head, tail = proc.stdout.split(_MARK, 1)
    distros = [ln.strip() for ln in head.splitlines() if ln.strip()]
    pkgs: dict[str, str] = {}
    for ln in tail.splitlines():
        parts = ln.strip().split(None, 1)
        if len(parts) == 2 and parts[0].startswith("ros-"):
            pkgs[parts[0]] = parts[1]
    return distros, pkgs, None


def _collect_refs(manifest: Manifest, descriptors: dict[str, Descriptor], env: dict[str, str],
                  names: list[str]) -> tuple[dict[str, list[str]], list[str]]:
    """Render each enabled stack's compose; return (image ref -> stack names, per-stack failures)."""
    from .bake import _service_images
    refs: dict[str, list[str]] = {}
    failures: list[str] = []
    for sensor in manifest.select(names, enabled_only=True):
        desc = descriptors[sensor.service]
        proc = run_launcher(sensor, desc, env, launcher_cmd(sensor, desc, "config"),
                            dry_run=False, capture=True)
        compose = None
        if proc.returncode == 0 and (proc.stdout or "").strip():
            try:
                compose = yaml.safe_load(proc.stdout)
            except yaml.YAMLError:
                compose = None
        if not (isinstance(compose, dict) and compose.get("services")):
            failures.append(f"{sensor.name}: launcher `config` produced no compose "
                            f"({(proc.stderr or '').strip()[:120] or f'exit {proc.returncode}'})")
            continue
        for ref in _service_images(compose).values():
            stacks = refs.setdefault(ref, [])
            if sensor.name not in stacks:
                stacks.append(sensor.name)
    return refs, failures


def audit(manifest: Manifest, descriptors: dict[str, Descriptor], env: dict[str, str], *,
          names: list[str] | None = None) -> int:
    if shutil.which("docker") is None:
        eprint("rig image audit: docker not found on PATH — audit inspects images with `docker run`")
        return 1
    refs, failures = _collect_refs(manifest, descriptors, env, names or [])
    if not refs and not failures:
        eprint("rig image audit: no enabled stack resolves any image — nothing to audit")
        return 0

    issues: list[tuple[str, str]] = [(WARN, f) for f in failures]
    distro = manifest.ros.distro
    rmw_pkg = (f"ros-{distro}-{manifest.ros.rmw.replace('_', '-')}"
               if distro and manifest.ros.rmw else None)
    ros_images: list[tuple[str, list[str], dict[str, str]]] = []  # (ref, distro dirs, pkgs)
    distro_bad = rmw_bad = 0
    for ref, stacks in refs.items():
        distros, pkgs, err = _inspect(ref)
        label = f"{ref} [{', '.join(stacks)}]"
        if err:
            issues.append((WARN, f"{label}: uninspectable (no shell/dpkg? unpullable?) — {err}"))
            continue
        if not distros and not pkgs:
            issues.append((INFO, f"{label}: non-ROS image — excluded from the ROS checks"))
            continue
        ros_images.append((ref, distros, pkgs))
        if distro and distros and distro not in distros:
            distro_bad += 1
            issues.append((ERROR, f"{label}: carries ROS distro {distros}, but vehicle.yaml "
                                  f"ros.distro is '{distro}' — rebuild it for the fleet's distro"))
        if distros and not pkgs:
            issues.append((WARN, f"{label}: /opt/ros/{{{', '.join(distros)}}} exists but zero "
                                 f"ros-* dpkg packages — source-built ROS? the package checks "
                                 f"cannot see it"))
        elif rmw_pkg and (not distros or distro in distros) and rmw_pkg not in pkgs:
            rmw_bad += 1
            issues.append((ERROR, f"{label}: declared rmw '{manifest.ros.rmw}' is not installed "
                                  f"({rmw_pkg} missing) — its nodes will run a different RMW than "
                                  f"the fleet graph"))

    # Cross-image agreement: any ros-* package that appears with two different versions is skew —
    # exactly the "two images, two rmw_zenoh_cpp builds, no session" failure this exists to catch.
    by_pkg: dict[str, dict[str, list[str]]] = {}  # pkg -> version -> image refs
    for ref, _, pkgs in ros_images:
        for pkg, ver in pkgs.items():
            by_pkg.setdefault(pkg, {}).setdefault(ver, []).append(ref)
    shared = {p: v for p, v in by_pkg.items() if sum(len(r) for r in v.values()) > 1}
    skewed = {p: v for p, v in shared.items() if len(v) > 1}
    for pkg, versions in sorted(skewed.items()):
        detail = "; ".join(f"{ver} in {', '.join(sorted(refs_))}"
                           for ver, refs_ in sorted(versions.items()))
        issues.append((ERROR, f"version skew: {pkg} — {detail}"))

    if ros_images and distro and not distro_bad:
        issues.append((OK, f"every ROS image carries /opt/ros/{distro}"))
    if ros_images and rmw_pkg and not rmw_bad:
        issues.append((OK, f"{rmw_pkg} present in every matching ROS image"))
    if shared and not skewed:
        issues.append((OK, f"{len(shared)} ros-* package(s) shared across images — versions agree"))

    errors = sum(1 for lvl, _ in issues if lvl == ERROR)
    eprint(f"rig image audit: {manifest.vehicle} — {len(refs)} image ref(s), "
           f"{len(ros_images)} ROS, {errors} error(s)")
    for lvl, msg in issues:
        eprint(f"  [{_SYMBOL[lvl]}] {msg}")
    if not issues:
        eprint("  [✓] nothing to check")
    return 1 if errors else 0
