"""rig init — scaffold a fresh, empty deployment: the files you author for ONE vehicle/fleet (the manifest,
the catalog, and a place for per-sensor configs). The rig tool itself stays separate (cloned/installed
once); a deployment is just config that `rig --root <dir>` — or `cd <dir> && rig` — operates on.

Two optional accelerators, with a principled asymmetry between them:

``--infra <name|path>`` (repeatable) fully wires a shared-infra service — example config copied,
``services.yaml`` routed, an ENABLED ``vehicle.yaml`` entry (zenoh-router pinned to order 0) — because
shared infra is decidable: a zenoh-rmw vehicle wants its router, period. A value containing ``/`` is a
service-dir path; a bare name scans the workspace (the target's parent's siblings, descending ONE level
into repos whose subdirs carry a ``rigging.yaml`` — so ``zenoh-router`` finds
``../rig-infra/zenoh-router``, a checkout of https://github.com/christomaszewski/rig-infra).

Both accelerators are init-time only (init refuses an existing deployment); ``rig add <name|path>``
(`add_service`, below) is their post-init sibling — same resolution, same asymmetry, one service at a
time, appended to the EXISTING files.

``--discover [DIR]`` scans a workspace (default: the target's parent) for rig-compatible service repos
(a ``rigging.yaml``), descending ONE level into collection repos whose subdirs are the service dirs
(rig-infra), and populates the CATALOG — but only a commented-out MENU in ``vehicle.yaml``. A repo
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
import subprocess
from pathlib import Path

import yaml

from . import RigError
from .common import eprint, load_yaml
from .descriptor import find_descriptor, load_descriptor

_VEHICLE_HEAD = """\
# vehicle.yaml — vehicle identity, fleet-wide settings, shared infra, and sensors.
{identity}
ros:
  # domain_id: 0        # defaults to vehicle_id
  rmw: rmw_zenoh_cpp    # rmw_zenoh needs a zenoh-router in infra: (below); use rmw_fastrtps_cpp for DDS
  distro: lyrical
images:
  registry: ""          # where stacks pull images from (e.g. devbox:5000); empty = local images
  tag: ""               # a VERSION (e.g. v1.3.0) -> RIG_IMAGE_TAG; rig build defaults --tag to it
platform: ""            # THIS host's hardware/OS target (e.g. jp7) -> RIG_TARGET_PLATFORM; services
                        #   with a build matrix pull <image>:<tag>-<platform>. Per-host fact —
                        #   /etc/rig/vehicle.local.yaml may carry it (rig provision --platform)
data_dir: ""            # host dir for recordings/logs/outputs -> RIG_DATA_DIR (e.g. /data); empty = none
# vars:                 # per-DEPLOYMENT {{{{var}}}} defaults (CHEATSHEET §1.6); FLEET values
#   gcs_ip: 10.0.0.10   #   ({{{{fleet_ids}}}} derived from the roster, {{{{gcs_ip}}}}, {{{{fleet_mode}}}},
#                       #   fleet vars: like peer_endpoint) come from fleet.yaml — pushed by
#                       #   `rig fleet up`, beating these defaults. rig derives
#                       #   {{{{fleet_peer_ids}}}} = fleet_ids minus THIS vehicle's id:
#                       #   connect: "{{{{map fleet_peer_ids peer_endpoint}}}}"
# env:                  # exported to launchers after interpolation (for compose ${{GCS_IP}} refs):
#   GCS_IP: "{{{{gcs_ip}}}}"
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
   image registry) — or let `rig add <name|path>` wire a service in for you.
2. Add a config per sensor under `config/sensors/` (or reference a nameless profile + per-sensor
   overrides), per shared infra service under `config/infra/`, and per autonomy stack (planners, SLAM,
   perception — up last, down first) under `config/autonomy/`. Wrote the manifest rows by hand?
   `rig fetch` materializes each missing config from the routed service's example.
3. Validate + run: `rig doctor` · `rig up --dry-run` · `rig up` · `rig status`.
4. Deploy: `rig vendor <svc> --from <repo>` · `rig bake --registry <host> --tag <t>` · ship the artifact ·
   on the vehicle `rig unbake <artifact> && ./run.sh up`.

