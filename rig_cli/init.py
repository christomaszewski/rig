"""rig init — scaffold a fresh, empty deployment: the files you author for ONE vehicle/fleet (the manifest,
the catalog, and a place for per-sensor configs). The rig tool itself stays separate (cloned/installed
once); a deployment is just config that `rig --root <dir>` — or `cd <dir> && rig` — operates on.

Two optional accelerators, with a principled asymmetry between them:

``--infra <template>`` (repeatable) fully wires a bundled ``templates/`` service — example config copied,
``services.yaml`` routed, an ENABLED ``vehicle.yaml`` entry (zenoh-router pinned to order 0) — because
shared infra is decidable: a zenoh-rmw vehicle wants its router, period.

``--discover [DIR]`` scans a workspace (default: the target's parent) for rig-compatible service repos
(a ``rigging.yaml``) and populates the CATALOG — but only a commented-out MENU in ``vehicle.yaml``. A repo
in the workspace proves the code exists, not that the hardware is on this vehicle; instance names, counts,
and order are the operator's call. Discovered example configs (declared ``examples:`` in rigging.yaml
first, ``sensors/*.example.yaml`` / ``config/*.example.yaml`` glob as fallback) are copied in as starting
material. Everything wired is echoed — paths especially, since stale path assumptions are the classic
workspace foot-gun.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

import yaml

from . import RigError
from .common import eprint, load_yaml
from .descriptor import find_descriptor, load_descriptor

_VEHICLE_HEAD = """\
# vehicle.yaml — vehicle identity, fleet-wide settings, shared infra, and sensors.
vehicle: {name}
vehicle_id: {vehicle_id}           # identity; decides the ROS domain (override via ros.domain_id) + exported as
                        #   VEHICLE_ID. (Planned: fleet mode resolves this on-vehicle from a host file/env.)
ros:
  # domain_id: 0        # defaults to vehicle_id
  rmw: rmw_zenoh_cpp    # rmw_zenoh needs a zenoh-router in infra: (below); use rmw_fastrtps_cpp for DDS
  distro: lyrical
images:
  registry: ""          # where stacks pull images from (e.g. devbox:5000); empty = local images
  tag: ""               # e.g. jp7 (the target's JetPack) -> RIG_IMAGE_TAG for platform-specific composes
data_dir: ""            # host dir for recordings/logs/outputs -> RIG_DATA_DIR (e.g. /data); empty = none
"""

_SERVICES_EMPTY = """\
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
2. Add a config per sensor under `config/sensors/` (or reference a nameless profile + per-sensor
   overrides), per shared infra service under `config/infra/`, and per autonomy stack (planners, SLAM,
   perception — up last, down first) under `config/autonomy/`.
3. Validate + run: `rig doctor` · `rig up --dry-run` · `rig up` · `rig status`.
4. Deploy: `rig vendor <svc> --from <repo>` · `rig bake --registry <host> --tag <t>` · ship the artifact ·
   on the vehicle `rig unbake <artifact> && ./run.sh up`.

Run rig from the cloned/installed tool: `cd` here and `/path/to/rig/rig <verb>` (rig detects this dir by its
`vehicle.yaml`), or from anywhere with `rig --root <this-dir> <verb>`.
"""


def _tool_templates() -> Path:
    """The bundled templates dir — resolved from THIS package (the tool may live anywhere)."""
    return Path(__file__).resolve().parent.parent / "templates"


def _safe_name(token: str) -> str:
    """A ROS-/compose-safe instance-name suggestion for menu stubs: [a-z][a-z0-9_]*."""
    name = re.sub(r"[^a-z0-9_]", "_", token.lower())
    return name if name and name[0].isalpha() else f"s_{name}"


def _yaml_str(value: str) -> str:
    """A YAML-safe scalar for generated files: bare when plainly safe, JSON-quoted otherwise — so a
    dirname like `veh #1`, `05`, or `no` can't truncate, retype, or break the vehicle.yaml it seeds."""
    return value if re.match(r"^[A-Za-z][A-Za-z0-9_-]*$", value) else json.dumps(value)


