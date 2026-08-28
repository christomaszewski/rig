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
        label: str | None, wall_clock: bool, force: bool, dry_run: bool) -> int:
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
    for n in notices:
        eprint(f"rig replay: {n}")

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

    _guard_clean_host(manifest, force)

    env = dispatch.fleet_env(manifest, descriptors)
    env["RIG_REPLAY_SOURCE"] = str(src_dir)
    env["RIG_REPLAY_TOPICS" if mode == "topics" else "RIG_REPLAY_EXCLUDE"] = value
    if not wall_clock:
        env["RIG_SIM_TIME"] = "1"

    if not dry_run:
        if manifest.data_dir:
            run_label = label or re.sub(r"[^A-Za-z0-9_-]", "-", f"replay-{src_id}")
            replay_doc = {"of": src_id, "source": str(src_dir), "with": list(names)}
            # our clean-host guard already ran (same evidence, replay-flavored message) — don't
            # fail-closed twice on one docker call
            runs_mod.new_run(manifest, root, run_label, force=True, replay=replay_doc)
            runs_mod.snapshot(manifest, root, stacks=[s.name for s, _ in pairs])
        else:
            eprint("rig replay: warning: no `data_dir` — this session gets no run dir, no "
                   "provenance, and no recording of the new outputs")
    count = f"{len(value.split())} topics" if mode == "topics" else "namespace-exclude"
    eprint(f"rig replay: {src_id} → {', '.join(names)}  [{mode}: {count}"
           f"{', wall clock' if wall_clock else ', sim time'}]")
    outcomes = dispatch.run_verb(pairs, env, "up", dry_run=dry_run)
    failed = [o for o in outcomes if o.returncode != 0]
    if failed:
        eprint(f"rig: {len(failed)}/{len(outcomes)} failed: "
               f"{', '.join(o.sensor.name for o in failed)}")
        return 1
    return 0