Run rig from the cloned/installed tool: `cd` here and `/path/to/rig/rig <verb>` (rig detects this dir by its
`vehicle.yaml`), or from anywhere with `rig --root <this-dir> <verb>`.
"""


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


def _resolve_service_dir(raw: str, parent: Path, *, label: str = "init --infra") -> Path:
    """Resolve one --infra / `rig add` token to a service dir. A token containing `/` is a repo path
    (cwd-relative, like the target argument itself). A bare name scans the workspace — the parent's
    siblings, descending ONE level into repos whose subdirs carry a rigging.yaml (so `zenoh-router`
    finds a sibling checkout OR `../rig-infra/zenoh-router`). Ambiguity is an error: the operator
    disambiguates with a path."""
    token = raw.strip().rstrip("/")  # tab-completion slash must not fork the service key / order-0 pin
    if "/" in token:
        tdir = Path(token).expanduser().resolve()
        if not tdir.is_dir() or find_descriptor(tdir) is None:
            raise RigError(f"{label}: not a service dir (no rigging.yaml): {raw}")
        return tdir
    hits: list[Path] = []
    if parent.is_dir():
        for repo in sorted(p for p in parent.iterdir() if p.is_dir() and not p.name.startswith(".")):
            cand = repo if repo.name == token else repo / token  # direct sibling OR one-level descent
            if cand.is_dir() and find_descriptor(cand) is not None:
                hits.append(cand.resolve())
    hits = list(dict.fromkeys(hits))
    if len(hits) > 1:
        raise RigError(f"{label}: '{token}' is ambiguous in the workspace: "
                       f"{', '.join(str(h) for h in hits)} — pass a path instead")
    if hits:
        return hits[0]
    raise RigError(f"{label}: unknown service '{raw}' — no service dir under {parent} (one level "
                   f"deep); clone https://github.com/christomaszewski/rig-infra beside your "
                   f"deployment, or pass a path")


def _plan_infra(tokens: list[str], parent: Path) -> list[tuple[str, Path, Path, str]]:
    """--infra pre-flight: resolve + validate EVERY requested service before anything is written, so a
    bad flag can't leave a half-wired target that misdiagnoses the retry. Returns
    (service, service-dir, example-src, instance) per request; raises on unknown names and
    instance-name collisions (e.g. both bag loggers declare `bag_logger` — and they mix distros anyway).
    The catalog key is the DESCRIPTOR's `service` (matches --discover routing), not the token."""
    plan: list[tuple[str, Path, Path, str]] = []
    seen: dict[str, str] = {}
    for raw in dict.fromkeys(tokens):  # de-dup, keep order
        tdir = _resolve_service_dir(raw, parent)
        desc = find_descriptor(tdir)  # never None: _resolve_service_dir only returns descriptor-bearing dirs
        svc = str(load_yaml(desc).get("service") or tdir.name)
        examples = sorted(tdir.glob("config/*.example.yaml"))
        if not examples:
            raise RigError(f"init --infra: '{svc}' ({tdir}) ships no config/*.example.yaml")
        instance = str(load_yaml(examples[0]).get("name") or svc)
        if instance in seen:
            raise RigError(f"init --infra: '{svc}' and '{seen[instance]}' collide on instance name "
                           f"'{instance}' — pick one")
        seen[instance] = svc
        plan.append((svc, tdir, examples[0], instance))
    return plan