def _entry(name: str, service: str, config: str, order: int) -> str:
    return f"- {{ name: {name}, service: {service}, config: {config}, enabled: true, order: {order} }}"


def _plan_templates(tokens: list[str]) -> list[tuple[str, Path, Path, str]]:
    """--infra pre-flight: resolve + validate EVERY requested template before anything is written, so a
    bad flag can't leave a half-wired target that misdiagnoses the retry. Returns
    (template, template-dir, example-src, instance) per request; raises on unknown names and
    instance-name collisions (e.g. both bag loggers declare `bag_logger` — and they mix distros anyway)."""
    plan: list[tuple[str, Path, Path, str]] = []
    seen: dict[str, str] = {}
    for raw in dict.fromkeys(tokens):  # de-dup, keep order
        t = raw.strip().strip("/")  # tab-completion slash must not fork the service key / order-0 pin
        tdir = _tool_templates() / t
        if not tdir.is_dir():
            avail = sorted(p.name for p in _tool_templates().iterdir() if p.is_dir())
            raise RigError(f"init --infra: unknown template '{raw}' — available: {', '.join(avail)}")
        examples = sorted(tdir.glob("config/*.example.yaml"))
        if not examples:
            raise RigError(f"init --infra: template '{t}' ships no config/*.example.yaml")
        instance = str(load_yaml(examples[0]).get("name") or t)
        if instance in seen:
            raise RigError(f"init --infra: '{t}' and '{seen[instance]}' collide on instance name "
                           f"'{instance}' — pick one")
        seen[instance] = t
        plan.append((t, tdir, examples[0], instance))
    return plan


def _wire_template(t: str, tdir: Path, example: Path, instance: str, target: Path,
                   services: dict[str, str], infra_rows: list[str], used_orders: list[int]) -> None:
    """--infra: wire one pre-validated template (config copy + catalog route + ENABLED manifest row)."""
    shutil.copy2(example, target / "config" / "infra" / f"{instance}.yaml")
    services[t] = os.path.relpath(tdir, target)
    order = 0 if t == "zenoh-router" else (max(used_orders, default=0) + 1)  # router FIRST, always
    used_orders.append(order)
    infra_rows.append(_entry(instance, t, f"config/infra/{instance}.yaml", order))
    eprint(f"  infra: {t} -> {services[t]} · config/infra/{instance}.yaml · order {order}")


def _copy_as_profile(src: Path, dest: Path) -> str | None:
    """Copy an example config, commenting out its top-level `name:` so it becomes a NAMELESS PROFILE —
    the manifest entry stamps the instance name in (rig's designed mechanism), instead of the example's
    placeholder name ('front', …) colliding with the menu stub's suggestion.

    The line transform only understands the plain single-line spelling, so the result is VERIFIED: if it
    no longer parses, or a top-level `name` survived (block scalar, next-line value, quoted key, …), fall
    back to a VERBATIM copy and return the example's own declared name — the caller then names the menu
    stub to match, keeping the manifest cross-check happy either way. Returns the surviving name, or
    None when the file really is nameless now."""
    lines = []
    for line in src.read_text().splitlines():
        if re.match(r"^name:\s", line):  # top-level only — indented name: keys (plugins) stay
            lines.append(f"# {line}   # (commented by rig init: the vehicle.yaml entry supplies the name)")
        else:
            lines.append(line)
    text = "\n".join(lines) + "\n"
    try:
        parsed = yaml.safe_load(text)
        survived = (parsed or {}).get("name") if isinstance(parsed, dict) else None
    except yaml.YAMLError:  # the transform broke the YAML (e.g. a block-scalar name)
        parsed, survived = None, "unparseable"
    if parsed is None or survived is not None:
        shutil.copy2(src, dest)  # verbatim; the example keeps its own name
        declared = load_yaml(src).get("name")
        return str(declared) if declared is not None else None
    dest.write_text(text)
    return None


