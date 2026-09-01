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
it) — a future per-sensor replay source (ROADMAP §2: camera-service replaying its own mkv+csv
recordings) consumes the same var; the bag player is merely its first consumer.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import RigError, dispatch, doctor as doctor_mod, graph as graph_mod, runs as runs_mod
from .common import eprint, load_yaml

PLAYER_SERVICE = "ros2-bag-player"


def resolve_source(manifest, ref: str) -> tuple[str, Path]:
    """(run-id, run-dir) for the SOURCE run: an id under the registry, or a path to a run dir
    anywhere (a run scp'd off a vehicle). Refuses the OPEN run (a recorder may still be writing
    it); WARNs on an unsealed source (`ended:` absent — bags may be incomplete); refuses a run
    with no bags/ (nothing to play)."""
    as_path = Path(ref).expanduser()
    if "/" in ref or as_path.is_dir():
        if not as_path.is_dir():
            raise RigError(f"replay: no run dir at {as_path}")
        run_id, run_dir = as_path.name, as_path
    else:
        data = runs_mod._root(manifest)  # same registry root/validation as every run verb
        run_dir = data / "runs" / ref
        if not run_dir.is_dir():
            raise RigError(f"replay: no run '{ref}' under {data / 'runs'} (see `rig runs`)")
        run_id = ref
    if manifest.data_dir:
        try:
            cur = runs_mod.current_run(runs_mod._root(manifest))
        except RigError:
            cur = None  # a broken `current` must not hide the source check — rotation will report it
        if cur is not None and cur[1].resolve() == run_dir.resolve():
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
    if not (run_dir / "bags").is_dir():
        raise RigError(f"replay: {run_id} has no bags/ — nothing to play")
    return run_id, run_dir


def select_topics(source_dir: Path, with_names: list[str]) -> tuple[str, str, list[str]]:
    """(mode, value, notices): ('topics', space-joined allow-list) from the source run's graph
    epochs, or ('exclude', namespace regex) as the WARNed fallback. Graph mode requires EVERY
    named instance observed in the source epochs — a service the source run never saw has unknown
    inputs, and guessing half a selection is worse than the honest heuristic."""
    notices: list[str] = []
    epochs = graph_mod.load_epochs(source_dir)
    if epochs:
        u = graph_mod.union(epochs)
        groups = graph_mod.group_nodes(u.nodes, with_names)
        unobserved = [n for n in with_names if n not in groups]
        if unobserved:
            notices.append(f"fallback: {', '.join(unobserved)} not observed in the source run's "
                           f"epochs — namespace heuristic (its inputs are unknown to the graph)")
        else:
            subs: set[str] = set()
            pubs: set[str] = set()
            for name in with_names:
                for fqn in groups[name]:
                    for e in u.nodes[fqn]:
                        if graph_mod.is_plumbing(e):
                            continue
                        if e.kind == "subs":
                            subs.add(e.name)
                        elif e.kind == "pubs":
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
        return None, notices
    notices.append(f"service replay: {len(allow)} recorded call target(s) — {', '.join(allow)}")
    return " ".join(allow), notices


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
                           f"(seconds from play start on the sim clock)")
        for key in ("service", "type"):
            if not isinstance(entry.get(key), str) or not entry[key]:
                raise RigError(f"replay --calls: {path.name}: calls #{i} needs `{key}`")
        if not isinstance(entry.get("request", {}), dict):
            raise RigError(f"replay --calls: {path.name}: calls #{i}: `request` must be a "
                           f"mapping (the srv type's own fields)")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _alignment_report(manifest, with_names: list[str], src_dir: Path) -> tuple[list[str], list[str]]:
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
    for s in (row for row in manifest.sensors if row.name in set(with_names)):
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


def _player_row(manifest):
    """The deployment's ros2-bag-player row — service-name detection (the doctor's zenoh-router
    precedent: rig knows its OWN companion services). Enabled state is irrelevant: replay selects
    it by explicit name."""
    rows = [s for s in manifest.sensors if s.service == PLAYER_SERVICE]
    if not rows:
        raise RigError(f"replay: no {PLAYER_SERVICE} row in vehicle.yaml — add one under "
                       f"`autonomy:` with a high order and `enabled: false` (it must come up "
                       f"LAST so subscribers exist before data flows), e.g.\n"
                       f"  - {{ name: bag_player, service: {PLAYER_SERVICE}, "
                       f"config: config/infra/bag_player.yaml, enabled: false, order: 999 }}")
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


