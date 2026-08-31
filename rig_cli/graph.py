"""Graph topology — reading the graph-snapshotter's epoch files (rig-graph-capture handoff §1).

The ros2-bag-logger's graph-snapshotter sidecar (rig-infra ≥ v1.7.0, enabled by the logger config's
`graph:` block) walks the live ROS 2 graph and appends append-only, change-deduped EPOCH files under
``<run>/graph/<writer>/epoch_<UTCstamp>.yaml`` — per node FQN, its pubs/subs/service servers/clients
with types, plus the window (`first`/`last`) that graph state was observed. Bags record data; epochs
record who talked to what.

This module is the READER half of the writer-dumb/reader-smart split: the sidecar records raw node
FQNs (it knows ROS, not rig); every view — the union across epochs, namespace→instance grouping
(instance `name` IS the ROS namespace), declared-vs-observed drift against a rigging `interface:`
block, the contract scaffold — is derived here at read time, from plain YAML. No union is ever
materialized at seal: one read path serves sealed, unsealed, and crashed runs identically
(`rig graph -o` materializes on demand). rig contains no ROS, before and after this feature.

Universal node plumbing (`/rosout`, `/parameter_events`, the per-node parameter/type-description
services) is recorded raw by the writer but HIDDEN from the human view, the contract scaffold, and
the declared-vs-observed checks — every node carries it, so it is noise in an interface and would
make every instance WARN undeclared. The union (and ``-o`` output) keeps it: derived views filter,
the data never lies.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass
from pathlib import Path

from . import RigError
from .common import eprint, load_yaml

# Epoch-file edge kinds (the writer's spelling) -> the rigging `interface:` kinds (the declared
# spelling). One mapping, used by scaffold and checks, so the two grammars can't drift apart.
KIND_TO_INTERFACE = {"pubs": "publishes", "subs": "subscribes",
                     "provides": "provides", "requires": "requires"}
_KIND_TAG = {"pubs": "pub", "subs": "sub", "provides": "srv", "requires": "cli"}
_KIND_RANK = {k: i for i, k in enumerate(_KIND_TAG)}  # display order: pub, sub, srv, cli


def _n_epochs(n: int) -> str:
    return f"{n} epoch{'s' if n != 1 else ''}"

# Every node carries these — noise in any interface view (see module docstring).
_PLUMBING_TOPICS = {"/rosout", "/parameter_events"}
_PLUMBING_SERVICE_SUFFIXES = (
    "/get_parameters", "/get_parameter_types", "/set_parameters", "/set_parameters_atomically",
    "/describe_parameters", "/list_parameters", "/get_type_description",
)

UNASSIGNED = "(unassigned)"  # nodes outside every instance namespace (recorder, tools, rogue nodes)


@dataclass(frozen=True)
class Edge:
    kind: str  # pubs | subs | provides | requires
    name: str  # topic or service path
    type: str  # e.g. sensor_msgs/msg/NavSatFix


@dataclass
class Epoch:
    path: Path
    writer: str  # graph/<writer>/ — the sidecar's config name (usually the logger instance's)
    first: str   # ISO8601 UTC, second resolution
    last: str
    nodes: dict[str, tuple[Edge, ...]]  # node FQN -> edges


@dataclass
class GraphUnion:
    epochs: int
    first: str
    last: str
    nodes: dict[str, dict[Edge, tuple[str, str]]]    # FQN -> edge -> (first, last) seen
    windows: dict[str, tuple[str, str]]              # FQN -> (first, last) the NODE was seen


def is_plumbing(edge: Edge) -> bool:
    if edge.kind in ("pubs", "subs"):
        # `…/_service_event` topics are the SERVICE channel's wire form (introspection events) —
        # data for rosbag2, plumbing for every derived view: an interface contract lists the
        # service itself, and replay's TOPIC selector must not double-carry the service channel
        # (RIG_REPLAY_SERVICES replays it properly, as calls).
        return edge.name in _PLUMBING_TOPICS or edge.name.endswith("/_service_event")
    return edge.name.endswith(_PLUMBING_SERVICE_SUFFIXES)


def _stamp(value) -> str:
    """Epoch stamps arrive as datetimes (the writer emits unquoted ISO stamps — YAML timestamps),
    or as strings from hand-made fixtures. Normalize to the contract's ISO8601-Z string, which also
    makes window min/max a plain string comparison."""
    if isinstance(value, datetime.datetime):
        if value.tzinfo is not None:
            value = value.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value)


def load_epochs(run_dir: Path) -> list[Epoch]:
    """Every parseable epoch under <run>/graph/*/ (sorted by path — writer, then stamp). Missing
    graph/ dir -> [] (the feature is simply absent from this run). Read paths fail SOFT: an
    unknown schema major or an unparseable file is skipped with a warning, never an error — one
    bad epoch must not hide the rest of the run's topology."""
    epochs: list[Epoch] = []
    graph_dir = run_dir / "graph"
    if not graph_dir.is_dir():
        return epochs
    for path in sorted(graph_dir.glob("*/epoch_*.yaml")):
        try:
            doc = load_yaml(path)
        except RigError as exc:
            eprint(f"rig graph: skipping {path.name}: {exc}")
            continue
        schema = doc.get("schema")
        if schema != 1:
            eprint(f"rig graph: skipping {path.name}: unknown schema {schema!r} (this rig reads 1)")
            continue
        nodes: dict[str, tuple[Edge, ...]] = {}
        for fqn, entry in (doc.get("nodes") or {}).items():
            edges = []
            for kind, key in (("pubs", "topic"), ("subs", "topic"),
                              ("provides", "service"), ("requires", "service")):
                for e in (entry or {}).get(kind) or []:
                    if isinstance(e, dict) and e.get(key):
                        edges.append(Edge(kind=kind, name=str(e[key]), type=str(e.get("type") or "?")))
            nodes[str(fqn)] = tuple(edges)
        epochs.append(Epoch(path=path, writer=path.parent.name,
                            first=_stamp(doc.get("first") or "?"),
                            last=_stamp(doc.get("last") or "?"), nodes=nodes))
    return epochs


