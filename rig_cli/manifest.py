"""vehicle.yaml — the vehicle's identity, fleet-wide ROS settings, shared `infra:` services, and `sensors:`.

Loading enforces the single most important correctness invariant rig owns: **globally-unique instance
`name`** across the whole vehicle (every identity a launcher derives — compose project, external volumes,
ROS namespace — comes from `name`). It also cross-checks each entry's `service`/`name` against the config's
own (the launcher trusts the config), derives the ROS domain from the vehicle id, and keeps shared
infrastructure (`infra:`, e.g. a zenoh router) in a tier that comes up before sensors and tears down after.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import RigError
from .common import load_yaml


@dataclass(frozen=True)
class Sensor:
    name: str
    service: str
    config: Path  # absolute path (rewritten to the rendered path once overrides/profile are resolved)
    enabled: bool
    order: int
    overrides: dict = field(default_factory=dict)  # per-instance patch deep-merged onto the config
    tier: str = "sensor"  # "infra" (shared, up first / down last) | "sensor"


@dataclass(frozen=True)
class RosSettings:
    domain_id: int
    rmw: str
    distro: str | None


@dataclass
class Manifest:
    vehicle: str
    ros: RosSettings
    sensors: list[Sensor]            # infra + sensor entries combined (each carries its `tier`)
    image_registry: str | None = None  # fleet-wide registry stacks pull from (None = local images)
    vehicle_id: object = None        # int|str; decides the ROS domain + exported as VEHICLE_ID
    image_tag: str | None = None     # fleet-wide image tag (e.g. a JetPack platform jp7); -> RIG_IMAGE_TAG
    data_dir: str | None = None      # host dir for recordings/logs/outputs; -> RIG_DATA_DIR

    def select(self, names: list[str], enabled_only: bool) -> list[Sensor]:
        """Resolve a name filter into a tiered, ordered list (infra before sensors). Explicit names win."""
        if names:
            by_name = {s.name: s for s in self.sensors}
            missing = [n for n in names if n not in by_name]
            if missing:
                raise RigError(f"unknown sensor(s): {', '.join(missing)}")
            chosen = [by_name[n] for n in names]
        else:
            chosen = [s for s in self.sensors if s.enabled or not enabled_only]
        return sorted(chosen, key=lambda s: (0 if s.tier == "infra" else 1, s.order))


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

        if name in seen:  # THE top correctness check — unique across infra + sensors
            raise RigError(f"duplicate name '{name}' ({cfg_path} and {seen[name]}); names must be unique "
                           f"across the vehicle — they key the compose project, volumes, and ROS namespace")
        seen[name] = cfg_path

        out.append(Sensor(name=name, service=service, config=cfg_path,
                          enabled=bool(entry.get("enabled", True)),
                          order=int(entry.get("order", (index + 1) * 10)),
                          overrides=overrides, tier=tier))
    return out


def project_name(name: str, vehicle_id=None) -> str:
    """The compose project for an instance: '<name>-vehicle-<id>' (or '<name>' with no vehicle id). rig owns
    this so containers are named consistently (<project>-<compose-service>-N) across launchers + bake."""
    return f"{name}-vehicle-{vehicle_id}" if vehicle_id not in (None, "") else name


def stack_summary(sensors: list[Sensor]) -> str:
    """A tier-aware count for human output, e.g. '2 sensors + 2 infra' — infra are stacks, not sensors."""
    infra = sum(1 for s in sensors if s.tier == "infra")
    sens = len(sensors) - infra
    parts = []
    if sens:
        parts.append(f"{sens} sensor{'' if sens == 1 else 's'}")
    if infra:
        parts.append(f"{infra} infra")
    return " + ".join(parts) or "0 stacks"


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
    vehicle_id = data.get("vehicle_id")
    ros_raw = data.get("ros") or {}
    ros = RosSettings(
        domain_id=_derive_domain(vehicle_id, ros_raw),
        rmw=str(ros_raw.get("rmw", "rmw_fastrtps_cpp")),
        distro=ros_raw.get("distro"),
    )

    seen: dict[str, Path] = {}
    infra = _parse_entries(data.get("infra"), "infra", root, seen)
    sensors = _parse_entries(data.get("sensors"), "sensor", root, seen)

    images = data.get("images") or {}
    image_registry = (str(images.get("registry") or "").strip()) or None
    image_tag = (str(images.get("tag") or "").strip()) or None
    data_dir = (str(data.get("data_dir") or "").strip()) or None
    return Manifest(vehicle=str(data.get("vehicle", "vehicle")), ros=ros, sensors=infra + sensors,
                    image_registry=image_registry, vehicle_id=vehicle_id, image_tag=image_tag,
                    data_dir=data_dir)
