"""rigging.yaml — the per-repo descriptor that tells rig how to drive a service's launcher.

This is the porting adapter: a repo becomes rig-compatible by shipping a launcher that honors the
contract (up/down/status/logs/config on one config; arbitrary config path; fleet ROS env; stdout/stderr
discipline) plus this small descriptor. The `verbs` map adapts a launcher whose CLI doesn't match rig's
logical verbs (e.g. gige-up takes compose subcommands, so status -> "ps").
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import RigError
from .common import load_yaml

# The descriptor filename rig reads from each service repo. `rigging.yaml` is canonical; `deploy.yaml` is
# accepted as a legacy fallback during the rename.
DESCRIPTOR_NAMES = ("rigging.yaml", "deploy.yaml")


def find_descriptor(repo: Path) -> Path | None:
    """First existing descriptor in `repo` (rigging.yaml preferred, deploy.yaml legacy), or None."""
    for name in DESCRIPTOR_NAMES:
        candidate = repo / name
        if candidate.exists():
            return candidate
    return None


# Logical verb -> default launcher arg string. Overridable per repo via the descriptor's `verbs:`.
DEFAULT_VERBS = {
    "up": "up -d",
    "down": "down",
    "status": "ps",
    "logs": "logs",
    "config": "config",
    # Optional: pre-pull images with NO container changes (prime a vehicle's cache while the registry is
    # reachable, then run offline). Launchers that pass compose subcommands through get it for free.
    "pull": "pull",
}


# The four `interface:` kinds, and the entry key each carries (`topic:` vs `service:`).
INTERFACE_KINDS = {"publishes": "topic", "subscribes": "topic",
                   "provides": "service", "requires": "service"}


@dataclass(frozen=True)
class InterfaceEdge:
    """One declared `interface:` entry. `name` without a leading `/` is instance-namespace-relative
    (instance `name` is the ROS namespace); a leading `/` means shared-bus (`/tf`). `type` is
    optional on hand-authored entries — `rig graph --contract` scaffolds always carry it."""
    name: str
    type: str | None


@dataclass(frozen=True)
class MsgsSource:
    """One `msgs.source` entry: an interface-package repo the fleet builds from source. `ref` is
    MANDATORY and must equal the pin the declaring service itself builds against — a drifted pin
    means the overlay's definitions are wire-incompatible with what the service publishes, and the
    failure (schema mismatch in the bag) is silent."""
    repo: str
    ref: str
    packages: tuple[str, ...]  # colcon --packages-up-to selection


@dataclass
class Descriptor:
    service: str
    repo: Path
    launcher: str
    verbs: dict[str, str]
    ros_distro: str | None
    external_volumes: list[str]  # name patterns (may contain `{name}`); GC'd only on final teardown
    host_ports: list[str]        # config paths to host-facing ports rig validates for clashes
    build_command: str | None = None             # `rig build` runs this: <command> <registry> [tag]
    build_images: list[str] = field(default_factory=list)  # image repos the build produces (certify checks
    #                                              the compose pulls them as :RIG_IMAGE_TAG — build/pull agreement)
    build_platforms: list[str] = field(default_factory=list)  # the build MATRIX (e.g. [jp7, jp6]): distinct
    #                                              image sets per hardware/OS target. Presence makes the
    #                                              service platform-dependent: rig composes its pull tag as
    #                                              <tag>-<platform> and certify checks each entry renders.
    build_provides: str | None = None            # "base": this service's build produces the deployment's
    #                                              base image (build.images[0]) — rig builds it FIRST and
    #                                              exports it as RIG_BASE_IMAGE; vehicle.yaml images.base
    #                                              overrides (see build.resolve_base_image)
    msgs_overlay_command: str | None = None      # build.msgs_overlay.command — the overlay build
    #                                              (base + the fleet's msgs union); base providers only
    msgs_overlay_image: str | None = None        # build.msgs_overlay.image — the image repo it
    #                                              produces; rig composes/exports it as RIG_MSGS_IMAGE
    msgs_apt: list[str] = field(default_factory=list)  # `msgs.apt`: distro-released interface pkgs
    #                                              (ROS names, underscores) this service's topics need
    msgs_source: list[MsgsSource] = field(default_factory=list)  # `msgs.source`: pinned from-source
    #                                              interface repos (see MsgsSource)
    platform_auto_detect: str | None = None      # the launcher's standalone host probe (e.g.
    #                                              /etc/nv_tegra_release) — informational; declared wins
    platform_override_env: str | None = None     # env var the launcher honors as platform override (e.g.
    #                                              CAM_PLATFORM) — rig mirrors RIG_TARGET_PLATFORM into it
    interface: dict[str, tuple[InterfaceEdge, ...]] | None = None  # `interface:` — the service's
    #                                              declared topic/service contract (publishes/
    #                                              subscribes/provides/requires). None = undeclared
    #                                              (distinct from declared-empty). Checked WARN-only
    #                                              against observed graph epochs (`rig graph --check`)
    mirror: list[str] = field(default_factory=list)  # third-party images to copy into the registry
    tier: str = "sensor"         # optional hint: "infra" = shared, up-first (dashboard, routers, loggers);
    #                              "autonomy" = graph consumer, up-last / down-first (planners, SLAM)
    examples: list[str] = field(default_factory=list)  # optional repo-relative example configs — copied by
    #                                              `rig init --discover`; default --config for `certify --repo`

    @property
    def launcher_path(self) -> Path:
        return self.repo / self.launcher

    def verb_args(self, verb: str) -> list[str]:
        """The launcher args for a logical verb (e.g. 'status' -> ['ps'])."""
        spec = self.verbs.get(verb)
        if spec is None:
            spec = DEFAULT_VERBS.get(verb, verb)
        return spec.split()


def load_descriptor(service: str, repo: Path) -> Descriptor:
    path = find_descriptor(repo)
    if path is None:
        # The bundled templates/ moved to rig-infra and were deleted (v0.1.35); an old services.yaml
        # path under them must fail with the pointer, not a mystery.
        bundled = Path(__file__).resolve().parent.parent / "templates"
        try:
            in_bundled = repo.resolve().is_relative_to(bundled)
        except (OSError, ValueError):
            in_bundled = False
        if in_bundled:
            raise RigError(
                f"{service}: rig's bundled templates/ moved to rig-infra "
                f"(https://github.com/christomaszewski/rig-infra) — clone it beside your deployment "
                f"and point services.yaml at ../rig-infra/{repo.name}"
            )
        raise RigError(
            f"{service}: no rigging.yaml in {repo} — is the service repo checked out, and is it "
            f"rig-compatible? (see README)"
        )
    data = load_yaml(path)
    declared = data.get("service", service)
    if declared != service:
        raise RigError(
            f"{path}: declares service '{declared}' but the catalog routes it as '{service}'"
        )
    verbs = dict(DEFAULT_VERBS)
    verbs.update(data.get("verbs") or {})

    examples_raw = data.get("examples")  # a single path or a list of them
    examples = [examples_raw] if isinstance(examples_raw, str) else list(examples_raw or [])

    tier = str(data.get("tier") or "sensor")
    if tier not in ("sensor", "infra", "autonomy"):  # a typo must not silently demote a service to sensor
        raise RigError(f"{path}: tier must be 'infra', 'sensor', or 'autonomy', not '{tier}'")

    build_raw = data.get("build")  # `build: <cmd>` or `build: { command: <cmd>, images: [...],
    #                                 platforms: [...], provides: base, msgs_overlay: {...} }`
    build_images: list[str] = []
    build_platforms: list[str] = []
    build_provides: str | None = None
    msgs_overlay_command: str | None = None
    msgs_overlay_image: str | None = None
    if isinstance(build_raw, str):
        build_command = build_raw
    elif isinstance(build_raw, dict):
        build_command = build_raw.get("command")
        build_images = list(build_raw.get("images") or [])
        build_platforms = [str(p) for p in (build_raw.get("platforms") or [])]
        if build_raw.get("provides") is not None:
            build_provides = str(build_raw["provides"])
        overlay_raw = build_raw.get("msgs_overlay")
        if overlay_raw is not None:
            if not isinstance(overlay_raw, dict):
                raise RigError(f"{path}: build.msgs_overlay must be a mapping with command, image")
            unknown = set(overlay_raw) - {"command", "image"}
            if unknown:  # a typo'd sub-key must not silently drop the overlay build
                raise RigError(f"{path}: build.msgs_overlay: unknown key(s) "
                               f"{', '.join(sorted(unknown))} — it carries only command, image")
            if not overlay_raw.get("command") or not overlay_raw.get("image"):
                raise RigError(f"{path}: build.msgs_overlay needs both `command` (the overlay build "
                               f"script, e.g. ../msgs/build-msgs.sh) and `image` (the repo it "
                               f"produces, e.g. fleet-ros-msgs)")
            msgs_overlay_command = str(overlay_raw["command"])
            msgs_overlay_image = str(overlay_raw["image"])
    else:
        build_command = None
    for p in build_platforms:  # platform names suffix image tags — they must be tag-safe fragments
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", p):
            raise RigError(f"{path}: build.platforms entry '{p}' is not a valid image-tag fragment "
                           f"([A-Za-z0-9][A-Za-z0-9._-]*)")
    if build_provides is not None and build_provides != "base":  # a typo must not silently drop the role
        raise RigError(f"{path}: build.provides must be 'base' (the only provided role), "
                       f"not '{build_provides}'")
    if build_provides == "base" and not build_images:
        raise RigError(f"{path}: build.provides: base needs build.images — the FIRST entry names the "
                       f"base image this build produces (what rig exports as RIG_BASE_IMAGE)")
    if msgs_overlay_command and build_provides != "base":
        raise RigError(f"{path}: build.msgs_overlay belongs on a base provider — the overlay builds "
                       f"FROM the deployment's base, so declare `provides: base` alongside it")

    msgs_raw = data.get("msgs")  # `msgs: { apt: [...], source: [{repo, ref, packages}] }` — the
    #                               interface packages this service's TOPICS use; rig unions these
    #                               across the fleet into the fleet-ros-msgs overlay the bag logger
    #                               records with. Independent of build:/mirror: — mirror-only
    #                               services publish types too.
    msgs_apt: list[str] = []
    msgs_source: list[MsgsSource] = []
    if msgs_raw is not None:
        if not isinstance(msgs_raw, dict):
            raise RigError(f"{path}: `msgs` must be a mapping with apt/source")
        unknown = set(msgs_raw) - {"apt", "source"}
        if unknown:  # a typo'd sub-key would silently drop packages from the overlay — fail loudly
            raise RigError(f"{path}: msgs: unknown key(s) {', '.join(sorted(unknown))} — it carries "
                           f"only apt, source")
        for name in (msgs_raw.get("apt") or []):
            name = str(name)
            if not re.match(r"^[a-z][a-z0-9_]*$", name):
                raise RigError(f"{path}: msgs.apt entry '{name}' is not a ROS package name — use the "
                               f"underscore form (e.g. mavros_msgs); the build maps it to "
                               f"ros-<distro>-<name with '_'→'-'> itself")
            msgs_apt.append(name)
        for i, entry in enumerate(msgs_raw.get("source") or []):
            if not isinstance(entry, dict):
                raise RigError(f"{path}: msgs.source #{i} must be a mapping with repo, ref, packages")
            unknown = set(entry) - {"repo", "ref", "packages"}
            if unknown:
                raise RigError(f"{path}: msgs.source #{i}: unknown key(s) "
                               f"{', '.join(sorted(unknown))} — it carries only repo, ref, packages")
            repo_url, ref, packages = entry.get("repo"), entry.get("ref"), entry.get("packages")
            if not repo_url or not ref or not packages:
                raise RigError(f"{path}: msgs.source #{i} needs `repo`, `ref` (the pin the service "
                               f"builds against — a drifted overlay pin is a silent schema mismatch "
                               f"in the bag), and `packages` (colcon --packages-up-to selection)")
            msgs_source.append(MsgsSource(repo=str(repo_url), ref=str(ref),
                                          packages=tuple(str(p) for p in packages)))

    interface_raw = data.get("interface")  # `interface: { publishes/subscribes: [{topic, type?}],
    #                               provides/requires: [{service, type?}] }` — the service's declared
    #                               topic/service contract, checked WARN-only against a run's observed
    #                               graph epochs (`rig graph --check`). Bootstrap from observation:
    #                               `rig graph --contract <instance>` prints the block. A bare string
    #                               entry is shorthand for {topic|service: <it>} (no type).
    interface: dict[str, tuple[InterfaceEdge, ...]] | None = None
    if interface_raw is not None:
        if not isinstance(interface_raw, dict):
            raise RigError(f"{path}: `interface` must be a mapping with "
                           f"{'/'.join(INTERFACE_KINDS)}")
        unknown = set(interface_raw) - set(INTERFACE_KINDS)
        if unknown:  # a typo'd kind would silently drop half the contract from the checks
            raise RigError(f"{path}: interface: unknown key(s) {', '.join(sorted(unknown))} — it "
                           f"carries only {', '.join(INTERFACE_KINDS)}")
        interface = {}
        for kind, key in INTERFACE_KINDS.items():
            edges: list[InterfaceEdge] = []
            for i, entry in enumerate(interface_raw.get(kind) or []):
                if isinstance(entry, str):
                    entry = {key: entry}
                if not isinstance(entry, dict):
                    raise RigError(f"{path}: interface.{kind} #{i} must be a mapping "
                                   f"{{{key}, type?}} or a bare name")
                unknown = set(entry) - {key, "type"}
                if unknown:
                    raise RigError(f"{path}: interface.{kind} #{i}: unknown key(s) "
                                   f"{', '.join(sorted(unknown))} — it carries only {key}, type "
                                   f"({'topics' if key == 'topic' else 'services'} here)")
                name = str(entry.get(key) or "")
                # Relative (instance-namespace) or absolute (shared-bus); never `~` (node-private
                # is not an instance-level concept) and never empty/trailing-slash/doubled-slash.
                if not re.match(r"^/?[A-Za-z0-9_][A-Za-z0-9_/]*$", name) or "//" in name \
                        or name.endswith("/"):
                    raise RigError(f"{path}: interface.{kind} #{i}: '{name}' is not a "
                                   f"{key} name — relative (fix) or absolute (/tf), "
                                   f"[A-Za-z0-9_/] only")
                etype = entry.get("type")
                if etype is not None:
                    etype = str(etype)
                    if not re.match(r"^[A-Za-z0-9_]+/(msg|srv|action)/[A-Za-z0-9_]+$", etype):
                        raise RigError(f"{path}: interface.{kind} #{i}: type '{etype}' is not the "
                                       f"full form (pkg/msg/Type, pkg/srv/Type)")
                edges.append(InterfaceEdge(name=name, type=etype))
            interface[kind] = tuple(edges)

    platform_raw = data.get("platform") or {}  # `platform: { auto_detect: <path>, override_env: <VAR> }`
    if not isinstance(platform_raw, dict):
        raise RigError(f"{path}: `platform` must be a mapping with auto_detect/override_env")
    unknown = set(platform_raw) - {"auto_detect", "override_env"}
    if unknown:  # a typo here silently breaks the routing standard — fail loudly
        raise RigError(f"{path}: platform: unknown key(s) {', '.join(sorted(unknown))} — it carries "
                       f"only auto_detect, override_env")
    override_env = platform_raw.get("override_env")
    if override_env is not None:
        override_env = str(override_env)
        if not re.match(r"^[A-Z][A-Z0-9_]*$", override_env):
            raise RigError(f"{path}: platform.override_env '{override_env}' — env names are "
                           f"UPPERCASE [A-Z][A-Z0-9_]*")
        from .manifest import RIG_OWNED_ENV
        if override_env in RIG_OWNED_ENV:
            raise RigError(f"{path}: platform.override_env '{override_env}' collides with a rig-owned "
                           f"variable — declare the service's OWN env name (e.g. CAM_PLATFORM)")

    return Descriptor(
        service=service,
        repo=repo,
        launcher=data.get("launcher") or f"{service}-up",
        verbs=verbs,
        ros_distro=data.get("ros_distro"),
        external_volumes=list(data.get("external_volumes") or []),
        host_ports=list(data.get("host_ports") or []),
        build_command=build_command,
        build_images=build_images,
        build_platforms=build_platforms,
        build_provides=build_provides,
        msgs_overlay_command=msgs_overlay_command,
        msgs_overlay_image=msgs_overlay_image,
        msgs_apt=msgs_apt,
        msgs_source=msgs_source,
        interface=interface,
        platform_auto_detect=(str(platform_raw["auto_detect"]) if platform_raw.get("auto_detect")
                              else None),
        platform_override_env=override_env,
        mirror=list(data.get("mirror") or []),
        tier=tier,
        examples=examples,
    )