def union(epochs: list[Epoch]) -> GraphUnion:
    """Merge epochs into one view; per node and per edge, the window is the min/max over the epochs
    that contain it. Duplicate epochs (a restart re-observing the same graph) merge away here —
    the writer deliberately never dedups across its own restarts."""
    nodes: dict[str, dict[Edge, tuple[str, str]]] = {}
    windows: dict[str, tuple[str, str]] = {}
    for ep in epochs:
        for fqn, edges in ep.nodes.items():
            w = windows.get(fqn)
            windows[fqn] = (min(w[0], ep.first), max(w[1], ep.last)) if w else (ep.first, ep.last)
            per = nodes.setdefault(fqn, {})
            for edge in edges:
                e = per.get(edge)
                per[edge] = (min(e[0], ep.first), max(e[1], ep.last)) if e else (ep.first, ep.last)
    first = min((ep.first for ep in epochs), default="?")
    last = max((ep.last for ep in epochs), default="?")
    return GraphUnion(epochs=len(epochs), first=first, last=last, nodes=nodes, windows=windows)


def group_nodes(fqns, instance_names) -> dict[str, list[str]]:
    """Node FQN -> owning instance, by longest instance-name prefix at a segment boundary
    (instance `name` is the ROS namespace — manifest.py's identity doctrine). Leftovers group
    under UNASSIGNED. Returns {instance|UNASSIGNED: [fqn, ...]} — only non-empty groups, instance
    manifest order preserved by the caller iterating its own name list."""
    groups: dict[str, list[str]] = {}
    for fqn in sorted(fqns):
        owners = [n for n in instance_names if fqn == f"/{n}" or fqn.startswith(f"/{n}/")]
        owner = max(owners, key=len) if owners else UNASSIGNED
        groups.setdefault(owner, []).append(fqn)
    return groups


# --- the derived views -------------------------------------------------------

def render(u: GraphUnion, run_id: str, instance_names) -> str:
    """The human view: nodes grouped by instance, edges tagged pub/sub/srv/cli, plumbing hidden
    (counted in the footer). Stdout is the report — machine consumers read the epochs themselves."""
    lines = [f"graph: {run_id} — {_n_epochs(u.epochs)}, {u.first} → {u.last}"]
    groups = group_nodes(u.nodes, instance_names)
    hidden = 0
    ordered = [n for n in instance_names if n in groups] \
        + ([UNASSIGNED] if UNASSIGNED in groups else [])
    for owner in ordered:
        lines.append(f"  {owner}")
        for fqn in groups[owner]:
            lines.append(f"    {fqn}")
            edges = u.nodes[fqn]
            keep = [e for e in sorted(edges, key=lambda e: (_KIND_RANK[e.kind], e.name, e.type))
                    if not is_plumbing(e)]
            hidden += len(edges) - len(keep)
            for e in keep:
                window = ""
                if edges[e] != u.windows[fqn]:  # only when narrower than the node's own window
                    window = f"  [{edges[e][0]} → {edges[e][1]}]"
                lines.append(f"      {_KIND_TAG[e.kind]}  {e.name}  {e.type}{window}")
    if hidden:
        lines.append(f"  ({hidden} standard node plumbing edges hidden — rosout, parameter and "
                     f"type-description services; `-o` keeps everything)")
    return "\n".join(lines)


