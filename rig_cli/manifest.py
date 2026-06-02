"""vehicle.yaml — which sensors THIS machine runs + fleet-wide ROS settings.

Loading enforces the single most important correctness invariant rig owns: **globally-unique instance
`name`** across the whole vehicle. Every identity a launcher derives (compose project, external volumes,
ROS namespace, ports) comes from `name`, so two sensors sharing a name collide. We also cross-check that
each manifest entry's `service`/`name` matches the sensor config's own `service`/`name` (the launcher
trusts the config), catching drift early.
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


@dataclass(frozen=True)
class RosSettings:
    domain_id: int
    rmw: str
    distro: str | None


@dataclass
class Manifest:
    vehicle: str
    ros: RosSettings
    sensors: list[Sensor]

    def select(self, names: list[str], enabled_only: bool) -> list[Sensor]:
        """Resolve a name filter into an ordered sensor list. Explicit names override `enabled`."""
        if names:
            by_name = {s.name: s for s in self.sensors}
            missing = [n for n in names if n not in by_name]
            if missing:
                raise RigError(f"unknown sensor(s): {', '.join(missing)}")
            chosen = [by_name[n] for n in names]
        else:
            chosen = [s for s in self.sensors if s.enabled or not enabled_only]
        return sorted(chosen, key=lambda s: s.order)


def load_manifest(root: Path) -> Manifest:
    data = load_yaml(root / "vehicle.yaml")
    ros_raw = data.get("ros") or {}
    ros = RosSettings(
        domain_id=int(ros_raw.get("domain_id", 0)),
        rmw=str(ros_raw.get("rmw", "rmw_fastrtps_cpp")),
        distro=ros_raw.get("distro"),
    )

    sensors: list[Sensor] = []
    seen: dict[str, Path] = {}
    for index, entry in enumerate(data.get("sensors") or []):
        entry = entry or {}
        name, service, cfg = entry.get("name"), entry.get("service"), entry.get("config")
        if not (name and service and cfg):
            raise RigError(f"vehicle.yaml: sensor #{index} needs `name`, `service`, and `config`")

        cfg_path = Path(cfg)
        cfg_path = cfg_path if cfg_path.is_absolute() else (root / cfg_path)
        cfg_path = cfg_path.resolve()
        if not cfg_path.exists():
            raise RigError(f"sensor '{name}': config not found: {cfg_path}")

        # The base config may be a complete named instance config OR a nameless profile (no name, maybe no
        # service) that the manifest completes. If service/name ARE present they must match — catch drift.
        cdata = load_yaml(cfg_path)
        if cdata.get("service") is not None and cdata.get("service") != service:
            raise RigError(
                f"sensor '{name}': vehicle.yaml service '{service}' != config service "
                f"'{cdata.get('service')}' in {cfg_path}"
            )
        if cdata.get("name") is not None and cdata.get("name") != name:
            raise RigError(
                f"sensor '{name}': vehicle.yaml name != config name '{cdata.get('name')}' in {cfg_path}"
            )

        overrides = entry.get("overrides") or {}
        if not isinstance(overrides, dict):
            raise RigError(f"sensor '{name}': `overrides` must be a mapping")

        # THE top correctness check.
        if name in seen:
            raise RigError(
                f"duplicate sensor name '{name}' ({cfg_path} and {seen[name]}); instance names must be "
                f"unique across the vehicle — they key the compose project, volumes, and ROS namespace"
            )
        seen[name] = cfg_path

        sensors.append(
            Sensor(
                name=name,
                service=service,
                config=cfg_path,
                enabled=bool(entry.get("enabled", True)),
                order=int(entry.get("order", (index + 1) * 10)),
                overrides=overrides,
            )
        )

    return Manifest(vehicle=str(data.get("vehicle", "vehicle")), ros=ros, sensors=sensors)
