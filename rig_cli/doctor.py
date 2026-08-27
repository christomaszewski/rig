"""Preflight checks — read-only. rig owns these vehicle-wide validations; it never mutates host state.

Levels: ERROR (block bring-up), WARN (proceed, but look), INFO (advisory). `name`-uniqueness and the
config/service cross-checks are enforced earlier in manifest loading; here we cover cross-service
concerns: one ROS distro, launchers present, host-facing port clashes, and coarse resource reminders.
"""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .catalog import ServiceEntry
from .common import load_yaml
from .descriptor import Descriptor
from .manifest import Manifest, stack_summary

ERROR, WARN, INFO, OK = "ERROR", "WARN", "INFO", "OK"
_SYMBOL = {ERROR: "✗", WARN: "!", INFO: "·", OK: "✓"}


@dataclass
class Issue:
    level: str
    message: str


def _get_path(data: dict, path: str):
    """Resolve a dotted config path, supporting a `key[k=v,...]` list selector that matches the first list
    item whose fields all equal the given values (case-insensitively, so `enabled=true` matches the YAML
    bool). E.g. `plugins[name=webrtc-bridge,enabled=true].params.port` resolves only when that plugin is
    enabled — so a disabled plugin's port isn't flagged as a clash."""
    cur = data
    for raw in path.split("."):
        match = re.match(r"^([^\[]+)(?:\[([^\]]+)\])?$", raw)
        if not match:
            return None
        key, sel = match.groups()
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
        if sel is not None:
            if not isinstance(cur, list):
                return None
            conds = [c.split("=", 1) for c in sel.split(",") if "=" in c]
            cur = next(
                (it for it in cur if isinstance(it, dict)
                 and all(str(it.get(k.strip())).lower() == v.strip().lower() for k, v in conds)),
                None,
            )
            if cur is None:
                return None
    return cur


