"""SIL replay — play a sealed run's recorded topics back through updated services (rig-replay-plan;
player contract frozen in ~/ws/infra/rig-replay-player-handoff.md §1).

`rig replay <run> <names…>` opens a NEW provenance-linked run and brings up: every enabled infra
row + the named instances (the services UNDER TEST, live) + the deployment's `ros2-bag-player` row
(declared in `autonomy:` with a high order, `enabled: false` — explicit names win in
Manifest.select, so the disabled row needs nothing special). The player joins the graph and plays
the SOURCE run's bags; the bag-logger rides along recording the new outputs — source bag holds the
original outputs, the replay run holds the new ones: a provenance-linked A/B pair.

Topic selection (the crux): PRIMARY = the source run's graph epochs (rig-graph-plan, rig-infra ≥
v1.7.0) — the with-set's OBSERVED subscribes minus its observed publishes. The subtraction is the
self-echo guard: a topic both produced and consumed inside the with-set is regenerated live, never
replayed (the services must not hear their own past outputs). FALLBACK for pre-epoch source runs =
the namespace heuristic (instance name IS the ROS namespace): play everything outside the
with-set, as one exclusion regex — loudly WARNed, it can't see cross-namespace inputs. ONE
selector mode per invocation: `RIG_REPLAY_TOPICS` (space-separated allow-list) XOR
`RIG_REPLAY_EXCLUDE` (regex), never both — the player refuses both-set as defense in depth.

Clock coherence is ONE rig-owned token: `RIG_SIM_TIME=1` (absent under `--wall-clock`, and under
every other verb — set-or-popped). The player derives `--clock` from it; service launchers wire
`use_sim_time` from the same var (their own adoption, the v0.2.34 arc). Two consumers, one token —
the incoherent state is unrepresentable.

The clean-host guard: replay refuses while ANY of this manifest's stacks run (not just conflicting
sensors). Recorders pin their run dir at process start — a logger surviving from a previous
session would keep writing into the OLD run, splitting the replay session's provenance. Fail
closed like the rotation guard; `--force` is the human override.

rig stays schema-opaque throughout: it exports env and dispatches; the player owns every rosbag2
mechanic. `RIG_REPLAY_SOURCE` is deliberately fleet-general (every launcher in the up-set gets
it).

Per-sensor SOURCES (ROADMAP §2, rig-sensor-replay-plan): an instance whose rigging declares
`replay.source` and whose recordings exist in the source run (`<run>/<data>`) is a SOURCE — fed
from its own recordings, not the bag. Sources are FOUND, never named: `rig replay <run>` with no
names REPRODUCES the run (infra + every source + the player for what the bag holds), and
`rig replay <run> <names…>` keeps its meaning — the names are under test, live — with the sources
riding along (a named source is still a source: a replay has no live camera to offer it; that is
the "new camera-service against this run's video" case). `--live NAME` forces one live (HIL).
`--skip-service SERVICE` omits all instances of that service and their recorded bag topics
(e.g. camera-service for a replay without cameras). The source run stays untouched.
A source's config is rendered for the session with the descriptor's `overrides` patch on top
(resolve.materialize's verb-time layer) — the next plain `up` re-renders without it. Every source
gets one timeline: the bag's zero as `replay_epoch_unix_ns`, one release instant
(`replay_start_at_unix_s` = now + --start-delay), the window, and the clock decision
(`replay_retime`: original under sim time, wall otherwise). No bags → no player, wall clock.
"""
from __future__ import annotations

import dataclasses
import re
import time
from pathlib import Path

from . import RigError, dispatch, doctor as doctor_mod, graph as graph_mod, runs as runs_mod
from .common import eprint, load_yaml
from .manifest import PLAYER_SERVICE  # noqa: F401 — re-exported: replay.PLAYER_SERVICE is the spelling callers use
# Windows (`--from`/`--to`, rig-replay-window-handoff): seconds from BAG START — the ONE zero
# shared with call scripts, results.yaml and export-calls, so a script means the same thing under
# any window. rig validates `0 <= from < to`, exports RIG_REPLAY_FROM_S / RIG_REPLAY_TO_S, and
# records `replay.window` (selection provenance, beside `with`); the player (rig-infra ≥ v1.12.0)
# maps them to --start-offset + the end bound, restores the latched topics the offset would skip,
# and filters the call script. `--auto-end` composes: the player exits at the window end.


def resolve_source(manifest, ref: str, *, require_bags: bool = True) -> tuple[str, Path]:
    """(run-id, run-dir) for the SOURCE run: an id under the registry, or a path to a run dir
    anywhere (a run scp'd off a vehicle). Refuses the OPEN run (a recorder may still be writing
    it); WARNs on an unsealed source (`ended:` absent — bags may be incomplete); refuses a run
    with no bags/ (nothing to play) unless the caller can also play instance recordings
    (`require_bags=False`: cmd decides once it knows the sources)."""
    run_id, run_dir = runs_mod.resolve_ref(manifest, ref, verb="replay")  # id | label | path;
    #                                                   the host registry read-through
    if runs_mod.is_open(manifest, run_dir):
        raise RigError(f"replay: {run_id} is the OPEN run — a recorder may still be writing "
                       f"it; `rig down --end-run` first")
    doc: dict = {}
    mpath = run_dir / "manifest.yaml"
    if mpath.exists():
        try:
            doc = load_yaml(mpath)
        except RigError:
            eprint(f"rig replay: warning: {mpath} is not parseable — treating as unsealed")
    if not doc.get("ended"):
        eprint(f"rig replay: warning: source run {run_id} is not sealed (`ended:` absent) — "
               f"the recording may be incomplete")
    if require_bags and not (run_dir / "bags").is_dir():
        raise RigError(f"replay: {run_id} has no bags/ — nothing to play")
    return run_id, run_dir


def source_manifest(src_dir: Path) -> dict:
    """The source run's manifest.yaml as a dict ({} when absent/unparseable — read-side, fail-soft)."""
    mpath = src_dir / "manifest.yaml"
    if not mpath.exists():
        return {}
    try:
        doc = load_yaml(mpath)
    except RigError:
        return {}
    return doc if isinstance(doc, dict) else {}


# ---- per-sensor sources (instances replaying their OWN recordings) ------------------------------

@dataclasses.dataclass(frozen=True)
class SourceSpec:
    row: object       # the manifest row (Sensor)
    rel: str          # run-relative recordings dir (the descriptor's `data`, {name} filled)
    path: Path        # <source run>/<rel>