def _wire_infra(t: str, tdir: Path, example: Path, instance: str, target: Path,
                   services: dict[str, str], infra_rows: list[str], used_orders: list[int]) -> None:
    """--infra: wire one pre-validated infra service (config copy + catalog route + ENABLED manifest row)."""
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
    children = sorted(p.resolve() for p in scan.iterdir() if p.is_dir() and not p.name.startswith("."))
    if find_descriptor(scan) is not None:  # --discover pointed AT a repo, not a workspace — take it
        children.insert(0, scan)
    candidates: list[Path] = []
    for child in children:
        if child == target:
            continue
        if find_descriptor(child) is not None:
            candidates.append(child)
            continue
        # One-level descent: a collection repo whose SUBDIRS are the service dirs (rig-infra). Bounded
        # depth by design; skip hidden dirs and `var/` (launcher-rendered state, never a service).
        candidates += sorted(
            sub.resolve() for sub in child.iterdir()
            if sub.is_dir() and not sub.name.startswith(".") and sub.name != "var"
            and find_descriptor(sub) is not None
        )
    for child in candidates:
        if find_descriptor(child) is None:
            continue
        try:
            svc = str(load_yaml(find_descriptor(child)).get("service") or child.name)
            desc = load_descriptor(svc, child)
        except RigError as exc:
            eprint(f"  discover: skipping {child.name} (bad rigging.yaml: {exc})")
            continue
        if svc in services:
            # The same dir arriving twice is the DESIGNED --infra ∩ --discover overlap (both resolve
            # from the workspace since v0.1.28) — already routed and reported, so stay quiet. The loud
            # warning is reserved for a genuine conflict: two different dirs claiming one service key.
            if (target / services[svc]).resolve() == child:
                continue
            eprint(f"  discover: duplicate service '{svc}' at {os.path.relpath(child, target)} — "
                   f"keeping {services[svc]}")
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


_IDENTITY_MARKERS = """\
vehicle: "{{vehicle}}"       # identity — per-HOST MARKERS by default (v0.2.20): nothing comes up as
vehicle_id: "{{vehicle_id}}" #   "vehicle 1" by accident. Each machine supplies its own: `sudo rig
                        #   provision --id N --name X` (/etc/rig/vehicle.local.yaml), or
                        #   RIG_VEHICLE_ID / RIG_VEHICLE_NAME, or a bench vehicle.local.yaml beside
                        #   this file. vehicle_id decides the ROS domain (override via
                        #   ros.domain_id) + is exported as VEHICLE_ID. A single-vehicle tree may
                        #   pin literals instead: `rig init <dir> --vehicle-id N`."""

_IDENTITY_LITERAL = """\
vehicle: {name}
vehicle_id: {vehicle_id}           # identity; decides the ROS domain (override via ros.domain_id) + exported as
                        #   VEHICLE_ID. Literal = a single-vehicle tree; per-HOST tiers still override
                        #   (`sudo rig provision`, RIG_VEHICLE_ID). Fleet trees use "{{{{vehicle_id}}}}"."""


def _identity_block(name: str, vehicle_id: int | None) -> str:
    """The identity lines: MARKERS (per-host, mandatory-from-local) unless --vehicle-id pinned a
    literal — then the dir name + id, the single-vehicle shape."""
    if vehicle_id is None:
        return _IDENTITY_MARKERS
    return _IDENTITY_LITERAL.format(name=_yaml_str(name), vehicle_id=vehicle_id)


def _vehicle_yaml(name: str, vehicle_id: int | None, infra_rows: list[str],
                  menu: dict[str, list[str]]) -> str:
    # NOTE: section headers are bare (`infra:`, no `[]`) — an explicit `[]` closes the value, making the
    # commented rows below un-uncommentable (YAML error). A bare header parses as null; the manifest
    # loader treats that as empty, and uncommenting a row Just Works.
    lines = [_VEHICLE_HEAD.format(identity=_identity_block(name, vehicle_id)).rstrip("\n")]
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


def _git_init_deployment(target: Path) -> None:
    """OQ-9's rollback affordance: a deployment is born a git repo, so operator-level rollback is
    always `git`. Skipped when git is absent or the target already lives inside a worktree (never
    nest); rig itself never commits again after this."""
    if shutil.which("git") is None:
        eprint("  git: not found — skipping repo init (version-control the deployment when you can)")
        return

    def run(*args):
        return subprocess.run(["git", "-C", str(target), *args], capture_output=True, text=True)

    if run("rev-parse", "--is-inside-work-tree").stdout.strip() == "true":
        eprint("  git: already inside a worktree — not nesting a repo")
        return
    if run("init", "-q").returncode != 0:
        eprint("  git: init failed — skipping")
        return
    run("add", "-A")
    commit = run("commit", "-q", "-m", "rig init: deployment scaffold")
    if commit.returncode != 0:  # fresh machine without git identity — commit with a local fallback
        commit = run("-c", "user.name=rig", "-c", "user.email=rig@localhost",
                     "commit", "-q", "-m", "rig init: deployment scaffold")
    eprint("  git: repo initialized, scaffold committed" if commit.returncode == 0
           else "  git: repo created; initial commit failed (configure git user.name/email)")


