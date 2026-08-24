"""rig build — get each service's images into the target registry, so an offline vehicle can pull them and
`rig bake --registry` can digest-pin them. Two recipes a service declares in `rigging.yaml`:

    build: tools/build-images.sh     # build + push the service's OWN images; rig runs `<cmd> <registry> [tag]`
    mirror: [eclipse/zenoh:latest]   # copy these existing/third-party images into <registry>/<image>

Work is per unique *service* (two camera instances build the service once). A service declares either, both,
or neither (neither ⇒ images are assumed already in the registry, or pulled from upstream with internet).
Specifying a full image ref directly is the per-service `${<SVC>_IMAGE}` override (handled by the launcher).

Extras a build command may honor from the env (all optional, all rig-owned — set-or-popped so shell
leakage can't flip them): RIG_BASE_IMAGE (the deployment's one base image — FROM it; see
resolve_base_image below), RIG_BUILD_NO_CACHE=1 (`rig build --no-cache` — pass e.g.
`docker build ${RIG_BUILD_NO_CACHE:+--no-cache}` to force a full rebuild), and RIG_ROS_RMW
(vehicle.yaml `ros.rmw` — install THIS rmw, so `rig image audit`'s rmw check has a prevention
counterpart instead of flagging an image the builder was never told about).
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


def resolve_base_image(manifest: Manifest, descriptors: dict[str, Descriptor], *,
                       registry: str | None = None, tag: str | None = None,
                       platform: str | None = None, base: str | None = None):
    """The deployment's ONE base image, as (ref, origin, error). Precedence: an explicit ref
    (`--base-image` / vehicle.yaml `images.base` — the operator's declaration, used verbatim) beats
    any `provides: base` service, whose ref rig composes exactly like the pull side:
    `<registry>/<build.images[0]>:<tag>` (platform-composed for a matrix provider). Several
    providers naming the SAME image (rig-infra's fleet-ros pattern — services sharing one
    ../base/build.sh) are one base; providers naming DIFFERENT images are ambiguous —
    (None, None, error), never a manifest-order guess. No base at all is fine: (None, None, None)."""
    explicit = base or manifest.image_base
    if explicit:
        return explicit, "images.base", None
    providers: dict[str, list[str]] = {}  # base image repo -> the services providing it
    for svc, desc in descriptors.items():
        if desc.build_provides == "base":
            providers.setdefault(desc.build_images[0], []).append(svc)
    if not providers:
        return None, None, None
    if len(providers) > 1:
        detail = "; ".join(f"'{repo}' from {', '.join(sorted(svcs))}"
                           for repo, svcs in sorted(providers.items()))
        return None, None, (f"base-image providers disagree: {detail} — set images.base to choose, "
                            f"or drop `provides: base` from one")
    repo, svcs = next(iter(providers.items()))
    # Agreeing on the image NAME is not agreeing on the base: the composed tag is a function of the
    # provider's own build matrix, so a matrix declared by only SOME of them would compose
    # `<tag>-<platform>` or not depending on which descriptor happens to come first — the
    # manifest-order guess this function exists to refuse. (Script identity is the other axis;
    # build() checks it where the resolved cwds are known.)
    matrices = {tuple(descriptors[s].build_platforms) for s in svcs}
    if len(matrices) > 1:
        detail = "; ".join(f"{s}: {descriptors[s].build_platforms or '(none)'}" for s in sorted(svcs))
        return None, None, (f"base-image providers for '{repo}' declare different build.platforms "
                            f"({detail}) — the composed base tag would depend on manifest order; "
                            f"align the riggings, or set images.base to pin the ref")
    reg = registry or manifest.image_registry
    composed = _service_tag(descriptors[svcs[0]], tag or manifest.image_tag,
                            platform or manifest.platform)
    ref = (f"{reg}/{repo}" if reg else repo) + (f":{composed}" if composed else "")
    return ref, f"provided by {', '.join(sorted(svcs))}", None


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


def _build_env(distro: str | None, desc: Descriptor | None = None, platform: str | None = None,
               base_image: str | None = None, no_cache: bool = False, rmw: str | None = None):
    """Env for a service's build command: vehicle.yaml `ros.distro` rides along as ROS_DISTRO, so a
    base-image build (fleet-ros) bakes the SAME distro the vehicle declares — the router/session
    version-match must not depend on the operator remembering an env var. (ROS_DISTRO is a
    conventional ROS var, not a rig-owned one: with no distro declared, an inherited shell value
    stays.) A declared platform rides the same way for platform-sensitive services
    (RIG_TARGET_PLATFORM + the service's own override_env), so a matrix build selects the DECLARED
    variant, not the build box's. The rig-owned build channel — RIG_BASE_IMAGE (FROM this),
    RIG_BUILD_NO_CACHE (full rebuild), RIG_ROS_RMW (install this rmw), RIG_TARGET_PLATFORM — is
    set-or-POPPED, matching fleet_env: a value leaked from the caller's shell must not silently
    redirect FROM lines, flip caching, install the wrong rmw, or retarget a platform build.
    RIG_ROS_RMW is rig-owned rather than the conventional RMW_IMPLEMENTATION precisely because that
    name is exported in most ROS shells: a dev box's `.bashrc` must not decide what a fleet image
    contains. (The service's own override_env is NOT popped — operators use it standalone.)"""
    env = dict(os.environ)
    if distro:
        env["ROS_DISTRO"] = distro
    plat_active = platform and desc is not None and (desc.build_platforms
                                                    or desc.platform_override_env)
    for key, value in (("RIG_BASE_IMAGE", base_image),
                       ("RIG_BUILD_NO_CACHE", "1" if no_cache else None),
                       ("RIG_ROS_RMW", rmw),
                       ("RIG_TARGET_PLATFORM", platform if plat_active else None)):
        if value:
            env[key] = value
        else:
            env.pop(key, None)
    if plat_active and desc.platform_override_env:
        env[desc.platform_override_env] = platform
    return env


def _service_tag(desc: Descriptor, tag: str | None, platform: str | None) -> str | None:
    """The tag arg a service's build command gets: for a matrix service with a declared platform,
    the COMPOSED `<tag>-<platform>` (bare `<platform>` with no tag) — push exactly what the vehicle's
    composed pull ref will ask for. Mirrors dispatch.service_env's pull-side composition."""
    if desc.build_platforms and platform:
        return f"{tag}-{platform}" if tag else platform
    return tag


def _env_note(distro: str | None, platform: str | None = None, base: str | None = None,
              no_cache: bool = False, rmw: str | None = None) -> str:
    parts = [f"{k}={v}" for k, v in (("ROS_DISTRO", distro), ("RIG_ROS_RMW", rmw),
                                     ("RIG_TARGET_PLATFORM", platform),
                                     ("RIG_BASE_IMAGE", base),
                                     ("RIG_BUILD_NO_CACHE", "1" if no_cache else None)) if v]
    return (" " + " ".join(parts)) if parts else ""


def _script_identity(script: Path):
    """What makes two base-build scripts ONE build: the resolved path — widened to the git tree
    hash of the script's directory when that directory is a CLEAN checkout, so identical content
    reached through two pinned clones (each registry install used to fetch its own copy of the
    collection repo; revs may still differ across service releases) dedupes to one build. This
    leans on a contract the providers must keep: a base build script's directory IS its whole
    build context (rig-infra's ``base/`` — Dockerfile + build.sh, self-contained). Anything short
    of a provably clean checkout (not git, dirty/untracked files, git errors) falls back to path
    identity — failing toward REFUSING two builds, never toward skipping one that might differ."""
    def git(*args):
        return subprocess.run(["git", "-C", str(script.parent), *args],
                              capture_output=True, text=True)
    status = git("status", "--porcelain", "--", ".")
    if status.returncode != 0 or status.stdout.strip():
        return ("path", str(script))
    tree = git("rev-parse", "HEAD:./")  # tree hash of the script's directory at HEAD
    if tree.returncode != 0:
        return ("path", str(script))
    return ("tree", script.name, tree.stdout.strip())


def _script_rev(script: Path) -> str:
    """`` (rev …)`` detail for the racing-providers error — names WHICH pins to align."""
    p = subprocess.run(["git", "-C", str(script.parent), "rev-parse", "--short", "HEAD"],
                       capture_output=True, text=True)
    return f" (rev {p.stdout.strip()})" if p.returncode == 0 else ""


def _mirror_steps(img: str, target: str):
    # pull -> tag -> push honors the daemon's insecure-registries (a plain-HTTP local registry).
    return [["docker", "pull", img], ["docker", "tag", img, target], ["docker", "push", target]]


def _one_captured(service: str, desc: Descriptor, cwd: Path, reg, tag, distro: str | None,
                  platform: str | None, base_image: str | None = None, no_cache: bool = False,
                  skip_note: str | None = None, rmw: str | None = None):
    """Concurrent worker: run a service's build + mirrors, capturing output. Returns (service, rc, text)."""
    log: list[str] = []
    rc = 0

    def run(cmd, run_cwd=None, env=None) -> int:
        p = subprocess.run(cmd, cwd=run_cwd, env=env, capture_output=True, text=True)
        out = (p.stdout + p.stderr).strip()
        if out:
            log.append(out)
        return p.returncode

    if desc.build_command and skip_note:
        log.append(f"build skipped: {skip_note}")
    elif desc.build_command:
        cmd, args = _build_cmd(desc, cwd, reg, _service_tag(desc, tag, platform))
        note = _env_note(distro, platform if desc.build_platforms else None, base_image, no_cache,
                         rmw)
        log.append(f"$ {desc.build_command} {' '.join(args)}  (cwd={cwd}){note}")
        if run(cmd, run_cwd=str(cwd),
               env=_build_env(distro, desc, platform, base_image, no_cache, rmw)):
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
          tag: str | None, dry_run: bool, jobs: int = 1, root: Path | None = None,
          platform: str | None = None, no_cache: bool = False,
          base_image: str | None = None) -> int:
    reg = registry or manifest.image_registry
    tag = tag or manifest.image_tag  # default the build tag to vehicle.yaml images.tag (a version)
    platform = platform or manifest.platform  # matrix services build/push <tag>-<platform>
    base_ref, base_origin, base_err = resolve_base_image(
        manifest, descriptors, registry=reg, tag=tag, platform=platform, base=base_image)
    if base_err:
        eprint(f"rig build: {base_err}")
        return 1
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
    rmw = manifest.ros.rmw
    for s in services:
        d = descriptors[s]
        if distro and d.build_command and d.ros_distro and d.ros_distro != distro:
            eprint(f"rig build: WARNING — {s} declares ros_distro '{d.ros_distro}' but vehicle.yaml "
                   f"ros.distro is '{distro}'; the build gets ROS_DISTRO={distro} and will bake THAT")
        # A matrix service built with no declared platform pushes an UNQUALIFIED tag — the vehicle's
        # composed pull ref (<tag>-<platform>) will never find it. (A platform-valued legacy tag is
        # exempt: it behaves exactly as before the platform field existed.)
        if (d.build_command and d.build_platforms and not platform
                and tag not in d.build_platforms):
            eprint(f"rig build: WARNING — {s} declares a platform matrix {d.build_platforms} but no "
                   f"platform is declared (vehicle.yaml `platform:` or --platform); pushing "
                   f"':{tag or 'latest'}' which composed pulls won't find")

    # Base staging. Dependent builds FROM the base, so a provider's build runs FIRST (stage 0);
    # identical provider builds (several riggings sharing one ../base/build.sh) dedupe by resolved
    # script path — one runs, the rest skip. An EXPLICIT external base makes a provider's build
    # pointless when the base is the only image it produces: nothing would pull it.
    stage0: list[str] = []
    skip_build: dict[str, str] = {}  # service -> why its build_command is skipped (mirrors still run)
    providers = [s for s in services
                 if descriptors[s].build_provides == "base" and descriptors[s].build_command]
    if base_origin == "images.base":
        for s in providers:
            if len(descriptors[s].build_images) == 1:
                skip_build[s] = (f"external base '{base_ref}' replaces its "
                                 f"'{descriptors[s].build_images[0]}'")
    elif providers:
        scripts: dict[tuple, str] = {}   # base-script identity -> first provider carrying it
        script_paths: dict[str, Path] = {}
        for s in providers:
            script = (cwds[s] / shlex.split(descriptors[s].build_command)[0]).resolve()
            script_paths[s] = script
            key = _script_identity(script)
            if key in scripts:
                skip_build[s] = f"base already built by '{scripts[key]}' ({script.name})"
            else:
                scripts[key] = s
                stage0.append(s)
        # Same image name, DIFFERENT base builds: the dedup above (path, widened to content
        # identity for clean checkouts) kept both, so both would run as [base] and push the same
        # ref — two different images racing for one tag, last writer wins. That is the very skew
        # `rig image audit` exists to catch, produced by rig itself; refuse BEFORE building
        # anything rather than pick by manifest order.
        if len(stage0) > 1:
            detail = "; ".join(f"{s}: {script_paths[s]}{_script_rev(script_paths[s])}"
                               for s in stage0)
            eprint(f"rig build: base-image providers for '{base_ref}' run different base builds: "
                   f"{detail} — two builds would race for one tag; align their source pins "
                   f"(identical base content dedupes to ONE build), or set images.base to use an "
                   f"external base instead")
            return 1

    if base_ref:
        eprint(f"rig build: base image {base_ref} ({base_origin}) -> RIG_BASE_IMAGE")
    for s in stage0:  # stage 0: the base — sequential, live-streamed, before any dependent build
        desc = descriptors[s]
        cmd, args = _build_cmd(desc, cwds[s], reg, _service_tag(desc, tag, platform))
        note = _env_note(distro, platform if desc.build_platforms else None, no_cache=no_cache,
                         rmw=rmw)
        eprint(f"build {s} [base]: {desc.build_command} {' '.join(args)}  (cwd={cwds[s]}){note}")
        if not dry_run and subprocess.run(cmd, cwd=str(cwds[s]),
                                          env=_build_env(distro, desc, platform, no_cache=no_cache,
                                                         rmw=rmw)).returncode:
            eprint(f"  build {s} FAILED — the other services build FROM {base_ref}; stopping here")
            return 1
        skip_build[s] = "built in the base stage above"

    if jobs > 1 and len(services) > 1 and not dry_run:  # concurrent: capture + print grouped per service
        eprint(f"rig build: {len(services)} services, up to {jobs} concurrent (output grouped per service)")
        with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as ex:
            futures = [ex.submit(_one_captured, s, descriptors[s], cwds[s], reg, tag, distro,
                                 platform, base_ref, no_cache, skip_build.get(s), rmw)
                       for s in services]
            for fut in concurrent.futures.as_completed(futures):
                svc, rc1, out = fut.result()
                eprint(f"\n───── {svc} {'✓' if not rc1 else '✗ FAILED'} ─────\n{out}")
                rc |= rc1
        return rc

    for s in services:  # sequential: live-streamed
        desc = descriptors[s]
        if desc.build_command and s in skip_build:
            eprint(f"build {s}: skipped — {skip_build[s]}")
        elif desc.build_command:
            cmd, args = _build_cmd(desc, cwds[s], reg, _service_tag(desc, tag, platform))
            note = _env_note(distro, platform if desc.build_platforms else None, base_ref, no_cache,
                             rmw)
            eprint(f"build {s}: {desc.build_command} {' '.join(args)}  (cwd={cwds[s]}){note}")
            if not dry_run and subprocess.run(cmd, cwd=str(cwds[s]),
                                              env=_build_env(distro, desc, platform, base_ref,
                                                             no_cache, rmw)).returncode:
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