def union_yaml(u: GraphUnion, run_id: str) -> str:
    """The materialized union for `-o` — epoch-shaped (schema/first/last/nodes in the writer's edge
    spelling, plumbing INCLUDED) plus a `unioned:` key naming the epoch count, so scripts written
    against epoch files read it unchanged. Derived output: rig-authored, regenerate at will."""
    import yaml
    nodes: dict = {}
    for fqn in sorted(u.nodes):
        entry: dict = {k: [] for k in KIND_TO_INTERFACE}
        for e in sorted(u.nodes[fqn], key=lambda e: (_KIND_RANK[e.kind], e.name, e.type)):
            key = "topic" if e.kind in ("pubs", "subs") else "service"
            entry[e.kind].append({key: e.name, "type": e.type})
        nodes[fqn] = entry
    doc = {"schema": 1, "unioned": u.epochs, "first": u.first, "last": u.last, "nodes": nodes}
    return f"# rig graph union — run {run_id}, {_n_epochs(u.epochs)}. Derived; regenerate at will.\n" \
        + yaml.safe_dump(doc, sort_keys=False, default_flow_style=None)


def contract(u: GraphUnion, instance: str, service: str, run_id: str) -> str:
    """The `interface:` scaffold for one instance, from its OBSERVED edges: names under the
    instance's own namespace stripped to relative form, shared-bus names kept absolute, plumbing
    dropped, deduped across the instance's nodes. Printed for pasting into the SERVICE repo's
    rigging.yaml — never auto-written (editing a vendored, registry-pinned copy manufactures
    drift), and an observed graph is per-config: prune conditional edges before publishing."""
    prefix = f"/{instance}/"
    per_kind: dict[str, set[tuple[str, str]]] = {k: set() for k in KIND_TO_INTERFACE}
    for fqn, edges in u.nodes.items():
        if not (fqn == f"/{instance}" or fqn.startswith(prefix)):
            continue
        for e in edges:
            if is_plumbing(e):
                continue
            name = e.name[len(prefix):] if e.name.startswith(prefix) else e.name
            per_kind[e.kind].add((name, e.type))
    lines = [f"# Observed interface of instance '{instance}' (service {service}) — run {run_id}, "
             f"{_n_epochs(u.epochs)}.",
             "# Relative names are instance-namespace-relative; absolute names are shared-bus.",
             "# Paste into the SERVICE repo's rigging.yaml; prune config-specific/conditional",
             "# edges before publishing — an observed graph is per-config truth, the declaration",
             "# is the superset. Drift shows as WARNs in `rig graph --check`.",
             "interface:"]
    for kind, declared_kind in KIND_TO_INTERFACE.items():
        key = "topic" if kind in ("pubs", "subs") else "service"
        entries = sorted(per_kind[kind])
        if not entries:
            lines.append(f"  {declared_kind}: []")
            continue
        lines.append(f"  {declared_kind}:")
        lines += [f"    - {{{key}: {n}, type: {t}}}" for n, t in entries]
    return "\n".join(lines)