def init(target: Path, *, vehicle_id: int | None = None, infra: list[str] | None = None,
         discover: Path | None = None, no_git: bool = False) -> Path:
    target = target.resolve()
    if (target / "vehicle.yaml").exists():
        raise RigError(f"init: {target} already has a vehicle.yaml (refusing to overwrite)")
    # Validate EVERYTHING that can fail BEFORE the first write — a failed init must leave no partial
    # target whose leftovers wedge or misdiagnose the corrected retry.
    plan = _plan_infra(infra or [], target.parent)
    if discover is not None and not discover.resolve().is_dir():
        raise RigError(f"init --discover: not a directory: {discover}")
    for sub in ("config/sensors", "config/infra", "config/autonomy", "services"):
        (target / sub).mkdir(parents=True, exist_ok=True)

    services: dict[str, str] = {}
    infra_rows: list[str] = []
    menu: dict[str, list[str]] = {"infra": [], "sensor": [], "autonomy": []}
    used_orders: list[int] = []
    for t, tdir, example, instance in plan:
        _wire_infra(t, tdir, example, instance, target, services, infra_rows, used_orders)
    discovered = _discover(discover, target, services, menu) if discover is not None else 0

    (target / "vehicle.yaml").write_text(_vehicle_yaml(target.name, vehicle_id, infra_rows, menu))
    (target / "services.yaml").write_text(_services_yaml(services))
    (target / "README.md").write_text(_README.format(name=target.name))
    (target / ".gitignore").write_text(  # vehicle.local.yaml + fleet.yaml are OPERATIONAL state
        "var/\n.venv/\n__pycache__/\n*.pyc\n.DS_Store\n"     # (bench identity / test-day roster)
        "vehicle.local.yaml\nfleet.yaml\n")                  # — committed only by choice
    (target / "config" / "sensors" / ".gitkeep").write_text("")
    (target / "config" / "infra" / ".gitkeep").write_text("")
    (target / "config" / "autonomy" / ".gitkeep").write_text("")
    (target / "services" / ".gitkeep").write_text("")

    eprint(f"initialized rig deployment '{target.name}' at {target}")
    if not no_git:
        _git_init_deployment(target)
    if discovered:
        eprint(f"  {discovered} service(s) catalogued from {discover} — vehicle.yaml holds a commented MENU;"
               f" uncomment the entries for hardware THIS vehicle carries, then `rig doctor`")
    if not infra_rows:
        eprint("  tip: rmw_zenoh needs a shared router — `rig init ... --infra zenoh-router` wires one")
    eprint("  next: edit services.yaml + vehicle.yaml, add config/{infra,sensors}/*, then `rig doctor`")
    return target


# --- rig add: wire ONE more service into an EXISTING deployment ------------------------------------

def _append_services_line(text: str, line: str) -> str | None:
    """Insert a route line into services.yaml's block-form `services:` mapping (handling the generated
    empty `services: {}` spelling by opening it). None = the file isn't in a shape rig can line-append
    to safely (e.g. hand-authored flow style `services: { ... }`)."""
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if re.match(r"^services:\s*(#.*)?$", ln):
            return "\n".join(lines[: i + 1] + [line] + lines[i + 1 :]) + "\n"
        if re.match(r"^services:\s*\{\}\s*(#.*)?$", ln):  # generated empty catalog — open the mapping
            return "\n".join(lines[:i] + ["services:", line] + lines[i + 1 :]) + "\n"
        if re.match(r"^services:", ln):
            return None
    return None


def _append_tier_row(text: str, section: str, row: str) -> str | None:
    """Insert a manifest row at the END of a tier section (`infra:`/`sensors:`/`autonomy:`) in
    vehicle.yaml. A missing section is appended as a new top-level block (older deployments predate
    `autonomy:`). None = the section exists but in a flow shape (`sensors: [...]`) rig won't edit."""
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if re.match(rf"^{section}:\s*(#.*)?$", ln):
            j = i + 1  # walk to the end of the section: its content is indented (rows, comments, blanks)
            while j < len(lines) and (not lines[j].strip() or lines[j].startswith(" ")):
                j += 1
            while j > i + 1 and not lines[j - 1].strip():  # keep trailing blanks below the new row
                j -= 1
            return "\n".join(lines[:j] + [f"  {row}"] + lines[j:]) + "\n"
        if re.match(rf"^{section}:", ln):
            return None
    return "\n".join(lines + ["", f"{section}:", f"  {row}"]) + "\n"


