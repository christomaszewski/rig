"""rig build — get each service's images into the target registry, so an offline vehicle can pull them and
`rig bake --registry` can digest-pin them. Two recipes a service declares in `rigging.yaml`:

    build: tools/build-images.sh     # build + push the service's OWN images; rig runs `<cmd> <registry> [tag]`
    mirror: [eclipse/zenoh:latest]   # copy these existing/third-party images into <registry>/<image>

Work is per unique *service* (two camera instances build the service once). A service declares either, both,
or neither (neither ⇒ images are assumed already in the registry, or pulled from upstream with internet).
Specifying a full image ref directly is the per-service `${<SVC>_IMAGE}` override (handled by the launcher).
"""
from __future__ import annotations

import concurrent.futures
import os
import shlex
import subprocess

from .common import eprint
from .descriptor import Descriptor
from .manifest import Manifest


def _build_cmd(desc: Descriptor, reg, tag):
    args = [a for a in (reg, tag) if a]  # build-images.sh takes: <registry> [tag]
    script = desc.repo / desc.build_command
    cmd = ([str(script), *args] if script.exists()
           else ["bash", "-lc", " ".join([desc.build_command, *map(shlex.quote, args)])])
    return cmd, args


def _build_env(distro: str | None):
    """Env for a service's build command: vehicle.yaml `ros.distro` rides along as ROS_DISTRO, so a
    base-image build (rig-infra's fleet-ros) bakes the SAME distro the vehicle declares — the
    router/session version-match must not depend on the operator remembering an env var. None (no
    declared distro) inherits the caller's env untouched."""
    return {**os.environ, "ROS_DISTRO": distro} if distro else None


def _distro_note(distro: str | None) -> str:
    return f" ROS_DISTRO={distro}" if distro else ""


def _mirror_steps(img: str, target: str):
    # pull -> tag -> push honors the daemon's insecure-registries (a plain-HTTP local registry).
    return [["docker", "pull", img], ["docker", "tag", img, target], ["docker", "push", target]]


def _one_captured(service: str, desc: Descriptor, reg, tag, distro: str | None):
    """Concurrent worker: run a service's build + mirrors, capturing output. Returns (service, rc, text)."""
    log: list[str] = []
    rc = 0

    def run(cmd, cwd=None, env=None) -> int:
        p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
        out = (p.stdout + p.stderr).strip()
        if out:
            log.append(out)
        return p.returncode

    if desc.build_command:
        cmd, args = _build_cmd(desc, reg, tag)
        log.append(f"$ {desc.build_command} {' '.join(args)}  (cwd={desc.repo}){_distro_note(distro)}")
        if run(cmd, cwd=str(desc.repo), env=_build_env(distro)):
            rc = 1
            log.append("  build FAILED")
    for img in desc.mirror:
        if not reg:
            log.append(f"mirror {img}: no registry; skipped")
            continue
        target = f"{reg}/{img}"
        log.append(f"mirror {img} -> {target}")
        for step in _mirror_steps(img, target):
            if run(step):
                rc = 1
                log.append(f"  mirror {img} FAILED")
                break
    return service, rc, "\n".join(log)


def build(manifest: Manifest, descriptors: dict[str, Descriptor], *, registry: str | None,
          tag: str | None, dry_run: bool, jobs: int = 1) -> int:
    reg = registry or manifest.image_registry
    tag = tag or manifest.image_tag  # default the build tag to vehicle.yaml images.tag (e.g. jp7)
    services = [s for s in dict.fromkeys(x.service for x in manifest.sensors)  # unique, manifest order
                if (d := descriptors.get(s)) and (d.build_command or d.mirror)]
    if not services:
        eprint("rig build: no in-use service declares `build:` or `mirror:` — nothing to do")
        return 0

    # ROS_DISTRO is about to be baked into whatever the build commands produce — a rigging that targets
    # a different distro is a wrong image about to happen, so say it HERE, at the moment it matters
    # (doctor raises the same mismatch as an ERROR at the vehicle level).
    distro = manifest.ros.distro
    for s in services:
        d = descriptors[s]
        if distro and d.build_command and d.ros_distro and d.ros_distro != distro:
            eprint(f"rig build: WARNING — {s} declares ros_distro '{d.ros_distro}' but vehicle.yaml "
                   f"ros.distro is '{distro}'; the build gets ROS_DISTRO={distro} and will bake THAT")

    rc = 0
    if jobs > 1 and len(services) > 1 and not dry_run:  # concurrent: capture + print grouped per service
        eprint(f"rig build: {len(services)} services, up to {jobs} concurrent (output grouped per service)")
        with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as ex:
            futures = [ex.submit(_one_captured, s, descriptors[s], reg, tag, distro) for s in services]
            for fut in concurrent.futures.as_completed(futures):
                svc, rc1, out = fut.result()
                eprint(f"\n───── {svc} {'✓' if not rc1 else '✗ FAILED'} ─────\n{out}")
                rc |= rc1
        return rc

    for s in services:  # sequential: live-streamed
        desc = descriptors[s]
        if desc.build_command:
            cmd, args = _build_cmd(desc, reg, tag)
            eprint(f"build {s}: {desc.build_command} {' '.join(args)}  (cwd={desc.repo}){_distro_note(distro)}")
            if not dry_run and subprocess.run(cmd, cwd=str(desc.repo), env=_build_env(distro)).returncode:
                rc = 1
                eprint(f"  build {s} FAILED")
        for img in desc.mirror:
            if not reg:
                eprint(f"mirror {s}: {img} — no registry (pass --registry or set images.registry); skipped")
                continue
            target = f"{reg}/{img}"
            eprint(f"mirror {s}: {img} -> {target}")
            if not dry_run and any(subprocess.run(st).returncode for st in _mirror_steps(img, target)):
                rc = 1
                eprint(f"  mirror {img} FAILED")
    return rc