def _repo_examples(repo: Path, declared: list[str]) -> list[Path]:
    """Example configs for a discovered repo: declared `examples:` first (missing ones warned), else the
    boilerplate-convention globs."""
    found = []
    for rel in declared:
        p = repo / rel
        if p.is_file():
            found.append(p)
        else:
            eprint(f"  discover: {repo.name}: declared example missing, skipped: {rel}")
    if found or declared:
        return found
    return sorted(repo.glob("sensors/*.example.yaml")) + sorted(repo.glob("config/*.example.yaml"))


def _discover(scan: Path, target: Path, services: dict[str, str],
              menu: dict[str, list[str]]) -> int:
    """--discover: catalog every sibling repo with a rigging.yaml; copy its examples as material; add
    commented-out MENU rows (never enabled — repo presence != hardware presence)."""
    scan = scan.resolve()  # resolve BOTH relpath ends — /tmp and /var are symlinks on macOS, and an
    if not scan.is_dir():  # unresolved side makes relpath walk to / instead of `../<repo>`
        raise RigError(f"init --discover: not a directory: {scan}")
    count = 0
    order = {"infra": 5, "sensor": 10, "autonomy": 10}
    candidates = sorted(p.resolve() for p in scan.iterdir() if p.is_dir() and not p.name.startswith("."))
    if find_descriptor(scan) is not None:  # --discover pointed AT a repo, not a workspace — take it
        candidates.insert(0, scan)
    for child in candidates:
        if child == target or find_descriptor(child) is None:
            continue
        try:
            svc = str(load_yaml(find_descriptor(child)).get("service") or child.name)
            desc = load_descriptor(svc, child)
        except RigError as exc:
            eprint(f"  discover: skipping {child.name} (bad rigging.yaml: {exc})")
            continue
        if svc in services:
            eprint(f"  discover: duplicate service '{svc}' in {child.name} — keeping the first")
            continue
        services[svc] = os.path.relpath(child, target)
        tier = desc.tier if desc.tier in ("infra", "autonomy") else "sensor"
        sub = {"infra": "infra", "sensor": "sensors", "autonomy": "autonomy"}[tier]
        copied = []
        for src in _repo_examples(child, desc.examples):
            stem = src.name[: -len(".example.yaml")] if src.name.endswith(".example.yaml") else src.stem
            dest = target / "config" / sub / f"{stem}.yaml"
            if dest.exists():
                eprint(f"  discover: {child.name}: config/{sub}/{stem}.yaml already exists, not overwritten")
                continue
            kept_name = _copy_as_profile(src, dest)  # non-None: copied verbatim, stub must use THIS name
            copied.append((stem, kept_name))
        for stem, kept_name in copied:
            stub = kept_name if kept_name else _safe_name(stem)
            menu[tier].append(f"# {_entry(stub, svc, f'config/{sub}/{stem}.yaml', order[tier])}")
            order[tier] += 5 if tier == "infra" else 10
        if not copied:
            n = _safe_name(svc)
            menu[tier].append(f"# {_entry(n, svc, f'config/{sub}/{n}.yaml', order[tier])}   # TODO: author this config")
            order[tier] += 5 if tier == "infra" else 10
        ex = ", ".join(f"config/{sub}/{s}.yaml" for s, _ in copied) or "no example found — author the config"
        eprint(f"  discover: {svc} -> {services[svc]} · {ex}")
        count += 1
    if count == 0:  # silence would read as "discovery didn't run" — say what happened and hint at the fix
        eprint(f"  discover: no service repos (rigging.yaml) found under {scan} — point --discover at the "
               f"workspace that holds your service checkouts")
    return count