def add_service(root: Path, token: str, tier: str | None = None) -> int:
    """`rig add <name|path>` — the post-init sibling of --infra/--discover, for a deployment that
    already exists (init refuses those). Resolves the token exactly like --infra (path form, or a
    bare name via the one-level workspace scan), copies the example config, routes services.yaml, and
    adds the vehicle.yaml row with init's own asymmetry: an infra-tier service gets an ENABLED row
    (shared infra is decidable), a sensor/autonomy-tier service gets a COMMENTED menu row (repo
    presence != hardware presence — enabling stays the operator's call).

    This is the ONE place rig edits operator-owned files, so it is belt-and-braces: parse first and
    refuse duplicates; append line-oriented entries only where the file has the generated block shape;
    re-parse and re-load the manifest after writing, restoring both files if that fails; and when a
    file is hand-authored in a shape rig can't safely append to (flow style), print the exact
    paste-ready lines instead of editing. The worst case is never a mangled manifest — just a snippet."""
    from .catalog import load_catalog
    from .manifest import load_manifest

    root = root.resolve()  # BOTH relpath ends must be resolved — /tmp and /var are symlinks on macOS,
    #                        and an unresolved side makes relpath walk to / instead of `../<repo>`
    veh_path, svc_path = root / "vehicle.yaml", root / "services.yaml"
    if not veh_path.exists():
        raise RigError(f"add: {root} is not a rig deployment (no vehicle.yaml) — `rig init` creates one")
    tdir = _resolve_service_dir(token, root.parent, label="add")
    desc_path = find_descriptor(tdir)
    svc = str(load_yaml(desc_path).get("service") or tdir.name) if desc_path else tdir.name
    desc = load_descriptor(svc, tdir)  # full validation — a tier typo errors here, not in the manifest
    declared = desc.tier if desc.tier in ("infra", "autonomy") else "sensor"
    if tier is not None and tier not in ("infra", "sensor", "autonomy"):
        raise RigError(f"add: --tier must be infra, sensor, or autonomy, not '{tier}'")
    if tier and tier != declared:  # the section you place a row in IS its runtime tier — rig honors it;
        #                            the note nudges toward fixing the declaration when the repo is yours
        eprint(f"  add: tier '{tier}' overrides {svc}'s declared '{declared}' — if the repo is yours, "
               f"declare `tier: {tier}` in its rigging.yaml so every deployment benefits")
    tier = tier or declared
    sub = {"infra": "infra", "sensor": "sensors", "autonomy": "autonomy"}[tier]

    routes = ((load_yaml(svc_path) or {}).get("services") or {}) if svc_path.exists() else {}
    if svc in routes:
        raise RigError(f"add: '{svc}' is already routed at {(routes[svc] or {}).get('path')} — a second "
                       f"INSTANCE is a vehicle.yaml row (unique name, per-entry overrides), not a "
                       f"second route")
    manifest = load_manifest(root)  # add needs a loadable deployment; also names/orders below
    rel = os.path.relpath(tdir, root)
    (root / "config" / sub).mkdir(parents=True, exist_ok=True)
    examples = _repo_examples(tdir, desc.examples)

    enabled_row = menu_row = None
    if tier == "infra" and examples:
        instance = str(load_yaml(examples[0]).get("name") or svc)
        if any(s.name == instance for s in manifest.sensors):
            raise RigError(f"add: instance name '{instance}' (from {examples[0].name}) already exists "
                           f"in vehicle.yaml — wire this one manually with a unique name")
        dest = root / "config" / "infra" / f"{instance}.yaml"
        if dest.exists():
            eprint(f"  add: config/infra/{instance}.yaml already exists — keeping it")
        else:
            shutil.copy2(examples[0], dest)
        orders = [s.order for s in manifest.sensors if s.tier == "infra"]
        order = 0 if svc == "zenoh-router" else max(orders, default=0) + 5  # router FIRST, always
        enabled_row = _entry(instance, svc, f"config/infra/{instance}.yaml", order)
    else:  # sensor/autonomy (and an example-less infra service): MENU only, never auto-enabled
        orders = [s.order for s in manifest.sensors if s.tier == tier]
        order = max(orders, default=0) + (5 if tier == "infra" else 10)
        if examples:
            src = examples[0]
            stem = src.name[: -len(".example.yaml")] if src.name.endswith(".example.yaml") else src.stem
            dest = root / "config" / sub / f"{stem}.yaml"
            if dest.exists():
                eprint(f"  add: config/{sub}/{stem}.yaml already exists — keeping it")
                kept = (load_yaml(dest) or {}).get("name")  # stub must match, or uncommenting cross-errors
            else:
                kept = _copy_as_profile(src, dest)
            stub = str(kept) if kept else _safe_name(stem)
            menu_row = f"# {_entry(stub, svc, f'config/{sub}/{stem}.yaml', order)}"
        else:
            n = _safe_name(svc)
            menu_row = f"# {_entry(n, svc, f'config/{sub}/{n}.yaml', order)}   # TODO: author this config"

    svc_line = f"  {svc}: {{ path: {rel} }}"
    veh_row = enabled_row or menu_row
    snippet = (f"    services.yaml, under `services:`:\n    {svc_line}\n"
               f"    vehicle.yaml, under `{sub}:`:\n      {veh_row}")
    orig_svc, orig_veh = svc_path.read_text() if svc_path.exists() else "services:\n", veh_path.read_text()
    new_svc = _append_services_line(orig_svc, svc_line)
    new_veh = _append_tier_row(orig_veh, sub, veh_row)
    if new_svc is None or new_veh is None:
        which = "services.yaml" if new_svc is None else "vehicle.yaml"
        eprint(f"add: {which} isn't in the generated block form — leaving BOTH files untouched "
               f"(config material copied); paste this yourself:\n{snippet}")
        return 0
    for text, name in ((new_svc, "services.yaml"), (new_veh, "vehicle.yaml")):
        try:
            yaml.safe_load(text)
        except yaml.YAMLError as exc:  # belt: never write a file that will not parse
            raise RigError(f"add: refusing to write {name} — the edited result would not parse ({exc}); "
                           f"wire it manually:\n{snippet}")
    svc_path.write_text(new_svc)
    veh_path.write_text(new_veh)
    try:  # braces: the real gates (name uniqueness, config cross-checks, route resolution) — undo on any failure
        load_catalog(root)
        load_manifest(root)
    except Exception as exc:
        svc_path.write_text(orig_svc)
        veh_path.write_text(orig_veh)
        raise RigError(f"add: verification failed after edit ({exc}) — both files restored; "
                       f"wire it manually:\n{snippet}")

    if enabled_row:
        eprint(f"add: {svc} -> {rel} · config/infra/{instance}.yaml · ENABLED, order {order}")
    else:
        eprint(f"add: {svc} -> {rel} · commented menu row under `{sub}:` — uncomment it when this "
               f"vehicle actually runs the stack")
    eprint("  next: `rig doctor`")
    return 0


