"""vehicle.yaml — the vehicle's identity, fleet-wide ROS settings, and the three stack tiers:
`infra:` (substrate), `sensors:` (producers), `autonomy:` (graph consumers).

Loading enforces the single most important correctness invariant rig owns: **globally-unique instance
`name`** across the whole vehicle (every identity a launcher derives — compose project, external volumes,
ROS namespace — comes from `name`). It also cross-checks each entry's `service`/`name` against the config's
own (the launcher trusts the config), derives the ROS domain from the vehicle id, and orders the tiers:
infra comes up first and tears down last; autonomy (planners, SLAM, perception — anything consuming the
graph) comes up after ALL sensors and stops FIRST, so the decider dies before its eyes. Ordering is a
courtesy, not correctness — consumers must still retry (discovery is dynamic).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import RigError
from .common import load_yaml
from .interpolate import MAP, MARKER, resolve_map, substitute_scalar

# THE machine's identity file — a property of the vehicle computer, not of any deployment tree
# (the boot-time systemd unit and an ssh operator must see the same vehicle_id, so this is
# system-level, never ~/.rig). Hardcoded default; RIG_VEHICLE_LOCAL overrides (tests, rootless).
MACHINE_LOCAL_DEFAULT = "/etc/rig/vehicle.local.yaml"
# The only keys a vehicle-local file may carry: the per-host knobs that genuinely vary across a
# fleet. Never sensor rows — a local file silently flipping stacks makes fleet debugging miserable.
LOCAL_KEYS = {"vehicle", "vehicle_id", "vars", "env", "data_dir", "images", "platform",
              "run_capture"}
# Env keys rig owns end-to-end (fleet_env sets them; an `env:` map may not shadow them).
RIG_OWNED_ENV = {"VEHICLE_ID", "ROS_DOMAIN_ID", "RMW_IMPLEMENTATION", "RIG_IMAGE_REGISTRY",
                 "RIG_IMAGE_TAG", "RIG_TARGET_PLATFORM", "RIG_DATA_DIR", "COMPOSE_PROJECT_NAME",
                 "RIG_BASE_IMAGE", "RIG_BUILD_NO_CACHE", "RIG_ROS_RMW", "RIG_MSGS_IMAGE",
                 "RIG_MSGS_MANIFEST",
                 # the SIL replay channel (replay.py sets them; every other verb POPS them — a
                 # leaked selector or sim-time token would silently corrupt a live session)
                 "RIG_REPLAY_SOURCE", "RIG_REPLAY_TOPICS", "RIG_REPLAY_EXCLUDE", "RIG_SIM_TIME",
                 "RIG_REPLAY_SERVICES", "RIG_REPLAY_CALLS"}


@dataclass(frozen=True)
class Sensor:
    name: str
    service: str
    config: Path  # absolute path (rewritten to the rendered path once overrides/profile are resolved)
    enabled: bool
    order: int
    overrides: dict = field(default_factory=dict)  # per-instance patch deep-merged onto the config
    tier: str = "sensor"  # "infra" (up first / down last) | "sensor" | "autonomy" (up last / down FIRST)
    profile: str | None = None  # registry provenance (`public/siyi-zr30@1.0.0`) — never part of identity;
    #                             the pinned payload hash lives in rig.lock's `instances` section
    overlays: tuple = ()  # ORDERED overlay bindings (fully-qualified refs) — layer 2; payload copies
    #                       live under config/.overlays/ so the deployment stays self-contained


@dataclass(frozen=True)
class RosSettings:
    domain_id: int
    rmw: str
    distro: str | None


# The hard ordering partition: every infra stack before every sensor before every autonomy stack,
# regardless of per-entry `order` (which only sorts within a tier). `down` reverses the whole list.
TIER_RANK = {"infra": 0, "sensor": 1, "autonomy": 2}


@dataclass
class Manifest:
    vehicle: str
    ros: RosSettings
    sensors: list[Sensor]            # infra + sensor + autonomy entries combined (each carries its `tier`)
    image_registry: str | None = None  # fleet-wide registry stacks pull from (None = local images)
    vehicle_id: object = None        # int|str; decides the ROS domain + exported as VEHICLE_ID
    image_tag: str | None = None     # fleet-wide image tag (a VERSION, e.g. v1.3.0); -> RIG_IMAGE_TAG.
    #                                  Legacy: a platform name here (jp7) still works, deprecated.
    image_base: str | None = None    # fleet-wide base image (a FULL ref) -> RIG_BASE_IMAGE; overrides
    #                                  any `provides: base` service (see build.resolve_base_image)
    platform: str | None = None      # THIS host's hardware/OS target (e.g. jp7) -> RIG_TARGET_PLATFORM;
    #                                  matrix services pull <tag>-<platform> (see dispatch.service_env)
    data_dir: str | None = None      # host dir for recordings/logs/outputs; -> RIG_DATA_DIR
    run_capture: bool = True         # lean-bake the tree into each opened run (.rig/artifact.tar.gz)
    #                                  so every run dir is self-contained for `rig reconstruct`;
    #                                  disk-tight vehicles opt out here or in vehicle.local.yaml
    vars: dict = field(default_factory=dict)      # resolved {{var}} context (built-ins + vars:)
    extra_env: dict = field(default_factory=dict)  # `env:` map, interpolated — fleet_env exports it
    missing_identity: tuple = ()  # mandatory per-vehicle keys nothing provides — loading stays
    #                               LAZY so management verbs (pkg list/remove/…) work on any box;
    #                               identity-CONSUMING commands (up/render/…) enforce via require_identity

    def select(self, names: list[str], enabled_only: bool) -> list[Sensor]:
        """Resolve a name filter into a tiered, ordered list (infra → sensors → autonomy). Explicit names win."""
        if names:
            by_name = {s.name: s for s in self.sensors}
            missing = [n for n in names if n not in by_name]
            if missing:
                raise RigError(f"unknown sensor(s): {', '.join(missing)}")
            chosen = [by_name[n] for n in names]
        else:
            chosen = [s for s in self.sensors if s.enabled or not enabled_only]
        return sorted(chosen, key=lambda s: (TIER_RANK[s.tier], s.order))


def _parse_entries(entries, tier: str, root: Path, seen: dict[str, Path]) -> list[Sensor]:
    out: list[Sensor] = []
    for index, entry in enumerate(entries or []):
        entry = entry or {}
        name, service, cfg = entry.get("name"), entry.get("service"), entry.get("config")
        if not (name and service and cfg):
            raise RigError(f"vehicle.yaml: {tier} #{index} needs `name`, `service`, and `config`")

        cfg_path = Path(cfg)
        cfg_path = (cfg_path if cfg_path.is_absolute() else (root / cfg_path)).resolve()
        if not cfg_path.exists():
            raise RigError(f"{tier} '{name}': config not found: {cfg_path}")

        # The base config may be a complete named config OR a nameless profile the manifest completes;
        # if service/name ARE present they must match — catch drift.
        cdata = load_yaml(cfg_path)
        if cdata.get("service") is not None and cdata.get("service") != service:
            raise RigError(f"{tier} '{name}': vehicle.yaml service '{service}' != config service "
                           f"'{cdata.get('service')}' in {cfg_path}")
        if cdata.get("name") is not None and cdata.get("name") != name:
            raise RigError(f"{tier} '{name}': vehicle.yaml name != config name '{cdata.get('name')}' in {cfg_path}")

        overrides = entry.get("overrides") or {}
        if not isinstance(overrides, dict):
            raise RigError(f"{tier} '{name}': `overrides` must be a mapping")
        overlays = entry.get("overlays") or []
        if not isinstance(overlays, list) or not all(isinstance(o, str) for o in overlays):
            raise RigError(f"{tier} '{name}': `overlays` must be a list of overlay refs")

        if name in seen:  # THE top correctness check — unique across infra + sensors
            raise RigError(f"duplicate name '{name}' ({cfg_path} and {seen[name]}); names must be unique "
                           f"across the vehicle — they key the compose project, volumes, and ROS namespace")
        seen[name] = cfg_path

        out.append(Sensor(name=name, service=service, config=cfg_path,
                          enabled=bool(entry.get("enabled", True)),
                          order=int(entry.get("order", (index + 1) * 10)),
                          overrides=overrides, tier=tier,
                          profile=(str(entry["profile"]) if entry.get("profile") else None),
                          overlays=tuple(overlays)))
    return out


def project_name(name: str, vehicle_id=None) -> str:
    """The compose project for an instance: '<name>-vehicle-<id>' (or '<name>' with no vehicle id). rig owns
    this so containers are named consistently (<project>-<compose-service>-N) across launchers + bake."""
    return f"{name}-vehicle-{vehicle_id}" if vehicle_id not in (None, "") else name


def stack_summary(sensors: list[Sensor]) -> str:
    """A tier-aware count for human output, e.g. '2 sensors + 2 infra + 1 autonomy' — infra and autonomy
    are stacks, not sensors."""
    infra = sum(1 for s in sensors if s.tier == "infra")
    autonomy = sum(1 for s in sensors if s.tier == "autonomy")
    sens = len(sensors) - infra - autonomy
    parts = []
    if sens:
        parts.append(f"{sens} sensor{'' if sens == 1 else 's'}")
    if infra:
        parts.append(f"{infra} infra")
    if autonomy:
        parts.append(f"{autonomy} autonomy")
    return " + ".join(parts) or "0 stacks"


def _local_sources(root: Path) -> list[dict]:
    """Vehicle-local files, highest precedence first: the deployment-local vehicle.local.yaml
    (bench/dev trees — artifacts never ship one), then the MACHINE identity file."""
    sources: list[dict] = []
    machine = Path(os.environ.get("RIG_VEHICLE_LOCAL") or MACHINE_LOCAL_DEFAULT)
    for path in (root / "vehicle.local.yaml", machine):
        if not path.is_file():
            continue
        data = load_yaml(path)
        unknown = set(data) - LOCAL_KEYS
        if unknown:
            raise RigError(f"{path}: unknown key(s): {', '.join(sorted(unknown))} — vehicle-local "
                           f"files carry only: {', '.join(sorted(LOCAL_KEYS))}")
        sources.append(data)
    return sources


def _shell_source() -> dict:
    """Shell overrides — only rig-namespaced env feeds vars, never arbitrary environment."""
    data: dict = {}
    if os.environ.get("RIG_VEHICLE_ID"):
        data["vehicle_id"] = os.environ["RIG_VEHICLE_ID"]
    if os.environ.get("RIG_VEHICLE_NAME"):
        data["vehicle"] = os.environ["RIG_VEHICLE_NAME"]
    shell_vars: dict = {}
    for key, value in os.environ.items():
        if key.startswith("RIG_VAR_"):
            name = key[len("RIG_VAR_"):]
            if not re.match(r"^[a-z][a-z0-9_]*$", name):
                raise RigError(f"{key}: var names are lowercase [a-z][a-z0-9_]* "
                               f"(shell spelling: RIG_VAR_<name>)")
            shell_vars[name] = value
    if shell_vars:
        data["vars"] = shell_vars
    return data


def _self_referencing(value, key: str) -> bool:
    """`vehicle_id: "{{vehicle_id}}"` — the field references ITSELF, i.e. vehicle.yaml provides
    no value and declares it supplied per vehicle (mandatory-from-local)."""
    return isinstance(value, str) and key in MARKER.findall(value)


def _effective(key: str, sources: list[dict], base):
    """Precedence walk (shell > deployment-local > machine > vehicle.yaml); a self-referencing
    value contributes nothing. None = no source provides it."""
    for candidate in [s.get(key) for s in sources] + [base]:
        if candidate is None or _self_referencing(candidate, key):
            continue
        return candidate
    return None


_PROVISION_HINT = ("provision this machine once: sudo rig provision --id <N> --name <name> "
                   "(writes {machine}), set RIG_VEHICLE_ID, or drop a bench vehicle.local.yaml "
                   "beside vehicle.yaml for dev work")


def _missing_mandatory(key: str, effective, base) -> bool:
    return effective is None and _self_referencing(base, key)


def require_identity(manifest: "Manifest", *, what: str) -> None:
    """The gate identity-CONSUMING commands call: per-vehicle values (mandatory identity markers,
    or manifest scalars whose vars nothing provides) must be resolved before anything renders
    configs, names compose projects, or exports the fleet env — never vehicle 0."""
    if not manifest.missing_identity:
        return
    keys = ", ".join(manifest.missing_identity)
    machine = os.environ.get("RIG_VEHICLE_LOCAL") or MACHINE_LOCAL_DEFAULT
    raise RigError(f"{what}: unresolved per-vehicle value(s): {keys} — "
                   + _PROVISION_HINT.format(machine=machine))


def _derive_domain(vehicle_id, ros_raw: dict) -> int:
    """Explicit `ros.domain_id` wins; else a numeric vehicle id IS the domain (so one knob picks both);
    else 0."""
    if "domain_id" in ros_raw:
        return int(ros_raw["domain_id"])
    if isinstance(vehicle_id, bool):  # bool is an int subclass — don't treat True/False as a domain
        return 0
    if isinstance(vehicle_id, int):
        return vehicle_id
    if isinstance(vehicle_id, str) and vehicle_id.isdigit():
        return int(vehicle_id)
    return 0


def load_manifest(root: Path) -> Manifest:
    data = load_yaml(root / "vehicle.yaml")

    # --- vehicle-local sources & {{var}} resolution (load-time pass) --------------------------
    # Precedence, most-specific-wins: shell > deployment-local > machine (/etc/rig) >
    # fleet.yaml (the FLEET tempo tier — pushed by `rig fleet up`, persists across reboots) >
    # vehicle.yaml. A self-referencing vehicle.yaml field ("{{vehicle_id}}") is MANDATORY.
    sources = [_shell_source()] + _local_sources(root)
    if (root / "fleet.yaml").is_file():
        from .fleet import vars_source
        sources.append(vars_source(root / "fleet.yaml"))
    eff_vehicle = _effective("vehicle", sources, data.get("vehicle"))
    eff_id = _effective("vehicle_id", sources, data.get("vehicle_id"))
    eff_data_dir = _effective("data_dir", sources, data.get("data_dir"))
    eff_platform = _effective("platform", sources, data.get("platform"))
    missing = tuple(key for key, eff in (("vehicle", eff_vehicle), ("vehicle_id", eff_id),
                                         ("data_dir", eff_data_dir), ("platform", eff_platform))
                    if _missing_mandatory(key, eff, data.get(key)))

    merged_vars: dict = {}
    merged_env: dict = {}
    for src in [data] + list(reversed(sources)):  # lowest precedence first; higher overwrites
        for bucket, merged in (("vars", merged_vars), ("env", merged_env)):
            extra = src.get(bucket) or {}
            if not isinstance(extra, dict):
                raise RigError(f"`{bucket}` must be a mapping")
            merged.update(extra)

    raw_ctx = dict(merged_vars)
    if eff_vehicle is not None:
        raw_ctx["vehicle"] = eff_vehicle
    if eff_id is not None:
        raw_ctx["vehicle_id"] = eff_id
    ctx = resolve_map(raw_ctx, where="vars")  # vars may reference identity/other vars; cycles error

    vehicle = str(ctx.get("vehicle", "vehicle"))
    vehicle_id = ctx.get("vehicle_id")
    ros_raw = data.get("ros") or {}
    ros = RosSettings(
        domain_id=_derive_domain(vehicle_id, ros_raw),
        rmw=str(ros_raw.get("rmw", "rmw_fastrtps_cpp")),
        distro=ros_raw.get("distro"),
    )
    ctx.setdefault("vehicle", vehicle)
    ctx["ros_domain_id"] = ros.domain_id
    # Derived built-in (like ros_domain_id, computed AFTER the fixpoint — so it cannot be
    # referenced by other vars:): the fleet minus THIS vehicle, for {{map fleet_peer_ids <tmpl>}}
    # peer-endpoint construction. String-normalized comparison: YAML gives ints, the shell tier
    # gives strings, and `7` vs "7" must exclude either way. Unprovisioned box (no vehicle_id):
    # stays absent, so a premature reference errors loudly instead of including self.
    if "fleet_ids" in ctx and "vehicle_id" in ctx:
        ids = ctx["fleet_ids"]
        if isinstance(ids, str):  # RIG_VAR_fleet_ids=1,2,7
            ids = [part.strip() for part in ids.split(",") if part.strip()]
        if isinstance(ids, (list, tuple)):
            me = str(ctx["vehicle_id"]).strip()
            ctx.setdefault("fleet_peer_ids", [i for i in ids if str(i).strip() != me])

    unresolved: list[str] = list(missing)  # identity keys first, then any manifest scalar whose
    #                                        vars nothing provides — loading stays LAZY throughout;
    #                                        require_identity gates the commands that CONSUME them

    def _lazy(value, label: str):
        if isinstance(value, str) and MAP.search(value):  # a literal {{map …}} reaching a driver
            raise RigError(f"{label}: the {{{{map …}}}} form renders in CONFIG files only — "
                           f"manifest fields and env: values take plain {{{{var}}}} markers")
        if isinstance(value, str) and MARKER.search(value):
            try:
                return substitute_scalar(value, ctx, where=label)
            except RigError:
                unresolved.append(label)
                return None
        return value

    eff_data_dir = _lazy(eff_data_dir, "data_dir")
    data_dir = (str(eff_data_dir or "").strip()) or None
    if data_dir is not None:
        ctx["data_dir"] = data_dir

    # The HOST's hardware/OS target (jp7): a per-host fact, so the vehicle-local tier can carry it
    # per machine. Resolved value joins the var context — configs may CONSUME {{platform}}, they
    # never declare it (portability across vehicles).
    eff_platform = _lazy(eff_platform, "platform")
    platform = (str(eff_platform or "").strip()) or None
    if platform is not None:
        ctx["platform"] = platform

    base_images = data.get("images") or {}
    eff_images = {}
    for sub in ("registry", "tag", "base"):
        value = _effective(sub, [s.get("images") or {} for s in sources], base_images.get(sub))
        value = _lazy(value, f"images.{sub}")
        eff_images[sub] = (str(value or "").strip()) or None

    extra_env: dict = {}
    for key, value in merged_env.items():
        if not re.match(r"^[A-Z][A-Z0-9_]*$", str(key)):
            raise RigError(f"env: '{key}' — exported names are UPPERCASE [A-Z][A-Z0-9_]*")
        if key in RIG_OWNED_ENV:
            raise RigError(f"env: '{key}' collides with a rig-owned variable — rig sets it from "
                           f"the manifest; use the manifest field instead")
        resolved_value = _lazy(value, f"env.{key}")
        if isinstance(resolved_value, (list, tuple, dict)):
            raise RigError(f"env: '{key}' interpolates to a {type(resolved_value).__name__} — "
                           f"exported environment values must be scalars (the {{{{map …}}}} form "
                           f"belongs in config files)")
        if resolved_value is not None or not isinstance(value, str):
            extra_env[key] = resolved_value if resolved_value is not None else value

    # --- rows ---------------------------------------------------------------------------------
    seen: dict[str, Path] = {}
    infra = _parse_entries(data.get("infra"), "infra", root, seen)
    sensors = _parse_entries(data.get("sensors"), "sensor", root, seen)
    autonomy = _parse_entries(data.get("autonomy"), "autonomy", root, seen)

    # Concatenation order matters beyond select(): bake iterates `manifest.sensors` as-is, so the tier
    # partition must already hold here for up.sh line order (autonomy last) and down.sh (reversed).
    return Manifest(vehicle=vehicle, ros=ros,
                    sensors=infra + sensors + autonomy,
                    image_registry=eff_images["registry"], vehicle_id=vehicle_id,
                    image_tag=eff_images["tag"], image_base=eff_images["base"], platform=platform,
                    data_dir=data_dir,
                    run_capture=bool(_effective("run_capture", sources,
                                                data.get("run_capture")) is not False),
                    vars=ctx, extra_env=extra_env,
                    missing_identity=tuple(unresolved))