def collect(
    manifest: Manifest, catalog: dict[str, ServiceEntry], descriptors: dict[str, Descriptor]
) -> list[Issue]:
    issues: list[Issue] = []

    vid = f" (id {manifest.vehicle_id})" if manifest.vehicle_id is not None else ""
    issues.append(Issue(OK, f"vehicle '{manifest.vehicle}'{vid} · ROS domain {manifest.ros.domain_id} · "
                            f"{manifest.ros.rmw}"))

    # rmw_zenoh needs a shared zenoh router (one per vehicle) in the infra tier — and the converse:
    # a router on a NON-zenoh fleet is a runtime failure waiting to happen. The router runs `rmw_zenohd`
    # out of the base image, and a base built from this vehicle's `ros.rmw` (RIG_ROS_RMW) installs THAT
    # rmw only — so the image simply has no zenohd in it. Builds clean, dies on `up`.
    _ZENOH_RMW = ("rmw_zenoh", "rmw_zenoh_cpp")
    zenoh_routers = [s.name for s in manifest.sensors
                     if s.tier == "infra" and "zenoh" in s.service and s.enabled]
    if manifest.ros.rmw in _ZENOH_RMW and manifest.sensors:
        if not any(s.tier == "infra" and "zenoh" in s.service for s in manifest.sensors):
            issues.append(Issue(WARN, "ros.rmw is zenoh but no zenoh router in `infra:` — ROS nodes may not "
                                       "discover each other; add a zenoh-router service (or switch RMW)"))
    elif zenoh_routers:
        issues.append(Issue(WARN, f"zenoh router enabled ({', '.join(zenoh_routers)}) but ros.rmw is "
                                  f"'{manifest.ros.rmw}' — the router runs rmw_zenohd from the base image, "
                                  f"which is built for the DECLARED rmw and carries no zenoh; disable the "
                                  f"router (or switch ros.rmw to rmw_zenoh_cpp)"))

    # Autonomy consumes the sensor graph — enabled autonomy with zero enabled sensors is a brain with no eyes.
    autonomy_on = [s.name for s in manifest.sensors if s.tier == "autonomy" and s.enabled]
    if autonomy_on and not any(s.tier == "sensor" and s.enabled for s in manifest.sensors):
        issues.append(Issue(WARN, f"autonomy enabled ({', '.join(autonomy_on)}) but no enabled sensors — "
                                   f"a brain with no eyes; enable the sensor stacks it consumes"))

    # One ROS distro across the vehicle (a shared DDS graph needs it). Vehicle-vs-services disagreement
    # is an ERROR, not a nitpick: `rig build` exports vehicle.yaml's ros.distro as ROS_DISTRO to build
    # commands, so a mismatch means the next `rig build` bakes images (rig-infra's fleet-ros) for a
    # distro the services don't target — the router/session version-match silently broken.
    distros: dict[str, list[str]] = {}
    for svc, desc in descriptors.items():
        if desc.ros_distro:
            distros.setdefault(desc.ros_distro, []).append(svc)
    if len(distros) > 1:
        issues.append(Issue(ERROR, f"mixed ROS distros across services: {dict(distros)} — rig needs one"))
    elif distros:
        only = next(iter(distros))
        if manifest.ros.distro and only != manifest.ros.distro:
            issues.append(
                Issue(ERROR, f"vehicle ros.distro={manifest.ros.distro} but the services target '{only}' "
                             f"— `rig build` bakes ROS_DISTRO={manifest.ros.distro} into built images "
                             f"(fleet-ros); align vehicle.yaml with the riggings (or vice versa)")
            )
        else:
            issues.append(Issue(OK, f"single ROS distro: {only}"))

    # The deployment's ONE base image (images.base, or a `provides: base` service). A provider
    # conflict is an ERROR — rig never guesses which base a fleet builds against; no base at all is
    # only advisory (services then build FROM their own defaults, and per-image rmw/distro skew is
    # possible — `rig image audit` detects it, a shared base prevents it).
    from .build import resolve_base_image
    base_ref, base_origin, base_err = resolve_base_image(manifest, descriptors)
    if base_err:
        issues.append(Issue(ERROR, base_err))
    elif base_ref:
        issues.append(Issue(OK, f"base image: {base_ref} ({base_origin}) -> RIG_BASE_IMAGE"))
    elif any(d.ros_distro for d in descriptors.values()):
        issues.append(Issue(INFO, "no deployment base image — set images.base (or mark one service's "
                                  "build `provides: base`) to build every ROS service FROM one image; "
                                  "builds get no RIG_BASE_IMAGE until then"))

    # The msgs overlay (fleet-ros-msgs = base + the union of the riggings' `msgs:` blocks). rosbag2
    # cannot record a topic whose message package isn't installed in the recorder's image — it logs
    # "unknown type" and KEEPS GOING, so a fleet with custom types silently gets bags missing them.
    # Declarations without the overlay mechanism are that failure waiting to happen: WARN at
    # preflight. Conflicts (one repo pinned at two refs; providers disagreeing) are ERRORs — `rig
    # build` refuses on the same conditions, and `up` blocks here.
    from .build import resolve_msgs_image
    msgs_ref, msgs_origin, msgs_err = resolve_msgs_image(manifest, descriptors)
    msgs_declaring = sorted(s for s, d in descriptors.items() if d.msgs_apt or d.msgs_source)
    if msgs_err:
        issues.append(Issue(ERROR, msgs_err))
    elif msgs_ref:
        issues.append(Issue(OK, f"msgs overlay: {msgs_ref} ({msgs_origin}) -> RIG_MSGS_IMAGE"))
    elif msgs_declaring:
        issues.append(Issue(WARN, f"{', '.join(msgs_declaring)} declare `msgs:` but no base provider "
                                  f"declares build.msgs_overlay — the bag logger records from the "
                                  f"bare base and bags silently miss those types; add "
                                  f"`msgs_overlay: {{command: ../msgs/build-msgs.sh, image: "
                                  f"fleet-ros-msgs}}` to the base provider's build block"))

    # Platform targeting: `platform:` is the host's declared hardware/OS target; a service's
    # build.platforms is its matrix. The declared platform must be IN each in-use matrix (a typo'd
    # platform means composed <tag>-<platform> refs that don't exist); a matrix service with no
    # declared platform falls back to the launcher's host auto-detection (works on the vehicle
    # itself, wrong on a dev box) and pulls unqualified tags. images.tag carrying a platform NAME is
    # the legacy conflation — deprecated, still honored when no platform: is declared.
    matrix_svcs = {s: d for s, d in descriptors.items() if d.build_platforms}
    if manifest.platform:
        for svc, desc in matrix_svcs.items():
            if manifest.platform not in desc.build_platforms:
                issues.append(Issue(ERROR, f"platform '{manifest.platform}' is not in {svc}'s build "
                                           f"matrix {desc.build_platforms} — its composed pull ref "
                                           f"<image>:<tag>-{manifest.platform} cannot exist"))
        if manifest.image_tag and any(manifest.image_tag in d.build_platforms
                                      for d in descriptors.values()):
            issues.append(Issue(WARN, f"images.tag '{manifest.image_tag}' is a platform name while "
                                      f"`platform: {manifest.platform}` is declared — the tag should "
                                      f"be a VERSION now (composed refs would read "
                                      f"'{manifest.image_tag}-{manifest.platform}')"))
        if matrix_svcs and not any(i.level == ERROR and "build matrix" in i.message for i in issues):
            issues.append(Issue(OK, f"platform '{manifest.platform}' -> RIG_TARGET_PLATFORM · composed "
                                    f"<tag>-{manifest.platform} tags for: {', '.join(sorted(matrix_svcs))}"))
    else:
        legacy = {s: d for s, d in descriptors.items()
                  if manifest.image_tag and manifest.image_tag in d.build_platforms}
        if legacy:
            issues.append(Issue(WARN, f"DEPRECATED — images.tag '{manifest.image_tag}' is a platform "
                                      f"name ({', '.join(sorted(legacy))} declare it in build."
                                      f"platforms); declare `platform: {manifest.image_tag}` and let "
                                      f"images.tag carry the version (composed pulls: <tag>-<platform>)"))
        for svc, desc in matrix_svcs.items():
            if svc in legacy:
                continue
            issues.append(Issue(WARN, f"{svc} declares a platform matrix {desc.build_platforms} but "
                                      f"no `platform:` is declared — rendering falls back to the "
                                      f"launcher's host auto-detection"
                                      + (f" ({desc.platform_auto_detect})" if desc.platform_auto_detect
                                         else "")
                                      + " and pulls carry no platform suffix; set `platform:` in "
                                        "vehicle.yaml (or /etc/rig/vehicle.local.yaml)"))

    # Launchers present + executable.
    for svc, desc in descriptors.items():
        lp = desc.launcher_path
        if not lp.exists():
            issues.append(Issue(ERROR, f"{svc}: launcher missing: {lp}"))
        elif not lp.stat().st_mode & 0o111:
            issues.append(Issue(WARN, f"{svc}: launcher not executable: {lp}"))

    # ROS stacks (sensor AND autonomy tiers) namespace a node by the instance name, and ROS 2 names allow
    # only [A-Za-z_][A-Za-z0-9_]* (no hyphens, no leading digit). Flag a name that would be an invalid namespace.
    _ros_name = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
    for sensor in manifest.sensors:
        desc = descriptors.get(sensor.service)
        if sensor.tier in ("sensor", "autonomy") and desc and desc.ros_distro and not _ros_name.match(sensor.name):
            issues.append(Issue(WARN, f"'{sensor.name}' ({sensor.service}) isn't a valid ROS 2 name (only "
                                       f"letters/digits/underscores, no hyphens) — the ROS node namespaced by "
                                       f"it will fail; rename to e.g. '{sensor.name.replace('-', '_')}'"))

    # Host-facing port clashes (only for services that declare host_ports in their rigging.yaml).
    ports: dict[int, list[str]] = {}
    for sensor in manifest.sensors:
        desc = descriptors.get(sensor.service)
        if not desc or not desc.host_ports:
            continue
        cfg = load_yaml(sensor.config)
        for path in desc.host_ports:
            value = _get_path(cfg, path)
            if isinstance(value, int):
                ports.setdefault(value, []).append(sensor.name)
    for port, owners in sorted(ports.items()):
        if len(owners) > 1:
            issues.append(Issue(ERROR, f"host port {port} claimed by multiple stacks: {owners}"))

    # Coarse resource reminders (rig treats driver configs as opaque, so this is advisory).
    cameras = [s.name for s in manifest.sensors if s.service == "camera-service" and s.enabled]
    if len(cameras) >= 2:
        issues.append(
            Issue(
                INFO,
                f"{len(cameras)} camera stacks enabled ({', '.join(cameras)}) — check the /dev/shm and "
                f"NVENC session budgets (≈frame_size×8 of shm per endpoint; Orin has finite encoders)",
            )
        )

    if shutil.which("docker") is None:
        issues.append(Issue(WARN, "docker not found on PATH — bring-up/status will fail"))

    try:  # unpublished authoring branches sitting in the registry caches — advisory only
        from .publish import pending_count
        pending = pending_count()
        if pending:
            issues.append(Issue(INFO, f"{pending} unpublished authoring branch(es) in the "
                                      f"registry caches — rig registry pending"))
    except Exception:  # doctor must never break on registry state
        pass

    return issues


def run(manifest: Manifest, catalog: dict[str, ServiceEntry], descriptors: dict[str, Descriptor],
        *, deep: bool = False) -> int:
    from .common import eprint

    issues = collect(manifest, catalog, descriptors)
    errors = sum(1 for i in issues if i.level == ERROR)
    eprint(f"rig doctor: {manifest.vehicle} — {stack_summary(manifest.sensors)}, {errors} error(s)")
    for issue in issues:
        eprint(f"  [{_SYMBOL[issue.level]}] {issue.message}")
    if not issues:
        eprint("  [✓] no issues")

    if deep:  # composition checked above; now certify each launcher (the component-level contract)
        import os

        from . import certify

        for service, desc in descriptors.items():
            sensor = next((s for s in manifest.sensors if s.service == service and s.enabled), None)
            if sensor is None:
                continue
            e, _ = certify.report(f"{service} (via {sensor.name})",
                                  certify.certify_target(desc, sensor.config, dict(os.environ)))
            errors += e
    return 1 if errors else 0
