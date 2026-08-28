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
from .manifest import Manifest, Sensor, project_name


def fleet_env(manifest: Manifest, descriptors: dict[str, Descriptor] | None = None) -> dict[str, str]:
    """The process env with which to call every launcher: inherit + pin the shared DDS graph.

    `descriptors` (when the caller has them) lets the deployment's base image resolve from a
    `provides: base` service, not just vehicle.yaml images.base — a compose that RUNS the base
    directly (zenoh-router on fleet-ros) reads `${RIG_BASE_IMAGE}`. The msgs-overlay image
    (RIG_MSGS_IMAGE) resolves the same way: exported only when services declare `msgs:` AND a base
    provider declares the overlay build — the bag logger's compose prefers it over the bare base,
    so exporting it is all it takes for bags to carry the fleet's custom types. A provider conflict
    resolves to NOTHING here (the var is popped): doctor reports it as the ERROR, and `up` blocks
    on that."""
    if getattr(manifest, "missing_identity", ()):  # belt — _load gates first
        from .manifest import require_identity
        require_identity(manifest, what="fleet env")
    base_ref = manifest.image_base
    msgs_ref = None
    if descriptors is not None:
        from .build import resolve_base_image, resolve_msgs_image
        base_ref, _, _ = resolve_base_image(manifest, descriptors)
        msgs_ref, _, _ = resolve_msgs_image(manifest, descriptors)
    env = dict(os.environ)
    env["ROS_DOMAIN_ID"] = str(manifest.ros.domain_id)
    env["RMW_IMPLEMENTATION"] = manifest.ros.rmw
    # rig OWNS these vars: when the manifest doesn't define one, POP any inherited value — a leaked
    # VEHICLE_ID from the shell would rename compose projects out from under the run/verify machinery,
    # and leaked registry/tag/base/data values would silently redirect pulls or outputs.
    for key, value in (("VEHICLE_ID", manifest.vehicle_id),
                       ("RIG_IMAGE_REGISTRY", manifest.image_registry),
                       ("RIG_IMAGE_TAG", manifest.image_tag),
                       ("RIG_BASE_IMAGE", base_ref),
                       ("RIG_MSGS_IMAGE", msgs_ref),
                       ("RIG_TARGET_PLATFORM", manifest.platform),
                       ("RIG_DATA_DIR", manifest.data_dir),
                       # the replay channel: None here on EVERY verb (popped) — replay.py alone
                       # sets them on its env copy after this returns (per-invocation, never
                       # manifest state)
                       ("RIG_REPLAY_SOURCE", None), ("RIG_REPLAY_TOPICS", None),
                       ("RIG_REPLAY_EXCLUDE", None), ("RIG_SIM_TIME", None)):
        if value not in (None, ""):
            env[key] = str(value)
        else:
            env.pop(key, None)
    # The operator-extensible tail on this channel: vehicle.yaml/local `env:` (already
    # interpolated; collisions with the rig-owned keys above were rejected at manifest load).
    env.update({key: str(value) for key, value in manifest.extra_env.items()})
    return env


def service_env(env: dict[str, str], desc: Descriptor) -> dict[str, str]:
    """The per-SERVICE view of the fleet env, applied to every launcher invocation (up/down/config/
    pull/status, bake's compose capture, certify). When the vehicle declares a platform
    (RIG_TARGET_PLATFORM is exported):

    - the service's declared `platform.override_env` (e.g. CAM_PLATFORM) is set to the same value —
      the vehicle.yaml declaration is authoritative; a launcher's own auto-detect stays the
      standalone/no-rig fallback (declared-wins: bake renders on dev boxes that aren't the target);
    - a service with a build MATRIX (`build.platforms`) gets the COMPOSED image tag,
      `RIG_IMAGE_TAG=<tag>-<platform>` (bare `<platform>` when no tag is set), so its compose pulls
      e.g. cam-core:v1.3.0-jp7 — `images.tag` itself means VERSION only. jp6/jp7 are both arm64
      (userspace differs), so the tag must carry the platform; multi-arch manifests can't.

    No platform declared: the env passes through untouched (the legacy platform-valued
    `images.tag: jp7` keeps behaving exactly as before).
    """
    platform = env.get("RIG_TARGET_PLATFORM")
    if not platform:
        return env
    out = dict(env)
    if desc.platform_override_env:
        out[desc.platform_override_env] = platform
    if desc.build_platforms:
        tag = env.get("RIG_IMAGE_TAG")
        out["RIG_IMAGE_TAG"] = f"{tag}-{platform}" if tag else platform
    return out


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
    # rig owns the compose project name (containers = <project>-<compose-service>-N). A launcher honors
    # COMPOSE_PROJECT_NAME unless it overrides with `-p` (see the launcher contract). The platform
    # routing (override_env mirror + composed per-platform tag) is per-service too.
    env = {**service_env(env, desc), "COMPOSE_PROJECT_NAME": project_name(sensor.name, env.get("VEHICLE_ID"))}
    pretty = " ".join(shlex.quote(part) for part in cmd)
    if dry_run:
        envline = (f"COMPOSE_PROJECT_NAME={env['COMPOSE_PROJECT_NAME']} "
                   f"ROS_DOMAIN_ID={env['ROS_DOMAIN_ID']} RMW_IMPLEMENTATION={env['RMW_IMPLEMENTATION']}")
        for key in ("VEHICLE_ID", "RIG_IMAGE_REGISTRY", "RIG_IMAGE_TAG", "RIG_BASE_IMAGE",
                    "RIG_MSGS_IMAGE", "RIG_TARGET_PLATFORM", "RIG_DATA_DIR",
                    "RIG_REPLAY_SOURCE", "RIG_REPLAY_TOPICS", "RIG_REPLAY_EXCLUDE",
                    "RIG_SIM_TIME", desc.platform_override_env or ""):
            if key and env.get(key):
                envline += f" {key}={env[key]}"
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
    """Run a streaming verb (up/down/config/logs/pull) across sensors in the given order."""
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
