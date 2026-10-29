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

# The operational-state trio (contract: the service-state adoption prompt; reference adoption
# ouster v0.2.0). NEVER in DEFAULT_VERBS: declaring all three in rigging `verbs:` IS the support
# claim — and `verb_args`'s bare-token fallback would otherwise hand `standby` to a
# compose-forwarding launcher as a compose subcommand. Undeclared = always active; rig skips the
# service on standby/activate fan-out. `state` reports exactly one JSON object on stdout with a
# `state` from STATE_VOCAB (state and HEALTH are read as a pair: transitioning+healthy = wait,
# transitioning+unhealthy = stuck).
STATE_VERBS = ("standby", "activate", "state")
STATE_VOCAB = ("active", "standby", "transitioning", "down")


# The four `interface:` kinds, and the entry key each carries (`topic:` vs `service:`).
INTERFACE_KINDS = {"publishes": "topic", "subscribes": "topic",
                   "provides": "service", "requires": "service"}


@dataclass(frozen=True)
class InterfaceEdge:
    """One declared `interface:` entry. `name` without a leading `/` is instance-namespace-relative
    (`ros_namespace` when declared, otherwise instance `name`); a leading `/` means shared-bus (`/tf`). `type` is
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


@dataclass(frozen=True)
class BuildSource:
    """One `build.sources` entry: a git repo the service's build would otherwise fetch itself
    (a CMake FetchContent, a `git clone` in a Dockerfile). rig resolves it through its local git
    mirror and hands the tree to the build as $RIG_BUILD_SOURCES/<name> — which the build script
    passes on as the BuildKit named context `<name>` — so the build works offline."""
    name: str  # the named-context / directory name
    repo: str
    ref: str


@dataclass(frozen=True)
class ReplaySource:
    """`replay.source` — how `rig replay` turns an instance into a replay of ITS OWN recordings
    (ROADMAP §2, the per-sensor source axis): `data` is the run-relative directory the instance
    writes and reads back (`{name}` = the instance name; rig checks it exists in the source run,
    nothing more), `overrides` the config patch that flips the instance to replay — deep-merged over
    its rendered config for the session, `{{replay_*}}` variables substituted (replay_source,
    replay_retime, replay_epoch_unix_ns, replay_start_at_unix_s, replay_from_s, replay_to_s,
    replay_session, name). rig never interprets the keys: the launcher owns the mechanics."""
    data: str
    overrides: dict
    binding: str | None = None  # direct: consumes replay_input_path and replay_input_sessions
    image_labels: dict[str, dict[str, str]] = field(default_factory=dict)

    def data_path(self, name: str) -> str:
        return self.data.replace("{name}", name)


@dataclass(frozen=True)
class ExportSource:
    """`export:` — this service can produce a SLIM copy of the data it wrote into a run, ON the
    vehicle, for `rig run export` (the trip off the vehicle). `data` is the run-relative directory
    the instance writes (`{name}` = the instance name; rig checks it exists in the run, nothing
    more). rig invokes the launcher's `export` verb with RIG_EXPORT_SOURCE (the run dir),
    RIG_EXPORT_DEST (the export dir the launcher writes `<data>` under), RIG_EXPORT_OPTIONS (a YAML
    file: the profile's block for this instance, verbatim — rig never interprets its keys) and
    RIG_EXPORT_PROFILE (its name). The launcher owns the mechanics (what a slim bag is)."""
    data: str
    modes: tuple[str, ...] = ("whole-run", "in-place")
    command: tuple[str, ...] = ()
    default_profile: str | None = None
    profiles: dict[str, str] = field(default_factory=dict)

    def data_path(self, name: str) -> str:
        return self.data.replace("{name}", name)


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
    # Runtime variants select userspace/hardware capabilities; architectures select CPU members.
    # Runtime tags compose as <tag>-<platform> and certify checks each declared variant.
    build_platforms: list[str] = field(default_factory=list)
    build_architectures: list[str] = field(default_factory=list)
    build_variants: dict = field(default_factory=dict)
    build_no_cache: bool | None = None
    build_multiarch: bool = False
    image_env: dict[str, str] = field(default_factory=dict)
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
    build_sources: list[BuildSource] = field(default_factory=list)  # `build.sources` (BuildSource)
    msgs_source: list[MsgsSource] = field(default_factory=list)  # `msgs.source`: pinned from-source
    #                                              interface repos (see MsgsSource)
    platform_auto_detect: str | None = None      # the launcher's standalone host probe (e.g.
    #                                              /etc/nv_tegra_release) — informational; declared wins
    platform_override_env: str | None = None     # env var the launcher honors as platform override (e.g.
    #                                              CAM_PLATFORM) — rig mirrors RIG_TARGET_PLATFORM into it
    replay_sim_time: bool = False  # `replay: {sim_time: true}` — the launcher PROMISES to wire
    #                                              `use_sim_time` from RIG_SIM_TIME (rig's one clock
    #                                              token; the player's --clock derives from the same
    #                                              var). Undeclared services WARN under `rig replay`
    #                                              sim time: they run wall clock against bag stamps
    replay_source: ReplaySource | None = None  # `replay: {source: {data, overrides}}` — this
    #                                              service can replay its own recordings from a run
    #                                              (see ReplaySource); None = a live-only service
    replay_service_introspection: bool = False  # `replay: {service_introspection: true}` — the
    #                                              launcher enables CONTENTS-level service
    #                                              introspection (servers+clients), so calls RECORD
    #                                              into bags and REPLAY at this service's servers.
    #                                              Undeclared services WARN when replay selects
    #                                              services (their recorded calls likely absent)
    replay_start: bool = True  # default live startup; recorded bag topics are independent
    replay_platform: str | None = None  # portable replay runtime when the host has no provisioned one
    export_source: ExportSource | None = None  # `export: {data}` — this service slims its own run
    #                                              data for `rig run export` (see ExportSource);
    #                                              None = its data travels as-is (or is omitted
    #                                              by the profile)
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
    ros_namespace: str | None = None  # observed ROS ownership namespace; optional {name} template

    @property
    def launcher_path(self) -> Path:
        return self.repo / self.launcher

    def verb_args(self, verb: str) -> list[str]:
        """The launcher args for a logical verb (e.g. 'status' -> ['ps'])."""
        spec = self.verbs.get(verb)
        if spec is None:
            spec = DEFAULT_VERBS.get(verb, verb)
        return spec.split()

    @property
    def declared_state_verbs(self) -> list[str]:
        """The operational-state verbs this rigging declares (membership in `verbs` IS the claim —
        STATE_VERBS never enter DEFAULT_VERBS, so declared ⇔ present)."""
        return [v for v in STATE_VERBS if v in self.verbs]

    @property
    def supports_states(self) -> bool:
        """All-three-or-none: only a complete trio counts (a partial declaration is a broken claim
        — certify ERRORs on it, doctor WARNs, and dispatch skips the service)."""
        return len(self.declared_state_verbs) == len(STATE_VERBS)


def _validate_ros_namespace(value: str, context: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"(?:/[A-Za-z_][A-Za-z0-9_]*)+", value):
        raise RigError(f"{context}: expected an absolute, non-root ROS namespace with valid "
                       "ROS name tokens, e.g. /robot/controller; only {name} may be substituted")


def resolve_ros_namespace(descriptor: Descriptor | None, name: str) -> str:
    """Resolve ROS ownership independently of Rig instance/recording identity.

    Undeclared descriptors retain the historical /<instance-name> convention, even for older
    instance names that are not valid ROS tokens. A declaration describes the launcher's actual
    namespace; it does not configure or remap that launcher.
    """
    template = getattr(descriptor, "ros_namespace", None)
    if template is None:
        return "/" + name
    context = f"{getattr(descriptor, 'service', 'service')}: ros_namespace for instance {name!r}"
    namespace = template.replace("{name}", name) if isinstance(template, str) else template
    _validate_ros_namespace(namespace, context)
    return namespace


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
    ros_namespace = data.get("ros_namespace")
    if "ros_namespace" in data:
        # Substitute a valid token to validate the template now; validate the actual instance name
        # when resolving it. Unknown/malformed placeholders retain braces and fail the ROS grammar.
        probe = (ros_namespace.replace("{name}", "rig_instance")
                 if isinstance(ros_namespace, str) else ros_namespace)
        _validate_ros_namespace(probe, f"{path}: ros_namespace")
    verbs = dict(DEFAULT_VERBS)
    verbs.update(data.get("verbs") or {})
    for key in ("images", "runtime-images"):
        if key in verbs and (not isinstance(verbs[key], str) or not verbs[key].strip()):
            raise RigError(f"{path}: verbs.{key} must be a nonempty launcher argument string")

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
    build_sources: list[BuildSource] = []
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
        sources_raw = build_raw.get("sources")
        if sources_raw is not None and not isinstance(sources_raw, list):
            raise RigError(f"{path}: build.sources must be a list of {{name, repo, ref}}")
        for i, entry in enumerate(sources_raw or []):
            if not isinstance(entry, dict) or set(entry) - {"name", "repo", "ref"}:
                raise RigError(f"{path}: build.sources #{i} must be a mapping with only name, "
                               f"repo, ref")
            name, repo_url, ref = (entry.get(k) for k in ("name", "repo", "ref"))
            if not name or not repo_url or not ref:
                raise RigError(f"{path}: build.sources #{i} needs `name` (the build context it "
                               f"arrives as), `repo` and `ref` (a tag, branch or commit)")
            # A directory name AND a Dockerfile stage name: lowercase, no separators to escape.
            if not re.match(r"^[a-z0-9][a-z0-9_-]*$", str(name)):
                raise RigError(f"{path}: build.sources name '{name}' must match [a-z0-9][a-z0-9_-]* "
                               f"(it names a directory and a BuildKit build context)")
            if any(s.name == str(name) for s in build_sources):
                raise RigError(f"{path}: build.sources name '{name}' is declared twice")
            build_sources.append(BuildSource(str(name), str(repo_url), str(ref)))
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

    export_raw = data.get("export")  # `export: { data: "bags/{name}" }` — the service slims its own
    #                               run data for `rig run export` (see ExportSource)
    export_source: ExportSource | None = None
    if export_raw is not None:
        if not isinstance(export_raw, dict):
            raise RigError(f"{path}: `export` must be a mapping with `data` (the run-relative "
                           f"directory the instance writes, e.g. bags/{{name}})")
        extra = set(export_raw) - {"data", "modes", "command", "default_profile", "profiles"}
        if extra:
            raise RigError(f"{path}: export: unknown key(s) {', '.join(sorted(extra))} — it "
                           f"carries data, modes, command, default_profile, profiles")
        data_rel = export_raw.get("data")
        if not isinstance(data_rel, str) or not data_rel.strip():
            raise RigError(f"{path}: export.data must be a run-relative directory the instance "
                           f"writes, e.g. bags/{{name}}")
        data_rel = data_rel.strip().strip("/")
        if ".." in Path(data_rel).parts:
            raise RigError(f"{path}: export.data must stay inside the run ({data_rel!r})")
        modes = export_raw.get("modes", ["whole-run", "in-place"])
        if (not isinstance(modes, list) or not modes or
                any(m not in ("whole-run", "in-place", "service") for m in modes) or
                len(set(modes)) != len(modes)):
            raise RigError(f"{path}: export.modes must list whole-run, in-place, and/or service")
        command = export_raw.get("command", [])
        profiles = export_raw.get("profiles", {})
        default = export_raw.get("default_profile")
        if (not isinstance(command, list) or
                any(not isinstance(v, str) or not v for v in command)):
            raise RigError(f"{path}: export.command must be an argument list")
        if not isinstance(profiles, dict) or any(
                not isinstance(k, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", k)
                or not isinstance(v, str) or not v for k, v in profiles.items()):
            raise RigError(f"{path}: export.profiles must map profile names to bundled files")
        if "service" in modes:
            if not command or not profiles or not isinstance(default, str) or default not in profiles:
                raise RigError(f"{path}: service export needs command, profiles and default_profile")
            if data_rel.count("{name}") != 1 or not data_rel.endswith("/{name}"):
                raise RigError(f"{path}: service export.data must end with /{{name}}")
            for rel in [command[0], *profiles.values()]:
                p = Path(rel)
                if (p.is_absolute() or ".." in p.parts or
                        not (repo / p).resolve().is_relative_to(repo.resolve()) or
                        not (repo / p).is_file()):
                    raise RigError(f"{path}: export file must exist inside the service bundle: {rel}")
        elif command or profiles or default is not None:
            raise RigError(f"{path}: export command/profiles require modes: [service]")
        export_source = ExportSource(data=data_rel, modes=tuple(modes), command=tuple(command),
                                     default_profile=default, profiles=dict(profiles))

    replay_raw = data.get("replay")  # `replay: { sim_time: true, service_introspection: true }` —
    #                               the launcher's PROMISES: use_sim_time wired from RIG_SIM_TIME,
    #                               and CONTENTS-level service introspection on its servers/clients
    #                               (each adoption is a small launcher change — see the prompts in
    #                               ~/ws/infra). Block-shaped for future replay capabilities
    #                               (e.g. a per-sensor replay SOURCE declaration).
    replay_sim_time = False
    replay_service_introspection = False
    replay_source: ReplaySource | None = None
    if replay_raw is not None:
        if not isinstance(replay_raw, dict):
            raise RigError(f"{path}: `replay` must be a mapping with "
                           f"sim_time/service_introspection/source/start/platform")
        unknown = set(replay_raw) - {"sim_time", "service_introspection", "source", "start", "platform"}
        if unknown:  # a typo'd key would silently drop the promise doctor relies on
            raise RigError(f"{path}: replay: unknown key(s) {', '.join(sorted(unknown))} — it "
                           f"carries only sim_time, service_introspection, source, start, platform")
        src_raw = replay_raw.get("source")
        if src_raw is not None:
            if not isinstance(src_raw, dict):
                raise RigError(f"{path}: replay.source must be a mapping with `data` (a run-relative "
                               f"directory, e.g. recordings/{{name}}) and `overrides` (the config patch)")
            extra = set(src_raw) - {"data", "overrides", "binding", "image_labels"}
            if extra:
                raise RigError(f"{path}: replay.source: unknown key(s) {', '.join(sorted(extra))} — "
                               f"it carries only data, overrides, binding, image_labels")
            data_rel = src_raw.get("data")
            if not isinstance(data_rel, str) or not data_rel.strip():
                raise RigError(f"{path}: replay.source.data must be a run-relative directory the "
                               f"instance records into, e.g. recordings/{{name}}")
            data_rel = data_rel.strip().strip("/")
            if ".." in Path(data_rel).parts:
                raise RigError(f"{path}: replay.source.data must stay inside the run ({data_rel!r})")
            ov = src_raw.get("overrides")
            if not isinstance(ov, dict) or not ov:
                raise RigError(f"{path}: replay.source.overrides must be a non-empty mapping — the "
                               f"config patch that flips the instance to replay its recordings")
            binding = src_raw.get("binding")
            if binding is not None and binding != "direct":
                raise RigError(f"{path}: replay.source.binding must be direct when declared")
            image_labels = src_raw.get("image_labels", {})
            if (not isinstance(image_labels, dict) or
                    any(image not in build_images or not isinstance(labels, dict) or not labels or
                        any(not isinstance(k, str) or not k or not isinstance(v, str) or not v
                            for k, v in labels.items()) for image, labels in image_labels.items())):
                raise RigError(f"{path}: replay.source.image_labels must map build image names "
                               "to nonempty string label requirements")
            if image_labels and binding != "direct":
                raise RigError(f"{path}: replay.source.image_labels requires binding: direct")
            replay_source = ReplaySource(data=data_rel, overrides=ov, binding=binding,
                                         image_labels=image_labels)
        for key in ("sim_time", "service_introspection", "start"):
            if key in replay_raw and not isinstance(replay_raw[key], bool):
                raise RigError(f"{path}: replay.{key} must be true or false (a declaration is a "
                               f"promise the launcher wires the capability from the fleet env)")
        replay_sim_time = bool(replay_raw.get("sim_time"))
        replay_service_introspection = bool(replay_raw.get("service_introspection"))
        if replay_raw.get("platform") is not None and replay_raw["platform"] not in build_platforms:
            raise RigError(f"{path}: replay.platform must be one of build.platforms")

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

    from .build_contract import parse_contract
    return Descriptor(
        **parse_contract(data, path),
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
        build_sources=build_sources,
        msgs_overlay_command=msgs_overlay_command,
        msgs_overlay_image=msgs_overlay_image,
        msgs_apt=msgs_apt,
        msgs_source=msgs_source,
        replay_sim_time=replay_sim_time,
        export_source=export_source,
        replay_source=replay_source,
        replay_start=(replay_raw or {}).get("start", True),
        replay_platform=(replay_raw or {}).get("platform"),
        replay_service_introspection=replay_service_introspection,
        interface=interface,
        platform_auto_detect=(str(platform_raw["auto_detect"]) if platform_raw.get("auto_detect")
                              else None),
        platform_override_env=override_env,
        mirror=list(data.get("mirror") or []),
        tier=tier,
        examples=examples,
        ros_namespace=ros_namespace,
    )