def discover_sources(manifest, descriptors, src_dir: Path, *, live: set[str] = frozenset()) \
        -> tuple[list[SourceSpec], list[str]]:
    """(sources, notices): every row whose rigging declares `replay.source` AND whose recordings
    exist in the source run is a source — data presence is the evidence, the run manifest's
    `stacks` only informs the notices. `live` names are forced live (HIL) even with recordings."""
    out: list[SourceSpec] = []
    notes: list[str] = []
    stacks = {str(n) for n in (source_manifest(src_dir).get("stacks") or [])}
    for row in manifest.sensors:
        rs = getattr(descriptors.get(row.service), "replay_source", None)
        if rs is None:
            # A reconstructed tree carries the service AS IT RAN: a capture from before the
            # service could replay its recordings has no `replay.source` in its vendored rigging
            # (and an image that could not honour it). The recordings are there; the code is not.
            rec = src_dir / "recordings" / row.name
            if rec.is_dir() and any(rec.iterdir()):
                notes.append(f"{row.name}: the source run holds its recordings (recordings/{row.name}) "
                             f"but {row.service}'s rigging declares no replay.source — this tree's "
                             f"{row.service} predates replay; `rig swap {row.name} <a current "
                             f"{row.service} checkout>` and replay again")
            continue
        rel = rs.data_path(row.name)
        path = src_dir / rel
        has = path.is_dir() and any(path.iterdir())
        if row.name in live:
            if has:
                notes.append(f"{row.name}: --live — comes up live although the source run holds "
                             f"its recordings ({rel})")
            continue
        if not has:
            if not stacks or row.name in stacks:
                notes.append(f"{row.name}: no recordings under {rel} in the source run — not "
                             f"replayed (was its recorder in standby?)")
            continue
        if stacks and row.name not in stacks:
            notes.append(f"{row.name}: recordings under {rel} although the source run's stacks "
                         f"never listed it — replayed anyway")
        out.append(SourceSpec(row=row, rel=rel, path=path))
    return out, notes


def skipped_topic_pattern(src_dir: Path, skipped: list[str], known: set[str]) -> tuple[str, list[str]]:
    """Exclude skipped namespaces plus their observed, exclusively published topics.
    Shared topics outside those namespaces stay: dropping /tf would also lose other sensors'
    transforms. Without graph epochs, only the namespace contract can identify ownership."""
    pattern = "^/(?:" + "|".join(re.escape(n) for n in skipped) + ")(?:/.*)?$"
    epochs = graph_mod.load_epochs(src_dir)
    if not epochs:
        return pattern, ["--skip-service: no graph epochs — excluding bag topics by instance "
                         "namespace only; remapped topics outside those namespaces cannot be identified"]
    u = graph_mod.union(epochs)
    groups = graph_mod.group_nodes(u.nodes, sorted(known))
    skipped_nodes = {fqn for name in skipped for fqn in groups.get(name, ())}
    omitted, kept = set(), set()
    for fqn, edges in u.nodes.items():
        pubs = {e.name for e in edges if e.kind == "pubs" and not graph_mod.is_plumbing(e)}
        (omitted if fqn in skipped_nodes else kept).update(pubs)
    exclusive = sorted(t for t in omitted - kept if not re.search(pattern, t))
    if exclusive:
        pattern += "|^(?:" + "|".join(re.escape(t) for t in exclusive) + ")$"
    return pattern, []


def bag_epoch_ns(src_dir: Path) -> int | None:
    """The bag's zero — the earliest `starting_time` across `<run>/bags/*/*/metadata.yaml`
    (rosbag2's plain-YAML index; the ONE zero windows and call scripts count from). None when
    no readable metadata."""
    best: int | None = None
    for meta in sorted((src_dir / "bags").glob("*/*/metadata.yaml")):
        try:
            doc = load_yaml(meta)
        except RigError:
            continue
        info = doc.get("rosbag2_bagfile_information") if isinstance(doc, dict) else None
        if not isinstance(info, dict):
            continue
        st = info.get("starting_time")
        ns = st.get("nanoseconds_since_epoch") if isinstance(st, dict) else st
        try:
            ns = int(ns)
        except (TypeError, ValueError):
            continue
        best = ns if best is None else min(best, ns)
    return best


def manifest_started_ns(doc: dict) -> int | None:
    """The run manifest's `started` (ISO) as unix ns — the timeline zero of a run with no bags."""
    import datetime
    try:
        started = datetime.datetime.fromisoformat(str(doc.get("started")).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=datetime.timezone.utc)
    return int(started.timestamp() * 1e9)


def bag_topics(src_dir: Path) -> set[str] | None:
    """Every topic the source run's bags hold (data topics; service events excluded) — None when
    no metadata is readable (no bags, a foreign layout)."""
    found = False
    topics: set[str] = set()
    for meta in sorted((src_dir / "bags").glob("*/*/metadata.yaml")):
        try:
            doc = load_yaml(meta)
        except RigError:
            continue
        info = doc.get("rosbag2_bagfile_information") if isinstance(doc, dict) else None
        if not isinstance(info, dict):
            continue
        found = True
        for row in info.get("topics_with_message_count") or []:
            if not isinstance(row, dict):
                continue
            name = str(((row.get("topic_metadata") or {}).get("name")) or "")
            if name and not name.endswith("/_service_event"):
                topics.add(name)
    return topics if found else None


def select_topics_reproduce(src_dir: Path, live_names: list[str]) -> tuple[str, str, list[str]]:
    """The no-names (REPRODUCE) selector: everything the bag holds MINUS what the live instances
    (infra + sources) publish themselves — from the bag index + the graph epochs. Fallback (no
    index): the namespace exclude over the live names."""
    notices: list[str] = []
    topics = bag_topics(src_dir)
    if topics is not None:
        pubs: set[str] = set()
        epochs = graph_mod.load_epochs(src_dir)
        if epochs and live_names:
            u = graph_mod.union(epochs)
            groups = graph_mod.group_nodes(u.nodes, sorted(set(live_names)))
            for name in live_names:
                for fqn in groups.get(name, ()):
                    for e in u.nodes[fqn]:
                        if e.kind == "pubs" and not graph_mod.is_plumbing(e):
                            pubs.add(e.name)
        elif live_names:
            notices.append("reproduce: no graph epochs — the live instances' own topics cannot be "
                           "subtracted from the bag by observation; falling back to the namespace "
                           "exclude")
            pattern = "^/(?:" + "|".join(re.escape(n) for n in live_names) + ")(?:/.*)?$"
            return "exclude", pattern, notices
        regen = sorted(topics & pubs)
        if regen:
            notices.append(f"reproduce: {', '.join(regen)} — regenerated live by "
                           f"{', '.join(sorted(set(live_names)))}, not played from the bag")
        return "topics", " ".join(sorted(topics - pubs)), notices
    notices.append("reproduce: no bag index readable — namespace exclude over the live instances")
    if not live_names:
        return "exclude", "^$", notices
    pattern = "^/(?:" + "|".join(re.escape(n) for n in live_names) + ")(?:/.*)?$"
    return "exclude", pattern, notices