def check(u: GraphUnion, manifest, descriptors) -> list[str]:
    """Declared-vs-observed drift, WARN-only in BOTH directions (the provenance posture): an
    undeclared observed edge is contract drift (refresh via --contract); a declared-but-unobserved
    edge may be lazy/conditional — an observed graph is per-config truth, the declaration a
    superset. Compared by NAME per kind (relative declarations resolve under the instance
    namespace); a declared type that contradicts every observed type on that name also WARNs."""
    warns: list[str] = []
    undeclared_instances = []
    for sensor in manifest.sensors:
        desc = descriptors.get(sensor.service)
        iface = getattr(desc, "interface", None) if desc else None
        prefix = f"/{sensor.name}/"
        observed: dict[str, dict[str, set[str]]] = {k: {} for k in KIND_TO_INTERFACE}  # kind -> name -> types
        for fqn, edges in u.nodes.items():
            if not (fqn == f"/{sensor.name}" or fqn.startswith(prefix)):
                continue
            for e in edges:
                if not is_plumbing(e):
                    observed[e.kind].setdefault(e.name, set()).add(e.type)
        seen_any = any(observed[k] for k in observed)
        if iface is None:
            if seen_any:
                undeclared_instances.append(sensor.name)
            continue
        if not seen_any:
            warns.append(f"{sensor.name}: interface declared but NO nodes observed under "
                         f"/{sensor.name} — instance down for the whole run, or renamed?")
            continue
        for kind, declared_kind in KIND_TO_INTERFACE.items():
            declared = {(e.name if e.name.startswith("/") else f"/{sensor.name}/{e.name}"): e.type
                        for e in iface.get(declared_kind, ())}
            tag = _KIND_TAG[kind]
            for name in sorted(set(observed[kind]) - set(declared)):
                types = "|".join(sorted(observed[kind][name]))
                warns.append(f"{sensor.name}: observed but undeclared — {tag} {name} ({types}); "
                             f"refresh with `rig graph --contract {sensor.name}`")
            for name in sorted(set(declared) - set(observed[kind])):
                warns.append(f"{sensor.name}: declared but not observed — {tag} {name} "
                             f"(lazy/conditional, or stale declaration)")
            for name, dtype in declared.items():
                if dtype and name in observed[kind] and dtype not in observed[kind][name]:
                    warns.append(f"{sensor.name}: type drift on {tag} {name} — declared {dtype}, "
                                 f"observed {'|'.join(sorted(observed[kind][name]))}")
    if undeclared_instances:
        warns.append(f"no interface: declared for {', '.join(undeclared_instances)} — "
                     f"`rig graph --contract <name>` scaffolds one (WARN-only; declaring is opt-in)")
    return warns


# --- run resolution + the verb ----------------------------------------------

def resolve_run(manifest, ref: str | None) -> tuple[str, Path]:
    """(run-id, run-dir). Default: the OPEN run, else the newest by stamp. An explicit ref is a
    run id under the registry, or a path to a run dir anywhere (a run scp'd from a vehicle)."""
    from . import runs as runs_mod
    if ref:
        as_path = Path(ref).expanduser()
        if "/" in ref or as_path.is_dir():
            if not as_path.is_dir():
                raise RigError(f"graph: no run dir at {as_path}")
            return as_path.name, as_path
        data = runs_mod._root(manifest)
        run_dir = data / "runs" / ref
        if not run_dir.is_dir():
            raise RigError(f"graph: no run '{ref}' under {data / 'runs'} (see `rig runs`)")
        return ref, run_dir
    data = runs_mod._root(manifest)
    cur = runs_mod.current_run(data)
    if cur is not None:
        return cur[0], cur[1]
    runs = sorted(d for d in (data / "runs").iterdir() if d.is_dir()) \
        if (data / "runs").is_dir() else []
    if not runs:
        raise RigError("graph: no runs recorded (the epochs live in run dirs — see `rig runs`)")
    return runs[-1].name, runs[-1]


def cmd(manifest, descriptors, *, run_ref: str | None, do_check: bool,
        contract_instance: str | None, out: str | None) -> int:
    run_id, run_dir = resolve_run(manifest, run_ref)
    epochs = load_epochs(run_dir)
    if not epochs:
        eprint(f"rig graph: no epochs under {run_dir / 'graph'} — enable the bag-logger's "
               f"`graph:` block (rig-infra ≥ v1.7.0) to capture topology into future runs")
        return 1
    u = union(epochs)
    if contract_instance is not None:
        row = next((s for s in manifest.sensors if s.name == contract_instance), None)
        if row is None:
            raise RigError(f"graph --contract: unknown instance '{contract_instance}' "
                           f"(vehicle.yaml names: {', '.join(s.name for s in manifest.sensors)})")
        print(contract(u, row.name, row.service, run_id))
        return 0
    if out:
        Path(out).write_text(union_yaml(u, run_id))
        eprint(f"rig graph: union of {_n_epochs(u.epochs)} -> {out}")
        if not do_check:
            return 0
    else:
        print(render(u, run_id, [s.name for s in manifest.sensors]))
    if do_check:
        warns = check(u, manifest, descriptors)
        for w in warns:
            print(f"[!] {w}")
        if not warns:
            print("[✓] observed graph matches every declared interface")
    return 0
