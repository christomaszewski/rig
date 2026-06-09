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

    # rmw_zenoh needs a shared zenoh router (one per vehicle) in the infra tier.
    if manifest.ros.rmw in ("rmw_zenoh", "rmw_zenoh_cpp") and manifest.sensors:
        if not any(s.tier == "infra" and "zenoh" in s.service for s in manifest.sensors):
            issues.append(Issue(WARN, "ros.rmw is zenoh but no zenoh router in `infra:` — ROS nodes may not "
                                       "discover each other; add a zenoh-router service (or switch RMW)"))

    # One ROS distro across the vehicle (a shared DDS graph needs it).
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
                Issue(WARN, f"vehicle ros.distro={manifest.ros.distro} but services target '{only}'")
            )
        else:
            issues.append(Issue(OK, f"single ROS distro: {only}"))

    # Launchers present + executable.
    for svc, desc in descriptors.items():
        lp = desc.launcher_path
        if not lp.exists():
            issues.append(Issue(ERROR, f"{svc}: launcher missing: {lp}"))
        elif not lp.stat().st_mode & 0o111:
            issues.append(Issue(WARN, f"{svc}: launcher not executable: {lp}"))

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

    return issues


def run(manifest: Manifest, catalog: dict[str, ServiceEntry], descriptors: dict[str, Descriptor]) -> int:
    from .common import eprint

    issues = collect(manifest, catalog, descriptors)
    errors = sum(1 for i in issues if i.level == ERROR)
    eprint(f"rig doctor: {manifest.vehicle} — {stack_summary(manifest.sensors)}, {errors} error(s)")
    for issue in issues:
        eprint(f"  [{_SYMBOL[issue.level]}] {issue.message}")
    if not issues:
        eprint("  [✓] no issues")
    return 1 if errors else 0