def cmd(manifest, catalog, descriptors, root: Path, *, run_ref: str, names: list[str],
        label: str | None, wall_clock: bool, force: bool, dry_run: bool,
        calls: str | None = None, export_calls: bool = False) -> int:
    if export_calls:
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
    if not names:
        raise RigError("replay: name the instance(s) under test — `rig replay <run> <name>…` "
                       "(they come up live; their recorded inputs are played at them)")
    player = _player_row(manifest)
    if player.name in names:
        raise RigError(f"replay: '{player.name}' is the player — it is added automatically; "
                       f"name the instances under test")
    blocking = [i for i in doctor_mod.collect(manifest, catalog, descriptors)
                if i.level == doctor_mod.ERROR]
    if blocking and not force:
        eprint("rig: preflight failed (pass --force to override):")
        for issue in blocking:
            eprint(f"  [✗] {issue.message}")
        return 1

    src_id, src_dir = resolve_source(manifest, run_ref)
    mode, value, notices = select_topics(src_dir, names)
    # Services ride the SAME session: verbatim (recorded requests at the with-set's servers,
    # provides − requires) — unless a call SCRIPT is given, which subsumes and SUPPRESSES
    # verbatim (script XOR verbatim: double-call discipline; rig-replay-calls-handoff §1.2).
    calls_path = Path(calls).expanduser().resolve() if calls else None
    calls_sha = validate_calls(calls_path) if calls_path else None
    services = None
    if calls_path is None and mode == "topics":
        # TOPICS mode only: lyrical's exclude regex knocks out topics AND services alike
        # (rig-infra v1.10.0's live finding), so arming SERVICES beside the namespace-fallback
        # EXCLUDE would let the regex silently kill the very calls rig selected. The injector
        # (--calls) is unaffected — it issues calls itself, outside bag playback.
        services, svc_notices = select_services(src_dir, names)
        notices += svc_notices
    for n in notices:
        eprint(f"rig replay: {n}")
    align_lines, drifted = _alignment_report(manifest, names, src_dir)
    for line in align_lines:
        eprint(f"  {line}")
    for issue in doctor_mod.replay_issues(manifest, descriptors, names,
                                          sim_time=not wall_clock,
                                          services=bool(services or calls_path)):
        eprint(f"  [{doctor_mod._SYMBOL[issue.level]}] {issue.message}")

    # Up-set: enabled infra (logger + its graph sidecar ride along, recording the A/B outputs)
    # + the with-set + the player — explicit names, so select's tier/order gives producers-first
    # and the player (autonomy, high order) LAST.
    infra = [s.name for s in manifest.sensors if s.tier == "infra" and s.enabled]
    up_names = list(dict.fromkeys(infra + list(names) + [player.name]))
    sensors = manifest.select(up_names, enabled_only=True)  # explicit names win over enabled:
    pairs = [(s, descriptors[s.service]) for s in sensors]
    if pairs[-1][0].name != player.name:
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
    env["RIG_REPLAY_TOPICS" if mode == "topics" else "RIG_REPLAY_EXCLUDE"] = value
    if calls_path is not None:
        env["RIG_REPLAY_CALLS"] = str(calls_path)  # SERVICES stays unset: script XOR verbatim
    elif services:
        env["RIG_REPLAY_SERVICES"] = services
    if not wall_clock:
        env["RIG_SIM_TIME"] = "1"

    if not dry_run:
        if manifest.data_dir:
            run_label = label or re.sub(r"[^A-Za-z0-9_-]", "-", f"replay-{src_id}")
            replay_doc = {"of": src_id, "source": str(src_dir), "with": list(names)}
            if drifted:
                replay_doc["config_drift"] = drifted  # the diff IS the experiment — name it
            if calls_sha:
                replay_doc["calls_sha"] = calls_sha
            # our clean-host guard already ran (same evidence, replay-flavored message) — don't
            # fail-closed twice on one docker call
            run_id = runs_mod.new_run(manifest, root, run_label, force=True, replay=replay_doc)
            if calls_path is not None:  # the script is provenance the standard snapshot can't
                import shutil            # see (an arbitrary file) — copy + hash it explicitly
                rig_dir = Path(manifest.data_dir) / "runs" / run_id / ".rig"
                rig_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(calls_path, rig_dir / "replay-calls.yaml")
            runs_mod.snapshot(manifest, root, stacks=[s.name for s, _ in pairs])
        else:
            eprint("rig replay: warning: no `data_dir` — this session gets no run dir, no "
                   "provenance, and no recording of the new outputs")
    count = f"{len(value.split())} topics" if mode == "topics" else "namespace-exclude"
    svc_note = (", scripted calls" if calls_path is not None
                else f", {len(services.split())} services" if services else "")
    eprint(f"rig replay: {src_id} → {', '.join(names)}  [{mode}: {count}{svc_note}"
           f"{', wall clock' if wall_clock else ', sim time'}]")
    outcomes = dispatch.run_verb(pairs, env, "up", dry_run=dry_run)
    failed = [o for o in outcomes if o.returncode != 0]
    if failed:
        eprint(f"rig: {len(failed)}/{len(outcomes)} failed: "
               f"{', '.join(o.sensor.name for o in failed)}")
        return 1
    return 0
