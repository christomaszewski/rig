"""CLI orchestration. All three verbs consume the same freshly validated replay plan."""
from __future__ import annotations

import dataclasses
import json
import re
import time
from pathlib import Path

from . import RigError, dispatch, replay, resolve, runs
from .common import eprint
from . import replay_plan as planner, replay_prepare as prepare, replay_recipe as recipes
from .replay_sources import source_path


def report(plan, *, as_json=False):
    if as_json:
        print(json.dumps(plan, indent=2, default=str))
        return
    eprint("rig replay: " + (", ".join(plan["selected"]) or "no instances selected"))
    for name, role in plan.get("roles", {}).items():
        eprint(f"  start {name}: {role['role']} ({role['reason']})")
    for name, reason in plan["omitted"].items():
        eprint(f"  omit {name}: {reason}")
    if plan["source"] and plan["source"]["bag"]:
        bag = plan["source"]["bag"]
        eprint(f"  bag: {bag['logger']}/{bag['session']}; "
               f"{len(plan['playback'].get('topics', []))} data topics")
        eprint("  topics: " + (", ".join(plan["playback"].get("topics", [])) or "(clock-only timeline)"))
        for original, destination in plan["playback"].get("remap", {}).items():
            eprint(f"  remap {original} -> {destination}")
    for name, binding in plan["recordings"].items():
        eprint(f"  recordings {name}: {binding['path']} ({', '.join(binding['sessions']) or 'all'})")
    for issue in plan["issues"]:
        eprint(f"  [{issue['status']}] {issue['message']}")
    eprint(f"  ready_to_prepare: {str(plan['ready_to_prepare']).lower()}; "
           f"ready_to_run: {str(plan['ready_to_run']).lower()}")


def load_context(root):
    from .manifest import load_manifest
    from .catalog import load_catalog
    from .descriptor import load_descriptor
    manifest, catalog = load_manifest(root, validate_configs=False), load_catalog(root)
    descriptors = {}
    for service in {r.service for r in manifest.sensors}:
        try:
            if service in catalog:
                descriptors[service] = load_descriptor(service, catalog[service].path)
        except RigError as exc:
            eprint(f"rig replay: {exc}")
    return manifest, catalog, descriptors


def command(args, root):
    if getattr(args, "format", None) == "json":
        args.json = True
    try:
        return _command(args, root)
    except (RigError, OSError) as exc:
        if getattr(args, "json", False):
            print(json.dumps(dict(version=1, ready_to_prepare=False, ready_to_run=False,
                                  issues=[dict(status="error", code="input", message=str(exc),
                                               phase="prepare")])))
            return 1
        raise


def _command(args, root):
    action = args.cmd.removeprefix("replay-") if args.cmd != "replay" else "run"
    options = vars(args).copy()
    recipe_path, recipe = recipes.load(root, args.recipe, create=action == "prepare")
    options["_recipe_path"] = str(recipe_path) if recipe_path else None
    if recipe_path and recipe.get("playback", {}).get("calls") and not args.calls:
        calls = Path(recipe["playback"]["calls"]).expanduser()
        options["calls"] = str(calls if calls.is_absolute() else recipe_path.parent / calls)
    manifest, catalog, descriptors = load_context(root)
    ref = recipes.bound_source(root, recipe_path, recipe, args.run)
    origin = source_path(manifest, ref)
    if action == "prepare" and recipe_path is None:
        name = re.sub(r"[^A-Za-z0-9_-]", "-", origin.name)[:60] or "source"
        name += "-" + recipes.digest(str(origin))[:8]
        recipe_path, recipe = recipes.load(root, name, create=True)
        options["_recipe_path"] = str(recipe_path)
    runtime = prepare.prepared(root, recipe_path, recipe, origin) if recipe_path else None
    options["_prepared_source"] = str(runtime) if runtime else None
    plan = planner.build(manifest, catalog, descriptors, root, str(origin), recipe, options)
    if runtime and prepare.location(root, recipe_path, origin).name != prepare.generation(plan):
        runtime = None  # Changed bindings/selections require a new immutable generation.
        if plan["needs_preparation"]:
            plan["issues"].append(dict(status="not_checked", code="stale_preparation", phase="run",
                                       message="Source bindings changed; prepare this selection again"))
            planner.finish(plan)
    if runtime:
        plan["preparation"] = str(prepare.location(root, recipe_path, origin))
    if action != "prepare" or args.dry_run or not plan["ready_to_prepare"]:
        report(plan, as_json=getattr(args, "json", False))
    if action == "check":
        return 0 if plan["ready_to_run"] else 1
    if action == "prepare":
        if not plan["ready_to_prepare"]:
            return 1
        if args.dry_run:
            eprint(f"Would prepare {recipe_path} and {prepare.location(root, recipe_path, origin)}")
            if args.player:
                eprint(f"Would add player from {args.player}")
            return 0
        return perform_prepare(args, root, recipe_path, recipe, origin, manifest,
                               catalog, descriptors, plan, options)
    if not plan["ready_to_prepare"] or any(
            i["status"] == "error" for i in plan["issues"]):
        return 1
    if not plan["ready_to_run"] and not args.dry_run:
        return 1
    if plan["needs_preparation"] and runtime is None:
        if args.dry_run:
            eprint("Dry run: prepare the source before launcher commands can be rendered")
            return 0
        raise RigError("Source needs preparation; run rig replay prepare RUN --recipe NAME first")
    runtime = runtime or Path(plan["source"]["root"])
    return execute(args, root, manifest, descriptors, plan, runtime)