def timeline_variables(src_dir: Path, *, has_bags: bool, sim_time: bool, start_delay_s: float,
                       w_from, w_to, session: str | None, now=time.time) -> dict:
    """The `{{replay_*}}` variables every source's overrides may reference (ReplaySource). One zero
    for the whole session — the bag's start, the timeline the player counts on; with NO bag there
    is nothing to align with, so no epoch is handed out and each source starts on its own first
    recorded frame (its own gap policy applies — a run that sat open for days must not replay
    its idle days as silence). One release instant, one clock decision. None values render as
    YAML null, which deep_merge treats as "delete the key": the launcher's own default applies."""
    epoch = bag_epoch_ns(src_dir) if has_bags else None
    if has_bags and epoch is None:
        epoch = manifest_started_ns(source_manifest(src_dir))   # a bag with no readable index
    start_at = (now() + float(start_delay_s)) if start_delay_s and start_delay_s > 0 else None
    return {
        "replay_source": str(src_dir),
        "replay_retime": "original" if sim_time else "wall",
        "replay_epoch_unix_ns": epoch,
        "replay_start_at_unix_s": start_at,
        "replay_from_s": w_from,
        "replay_to_s": w_to,
        "replay_session": session,
    }


def render_sources(manifest, descriptors, root: Path, sources: list[SourceSpec],
                   variables: dict) -> dict[str, Path]:
    """Render each source's config for the session: its (already materialized) row + the
    descriptor's `overrides` patch, `{{replay_*}}` + `{{name}}` substituted. Returns name ->
    rendered path (var/rendered/replay/<name>.yaml — its OWN directory, because every rig verb
    re-materializes var/rendered/<name>.yaml at load and a `rig status` mid-session must not flip
    the file a restarting container would read; the run snapshot captures it as provenance)."""
    from . import resolve
    out: dict[str, Path] = {}
    for spec in sources:
        rs = descriptors[spec.row.service].replay_source
        vars_ = {**(manifest.vars or {}), **variables, "name": spec.row.name}
        out[spec.row.name] = resolve.materialize(spec.row, root, vars_, extra_overrides=rs.overrides,
                                                 out_subdir="replay")
    return out


def select_topics(source_dir: Path, with_names: list[str],
                  live_names: list[str] | None = None) -> tuple[str, str, list[str]]:
    """(mode, value, notices): ('topics', space-joined allow-list) from the source run's graph
    epochs, or ('exclude', namespace regex) as the WARNed fallback. Graph mode requires EVERY
    named instance observed in the source epochs — a service the source run never saw has unknown
    inputs, and guessing half a selection is worse than the honest heuristic.

    Subscribes come from the WITH-SET; the publish SUBTRACTION covers every LIVE instance
    (`live_names` — the whole up-set minus the player: infra rides along and regenerates its own
    topics too, so the bag must not double-publish them). The player row is NEVER in the
    subtraction: in a replay-of-a-replay, the source epochs attribute the recorded inputs to the
    player's node — subtracting those would empty the selection and break chaining."""
    notices: list[str] = []
    live = with_names if live_names is None else live_names
    epochs = graph_mod.load_epochs(source_dir)
    if epochs:
        u = graph_mod.union(epochs)
        groups = graph_mod.group_nodes(u.nodes, sorted(set(with_names) | set(live)))
        unobserved = [n for n in with_names if n not in groups]
        if unobserved:
            notices.append(f"fallback: {', '.join(unobserved)} not observed in the source run's "
                           f"epochs — namespace heuristic (its inputs are unknown to the graph)")
        else:
            subs: set[str] = set()
            pubs: set[str] = set()
            for name in with_names:
                for fqn in groups.get(name, ()):
                    for e in u.nodes[fqn]:
                        if graph_mod.is_plumbing(e):
                            continue
                        if e.kind == "subs":
                            subs.add(e.name)
                        elif e.kind == "pubs":
                            pubs.add(e.name)
            for name in set(live) - set(with_names):  # live-but-not-under-test (infra): pubs only
                for fqn in groups.get(name, ()):
                    for e in u.nodes[fqn]:
                        if e.kind == "pubs" and not graph_mod.is_plumbing(e):
                            pubs.add(e.name)
            echo = sorted(subs & pubs)
            if echo:
                notices.append(f"self-echo guard: {', '.join(echo)} — produced AND consumed "
                               f"inside the with-set; regenerated live, not replayed")
            allow = sorted(subs - pubs)
            if allow:
                return "topics", " ".join(allow), notices
            notices.append("fallback: the with-set has no external subscribes in the source "
                           "epochs — namespace heuristic")
    else:
        notices.append("fallback: source run has no graph epochs — namespace heuristic (enable "
                       "the bag-logger's `graph:` block, rig-infra ≥ v1.7.0, for exact selection)")
    pattern = "^/(?:" + "|".join(re.escape(n) for n in with_names) + ")(?:/.*)?$"
    return "exclude", pattern, notices