# --- rig fetch: materialize example configs for hand-authored manifests ----------------------------

def fetch(root: Path) -> int:
    """`rig fetch` — the missing support for the HAND-AUTHORED workflow: `rig init`, write
    vehicle.yaml + services.yaml yourself, then fetch materializes the config files. Until it runs,
    the deployment is unloadable (load_manifest errors on a row whose config file is missing), so
    fetch deliberately reads vehicle.yaml RAW instead of through the manifest loader.

    Two passes, mirroring the two directions people author from:
    1. vehicle.yaml-driven — every row whose `config:` path is missing gets the routed service's first
       declared example copied TO THAT PATH as a nameless profile (the row stamps the name).
    2. services.yaml-driven — routed services referenced by NO row get their examples copied into
       config/{infra,sensors,autonomy}/ as material, with a suggested row ECHOED, never written.

    The contract: fetch never touches vehicle.yaml or services.yaml and never overwrites an existing
    config — only missing files are created, so reruns are no-ops. (`rig pull` is images; `rig fetch`
    is configs.)"""
    from .manifest import load_manifest

    root = root.resolve()
    veh_path, svc_path = root / "vehicle.yaml", root / "services.yaml"
    if not veh_path.exists():
        raise RigError(f"fetch: {root} is not a rig deployment (no vehicle.yaml) — `rig init` creates one")
    data = load_yaml(veh_path) or {}
    routes = ((load_yaml(svc_path) or {}).get("services") or {}) if svc_path.exists() else {}

    repos: dict[str, Path] = {}
    descs: dict[str, object] = {}
    for svc, entry in routes.items():
        p = Path(str((entry or {}).get("path") or ""))
        repo = (p if p.is_absolute() else root / p).resolve()
        try:
            descs[svc] = load_descriptor(svc, repo)
            repos[svc] = repo
        except RigError as exc:  # a dead route must not abort the whole pass — report, keep going
            eprint(f"  fetch: route '{svc}' unusable ({exc}); skipped")

    fetched, present = 0, 0
    referenced: set[str] = set()
    for key, tier in (("infra", "infra"), ("sensors", "sensor"), ("autonomy", "autonomy")):
        for entry in data.get(key) or []:
            entry = entry or {}
            name, svc, cfg = entry.get("name"), entry.get("service"), entry.get("config")
            if not (name and svc and cfg):
                continue  # malformed rows are doctor's report, not fetch's
            referenced.add(svc)
            cfg_path = Path(str(cfg))
            cfg_path = cfg_path if cfg_path.is_absolute() else root / cfg_path
            if cfg_path.exists():
                present += 1
                continue
            if svc not in repos:
                eprint(f"  fetch: row '{name}' needs service '{svc}', which services.yaml doesn't "
                       f"route (or its route failed above) — cannot fetch {cfg}")
                continue
            examples = _repo_examples(repos[svc], descs[svc].examples)
            if not examples:
                eprint(f"  fetch: row '{name}' ({svc}): no example config in {repos[svc]} — "
                       f"author {cfg} yourself")
                continue
            src = examples[0]
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            surviving = _copy_as_profile(src, cfg_path)  # nameless profile: the ROW stamps the name
            note = "nameless profile"
            if surviving and surviving != name:
                note = f"VERBATIM — kept its own name '{surviving}'"
                eprint(f"  fetch: WARNING — {cfg} kept the example's name '{surviving}' (rig couldn't "
                       f"neutralize its spelling) but the row says '{name}'; align them or the "
                       f"manifest cross-check will error")
            elif surviving:
                note = "verbatim"
            extra = f"; also available: {', '.join(e.name for e in examples[1:])}" if len(examples) > 1 else ""
            eprint(f"  fetch: {name} ({svc}) <- {src.relative_to(repos[svc])} ({note}{extra})")
            fetched += 1

    for svc in routes:
        if svc in referenced or svc not in repos:
            continue
        desc = descs[svc]
        tier = desc.tier if desc.tier in ("infra", "autonomy") else "sensor"
        sub = {"infra": "infra", "sensor": "sensors", "autonomy": "autonomy"}[tier]
        for src in _repo_examples(repos[svc], desc.examples):
            stem = src.name[: -len(".example.yaml")] if src.name.endswith(".example.yaml") else src.stem
            dest = root / "config" / sub / f"{stem}.yaml"
            if dest.exists():
                present += 1
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            kept_name = _copy_as_profile(src, dest)
            stub = str(kept_name) if kept_name else _safe_name(stem)
            eprint(f"  fetch: material for un-referenced service '{svc}' -> config/{sub}/{stem}.yaml")
            eprint(f"         suggested row (under `{sub}:`): "
                   f"{_entry(stub, svc, f'config/{sub}/{stem}.yaml', 10)}")
            fetched += 1

    eprint(f"rig fetch: {fetched} config(s) fetched, {present} already present (never overwritten)")
    try:  # tell the operator whether the hand-authored deployment is now loadable
        load_manifest(root)
        eprint("  manifest loads — next: `rig doctor`")
    except RigError as exc:
        eprint(f"  manifest still doesn't load ({exc}) — finish the rows above, then `rig doctor`")
    return 0
