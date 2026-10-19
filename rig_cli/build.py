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
counterpart instead of flagging an image the builder was never told about). The msgs-overlay build
additionally gets RIG_MSGS_MANIFEST — the path to the rendered union of the riggings' `msgs:`
blocks (see msgs_union / resolve_msgs_image below).
"""
from __future__ import annotations

import concurrent.futures
import os
import shlex
import subprocess
import tempfile
from pathlib import Path

import yaml

from .refs import unqualified
from .common import eprint
from .build_progress import BuildProgress
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


def msgs_union(descriptors: dict[str, Descriptor]):
    """The deduped union of every service's `msgs:` declaration, as (manifest_dict, error).
    (None, None) = no declarations anywhere (no overlay to build — the logger compose falls back
    to the bare base, which is correct). `apt` dedupes; `source` merges by repo — the same repo at
    the same ref unions the package lists, at DIFFERENT refs it is refused naming the declaring
    services ("align the riggings"), never resolved by manifest order: a drifted pin is a silent
    schema mismatch in the recorded bags."""
    apt: dict[str, None] = {}
    by_repo: dict[str, dict[str, dict]] = {}  # repo -> ref -> {packages: set, services: set}
    for svc in sorted(descriptors):
        desc = descriptors[svc]
        for name in desc.msgs_apt:
            apt.setdefault(name)
        for src in desc.msgs_source:
            slot = by_repo.setdefault(src.repo, {}).setdefault(
                src.ref, {"packages": set(), "services": set()})
            slot["packages"].update(src.packages)
            slot["services"].add(svc)
    for repo, refs in sorted(by_repo.items()):
        if len(refs) > 1:
            detail = "; ".join(f"'{ref}' from {', '.join(sorted(info['services']))}"
                               for ref, info in sorted(refs.items()))
            return None, (f"msgs: services pin '{repo}' at different refs: {detail} — the overlay "
                          f"can bake only one; align the riggings on the pin the services actually "
                          f"build against")
    if not apt and not by_repo:
        return None, None
    manifest: dict = {}
    if apt:
        manifest["apt"] = sorted(apt)
    if by_repo:
        manifest["source"] = [
            {"repo": repo, "ref": next(iter(refs)),
             "packages": sorted(next(iter(refs.values()))["packages"])}
            for repo, refs in sorted(by_repo.items())]
    return manifest, None


def resolve_msgs_image(manifest: Manifest, descriptors: dict[str, Descriptor], *,
                       registry: str | None = None, tag: str | None = None,
                       platform: str | None = None):
    """The deployment's msgs-overlay image, as (ref, origin, error) — RIG_BASE_IMAGE's shape.
    (None, None, None) when there is nothing to overlay: no `msgs:` declared anywhere (empty
    union = no overlay = the bare base is correct), or declarations but no base provider carries
    `build.msgs_overlay` (doctor WARNs there — bags would silently miss the declared types).
    The ref composes exactly like the pull side, `<registry>/<msgs_overlay.image>:<tag>`, with the
    tag platform-composed through the PROVIDER's matrix — the overlay is FROM the base, so it
    inherits the base's matrix. Providers disagreeing on the overlay image or the matrix are an
    error, never a manifest-order guess."""
    union, err = msgs_union(descriptors)
    if err:
        return None, None, err
    if union is None:
        return None, None, None
    providers: dict[str, list[str]] = {}  # overlay image repo -> the services declaring it
    for svc, desc in descriptors.items():
        if desc.msgs_overlay_image:
            providers.setdefault(desc.msgs_overlay_image, []).append(svc)
    if not providers:
        return None, None, None
    if len(providers) > 1:
        detail = "; ".join(f"'{repo}' from {', '.join(sorted(svcs))}"
                           for repo, svcs in sorted(providers.items()))
        return None, None, (f"msgs-overlay providers disagree on the image: {detail} — align the "
                            f"riggings, or drop `msgs_overlay` from one")
    repo, svcs = next(iter(providers.items()))
    matrices = {tuple(descriptors[s].build_platforms) for s in svcs}
    if len(matrices) > 1:  # same order-dependence hole resolve_base_image refuses for the base
        detail = "; ".join(f"{s}: {descriptors[s].build_platforms or '(none)'}" for s in sorted(svcs))
        return None, None, (f"msgs-overlay providers for '{repo}' declare different build.platforms "
                            f"({detail}) — the composed overlay tag would depend on manifest order; "
                            f"align the riggings")
    reg = registry or manifest.image_registry
    composed = _service_tag(descriptors[svcs[0]], tag or manifest.image_tag,
                            platform or manifest.platform)
    ref = (f"{reg}/{repo}" if reg else repo) + (f":{composed}" if composed else "")
    return ref, f"provided by {', '.join(sorted(svcs))}", None


def _build_cmd(desc: Descriptor, cwd: Path, reg, tag, command: str | None = None):
    command = command or desc.build_command  # `command` override: the msgs-overlay build script
    args = [a for a in (reg, tag) if a]  # build-images.sh takes: <registry> [tag]
    script = cwd / command
    cmd = ([str(script), *args] if script.exists()
           else ["bash", "-lc", " ".join([command, *map(shlex.quote, args)])])
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
               base_image: str | None = None, no_cache: bool = False, rmw: str | None = None,
               msgs_manifest: str | None = None):
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
    for key in ('RIG_BUILD_PLATFORMS', 'RIG_BUILD_RESULT', 'RIG_BUILD_PULL', 'RIG_TARGET_ARCH'):
        env.pop(key, None)
    if distro:
        env["ROS_DISTRO"] = distro
    plat_active = platform and desc is not None and (desc.build_platforms
                                                    or desc.platform_override_env)
    for key, value in (("RIG_BASE_IMAGE", base_image),
                       ("RIG_BUILD_NO_CACHE", "1" if no_cache else None),
                       ("RIG_ROS_RMW", rmw),
                       ("RIG_MSGS_MANIFEST", msgs_manifest),  # the union manifest — overlay build only
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


def _build_single(manifest: Manifest, descriptors: dict[str, Descriptor], *, registry: str | None,
          tag: str | None, dry_run: bool, jobs: int = 1, root: Path | None = None,
          platform: str | None = None, no_cache: bool = False,
          base_image: str | None = None, verbose: bool = False) -> int:
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

    # Msgs-overlay staging (fleet-ros-msgs = base + the union of the riggings' `msgs:` blocks — the
    # image the bag logger records with, so bags carry the fleet's custom types). Every refusal
    # (one repo pinned at two refs, providers disagreeing on the overlay image, two overlay scripts
    # that aren't content-identical) lands HERE, before anything builds. An empty union skips the
    # overlay entirely: the logger compose falls back to the bare base, which is correct. Note an
    # EXTERNAL images.base does NOT skip this (unlike the provider's own base build): an external
    # base still needs its overlay — the build runs FROM the external ref via RIG_BASE_IMAGE.
    msgs_ref, msgs_origin, msgs_err = resolve_msgs_image(
        manifest, descriptors, registry=reg, tag=tag, platform=platform)
    if msgs_err:
        eprint(f"rig build: {msgs_err}")
        return 1
    msgs_declaring = sorted(s for s, d in descriptors.items() if d.msgs_apt or d.msgs_source)
    if msgs_declaring and not msgs_ref:
        eprint(f"rig build: WARNING — {', '.join(msgs_declaring)} declare `msgs:` but no base "
               f"provider declares build.msgs_overlay — no fleet-ros-msgs overlay is built, and "
               f"the bag logger records from the bare base, silently missing those types")
    msgs_runner: str | None = None
    if msgs_ref:
        union, _ = msgs_union(descriptors)
        overlay_provs = [s for s in services if descriptors[s].msgs_overlay_command]
        idents: dict[tuple, str] = {}  # overlay-script identity -> first provider carrying it
        opaths: dict[str, Path] = {}
        for s in overlay_provs:
            script = (cwds[s] / shlex.split(descriptors[s].msgs_overlay_command)[0]).resolve()
            opaths[s] = script
            idents.setdefault(_script_identity(script), s)
        if len(idents) > 1:  # same doctrine as the base race: two builds for one tag — refuse
            detail = "; ".join(f"{s}: {opaths[s]}{_script_rev(opaths[s])}"
                               for s in idents.values())
            eprint(f"rig build: msgs-overlay providers for '{msgs_ref}' run different overlay "
                   f"builds: {detail} — two builds would race for one tag; align their source pins "
                   f"(identical overlay content dedupes to ONE build)")
            return 1
        msgs_runner = next(iter(idents.values()), None)

    if base_ref:
        eprint(f"rig build: base image {base_ref} ({base_origin}) -> RIG_BASE_IMAGE")
    if msgs_ref and msgs_runner:
        eprint(f"rig build: msgs overlay {msgs_ref} ({msgs_origin}) -> RIG_MSGS_IMAGE")
    operations = []
    groups = {0: [], 1: [], 2: {s: [] for s in services}}

    def add(label, commands, service, stage, env=None, cwd=None):
        index = len(operations)
        operations.append((label, commands, cwd, env))
        (groups[2][service] if stage == 2 else groups[stage]).append(index)
        return index

    def add_build(s, stage, command=None, parent=None):
        desc = descriptors[s]
        cmd, _ = _build_cmd(desc, cwds[s], reg, _service_tag(desc, tag, platform), command=command)
        kind = ' [base]' if stage == 0 else ' [msgs]' if stage == 1 else ''
        return add(f'build {s}{kind}', [cmd], s, stage,
                   _build_env(distro, desc, platform, parent, no_cache, rmw), cwds[s])

    for s in stage0:
        add_build(s, 0)
        skip_build[s] = 'built in the base stage above'
    overlay_index = None
    if msgs_runner:
        if reg:
            overlay_index = add_build(msgs_runner, 1, descriptors[msgs_runner].msgs_overlay_command, base_ref)
        else:
            eprint(f'build {msgs_runner} [msgs]: no registry; skipped')
    for s in services:
        desc = descriptors[s]
        if desc.build_command and s in skip_build:
            if s not in stage0:
                eprint(f'build {s}: skipped — {skip_build[s]}')
        elif desc.build_command:
            add_build(s, 2, parent=base_ref)
        for img in desc.mirror:
            if not reg:
                eprint(f'mirror {s}: {img} — no registry (pass --registry or set images.registry); skipped')
                continue
            add(f'mirror {img}', _mirror_steps(img, f'{reg}/{img}'), s, 2)
    if dry_run:
        for label, commands, cwd, env in operations:
            note = ' '.join(f'{k}={v}' for k, v in (env or {}).items()
                            if k in ('ROS_DISTRO', 'RIG_TARGET_PLATFORM', 'RIG_BASE_IMAGE',
                                     'RIG_BUILD_NO_CACHE', 'RIG_ROS_RMW'))
            eprint(f'{label}: ' + ' && '.join(shlex.join(cmd) for cmd in commands)
                   + (f' (cwd={cwd})' if cwd else '') + (' ' + note if note else ''))
        return rc
    if not operations:
        return rc
    from . import RigError
    with tempfile.TemporaryDirectory(prefix='rig-build-') as tmp, \
            BuildProgress([op[0] for op in operations], root=root, verbose=verbose) as progress:
        if overlay_index is not None:
            msgs_path = Path(tmp) / 'msgs.yaml'
            msgs_path.write_text(yaml.safe_dump(union, sort_keys=False))
            operations[overlay_index][3]['RIG_MSGS_MANIFEST'] = str(msgs_path)

        def execute(index):
            label, commands, cwd, env = operations[index]
            try:
                with progress.step(index) as step:
                    for cmd in commands:
                        step.run(cmd, cwd=cwd, env=env)
                return 0
            except RigError:
                eprint(f'{label} FAILED')
                return 1

        for index in groups[0]:
            if execute(index):
                eprint(f'rig build: base failed — dependent builds require {base_ref}; stopping here')
                return 1
        for index in groups[1]:
            rc |= execute(index)

        def execute_service(indices):
            status = 0
            for index in indices:
                status |= execute(index)
            return status

        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
            for status in pool.map(execute_service, groups[2].values()):
                rc |= status
    return rc


def build_registry(root: Path | None, manifest: Manifest, registry: str | None = None):
    """Select only the build destination; never merge local files into release inputs."""
    if registry:
        return registry, '--registry'
    from .common import load_yaml
    from .interpolate import MARKER, MAP
    from .manifest import machine_file
    from . import RigError
    paths = ([root / 'vehicle.local.yaml'] if root is not None else []) + [machine_file()]
    for path in paths:
        if not path.is_file():
            continue
        images = load_yaml(path).get('images')
        if images is None:
            continue
        if not isinstance(images, dict):
            raise RigError(f'{path}: images must be a mapping')
        value = images.get('registry')
        if value is None or value == '':
            continue
        if not isinstance(value, str) or MARKER.search(value) or MAP.search(value):
            raise RigError(f'{path}: build images.registry must be a literal registry address; '
                           'set host:port[/namespace] or pass --registry')
        if value.strip():
            return value.strip().rstrip('/'), str(path)
    return manifest.image_registry, 'vehicle.yaml'


def build(manifest: Manifest, descriptors: dict[str, Descriptor], *, registry: str | None,
          tag: str | None, dry_run: bool, jobs: int = 1, root: Path | None = None,
          platform: str | None = None, arch: str | None = None, no_cache: bool = False,
          base_image: str | None = None, scopes=None, verbose: bool = False) -> int:
    registry, registry_source = build_registry(root, manifest, registry)
    if registry:
        eprint(f'rig build: registry {registry} (from {registry_source})')
    unresolved = [key for key, value in (('images.registry', registry), ('images.tag', tag))
                  if key in manifest.missing_identity and not value]
    if unresolved:
        eprint(f'rig build: unresolved deployment settings: {", ".join(unresolved)}; '
               'pass --registry/--tag to supply build values')
    used = {s.service for s in manifest.sensors}
    descriptors = {s: d for s, d in descriptors.items() if s in used}
    from .targets import parse_scopes
    scopes = parse_scopes(scopes)
    if scopes != ["all"] or arch or any(d.build_architectures or d.build_platforms for d in descriptors.values()):
        from .matrix_build import run
        return run(manifest, descriptors, registry=registry, tag=tag, dry_run=dry_run,
                   jobs=jobs, root=root, platform=platform, arch=arch, no_cache=no_cache,
                   base_image=base_image, scopes=scopes, verbose=verbose)
    if no_cache:
        for service, desc in descriptors.items():
            if desc.build_command and desc.build_no_cache is not True:
                from . import RigError
                raise RigError(f'{service}: --no-cache requires build.capabilities.no_cache; '
                               'update the service and run rig certify')
    if any(d.build_command for d in descriptors.values()):
        eprint('rig build: legacy build contract (no architecture matrix); update service metadata '
               'to publish a portable release with companion digests')
    return _build_single(manifest, descriptors, registry=registry, tag=tag, dry_run=dry_run,
                         jobs=jobs, root=root, platform=platform, no_cache=no_cache,
                         base_image=base_image, verbose=verbose)