def _vehicle_yaml(name: str, vehicle_id: int, infra_rows: list[str], menu: dict[str, list[str]]) -> str:
    # NOTE: section headers are bare (`infra:`, no `[]`) — an explicit `[]` closes the value, making the
    # commented rows below un-uncommentable (YAML error). A bare header parses as null; the manifest
    # loader treats that as empty, and uncommenting a row Just Works.
    lines = [_VEHICLE_HEAD.format(name=_yaml_str(name), vehicle_id=vehicle_id).rstrip("\n")]
    lines.append(f"{'infra:':<22}# shared services brought up FIRST (e.g. a zenoh router for rmw_zenoh):")
    if not infra_rows and not menu["infra"]:
        lines.append("  # - { name: zenoh-router, service: zenoh-router, config: config/infra/zenoh-router.yaml, enabled: true, order: 0 }")
    lines += [f"  {row}" for row in infra_rows]
    lines += [f"  {row}" for row in menu["infra"]]
    if menu["sensor"]:  # a discovered MENU: real rows never come from init — hardware presence is yours to declare
        lines.append(f"{'sensors:':<22}# a discovered MENU — uncomment the entries THIS vehicle runs:")
        lines += [f"  {row}" for row in menu["sensor"]]
    else:
        lines.append("sensors:")
        lines.append("  # - { name: gnss_primary, service: novatel, config: config/sensors/gnss_primary.yaml, enabled: true, order: 10 }")
    lines.append(f"{'autonomy:':<22}# graph consumers (planners, SLAM, perception) — up LAST, down FIRST:")
    if menu["autonomy"]:
        lines += [f"  {row}" for row in menu["autonomy"]]
    else:
        lines.append("  # - { name: planner, service: my-planner, config: config/autonomy/planner.yaml, enabled: true, order: 10 }")
    return "\n".join(lines) + "\n"


def _services_yaml(services: dict[str, str]) -> str:
    if not services:
        return _SERVICES_EMPTY
    width = max(len(s) for s in services) + 1
    lines = ["# services.yaml — service routing key -> where its repo lives (resolved relative to this dir).",
             "# (paths were computed at `rig init` time — re-check them if this deployment or a repo moves)",
             "services:"]
    lines += [f"  {(s + ':').ljust(width + 1)} {{ path: {p} }}" for s, p in services.items()]
    return "\n".join(lines) + "\n"


def init(target: Path, *, vehicle_id: int = 1, infra: list[str] | None = None,
         discover: Path | None = None) -> Path:
    target = target.resolve()
    if (target / "vehicle.yaml").exists():
        raise RigError(f"init: {target} already has a vehicle.yaml (refusing to overwrite)")
    # Validate EVERYTHING that can fail BEFORE the first write — a failed init must leave no partial
    # target whose leftovers wedge or misdiagnose the corrected retry.
    plan = _plan_templates(infra or [])
    if discover is not None and not discover.resolve().is_dir():
        raise RigError(f"init --discover: not a directory: {discover}")
    for sub in ("config/sensors", "config/infra", "config/autonomy", "services"):
        (target / sub).mkdir(parents=True, exist_ok=True)

    services: dict[str, str] = {}
    infra_rows: list[str] = []
    menu: dict[str, list[str]] = {"infra": [], "sensor": [], "autonomy": []}
    used_orders: list[int] = []
    for t, tdir, example, instance in plan:
        _wire_template(t, tdir, example, instance, target, services, infra_rows, used_orders)
    discovered = _discover(discover, target, services, menu) if discover is not None else 0

    (target / "vehicle.yaml").write_text(_vehicle_yaml(target.name, vehicle_id, infra_rows, menu))
    (target / "services.yaml").write_text(_services_yaml(services))
    (target / "README.md").write_text(_README.format(name=target.name))
    (target / ".gitignore").write_text("var/\n.venv/\n__pycache__/\n*.pyc\n.DS_Store\n")
    (target / "config" / "sensors" / ".gitkeep").write_text("")
    (target / "config" / "infra" / ".gitkeep").write_text("")
    (target / "config" / "autonomy" / ".gitkeep").write_text("")
    (target / "services" / ".gitkeep").write_text("")

    eprint(f"initialized rig deployment '{target.name}' at {target}")
    if discovered:
        eprint(f"  {discovered} service(s) catalogued from {discover} — vehicle.yaml holds a commented MENU;"
               f" uncomment the entries for hardware THIS vehicle carries, then `rig doctor`")
    if not infra_rows:
        eprint("  tip: rmw_zenoh needs a shared router — `rig init ... --infra zenoh-router` wires one")
    eprint("  next: edit services.yaml + vehicle.yaml, add config/{infra,sensors}/*, then `rig doctor`")
    return target
