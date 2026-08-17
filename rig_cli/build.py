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
from pathlib import Path

from .refs import unqualified
from .common import eprint
from .descriptor import Descriptor
from .manifest import Manifest


def _build_cmd(desc: Descriptor, cwd: Path, reg, tag):
    args = [a for a in (reg, tag) if a]  # build-images.sh takes: <registry> [tag]
    script = cwd / desc.build_command
    cmd = ([str(script), *args] if script.exists()
           else ["bash", "-lc", " ".join([desc.build_command, *map(shlex.quote, args)])])
    return cmd, args


def _resolve_build_cwd(service: str, desc: Descriptor, root: Path | None):
    """Where to RUN the build command: the routed repo when it carries the build entrypoint (dev
    checkouts, or vendored dirs that happen to include it) — else the PINNED source checkout from
    rig.lock, because registry installs vendor the launch surface, never the build context
    (`../base/build.sh` siblings, `tools/build-*.sh`). Returns (cwd, note) — cwd None means
    unbuildable, note carries the pointed error."""
    head = shlex.split(desc.build_command)[0]
    if (desc.repo / head).exists():
        return desc.repo, ""
    if "/" not in head and not head.startswith("."):  # a bare PATH command (`make`, `docker`) —
        return desc.repo, ""  # bash -lc resolves it; only repo-relative paths need build context
    packages = {}
    if root is not None:
        from .lock import load_lock
        packages = load_lock(root).get("packages") or {}
    ref = next((r for r, info in packages.items() if (info or {}).get("kind") == "service"
                and unqualified(r) == service), None)
    source = (packages.get(ref) or {}).get("source") if ref else None
    if not source:
        return None, (f"'{head}' not found under {desc.repo} — vendored surfaces carry launch "
                      f"files, not build context, and rig.lock has no source pin for '{service}'; "
                      f"route services.yaml at a full checkout to build it")
    from .install import _fetch_source
    src = _fetch_source(service, source)
    return src, (f"vendored dir has no build context — using the pinned source checkout "
                 f"({str(source.get('rev'))[:12]}…)")


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


def _one_captured(service: str, desc: Descriptor, cwd: Path, reg, tag, distro: str | None):
    """Concurrent worker: run a service's build + mirrors, capturing output. Returns (service, rc, text)."""
    log: list[str] = []
    rc = 0

    def run(cmd, run_cwd=None, env=None) -> int:
        p = subprocess.run(cmd, cwd=run_cwd, env=env, capture_output=True, text=True)
        out = (p.stdout + p.stderr).strip()
        if out:
            log.append(out)
        return p.returncode

    if desc.build_command:
        cmd, args = _build_cmd(desc, cwd, reg, tag)
        log.append(f"$ {desc.build_command} {' '.join(args)}  (cwd={cwd}){_distro_note(distro)}")
        if run(cmd, run_cwd=str(cwd), env=_build_env(distro)):
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
          tag: str | None, dry_run: bool, jobs: int = 1, root: Path | None = None) -> int:
    reg = registry or manifest.image_registry
    tag = tag or manifest.image_tag  # default the build tag to vehicle.yaml images.tag (e.g. jp7)
    services = [s for s in dict.fromkeys(x.service for x in manifest.sensors)  # unique, manifest order
                if (d := descriptors.get(s)) and (d.build_command or d.mirror)]
    if not services:
        eprint("rig build: no in-use service declares `build:` or `mirror:` — nothing to do")
        return 0

    # Resolve each build's cwd up front (may fetch pinned sources — do it before any concurrency).
    rc = 0
    cwds: dict[str, Path] = {}
    for s in list(services):
        d = descriptors[s]
        if not d.build_command:
            cwds[s] = d.repo
            continue
        cwd, note = _resolve_build_cwd(s, d, root)
        if cwd is None:
            eprint(f"rig build: {s}: {note}")
            rc = 1
            services.remove(s)
            continue
        if note:
            eprint(f"  {s}: {note}")
        cwds[s] = cwd

    # ROS_DISTRO is about to be baked into whatever the build commands produce — a rigging that targets
    # a different distro is a wrong image about to happen, so say it HERE, at the moment it matters
    # (doctor raises the same mismatch as an ERROR at the vehicle level).
    distro = manifest.ros.distro
    for s in services:
        d = descriptors[s]
        if distro and d.build_command and d.ros_distro and d.ros_distro != distro:
            eprint(f"rig build: WARNING — {s} declares ros_distro '{d.ros_distro}' but vehicle.yaml "
                   f"ros.distro is '{distro}'; the build gets ROS_DISTRO={distro} and will bake THAT")

    if jobs > 1 and len(services) > 1 and not dry_run:  # concurrent: capture + print grouped per service
        eprint(f"rig build: {len(services)} services, up to {jobs} concurrent (output grouped per service)")
        with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as ex:
            futures = [ex.submit(_one_captured, s, descriptors[s], cwds[s], reg, tag, distro)
                       for s in services]
            for fut in concurrent.futures.as_completed(futures):
                svc, rc1, out = fut.result()
                eprint(f"\n───── {svc} {'✓' if not rc1 else '✗ FAILED'} ─────\n{out}")
                rc |= rc1
        return rc

    for s in services:  # sequential: live-streamed
        desc = descriptors[s]
        if desc.build_command:
            cmd, args = _build_cmd(desc, cwds[s], reg, tag)
            eprint(f"build {s}: {desc.build_command} {' '.join(args)}  (cwd={cwds[s]}){_distro_note(distro)}")
            if not dry_run and subprocess.run(cmd, cwd=str(cwds[s]), env=_build_env(distro)).returncode:
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
