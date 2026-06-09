"""rig build — get each service's images into the target registry, so an offline vehicle can pull them and
`rig bake --registry` can digest-pin them. Two recipes a service declares in `rigging.yaml`:

    build: tools/build-images.sh     # build + push the service's OWN images; rig runs `<cmd> <registry> [tag]`
    mirror: [eclipse/zenoh:latest]   # copy these existing/third-party images into <registry>/<image>

A service declares either, both, or neither (neither ⇒ images are assumed already in the registry, or pulled
from upstream with internet). Specifying a full image ref directly is the per-service `${<SVC>_IMAGE}`
override (orthogonal — handled by the launcher, not here).
"""
from __future__ import annotations

import shlex
import subprocess

from .common import eprint
from .descriptor import Descriptor
from .manifest import Manifest


def build(manifest: Manifest, descriptors: dict[str, Descriptor], *, registry: str | None,
          tag: str | None, dry_run: bool) -> int:
    reg = registry or manifest.image_registry
    tag = tag or manifest.image_tag  # default the build tag to vehicle.yaml images.tag (e.g. jp7)
    services = list(dict.fromkeys(s.service for s in manifest.sensors))  # manifest order (infra first), deduped
    rc = 0
    did = 0

    for service in services:
        desc = descriptors.get(service)
        if desc is None:
            continue

        if desc.build_command:
            did += 1
            args = [a for a in (reg, tag) if a]  # build-images.sh takes: <registry> [tag]
            script = desc.repo / desc.build_command
            cmd = ([str(script), *args] if script.exists()
                   else ["bash", "-lc", " ".join([desc.build_command, *map(shlex.quote, args)])])
            eprint(f"build {service}: {desc.build_command} {' '.join(args)}  (cwd={desc.repo})")
            if not dry_run and subprocess.run(cmd, cwd=str(desc.repo)).returncode:
                rc = 1
                eprint(f"  build {service} FAILED")

        for img in desc.mirror:
            did += 1
            if not reg:
                eprint(f"mirror {service}: {img} — no registry (pass --registry or set images.registry); skipped")
                continue
            target = f"{reg}/{img}"
            eprint(f"mirror {service}: {img} -> {target}")
            if dry_run:
                continue
            # pull -> tag -> push: honors the docker daemon's insecure-registries (a plain-HTTP local
            # registry), unlike `buildx imagetools`. Mirrors the dev host's arch — right for a single-arch
            # (arm64 Jetson) fleet; for a multi-arch index against a TLS registry, use skopeo/crane.
            steps = [["docker", "pull", img], ["docker", "tag", img, target], ["docker", "push", target]]
            if any(subprocess.run(s).returncode for s in steps):
                rc = 1
                eprint(f"  mirror {img} FAILED")

    if did == 0:
        eprint("rig build: no in-use service declares `build:` or `mirror:` — nothing to do")
    return rc