def perform_prepare(args, root, recipe_path, recipe, origin, manifest, catalog, descriptors, plan, options):
    import fcntl
    folder = recipes.workspace(root, recipe_path)
    folder.mkdir(parents=True, exist_ok=True)
    lock = (folder / ".prepare.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise RigError("Another preparation is already using this recipe workspace")
    saved = {}
    created_recipe = False
    try:
        if args.player and not plan["player"]:
            from .reconstruct import _enable_replay_target, wire_player
            target = _enable_replay_target(args.player)
            if target[0] == "ref" and not re.search(r"@\d+\.\d+\.\d+$", str(target[1])):
                raise RigError("--player registry references must be pinned with @VERSION")
            # Record all files wiring may change; restore only this transaction's writes.
            import tempfile
            import shutil
            backup = Path(tempfile.mkdtemp(prefix="replay-wiring-"))
            tracked = ["vehicle.yaml", "services.yaml", "rig.lock", "config", "services", "registries.yaml"]
            for name in tracked:
                p = root / name
                saved[name] = p.exists()
                if p.is_dir():
                    shutil.copytree(p, backup / name, symlinks=True)
                elif p.is_file():
                    (backup / name).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(p, backup / name)
            wire_player(root, target)
            manifest, catalog, descriptors = load_context(root)
            plan = planner.build(manifest, catalog, descriptors, root, str(origin), recipe, options)
        if not recipe_path.exists():
            recipe = {**recipe, "source": plan["source"]["id"]}
            prepare.write_yaml(recipe_path, recipe)
            created_recipe = True
            plan["recipe"] = recipe
        runtime = prepare.create(root, recipe_path, recipe, plan, manifest, descriptors)
        options["_prepared_source"] = str(runtime)
        plan = planner.build(manifest, catalog, descriptors, root, str(origin), recipe, options)
        # Recovered bag timing can narrow automatic camera session selection. Publish that
        # selection now, so the first successful prepare is already reusable by check/run.
        if (plan["ready_to_prepare"] and
                prepare.location(root, recipe_path, origin).name != prepare.generation(plan)):
            runtime = prepare.create(root, recipe_path, recipe, plan, manifest, descriptors)
            options["_prepared_source"] = str(runtime)
            plan = planner.build(manifest, catalog, descriptors, root, str(origin), recipe, options)
        plan["preparation"] = str(prepare.location(root, recipe_path, origin))
        prepare.refresh(root, recipe_path, plan)
        report(plan, as_json=getattr(args, "json", False))
        eprint(f"Prepared recipe: {recipe_path}")
        eprint(f"Replay with: rig replay --recipe {recipe_path}")
        return 0 if plan["ready_to_run"] else 1
    except BaseException:
        if saved:
            for name, existed in saved.items():
                p = root / name
                if p.is_dir():
                    shutil.rmtree(p)
                elif p.exists():
                    p.unlink()
                if existed:
                    if (backup / name).is_dir():
                        shutil.copytree(backup / name, p, symlinks=True)
                    else:
                        p.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(backup / name, p)
        if created_recipe:
            recipe_path.unlink(missing_ok=True)
        raise
    finally:
        if saved:
            shutil.rmtree(backup)
        lock.close()


def execute(args, root, manifest, descriptors, plan, runtime):
    for path, fingerprint in plan.get("control_files", {}).items():
        p = Path(path)
        current = recipes.digest(p.read_bytes().hex()) if p.is_file() else None
        if current != fingerprint:
            raise RigError(f"Deployment changed since planning: {p}; check again")
    for files in plan.get("launch_surfaces", {}).values():
        for path, fingerprint in files.items():
            if not Path(path).is_file() or recipes.digest(Path(path).read_bytes().hex()) != fingerprint:
                raise RigError(f"Launcher changed since planning: {path}; check again")
    if not args.dry_run and not args.export_calls:
        replay._guard_clean_host(manifest, args.force)
    source = plan["source"]
    bag = source["bag"]
    gate = time.time() + args.start_delay if args.start_delay else None
    variables = dict(manifest.vars or {})
    variables.update(replay_source=str(runtime), replay_session=args.session,
                     replay_retime="original" if plan["sim_time"] else "wall",
                     replay_start_at_unix_s=gate, replay_epoch_unix_ns=(bag or {}).get("start_ns"),
                     replay_from_s=plan["window"]["start"], replay_to_s=plan["window"]["end"])
    rows = {r.name: r for r in manifest.sensors}
    selected = []
    # Each execution gets immutable config paths; subsequent prepare/check cannot alter a live mount.
    session_root = root / "var" / "replay" / "sessions" / f"{time.time_ns()}"
    env = dispatch.fleet_env(manifest, descriptors)
    env["RIG_REPLAY_SOURCE"] = str(runtime)
    if plan["sim_time"]:
        env["RIG_SIM_TIME"] = "1"
    if gate:
        env["RIG_REPLAY_START_AT_UNIX_S"] = f"{gate:.3f}"
    env["RIG_REPLAY_FROM_S"] = str(plan["window"]["start"])
    if plan["window"]["end"] is not None:
        env["RIG_REPLAY_TO_S"] = str(plan["window"]["end"])
    if plan["player"]:
        path = session_root / "player.json"
        if not args.dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            document = prepare.player_document(plan, runtime)
            if document["calls"]:
                import shutil
                frozen_calls = session_root / "calls.yaml"
                shutil.copy2(document["calls"], frozen_calls)
                if replay.validate_calls(frozen_calls) != plan["calls_sha"]:
                    raise RigError("Call script changed since planning")
                document["calls"] = str(frozen_calls)
            path.write_text(json.dumps(document))
        env["RIG_REPLAY_PLAN"] = str(path)
    for name in plan["selected"]:
        row, desc = rows[name], descriptors[rows[name].service]
        config = resolve._layered_dict(row, root, variables={**variables, "name": name},
                                     extra_overrides=desc.replay_source.overrides
                                     if name in plan["recordings"] else None)
        path = session_root / "config" / (name + ".yaml")
        if not args.dry_run:
            prepare.write_yaml(path, config)
        selected.append(dataclasses.replace(row, config=path, enabled=True))
    pairs = [(r, descriptors[r.service]) for r in selected]
    by_name = {r.name: r for r in selected}
    execution_manifest = dataclasses.replace(manifest, sensors=[
        by_name.get(r.name, dataclasses.replace(r, enabled=False)) for r in manifest.sensors])
    from .replay_sources import inventory_fingerprint
    if inventory_fingerprint(source["path"]) != source["fingerprint"]:
        raise RigError("Source changed since planning; check/prepare again")
    for binding in plan["recordings"].values():
        if inventory_fingerprint(binding["path"]) != binding["fingerprint"]:
            raise RigError("Recordings changed since planning; check/prepare again")
    if plan["playback"]["calls"] and replay.validate_calls(
            Path(plan["playback"]["calls"])) != plan["calls_sha"]:
        raise RigError("Call script changed since planning; check again")
    if args.export_calls:
        if args.names:
            raise RigError("--export-calls takes no instance names")
        player = next((r for r in selected if r.name == plan["player"]), None)
        if not player:
            raise RigError("--export-calls requires a selected bag and player")
        outcomes = dispatch.run_verb([(player, descriptors[player.service])], env, "export-calls",
                                    dry_run=args.dry_run)
        return int(any(o.returncode for o in outcomes))
    if not args.dry_run and manifest.data_dir:
        run_id = runs.new_run(manifest, root, args.label or re.sub(
            r"[^A-Za-z0-9_-]", "-", "replay-" + source["id"]), force=True,
            replay=dict({"of": source["id"], "source": source["path"], "with": args.names},
                        recipe=plan["recipe"], fingerprint=plan["fingerprint"],
                        source_fingerprint=source["fingerprint"], runtime_source=str(runtime), bag=bag,
                        execution=plan.get("execution"), epoch_unix_ns=(bag or {}).get("start_ns"),
                        skipped={"services": args.skip_services, "instances": args.skip_instances},
                        not_started=plan["omitted"],
                        playback=plan["playback"], window=plan["window"],
                        sources=plan["recordings"], selected=plan["selected"],
                        clock="sim" if plan["sim_time"] else "wall",
                        start_at_unix_s=gate, calls_sha=plan.get("calls_sha")))
        import shutil
        run_dir = Path(manifest.data_dir) / "runs" / run_id / ".rig" / "replay"
        run_dir.mkdir(parents=True, exist_ok=True)
        prepare.write_yaml(run_dir / "resolved.yaml", plan)
        prepare.write_yaml(run_dir / "recipe.yaml", plan["recipe"])
        if plan.get("preparation"):
            shutil.copy2(Path(plan["preparation"]) / "provenance.yaml", run_dir / "provenance.yaml")
        if plan["playback"]["calls"]:
            shutil.copy2(plan["playback"]["calls"], run_dir / "calls.yaml")
        runs.snapshot(execution_manifest, root, stacks=plan["selected"])
    outcomes = dispatch.run_verb(pairs, env, "up", dry_run=args.dry_run)
    if any(o.returncode for o in outcomes):
        return 1
    if gate and not args.dry_run and time.time() > gate:
        eprint("rig replay: warning: startup outlasted the release gate; increase --start-delay "
               "to avoid losing early inputs")
    if args.auto_end is not None and not args.dry_run:
        player = next(r for r in selected if r.name == plan["player"])
        return replay.auto_end(execution_manifest, descriptors, root, pairs, env, player, args.auto_end)
    return 0