def select_services(source_dir: Path, with_names: list[str]) -> tuple[str | None, list[str]]:
    """(space-joined service allow-list | None, notices) — the topic rule's twin: the with-set's
    observed `provides` MINUS its observed `requires` (a with-set client re-issues its own calls
    live; replaying them double-calls), plumbing-filtered. EPOCHS-ONLY, no namespace fallback:
    a heuristic guess about which CALLS to re-issue is an action, not a subscription — without
    observation rig selects none (verbatim service replay simply doesn't arm)."""
    notices: list[str] = []
    epochs = graph_mod.load_epochs(source_dir)
    if not epochs:
        return None, notices  # the topic selector already WARNed about missing epochs
    u = graph_mod.union(epochs)
    groups = graph_mod.group_nodes(u.nodes, with_names)
    if any(n not in groups for n in with_names):
        return None, notices  # unobserved instance: the topic selector already fell back + WARNed
    provides: set[str] = set()
    requires: set[str] = set()
    for name in with_names:
        for fqn in groups[name]:
            for e in u.nodes[fqn]:
                if graph_mod.is_plumbing(e):
                    continue
                if e.kind == "provides":
                    provides.add(e.name)
                elif e.kind == "requires":
                    requires.add(e.name)
    echo = sorted(provides & requires)
    if echo:
        notices.append(f"service self-echo guard: {', '.join(echo)} — provided AND required "
                       f"inside the with-set; the live client re-issues those calls")
    allow = sorted(provides - requires)
    if not allow:
        notices.append("service replay: not armed — " + (
            "every observed server of the with-set is also called from inside it (self-echo: "
            "the live client re-issues those calls)" if provides else
            "the with-set has no observed service servers beyond parameter plumbing"))
        return None, notices
    notices.append(f"service replay: {len(allow)} recorded call target(s) — {', '.join(allow)}")
    return " ".join(allow), notices


def source_service_events(src_dir: Path) -> tuple[int, int] | None:
    """(service-event topics with messages, total events) summed over every recording under
    `<run>/bags/*/*/metadata.yaml` — rosbag2's plain-YAML index, the same file the player reads
    host-side (bag CONTENTS stay opaque to rig; this is the table of contents). None when no
    metadata is readable (no bags, a foreign layout) — say nothing rather than guess."""
    found, topics, events = False, 0, 0
    for meta in sorted((src_dir / "bags").glob("*/*/metadata.yaml")):
        try:
            doc = load_yaml(meta)
        except RigError:
            continue
        info = doc.get("rosbag2_bagfile_information") if isinstance(doc, dict) else None
        if not isinstance(info, dict):
            continue
        found = True
        for row in info.get("topics_with_message_count") or []:
            if not isinstance(row, dict):
                continue
            name = str(((row.get("topic_metadata") or {}).get("name")) or "")
            try:
                count = int(row.get("message_count") or 0)
            except (TypeError, ValueError):
                count = 0
            if name.endswith("/_service_event") and count > 0:
                topics += 1
                events += count
    return (topics, events) if found else None


def service_replay_notices(src_dir: Path, *, mode: str, services: str | None,
                           calls_path: Path | None) -> list[str]:
    """Say WHY verbatim service replay is or isn't in play — a silent nothing is the failure
    class this exists to remove (a reconstructed flight replayed with no calls, and no line said
    why). Two independent facts: whether the SOURCE RUN recorded any service events at all
    (record-time-or-never: `record.services` on at record time AND the servers running
    CONTENTS-level introspection), and whether rig's selector armed anything (epochs-only;
    `select_services` names its own reasons in topics mode). Script mode needs neither — the
    injector calls live servers itself — so it says nothing."""
    if calls_path is not None:
        return []
    notes: list[str] = []
    scan = source_service_events(src_dir)
    if scan is not None and scan[0] == 0:
        notes.append("warning: the source run recorded NO service events — no recorded call can "
                     "replay, whatever the selection (record-time-or-never: `record.services` on "
                     "at record time AND the servers running CONTENTS-level introspection); "
                     "--calls can still inject scripted calls at the live servers")
        return notes  # a not-armed reason underneath would be noise
    if scan is not None:
        notes.append(f"source run holds {scan[1]} recorded service event(s) across {scan[0]} "
                     f"service(s)")
    if services is None and mode != "topics":
        notes.append("service replay: not armed — namespace fallback (no epochs, or an instance "
                     "the source run never observed): verbatim calls replay only from OBSERVED "
                     "graph epochs, and the fallback's exclude regex would drop them on lyrical "
                     "anyway")
    return notes


def validate_calls(path: Path) -> str:
    """Shallow-validate a call script (rig-replay-calls-handoff §1.2) and return its sha256 for
    the run's provenance. SHALLOW on purpose: schema/t/shape here; the request BODIES are the srv
    types' own schemas — the injector validates those against the types at load (rig has no ROS
    and stays opaque). Refusals name the entry index, never a YAML line."""
    import hashlib

    if not path.is_file():
        raise RigError(f"replay --calls: no file at {path}")
    doc = load_yaml(path)
    if doc.get("schema") != 1:
        raise RigError(f"replay --calls: {path.name}: schema must be 1, not "
                       f"{doc.get('schema')!r}")
    calls = doc.get("calls")
    if not isinstance(calls, list) or not calls:
        raise RigError(f"replay --calls: {path.name}: `calls` must be a non-empty list")
    for i, entry in enumerate(calls):
        if not isinstance(entry, dict):
            raise RigError(f"replay --calls: {path.name}: calls #{i} must be a mapping")
        t = entry.get("t")
        if not isinstance(t, (int, float)) or isinstance(t, bool) or t < 0:
            raise RigError(f"replay --calls: {path.name}: calls #{i}: t must be a number ≥ 0 "
                           f"(seconds from BAG START on the sim clock)")
        for key in ("service", "type"):
            if not isinstance(entry.get(key), str) or not entry[key]:
                raise RigError(f"replay --calls: {path.name}: calls #{i} needs `{key}`")
        if not isinstance(entry.get("request", {}), dict):
            raise RigError(f"replay --calls: {path.name}: calls #{i}: `request` must be a "
                           f"mapping (the srv type's own fields)")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_window(from_s, to_s) -> tuple[float | None, float | None]:
    """`--from`/`--to` -> (from, to) as floats or None. Seconds from BAG START (rosbag2's
    `starting_time` — the ONE zero shared with call scripts, results.yaml and export-calls;
    rig-replay-window-handoff §1.1). rig guarantees `0 <= from < to`; the player re-checks
    against the bag's real duration (rig is bag-opaque): an empty window refuses there too, an
    end past the bag clamps with a WARN."""
    import math
    parsed: list[float | None] = []
    for flag, value in (("--from", from_s), ("--to", to_s)):
        if value is None:
            parsed.append(None)
            continue
        try:
            f = float(value)
        except (TypeError, ValueError):
            raise RigError(f"replay {flag}: takes seconds (a number), got {value!r}")
        if not math.isfinite(f) or f < 0:
            raise RigError(f"replay {flag}: must be a finite number of seconds >= 0 (from bag "
                           f"start), got {value!r}")
        parsed.append(f)
    w_from, w_to = parsed
    if w_to is not None and w_to <= 0:
        raise RigError("replay --to: must be > 0 (seconds from bag start; the end is exclusive)")
    if w_from is not None and w_to is not None and w_from >= w_to:
        raise RigError(f"replay: empty window — --from {w_from:g} must be < --to {w_to:g}")
    return w_from, w_to


