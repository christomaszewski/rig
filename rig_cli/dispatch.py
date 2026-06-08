"""Invoke a service's launcher for a sensor: build the command, inject fleet ROS env, run (or dry-run).

rig adds NOTHING per-stack here — it shells out to `<launcher> <config> <verb-args>` in the service repo,
exporting only the fleet-wide ROS env. The launcher owns parsing, params rendering, profiles, devices,
volumes, and compose.
"""
from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass

from . import RigError
from .common import eprint
from .descriptor import Descriptor
from .manifest import Manifest, Sensor


def fleet_env(manifest: Manifest) -> dict[str, str]:
    """The process env with which to call every launcher: inherit + pin the shared DDS graph."""
    env = dict(os.environ)
    env["ROS_DOMAIN_ID"] = str(manifest.ros.domain_id)
    env["RMW_IMPLEMENTATION"] = manifest.ros.rmw
    if manifest.vehicle_id is not None:
        env["VEHICLE_ID"] = str(manifest.vehicle_id)  # vehicle identity for containers that want it
    if manifest.image_registry:
        env["RIG_IMAGE_REGISTRY"] = manifest.image_registry  # each compose prefixes its repo:tag with this
    return env


def launcher_cmd(sensor: Sensor, desc: Descriptor, verb: str, extra: list[str] | None = None) -> list[str]:
    launcher = desc.launcher_path
    if not launcher.exists():
        raise RigError(
            f"{sensor.service}: launcher not found: {launcher} (is the service repo checked out?)"
        )
    return [str(launcher), str(sensor.config), *desc.verb_args(verb), *(extra or [])]


@dataclass
class Outcome:
    sensor: Sensor
    returncode: int


def run(
    sensor: Sensor,
    desc: Descriptor,
    env: dict[str, str],
    cmd: list[str],
    *,
    dry_run: bool,
    capture: bool = False,
) -> subprocess.CompletedProcess:
    pretty = " ".join(shlex.quote(part) for part in cmd)
    if dry_run:
        envline = (f"ROS_DOMAIN_ID={env['ROS_DOMAIN_ID']} "
                   f"RMW_IMPLEMENTATION={env['RMW_IMPLEMENTATION']}")
        if env.get("VEHICLE_ID"):
            envline += f" VEHICLE_ID={env['VEHICLE_ID']}"
        if env.get("RIG_IMAGE_REGISTRY"):
            envline += f" RIG_IMAGE_REGISTRY={env['RIG_IMAGE_REGISTRY']}"
        eprint(f"  {sensor.name} [{sensor.service}]  (cwd={desc.repo})")
        eprint(f"    {envline} \\")
        eprint(f"    {pretty}")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    if not capture:
        eprint(f"==> {sensor.name} [{sensor.service}]: {pretty}")
    return subprocess.run(cmd, env=env, cwd=str(desc.repo), capture_output=capture, text=True)


def run_verb(
    pairs: list[tuple[Sensor, Descriptor]],
    env: dict[str, str],
    verb: str,
    *,
    extra: list[str] | None = None,
    dry_run: bool = False,
) -> list[Outcome]:
    """Run a streaming verb (up/down/config/logs) across sensors in the given order."""
    outcomes: list[Outcome] = []
    for sensor, desc in pairs:
        cmd = launcher_cmd(sensor, desc, verb, extra)
        result = run(sensor, desc, env, cmd, dry_run=dry_run)
        outcomes.append(Outcome(sensor, result.returncode))
    return outcomes


def purge_external_volumes(sensor: Sensor, desc: Descriptor, *, dry_run: bool) -> None:
    """Remove a service's declared external volumes — FINAL teardown only. `docker volume rm` refuses a
    volume that's still in use, which is exactly the safety we want (a consumer may still be attached)."""
    for pattern in desc.external_volumes:
        volume = pattern.format(name=sensor.name)
        cmd = ["docker", "volume", "rm", volume]
        if dry_run:
            eprint(f"    purge: {' '.join(cmd)}")
            continue
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            eprint(f"    purged volume {volume}")
        else:
            eprint(f"    kept volume {volume} ({result.stderr.strip() or 'in use or absent'})")
