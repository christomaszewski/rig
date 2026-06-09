"""rig init — scaffold a fresh, empty deployment: the files you author for ONE vehicle/fleet (the manifest,
the catalog, and a place for per-sensor configs). The rig tool itself stays separate (cloned/installed
once); a deployment is just config that `rig --root <dir>` — or `cd <dir> && rig` — operates on."""
from __future__ import annotations

from pathlib import Path

from . import RigError
from .common import eprint

_VEHICLE = """\
# vehicle.yaml — vehicle identity, fleet-wide settings, shared infra, and sensors.
vehicle: my-vehicle
vehicle_id: 1           # identity; decides the ROS domain (override via ros.domain_id) + exported as VEHICLE_ID
ros:
  # domain_id: 0        # defaults to vehicle_id
  rmw: rmw_zenoh_cpp    # rmw_zenoh needs a zenoh-router in infra: (below); use rmw_fastrtps_cpp for DDS
  distro: lyrical
images:
  registry: ""          # where stacks pull images from (e.g. devbox:5000); empty = local images
  tag: ""               # e.g. jp7 (the target's JetPack) -> RIG_IMAGE_TAG for platform-specific composes
infra: []               # shared services brought up FIRST (e.g. a zenoh router for rmw_zenoh):
  # - { name: zenoh-router, service: zenoh-router, config: config/infra/zenoh-router.yaml, enabled: true, order: 0 }
sensors: []
  # - { name: gnss_primary, service: novatel, config: config/sensors/gnss_primary.yaml, enabled: true, order: 10 }
"""

_SERVICES = """\
# services.yaml — service routing key -> where its repo lives (resolved relative to this dir).
# Dev: point at sibling checkouts. Deploy: `rig vendor <svc> --from <repo>` copies launch surfaces under
# services/<svc>/ and you repoint here.
services: {}
  # novatel: { path: ../novatel }
"""

_README = """\
# {name} — a rig deployment

The manifest + per-sensor configs for one vehicle/fleet (no driver source lives here).

1. Edit `services.yaml` (where each service repo is) and `vehicle.yaml` (which sensors, fleet ROS env,
   image registry).
2. Add a config per sensor under `config/sensors/` (or reference a nameless profile + per-sensor overrides).
3. Validate + run: `rig doctor` · `rig up --dry-run` · `rig up` · `rig status`.
4. Deploy: `rig vendor <svc> --from <repo>` · `rig bake --registry <host> --tag <t>` · ship the artifact ·
   on the vehicle `rig unbake <artifact> && ./run.sh up`.

Run rig from the cloned/installed tool: `cd` here and `/path/to/rig/rig <verb>` (rig detects this dir by its
`vehicle.yaml`), or from anywhere with `rig --root <this-dir> <verb>`.
"""


def init(target: Path) -> Path:
    target = target.resolve()
    if (target / "vehicle.yaml").exists():
        raise RigError(f"init: {target} already has a vehicle.yaml (refusing to overwrite)")
    (target / "config" / "sensors").mkdir(parents=True, exist_ok=True)
    (target / "services").mkdir(parents=True, exist_ok=True)
    (target / "vehicle.yaml").write_text(_VEHICLE)
    (target / "services.yaml").write_text(_SERVICES)
    (target / "README.md").write_text(_README.format(name=target.name))
    (target / ".gitignore").write_text("var/\n.venv/\n__pycache__/\n*.pyc\n.DS_Store\n")
    (target / "config" / "sensors" / ".gitkeep").write_text("")
    (target / "services" / ".gitkeep").write_text("")
    eprint(f"initialized rig deployment at {target}")
    eprint("  next: edit services.yaml + vehicle.yaml, add config/sensors/*, then `rig doctor`")
    return target