def _window_label(w_from: float | None, w_to: float | None) -> str:
    lo = f"{w_from:g}" if w_from is not None else "0"
    hi = f"{w_to:g}" if w_to is not None else "end"
    return f"[{lo}, {hi})"


def source_wall_duration_s(src_dir: Path) -> float | None:
    """The source run's WALL duration from its manifest (`started`/`ended`, ISO) — a bag-opaque
    sanity bound for a window (bag time ≈ wall time for a live recording). None when unknown."""
    import datetime
    try:
        doc = load_yaml(src_dir / "manifest.yaml")
    except RigError:
        return None
    try:
        started = datetime.datetime.fromisoformat(str(doc.get("started")).replace("Z", "+00:00"))
        ended = datetime.datetime.fromisoformat(str(doc.get("ended")).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    secs = (ended - started).total_seconds()
    return secs if secs > 0 else None


def window_notices(w_from: float | None, w_to: float | None, *, wall_s: float | None,
                   calls_path: Path | None) -> list[str]:
    """Notices for a window: bounds beyond the source run's wall duration (the player refuses a
    `from` past the bag end and clamps `to` — rig only warns, bag-opaque), and the call-script
    intersection (the injector's filter is authoritative: t < from is skipped, t >= to never
    reached; rig's shallow parse already reads every t, so say it up front)."""
    notes: list[str] = []
    if wall_s is not None:
        if w_from is not None and w_from > wall_s:
            notes.append(f"warning: --from {w_from:g}s is beyond the source run's wall duration "
                         f"(~{wall_s:.0f}s) — the player refuses a window that starts after "
                         f"the bag ends")
        elif w_to is not None and w_to > wall_s:
            notes.append(f"warning: --to {w_to:g}s is beyond the source run's wall duration "
                         f"(~{wall_s:.0f}s) — the player clamps the end to the bag")
    if calls_path is not None:
        ts = [float(c["t"]) for c in (load_yaml(calls_path).get("calls") or [])]
        lo = w_from if w_from is not None else 0.0
        inside = [t for t in ts if t >= lo and (w_to is None or t < w_to)]
        if not inside:
            notes.append(f"warning: none of the {len(ts)} scripted call(s) falls inside the "
                         f"window {_window_label(w_from, w_to)} — the injector will fire "
                         f"nothing (t counts from bag start, never from the window)")
        elif len(inside) < len(ts):
            notes.append(f"{len(ts) - len(inside)} of {len(ts)} scripted calls fall outside the "
                         f"window {_window_label(w_from, w_to)} — the injector skips them "
                         f"(named in results.yaml's `# window:` line)")
    return notes


def _alignment_report(manifest, with_names: list[str], src_dir: Path,
                      sources: list[str] = ()) -> tuple[list[str], list[str]]:
    """(eprint lines, drifted instance names) — the source↔current alignment layer. The drift IS
    the experiment: each with-set instance's CURRENT rendered config vs the source run's LAST
    sealed snapshot, byte-compared; plus a WARN when the source run never ran an instance at all
    (its `stacks:`). All read-side and fail-soft — a sparse old run degrades to 'unknown'."""
    lines: list[str] = []
    drifted: list[str] = []
    doc: dict = {}
    mpath = src_dir / "manifest.yaml"
    if mpath.exists():
        try:
            doc = load_yaml(mpath)
        except RigError:
            pass
    stacks = {str(s) for s in (doc.get("stacks") or [])}
    if stacks:
        for name in with_names:
            if name not in stacks:
                lines.append(f"[!] {name}: not in the source run's recorded stacks — its "
                             f"'recorded inputs' come from a session it never ran in")
    ups = doc.get("ups") or []
    digest = (ups[-1] or {}).get("config") if ups and isinstance(ups[-1], dict) else None
    snap = (src_dir / ".rig" / "config" / str(digest)) if digest else None
    for s in (row for row in manifest.sensors if row.name in set(with_names) | set(sources)):
        if s.name in set(sources):
            lines.append(f"[≈] {s.name}: replayed from its own recordings — config rendered for "
                         f"the session (drift not compared)")
            continue
        recorded = snap / "rendered" / f"{s.name}.yaml" if snap else None
        if recorded is None or not recorded.exists():
            lines.append(f"[·] {s.name}: no rendered config in the source snapshot — drift unknown")
            continue
        try:
            same = Path(s.config).read_bytes() == recorded.read_bytes()
        except OSError:
            same = False
        if same:
            lines.append(f"[✓] {s.name}: config identical to the source run")
        else:
            drifted.append(s.name)
            lines.append(f"[≠] {s.name}: config DIFFERS from the source run — this diff is the "
                         f"experiment (recorded in the replay manifest)")
    return lines, drifted


def _player_row(manifest, required: bool = True):
    """The deployment's ros2-bag-player row — service-name detection (the doctor's zenoh-router
    precedent: rig knows its OWN companion services). Enabled state is irrelevant: replay selects
    it by explicit name. `required=False` returns None when absent (a session with only
    per-sensor sources needs no player)."""
    rows = [s for s in manifest.sensors if s.service == PLAYER_SERVICE]
    if not rows and not required:
        return None
    if not rows:
        raise RigError(f"replay: no {PLAYER_SERVICE} row in vehicle.yaml — add one under "
                       f"`autonomy:` with a high order and `enabled: false` (it must come up "
                       f"LAST so subscribers exist before data flows), e.g.\n"
                       f"  - {{ name: bag_player, service: {PLAYER_SERVICE}, "
                       f"config: config/autonomy/bag_player.yaml, enabled: false, order: 999 }}\n"
                       f"  (a reconstructed tree: `rig reconstruct <run> --enable-replay "
                       f"<{PLAYER_SERVICE} dir | registry ref>` wires it)")
    if len(rows) > 1:
        raise RigError(f"replay: {len(rows)} {PLAYER_SERVICE} rows "
                       f"({', '.join(s.name for s in rows)}) — one player per replay session")
    return rows[0]


def _guard_clean_host(manifest, force: bool) -> None:
    """Refuse while ANY of this manifest's stacks run — and refuse when we CANNOT TELL (fail
    closed, the rotation guard's posture). NOTE: `running_projects` covers enabled rows; a
    crashed previous replay's player container (disabled row) is outside its view — `up` on it
    again is what recovers that."""
    if force:
        return
    try:
        live = runs_mod.running_projects(manifest)
    except RigError as exc:
        raise RigError(f"replay: {exc} — retry, or --force")
    if live:
        raise RigError(f"replay: stacks are running ({', '.join(live)}) — a replay session "
                       f"starts from a quiet host (recorders pin their run dir at process start; "
                       f"survivors would keep writing into the OLD run). `rig down` first, or "
                       f"--force")


def _player_finished(manifest, player) -> bool | None:
    """True = the player's compose project is no longer running, False = still running, None =
    docker couldn't answer (the caller must FAIL SAFE: never auto-tear-down on uncertainty).
    Direct project check — `running_projects` covers enabled rows only, and the player row is
    disabled by doctrine."""
    import json
    import subprocess

    from .manifest import project_name
    expected = project_name(player.name, manifest.vehicle_id)
    try:
        proc = subprocess.run(["docker", "compose", "ls", "-a", "--format", "json"],
                              capture_output=True, text=True, timeout=15)
        if proc.returncode != 0:
            return None
        rows = json.loads(proc.stdout or "[]")
    except Exception:  # noqa: BLE001 — timeout/parse/missing docker: cannot tell
        return None
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and row.get("Name") == expected:
            return "running" not in str(row.get("Status", "")).lower()
    return True  # project gone entirely = finished


def auto_end(manifest, descriptors, root: Path, pairs, env, player, grace_s: int) -> int:
    """`--auto-end`: wait for the player to finish the bag, breathe `grace_s` (the last message
    PUBLISHES when the player exits — consumers are still draining callbacks and the B-side bag
    is still being written), then run cmd_down's exact end-run sequence over the session's own
    up-set: capture docker logs BEFORE down (compose removes the containers), down in reverse
    (player first — it already exited), seal only on a clean full down. Ctrl+C and every
    cannot-tell docker failure LEAVE THE STACK UP with the manual command named — an unattended
    teardown must never fire on uncertainty."""
    import time
    eprint(f"rig replay: waiting for {player.name} to finish the bag "
           f"(Ctrl+C leaves the session running)…")
    misses = 0
    try:
        while True:
            state = _player_finished(manifest, player)
            if state is True:
                break
            if state is None:
                misses += 1
                if misses >= 5:
                    eprint("rig replay: docker cannot answer — leaving the session UP "
                           "(auto-end never tears down on uncertainty); "
                           "`rig down --end-run` when ready")
                    return 1
            else:
                misses = 0
            time.sleep(3)
    except KeyboardInterrupt:
        eprint("rig replay: interrupted — session left running; `rig down --end-run` when ready")
        return 130
    eprint(f"rig replay: bag finished — {grace_s}s grace for consumers to drain, then sealing")
    time.sleep(grace_s)
    if manifest.data_dir:
        # Replay explicitly launches disabled rows, including the player and source instances.
        runs_mod.capture_docker_logs(manifest, also={s.name for s, _ in pairs})
    outcomes = dispatch.run_verb(list(reversed(pairs)), env, "down")
    failed = [o for o in outcomes if o.returncode != 0]
    if failed:
        eprint(f"rig: down failed for {', '.join(o.sensor.name for o in failed)} — leaving the "
               f"run open (cannot seal with stacks possibly live)")
        return 1
    if manifest.data_dir:
        runs_mod.end_run(manifest, root)
    return 0


def cmd(manifest, catalog, descriptors, root: Path, *, run_ref: str, names: list[str],
        label: str | None, wall_clock: bool, force: bool, dry_run: bool,
        calls: str | None = None, export_calls: bool = False,
        auto_end_grace: int | None = None, window_from=None, window_to=None,
        live: list[str] | None = None, session: str | None = None,
        start_delay: float = 20.0, skip_services: list[str] | None = None) -> int:
    if export_calls:
        if skip_services:
            raise RigError("replay --export-calls: --skip-service applies to replay sessions, "
                           "not call-script exports")
        # NOT a session: one launcher-verb dispatch (the export runs in a one-shot container —
        # ROS stays on the player's side of the line; rig resolves run + row + launcher, which
        # is exactly what it resolves for a replay anyway). Clean YAML rides the child's stdout
        # (`> calls.yaml`); rig's own chatter stays on stderr like everything else.
        if names:
            raise RigError("replay --export-calls: takes no instance names — it derives the "
                           "call timeline from the SOURCE run's recorded service events")
        if calls:
            raise RigError("replay --export-calls: exports a script; --calls plays one — "
                           "one direction per invocation")
        if window_from is not None or window_to is not None:
            raise RigError("replay --export-calls: exports the WHOLE recording (t from bag "
                           "start) — --from/--to apply at replay, where the injector filters")
        src_id, src_dir = resolve_source(manifest, run_ref)
        player = _player_row(manifest)
        env = dispatch.fleet_env(manifest, descriptors)
        env["RIG_REPLAY_SOURCE"] = str(src_dir)
        eprint(f"rig replay: exporting recorded calls from {src_id} "
               f"(empty output = the run recorded no service events — introspection is "
               f"record-time-or-never)")
        outcomes = dispatch.run_verb([(player, descriptors[player.service])], env,
                                     "export-calls", dry_run=dry_run)
        return 0 if all(o.returncode == 0 for o in outcomes) else 1

    with_names = list(dict.fromkeys(names))          # under test (live); [] = REPRODUCE the run
    live_flags = list(dict.fromkeys(live or []))
    skip_flags = list(dict.fromkeys(skip_services or []))
    known_services = {s.service for s in manifest.sensors}
    for service in skip_flags:
        if service not in known_services:
            raise RigError(f"replay --skip-service: unknown service '{service}' (see vehicle.yaml)")
        if service == PLAYER_SERVICE:
            raise RigError(f"replay --skip-service: cannot skip {PLAYER_SERVICE}; it plays the "
                           "remaining bag topics")
    skipped = [s.name for s in manifest.sensors if s.service in skip_flags]
    conflicts = sorted(set(skipped) & set(with_names + live_flags))
    if conflicts:
        raise RigError(f"replay --skip-service: {', '.join(conflicts)} also requested under test "
                       "or with --live — remove the conflicting selection")
    # Omitted services must not contribute platform/port/launcher preflight errors. Keep the
    # original manifest for the clean-host guard and run snapshot: even an omitted camera
    # left running from an earlier session must prevent a replay from silently keeping it up.
    active_manifest = dataclasses.replace(manifest, sensors=[s for s in manifest.sensors
                                                            if s.name not in skipped])
    active_descriptors = {k: v for k, v in descriptors.items() if k not in skip_flags}
    player = _player_row(manifest, required=False)   # needed iff the bag has something to play
    if player is not None and (player.name in with_names or player.name in live_flags):
        raise RigError(f"replay: '{player.name}' is the player — it is added automatically; "
                       f"name the instances under test")
    known = {s.name for s in manifest.sensors}
    for n in with_names + live_flags:
        if n not in known:
            raise RigError(f"replay: unknown instance '{n}' (see vehicle.yaml)")
    w_from, w_to = validate_window(window_from, window_to)  # cheap, before any docker preflight
    blocking = [i for i in doctor_mod.collect(active_manifest, catalog, active_descriptors)
                if i.level == doctor_mod.ERROR]
    if blocking and not force:
        eprint("rig: preflight failed (pass --force to override):")
        for issue in blocking:
            eprint(f"  [✗] {issue.message}")
        return 1

    src_id, src_dir = resolve_source(manifest, run_ref, require_bags=False)
    has_bags = (src_dir / "bags").is_dir()
    sources, notices = discover_sources(active_manifest, active_descriptors, src_dir, live=set(live_flags))
    if skipped:
        notices.append(f"--skip-service: omitting {', '.join(skipped)} "
                       f"(services: {', '.join(skip_flags)})")
    source_names = [sp.row.name for sp in sources]
    if not has_bags and not sources:
        if skipped:
            raise RigError("replay --skip-service: no recorded inputs remain to replay")
        raise RigError(f"replay: {src_id} has no bags/ and no instance recordings — nothing to play")
    infra = [s.name for s in active_manifest.sensors if s.tier == "infra" and s.enabled]
    # everything that PUBLISHES live in this session — infra regenerates its own topics, sources
    # re-deliver theirs, the with-set computes its outputs: the bag must not double-publish any
    live_names = list(dict.fromkeys(infra + source_names + with_names))

    mode = value = None
    if has_bags and player is None:
        raise RigError(f"replay: {src_id} holds bags but this vehicle.yaml has no {PLAYER_SERVICE} "
                       f"row — add one under `autonomy:` (enabled: false, order: 999), or "
                       f"`rig reconstruct <run> --enable-replay …`")
    if has_bags:
        if with_names:
            mode, value, sel_notices = select_topics(src_dir, with_names, live_names=live_names)
        else:
            mode, value, sel_notices = select_topics_reproduce(src_dir, live_names)
        notices += sel_notices
        if skipped:
            pattern, skip_notices = skipped_topic_pattern(src_dir, skipped, known)
            notices += skip_notices
            if mode == "topics":
                value = " ".join(t for t in value.split() if not re.search(pattern, t))
            else:
                value = f"(?:{value})|(?:{pattern})"
                recorded = bag_topics(src_dir)
                if recorded is not None and not any(not re.search(value, t) for t in recorded):
                    mode, value = "topics", ""
        if mode == "topics" and not value.strip():
            notices.append("no bag topics remain after replay selection — the bag player is not "
                           "started" if skipped else "every recorded topic is regenerated live in "
                           "this session — the bag player is not started")
            player, mode, value = None, None, None
    else:
        player = None
        notices.append(f"{src_id} has no bags/ — a session of instance recordings only "
                       f"({', '.join(source_names)}); no player")
    if skipped and player is None and not sources:
        raise RigError("replay --skip-service: no recorded inputs remain to replay")
    sim_time = (not wall_clock) and player is not None   # no player = no /clock = no sim time
    if not wall_clock and player is None:
        notices.append("wall clock: no bag player in this session, so no /clock — the sources "
                       "retime their recordings onto now")

    # Services ride the SAME session: verbatim (recorded requests at the with-set's servers,
    # provides − requires) — unless a call SCRIPT is given, which subsumes and SUPPRESSES
    # verbatim (script XOR verbatim: double-call discipline; rig-replay-calls-handoff §1.2).
    calls_path = Path(calls).expanduser().resolve() if calls else None
    calls_sha = validate_calls(calls_path) if calls_path else None
    if calls_path is not None and player is None:
        raise RigError("replay --calls: scripted calls ride the bag player, and this session has "
                       "none (no bags to play)")
    services = None
    if player is not None and calls_path is None and mode == "topics" and with_names:
        # TOPICS mode only: lyrical's exclude regex knocks out topics AND services alike
        # (rig-infra v1.10.0's live finding), so arming SERVICES beside the namespace-fallback
        # EXCLUDE would let the regex silently kill the very calls rig selected. The injector
        # (--calls) is unaffected — it issues calls itself, outside bag playback.
        services, svc_notices = select_services(src_dir, with_names)
        notices += svc_notices
    if player is not None:
        notices += service_replay_notices(src_dir, mode=mode, services=services,
                                          calls_path=calls_path)
    if w_from is not None or w_to is not None:  # the window: bag-opaque sanity + script overlap
        notices += window_notices(w_from, w_to, wall_s=source_wall_duration_s(src_dir),
                                  calls_path=calls_path)
    if auto_end_grace is not None and player is None:
        raise RigError("replay --auto-end: waits for the bag player, and this session has none "
                       "(a sources-only replay holds at its end — `rig down --end-run` when done)")
    for n in notices:
        eprint(f"rig replay: {n}")
    align_lines, drifted = _alignment_report(manifest, with_names, src_dir, sources=source_names)
    for line in align_lines:
        eprint(f"  {line}")
    for issue in doctor_mod.replay_issues(manifest, descriptors, with_names,
                                          sim_time=sim_time,
                                          services=bool(services or calls_path),
                                          sources=source_names):
        eprint(f"  [{doctor_mod._SYMBOL[issue.level]}] {issue.message}")

    # The timeline every source shares (rig-sensor-replay-plan §4/§5): the bag's zero, one release
    # instant, the window, the clock decision — rendered into each source's config via its
    # descriptor's overrides. Computed once, BEFORE the up, so every producer gets the same values.
    variables = timeline_variables(src_dir, has_bags=has_bags, sim_time=sim_time,
                                   start_delay_s=start_delay, w_from=w_from, w_to=w_to,
                                   session=session)

    # Up-set: enabled infra (logger + its graph sidecar ride along, recording the A/B outputs)
    # + the sources + the with-set + the player — explicit names, so select's tier/order gives
    # producers-first and the player (autonomy, high order) LAST.
    up_names = list(dict.fromkeys(infra + source_names + with_names
                                  + ([player.name] if player is not None else [])))
    sensors = manifest.select(up_names, enabled_only=True)  # explicit names win over enabled:
    rendered = render_sources(manifest, descriptors, root, sources, variables) if not dry_run \
        else {}
    sensors = [dataclasses.replace(s, config=rendered[s.name]) if s.name in rendered else s
               for s in sensors]
    pairs = [(s, descriptors[s.service]) for s in sensors]
    if player is not None and pairs[-1][0].name != player.name:
        eprint(f"rig replay: warning: {player.name} is not LAST in the up order — declare its "
               f"row in `autonomy:` with a high `order` (a player starting before its "
               f"subscribers drops the bag head)")

    if dry_run:  # a preview must not require a quiet host — surface the refusal, keep going
        try:
            _guard_clean_host(manifest, force)
        except RigError as exc:
            eprint(f"  [!] dry-run: a real replay would refuse here — {exc}")
    else:
        _guard_clean_host(manifest, force)

    env = dispatch.fleet_env(manifest, descriptors)
    env["RIG_REPLAY_SOURCE"] = str(src_dir)
    if player is not None:
        env["RIG_REPLAY_TOPICS" if mode == "topics" else "RIG_REPLAY_EXCLUDE"] = value
    if calls_path is not None:
        env["RIG_REPLAY_CALLS"] = str(calls_path)  # SERVICES stays unset: script XOR verbatim
    elif services:
        env["RIG_REPLAY_SERVICES"] = services
    if sim_time:
        env["RIG_SIM_TIME"] = "1"
    if variables.get("replay_start_at_unix_s") is not None:
        # the release instant, for a player that starts paused and resumes itself there
        # (rig-infra adoption pending; the sources already honour it through their config)
        env["RIG_REPLAY_START_AT_UNIX_S"] = f"{variables['replay_start_at_unix_s']:.3f}"
    if w_from is not None:  # seconds from bag start — the player maps them to --start-offset and
        env["RIG_REPLAY_FROM_S"] = f"{w_from:g}"  # the end bound, restores latches the offset
    if w_to is not None:                          # would skip, and hands the injector the same zero
        env["RIG_REPLAY_TO_S"] = f"{w_to:g}"

    if not dry_run:
        if manifest.data_dir:
            run_label = label or re.sub(r"[^A-Za-z0-9_-]", "-", f"replay-{src_id}")
            replay_doc: dict = {"of": src_id, "source": str(src_dir), "with": list(with_names),
                                "clock": "sim" if sim_time else "wall"}
            if skipped:
                replay_doc["skipped"] = {"services": skip_flags, "instances": skipped}
            if sources:
                replay_doc["sources"] = {sp.row.name: ({"data": sp.rel, "session": session}
                                                       if session else {"data": sp.rel})
                                         for sp in sources}
            if variables.get("replay_epoch_unix_ns") is not None:
                replay_doc["epoch_unix_ns"] = variables["replay_epoch_unix_ns"]
            if variables.get("replay_start_at_unix_s") is not None:
                replay_doc["start_at_unix_s"] = variables["replay_start_at_unix_s"]
            if live_flags:
                replay_doc["live"] = live_flags
            if drifted:
                replay_doc["config_drift"] = drifted  # the diff IS the experiment — name it
            if calls_sha:
                replay_doc["calls_sha"] = calls_sha
            if w_from is not None or w_to is not None:  # selection provenance, like `with`
                replay_doc["window"] = {k: v for k, v in (("from", w_from), ("to", w_to))
                                        if v is not None}
            # our clean-host guard already ran (same evidence, replay-flavored message) — don't
            # fail-closed twice on one docker call
            run_id = runs_mod.new_run(manifest, root, run_label, force=True, replay=replay_doc)
            if calls_path is not None:  # the script is provenance the standard snapshot can't
                import shutil            # see (an arbitrary file) — copy + hash it explicitly
                rig_dir = Path(manifest.data_dir) / "runs" / run_id / ".rig"
                rig_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(calls_path, rig_dir / "replay-calls.yaml")
            # the snapshot reads each row's config path: hand it the replay-rendered rows
            snap_manifest = dataclasses.replace(manifest, sensors=[
                next((r for r in sensors if r.name == s.name), s) for s in manifest.sensors])
            runs_mod.snapshot(snap_manifest, root, stacks=[s.name for s, _ in pairs])
        else:
            eprint("rig replay: warning: no `data_dir` — this session gets no run dir, no "
                   "provenance, and no recording of the new outputs")
    if player is not None:
        count = f"{len(value.split())} topics" if mode == "topics" else "namespace-exclude"
    else:
        count = "no bag"
    svc_note = (", scripted calls" if calls_path is not None
                else f", {len(services.split())} services" if services else "")
    window_note = (f", window {_window_label(w_from, w_to)}s"
                   if (w_from is not None or w_to is not None) else "")
    what = (f"sources: {', '.join(source_names)}" if source_names else "") \
        + ("; " if source_names and with_names else "") \
        + (f"under test: {', '.join(with_names)}" if with_names else "")
    gate = (f", release in {start_delay:g}s" if variables.get("replay_start_at_unix_s") and sources
            else "")
    eprint(f"rig replay: {src_id} → {what or 'reproduce'}  [{count}{svc_note}{window_note}"
           f"{', wall clock' if not sim_time else ', sim time'}{gate}]")
    up_started = time.time()
    outcomes = dispatch.run_verb(pairs, env, "up", dry_run=dry_run)
    failed = [o for o in outcomes if o.returncode != 0]
    if failed:
        eprint(f"rig: {len(failed)}/{len(outcomes)} failed: "
               f"{', '.join(o.sensor.name for o in failed)}")
        return 1
    gate = variables.get("replay_start_at_unix_s")
    if sources and gate is not None and not dry_run and time.time() > gate:
        # the sources resumed themselves at the gate while later stacks were still coming up
        import math
        late = time.time() - gate
        eprint(f"rig replay: warning: the up outlasted the release gate by {late:.0f}s — the sources "
               f"started playing before the last stack was up; pass --start-delay "
               f"{math.ceil(time.time() - up_started + 5)} next time")
    if auto_end_grace is not None and not dry_run:
        # NOTE: incompatible with the player config's `loop: true` (the player never exits —
        # rig can't see that knob, schema-opaque; the wait just runs until Ctrl+C).
        return auto_end(manifest, descriptors, root, pairs, env, player, auto_end_grace)
    return 0
