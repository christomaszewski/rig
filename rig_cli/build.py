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


def _mirror_steps(img: str, target: str):
    # pull -> tag -> push honors the daemon's insecure-registries (a plain-HTTP local registry).
    return [["docker", "pull", img], ["docker", "tag", img, target], ["docker", "push", target]]


def _one_captured(service: str, desc: Descriptor, reg, tag):
    """Concurrent worker: run a service's build + mirrors, capturing output. Returns (service, rc, text)."""
    log: list[str] = []
    rc = 0

    def run(cmd, cwd=None) -> int:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
        out = (p.stdout + p.stderr).strip()
        if out:
            log.append(out)
        return p.returncode

    if desc.build_command:
        cmd, args = _build_cmd(desc, reg, tag)
        log.append(f"$ {desc.build_command} {' '.join(args)}  (cwd={desc.repo})")
        if run(cmd, cwd=str(desc.repo)):
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

    rc = 0
    if jobs > 1 and len(services) > 1 and not dry_run:  # concurrent: capture + print grouped per service
        eprint(f"rig build: {len(services)} services, up to {jobs} concurrent (output grouped per service)")
        with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as ex:
            futures = [ex.submit(_one_captured, s, descriptors[s], reg, tag) for s in services]
            for fut in concurrent.futures.as_completed(futures):
                svc, rc1, out = fut.result()
                eprint(f"\n───── {svc} {'✓' if not rc1 else '✗ FAILED'} ─────\n{out}")
                rc |= rc1
        return rc

    for s in services:  # sequential: live-streamed
        desc = descriptors[s]
        if desc.build_command:
            cmd, args = _build_cmd(desc, reg, tag)
            eprint(f"build {s}: {desc.build_command} {' '.join(args)}  (cwd={desc.repo})")
            if not dry_run and subprocess.run(cmd, cwd=str(desc.repo)).returncode:
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
