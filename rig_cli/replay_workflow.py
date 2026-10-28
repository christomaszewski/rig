"""CLI orchestration. All three verbs consume the same freshly validated replay plan."""
from __future__ import annotations

import copy
import dataclasses
import json
import os
import re
import shlex
import subprocess
import time
from pathlib import Path

from . import RigError, dispatch, replay, resolve, runs
from .common import eprint, load_yaml
from .manifest import project_name
from . import replay_plan as planner, replay_prepare as prepare, replay_recipe as recipes
from .replay_sources import source_path, source_fingerprint, binding_variables, verify_binding
from . import session_recipe as sessions


class _PreparationNotReady(Exception):
    """Roll back unpublished preparation changes before reporting the detailed plan."""


def report(plan, *, as_json=False):
    if as_json:
        print(json.dumps(plan, indent=2, default=str))
        return
    eprint("rig replay: " + (", ".join(plan["selected"]) or "no instances selected"))
    for name, role in plan.get("roles", {}).items():
        eprint(f"  start {name}: {role['role']} ({role['reason']})")
    for name, reason in plan["omitted"].items():
        eprint(f"  omit {name}: {reason}")
    for name, namespace in plan.get("recorded_namespaces", {}).items():
        source = plan.get("recorded_namespace_sources", {}).get(name, "recipe")
        eprint(f"  recorded ROS namespace {name}: {namespace} ({source})")
    if plan["source"] and plan["source"]["bag"]:
        bag = plan["source"]["bag"]
        eprint(f"  bag: {bag['logger']}/{bag['session']}; "
               f"{len(plan['playback'].get('topics', []))} data topics")
        eprint("  topics: " + (", ".join(plan["playback"].get("topics", [])) or "(clock-only timeline)"))
        mode = plan.get("service_selection", {}).get("mode", "manual")
        eprint(f"  service calls ({mode}, {plan['playback'].get('services_source', 'service')} origin): " + (plan["playback"].get("calls") or
               ", ".join(plan["playback"].get("services", [])) or "(none selected)"))
        if mode == "auto":
            for record in plan["service_selection"]["records"]:
                if record["decision"] == "exclude":
                    eprint(f"  omit recorded calls {record['service']}: {record['reason']}")
        regenerated = plan.get("excluded_regenerated", [])
        if regenerated:
            kept = set(plan.get("recipe", {}).get("playback", {}).get("keep", []))
            excluded = [t for t in regenerated if t not in kept]
            if excluded:
                eprint("  omit regenerated outputs: " + ", ".join(excluded))
        for original, destination in plan["playback"].get("remap", {}).items():
            eprint(f"  remap {original} -> {destination}")
    for name, binding in plan["recordings"].items():
        proxy = binding.get("proxy")
        mode = (f"proxy {proxy['profile']} / {proxy['export_id']} "
                f"({'lossy' if proxy.get('lossy', True) else 'lossless'})") if proxy else "original"
        eprint(f"  recordings {name}: {mode}; {binding['path']} "
               f"({', '.join(binding['sessions']) or 'all'})")
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
    if args.start_delay is None:
        args.start_delay = 0.0 if getattr(args, "startup", "legacy") == "coordinated" else 20.0
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


def _session_arguments(args, recipe):
    """V2 exact selection cannot be silently expanded or reduced by legacy CLI switches."""
    if recipe.get("version") != 2:
        return args
    args = copy.copy(args)
    selected = set(recipe["session"]["instances"])
    additions = set(args.names or []) | set(args.live or [])
    if additions - selected:
        raise RigError("Session recipe selects exact instances; add these to session.instances: " +
                       ", ".join(sorted(additions - selected)))
    if selected & set(args.skip_instances or []):
        raise RigError("Cannot skip an instance selected by session.instances; edit the session recipe")
    if args.include_services or args.skip_services:
        raise RigError("Session recipes use exact instance selection; edit session.instances instead "
                       "of --include-service/--skip-service")
    if args.export_calls:
        raise RigError("--export-calls uses a standalone player; use a version-1 replay recipe")
    clock = recipe["session"].get("clock", "source")
    if args.wall_clock and clock != "wall":
        raise RigError("--wall-clock conflicts with session.clock: source; edit the session recipe")
    args.wall_clock = clock == "wall"
    return args


def _build_plan(manifest, catalog, descriptors, root, ref, recipe, options, **kwargs):
    """Lower provider bindings while retaining full session intent in hashes and provenance."""
    plan = planner.build(manifest, catalog, descriptors, root, ref,
                         sessions.provider_recipe(recipe, "replay"), options, **kwargs)
    if recipe.get("version") == 2:
        plan["session_recipe"] = recipe
        plan["fingerprint"] = recipes.digest(dict(planned=plan["fingerprint"], session_recipe=recipe))
        if recipe["session"].get("clock", "source") == "source" and not plan["sim_time"]:
            plan["issues"].append(dict(status="error", code="session_clock", phase="prepare",
                message="session.clock: source requires a bag timeline for replay; choose wall "
                        "for a recordings-only replay"))
            planner.finish(plan)
    return plan


def _session_environment(manifest, descriptors):
    """Inventory, ownership checks and launchers must address the same Docker engine."""
    from .session_runtime import DOCKER_ENV
    env = dispatch.fleet_env(manifest, descriptors)
    if any(env.get(key) != os.environ.get(key) for key in DOCKER_ENV):
        raise RigError("Session Docker routing must come from the shell; remove Docker routing "
                       "overrides from vehicle.extra_env before replay")
    return env


def _command(args, root):
    from .service_exports import VerificationContext
    verification = VerificationContext()
    planning_started = time.monotonic()
    action = args.cmd.removeprefix("replay-") if args.cmd != "replay" else "run"
    recipe_path, recipe = recipes.load(root, args.recipe, create=action == "prepare")
    manifest, catalog, descriptors = load_context(root)
    if action == "prepare" and recipe_path is None:
        origin = source_path(manifest, args.run)
        name = re.sub(r"[^A-Za-z0-9_-]", "-", origin.name)[:60] or "source"
        name += "-" + recipes.digest(str(origin))[:8]
        # Implicit preparation creates/uses its replay-local recipe. A same-named
        # shared recipe is opt-in through --recipe, never an accidental selector.
        recipe_path, recipe = recipes.load(root, str(root / "replay" / (name + ".yaml")), create=True)
    args = _session_arguments(args, recipe)
    options = vars(args).copy()
    options["_recipe_path"] = str(recipe_path) if recipe_path else None
    provider_recipe = sessions.provider_recipe(recipe, "replay")
    if recipe_path and provider_recipe.get("playback", {}).get("calls") and not args.calls:
        calls = Path(provider_recipe["playback"]["calls"]).expanduser()
        options["calls"] = str(calls if calls.is_absolute() else recipe_path.parent / calls)
    manifest = sessions.apply_session(manifest, recipe)
    if recipe.get("version") == 2:
        _session_environment(manifest, descriptors)
    eprint("rig replay: planning selection and verifying recorded inputs")
    ref = recipes.bound_source(root, recipe_path, recipe, args.run)
    origin = source_path(manifest, ref)
    use_proxies = bool(options.get("use_proxies"))
    if recipe_path and use_proxies:
        options["_prepared_proxies"] = prepare.proxy_pins(root, recipe_path, recipe, origin)
    runtime = prepare.prepared(root, recipe_path, recipe, origin, use_proxies=use_proxies) if recipe_path else None
    options["_prepared_source"] = str(runtime) if runtime else None
    plan = _build_plan(manifest, catalog, descriptors, root, str(origin), recipe, options,
                         verification=verification)
    if runtime and prepare.location(root, recipe_path, origin, use_proxies=use_proxies).name != prepare.generation(plan):
        runtime = None  # Changed bindings/selections require a new immutable generation.
        # Rebuild without the old runtime: its repaired bag metadata and rendered root must
        # not leak into an execution that is about to fall back to the untouched source.
        options["_prepared_source"] = None
        plan = _build_plan(manifest, catalog, descriptors, root, str(origin), recipe, options,
                             verification=verification)
        if plan["needs_preparation"]:
            plan["issues"].append(dict(status="not_checked", code="stale_preparation", phase="run",
                                       message="Source bindings changed; prepare this selection again"))
            planner.finish(plan)
    if runtime:
        plan["preparation"] = str(prepare.location(root, recipe_path, origin, use_proxies=use_proxies))
    plan["planning_s"] = time.monotonic() - planning_started
    eprint(f"rig replay: planning and source inspection finished in {plan['planning_s']:.1f}s")
    # Planning never pulls. Execution acquires only this frozen inventory, then
    # updates image evidence without re-reading large bags/proxies or re-querying tags.
    if (action == "run" and not args.dry_run and plan.get("image_inventory") and plan.get("image_inspection")
            and not any(i["status"] == "error" for i in plan["issues"])
            and not any(i["status"] == "not_checked" and i["code"] not in
                        ("runtime_images", "normalization", "stale_preparation", "recorded_calls",
                         "coordination_readiness") for i in plan["issues"])):
        from . import runtime_images
        _verify_launch_inputs(plan)
        started = time.monotonic()
        eprint("rig replay: acquiring and verifying runtime images")
        inspected = runtime_images.acquire_images(plan["image_inventory"],
                        policy=plan["pull_policy"], inspection=plan["image_inspection"])
        planner.apply_image_report(plan, inspected)
        plan["image_acquisition_s"] = time.monotonic() - started
        plan["fingerprint"] = recipes.digest(dict(planned=plan["fingerprint"],
                                                   images=plan["execution"]["images"]))
    if (action == "run" and not args.dry_run and
            not any(i["status"] == "error" or
                    (i["status"] == "not_checked" and i["code"] not in ("recorded_calls", "coordination_readiness"))
                    for i in plan["issues"])):
        _verify_launch_inputs(plan)
        started = time.monotonic()
        planner.inspect_recorded_calls(plan, manifest, descriptors, root, execute=True,
                                       verification=verification)
        from .replay_coordinator import refresh_requirements
        refresh_requirements(plan, manifest, descriptors,
                             [r for r in manifest.sensors if r.name in plan["selected"]])
        planner.finish(plan)
        plan["call_inspection_s"] = time.monotonic() - started
        plan["fingerprint"] = recipes.digest(dict(planned=plan["fingerprint"],
            playback=plan["playback"], call_inspection=plan.get("call_inspection"), startup=plan.get("startup")))
    if action != "prepare" or args.dry_run or not plan["ready_to_prepare"]:
        report(plan, as_json=getattr(args, "json", False))
    if action == "check":
        return 0 if plan["ready_to_run"] else 1
    if action == "prepare":
        if not plan["ready_to_prepare"]:
            return 1
        if args.dry_run:
            eprint(f"Would prepare {recipe_path} and "
                   f"{prepare.location(root, recipe_path, origin, key=prepare.generation(plan))}")
            if args.player:
                eprint(f"Would add player from {args.player}")
            return 0
        return perform_prepare(args, root, recipe_path, recipe, origin, manifest,
                               catalog, descriptors, plan, options, verification=verification)
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


def perform_prepare(args, root, recipe_path, recipe, origin, manifest, catalog, descriptors, plan, options,
                    *, verification=None):
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
            manifest = sessions.apply_session(manifest, recipe)
            plan = _build_plan(manifest, catalog, descriptors, root, str(origin), recipe, options,
                                 verification=verification)
        if ((plan["source"].get("bag") or {}).get("repair")
                and not plan.get("image_pins", {}).get(plan["player"])):
            plan["issues"].append(dict(status="error", code="repair_image", phase="prepare",
                message="Metadata repair needs a locally verified player image; prepare never pulls. "
                        "Acquire the reported player image before preparing this source"))
            planner.finish(plan)
            raise _PreparationNotReady
        if not recipe_path.exists():
            recipe = {**recipe, "source": plan["source"]["id"]}
            prepare.write_yaml(recipe_path, recipe)
            created_recipe = True
            plan["recipe"] = recipe
        runtime = prepare.create(root, recipe_path, recipe, plan, manifest, descriptors)
        options["_prepared_source"] = str(runtime)
        use_proxies = bool(options.get("use_proxies"))
        if use_proxies:
            options["_prepared_proxies"] = prepare.proxy_pins(root, recipe_path, recipe, origin)
        plan = _build_plan(manifest, catalog, descriptors, root, str(origin), recipe, options,
                             verification=verification)
        # Recovered bag timing can narrow automatic camera session selection. Publish that
        # selection now, so the first successful prepare is already reusable by check/run.
        if (plan["ready_to_prepare"] and
                prepare.location(root, recipe_path, origin, use_proxies=use_proxies).name != prepare.generation(plan)):
            runtime = prepare.create(root, recipe_path, recipe, plan, manifest, descriptors)
            options["_prepared_source"] = str(runtime)
            plan = _build_plan(manifest, catalog, descriptors, root, str(origin), recipe, options,
                                 verification=verification)
        plan["preparation"] = str(prepare.location(root, recipe_path, origin, use_proxies=use_proxies))
        if plan["ready_to_prepare"]:
            _verify_launch_inputs(plan)
            planner.inspect_recorded_calls(plan, manifest, descriptors, root, execute=True,
                                           verification=verification)
            plan["fingerprint"] = recipes.digest(dict(planned=plan["fingerprint"],
                playback=plan["playback"], call_inspection=plan.get("call_inspection")))
        prepare.refresh(root, recipe_path, plan)
        report(plan, as_json=getattr(args, "json", False))
        eprint(f"Prepared recipe: {recipe_path}")
        replay_command = ["rig", "replay", "--recipe", str(recipe_path)]
        if use_proxies:
            replay_command.append("--use-proxies")
        for flag, values in (("--skip-service", args.skip_services),
                             ("--skip-instance", args.skip_instances)):
            for value in values:
                replay_command.extend([flag, value])
        eprint("Replay with: " + shlex.join(replay_command))
        return 0 if plan["ready_to_run"] else 1
    except BaseException as exc:
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
        if isinstance(exc, _PreparationNotReady):
            report(plan, as_json=getattr(args, "json", False))
            return 1
        raise
    finally:
        if saved:
            shutil.rmtree(backup)
        lock.close()


def _project_container_ids(manifest, row, env=None):
    project = project_name(row.name, manifest.vehicle_id)
    try:
        result = subprocess.run(
            ["docker", "ps", "-aq", "--no-trunc", "--filter", f"label=com.docker.compose.project={project}"],
            capture_output=True, text=True, timeout=15, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RigError(f"Cannot inspect replay project {project}: {exc}") from exc
    ids = (result.stdout or "").split()
    if result.returncode or any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in ids):
        raise RigError(f"Cannot inspect replay project {project}: "
                       f"{(result.stderr or '').strip() or 'invalid Docker inventory'}")
    return ids


def _empty_projects(manifest, pairs):
    """Only an initially empty project can be cleaned up as this invocation's resource."""
    return {row.name for row, _ in pairs if not _project_container_ids(manifest, row)}


def _quiesce_projects(manifest, pairs):
    """Stop without removing containers before potentially slow log capture."""
    try:
        ids = list(dict.fromkeys(cid for row, _ in pairs for cid in _project_container_ids(manifest, row)))
        if ids:
            result = subprocess.run(["docker", "stop", "--time", "1", *ids],
                                    capture_output=True, text=True, timeout=30)
            if result.returncode:
                raise RigError((result.stderr or "docker stop failed").strip())
        return True
    except (RigError, OSError, subprocess.TimeoutExpired) as exc:
        eprint(f"rig replay: could not stop new containers before cleanup: {exc}; skipping log capture")
        return False


def _save_execution(session_root, run_dir, state, *, required=True):
    """Persist to this invocation's run, never whichever run is current later."""
    try:
        prepare.write_yaml(session_root / "execution.yaml", state)
        if run_dir is not None:
            prepare.write_yaml(run_dir / ".rig" / "replay" / "execution.yaml", state)
            path = run_dir / "manifest.yaml"
            if path.is_file():
                document = load_yaml(path)
                metadata = document.setdefault("replay", {})
                if not isinstance(metadata, dict):
                    raise RigError(f"Replay metadata is malformed in {path}")
                metadata["start_at_unix_s"] = state.get("start_at_unix_s")
                metadata["startup"] = state
                prepare.write_yaml(path, document)
    except (RigError, OSError) as exc:
        if required:
            raise
        eprint(f"rig replay: could not save startup outcome: {exc}")


def _abort_start(manifest, attempted, empty, env):
    """Stop newly created projects after launch failure, keeping preexisting projects intact."""
    owned = [(row, desc) for row, desc in attempted if row.name in empty]
    retained = [row.name for row, _ in attempted if row.name not in empty]
    quiesced = _quiesce_projects(manifest, owned) if owned else True
    if quiesced and owned and manifest.data_dir:
        owned_manifest = dataclasses.replace(manifest, sensors=[row for row, _ in owned])
        runs.capture_docker_logs(owned_manifest, also={row.name for row, _ in owned})
    failed = []
    for row, desc in reversed(owned):
        try:
            outcomes = dispatch.run_verb([(row, desc)], env, "down")
            if any(o.returncode for o in outcomes):
                failed.append(row.name)
        except (RigError, OSError) as exc:
            eprint(f"rig replay: cleanup failed for {row.name}: {exc}")
            failed.append(row.name)
    if retained:
        eprint("rig replay: retaining preexisting projects: " + ", ".join(retained))
    if failed:
        eprint("rig replay: cleanup incomplete for " + ", ".join(failed) + "; inspect before retrying")
    return dict(stopped=[row.name for row, _ in owned if row.name not in failed],
                retained=retained, failed=failed, quiesced=quiesced)


def _verify_launch_inputs(plan):
    for path, fingerprint in plan.get("control_files", {}).items():
        p = Path(path)
        current = recipes.digest(p.read_bytes().hex()) if p.is_file() else None
        if current != fingerprint:
            raise RigError(f"Deployment changed since planning: {p}; check again")
    for files in plan.get("launch_surfaces", {}).values():
        for path, fingerprint in files.items():
            if not Path(path).is_file() or recipes.digest(Path(path).read_bytes().hex()) != fingerprint:
                raise RigError(f"Launcher changed since planning: {path}; check again")


def _abort_owned(container_ids):
    """Never tear down a whole Compose project during coordinated failure cleanup."""
    if not container_ids:
        return dict(removed=[], failed=[])
    try:
        result = subprocess.run(["docker", "rm", "--force", *sorted(container_ids)],
                                capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise RigError((result.stderr or "docker rm failed").strip())
        return dict(removed=sorted(container_ids), failed=[])
    except (RigError, OSError, subprocess.TimeoutExpired) as exc:
        eprint(f"rig replay: exact-container cleanup incomplete: {exc}")
        return dict(removed=[], failed=sorted(container_ids), error=str(exc))


def _finish_owned(container_ids):
    """Successful playback must let recorders finalize before exact-ID removal.

    Docker honors each image's stop signal. Bound the drain to 30 seconds; a
    forced kill or failed shutdown leaves the containers and run for inspection.
    """
    ids = sorted(container_ids)
    if not ids:
        return
    try:
        stopped = subprocess.run(["docker", "stop", "--time", "30", *ids],
                                 capture_output=True, text=True, timeout=45)
        if stopped.returncode:
            raise RigError((stopped.stderr or "docker stop failed").strip())
        inspected = subprocess.run(["docker", "inspect", "--type", "container", *ids],
                                   capture_output=True, text=True, timeout=10)
        docs = json.loads(inspected.stdout) if inspected.returncode == 0 else []
        if not isinstance(docs, list) or {d.get("Id") for d in docs} != set(ids):
            raise ValueError("owned containers disappeared during shutdown")
        for doc in docs:
            state = doc["State"]
            if (state.get("Running") or state.get("Restarting") or state.get("OOMKilled")
                    or state.get("Status") != "exited" or state.get("ExitCode") not in (0, 130, 143)):
                raise ValueError(f"{doc['Id'][:12]}: shutdown did not finish cleanly ({state.get('ExitCode')})")
        removed = subprocess.run(["docker", "rm", *ids], capture_output=True, text=True, timeout=30)
        if removed.returncode:
            raise RigError((removed.stderr or "docker rm failed").strip())
    except (KeyError, TypeError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
        raise RigError(f"Owned-container finalization failed: {exc}") from exc


def _check_owned_health(container_ids, *, allow_player_exit=False, completed_ids=()):
    if not container_ids:
        return
    try:
        result = subprocess.run(["docker", "inspect", "--type", "container", *sorted(container_ids)],
                                capture_output=True, text=True, timeout=10)
        docs = json.loads(result.stdout) if result.returncode == 0 else []
        if not isinstance(docs, list) or {d.get("Id") for d in docs} != set(container_ids):
            raise ValueError("owned containers disappeared")
        for doc in docs:
            state = doc["State"]
            role = doc["Config"].get("Labels", {}).get("com.docker.compose.service")
            if state.get("OOMKilled") or state.get("Restarting"):
                raise ValueError(f"{doc['Id'][:12]}: OOM or restart during startup")
            if state.get("Running") is not True and not ((doc["Id"] in completed_ids or
                    (allow_player_exit and role in ("bag-player", "call-injector"))) and state.get("Status") == "exited"
                    and state.get("ExitCode") == 0):
                raise ValueError(f"{doc['Id'][:12]} ({role}): stopped during startup")
    except (KeyError, TypeError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
        raise RigError(f"Replay participant container health lost: {exc}") from exc


def execute(args, root, manifest, descriptors, plan, runtime):
    if args.start_delay is None:
        args.start_delay = 0.0 if plan.get("startup", {}).get("mode") == "coordinated" else 20.0
    from .replay_coordinator import project_locks
    if not args.dry_run:
        from .session_runtime import session_lock
        with session_lock(root), project_locks(manifest, plan["selected"], dispatch.fleet_env(manifest, descriptors)):
            shared = plan.get("session_recipe", {}).get("version") == 2
            if (shared or plan.get("startup", {}).get("mode") == "coordinated") and not args.export_calls:
                rows = {r.name: r for r in manifest.sensors}
                for name in plan["selected"]:
                    if _project_container_ids(manifest, rows[name]):
                        mode = "shared session" if shared else "coordinated"
                        raise RigError(f"{name}: {mode} startup requires an empty project; stop it first")
            return _execute(args, root, manifest, descriptors, plan, runtime)
    return _execute(args, root, manifest, descriptors, plan, runtime)


def _execute(args, root, manifest, descriptors, plan, runtime):
    """Keep v2 replay lifecycle evidence even when startup or auto-end raises."""
    from . import session_runtime
    lifecycle = {}
    if not args.dry_run:
        if session_runtime.active_session(root):
            raise RigError("A saved session exists; run rig down --end-run before replay")
        if plan.get("session_recipe", {}).get("version") == 2:
            lifecycle["engine_id"] = session_runtime.engine_id(_session_environment(manifest, descriptors))
    try:
        return _execute_inner(args, root, manifest, descriptors, plan, runtime, lifecycle)
    except BaseException as exc:
        lifecycle["error"] = str(exc) or type(exc).__name__
        raise
    finally:
        if lifecycle.get("path") and not lifecycle.get("cleared"):
            saved = lifecycle["state"]
            execution = lifecycle.get("execution", {})
            status = execution.get("status", "failed")
            saved["status"] = "running" if status == "started" else status
            saved["attempted"] = list(execution.get("attempted", saved["attempted"]))
            for key in ("cleanup", "error", "container_ids"):
                if key in execution:
                    saved[key] = execution[key]
            if lifecycle.get("error"):
                saved.update(status="failed", error=lifecycle["error"])
            try:
                session_runtime.save(lifecycle["path"], saved)
            except (RigError, OSError) as exc:
                # The pointer and pre-launch evidence already exist. Preserve the
                # startup failure while allowing frozen manual teardown to retry.
                eprint(f"rig replay: could not save session outcome: {exc}")


def _execute_inner(args, root, manifest, descriptors, plan, runtime, lifecycle):
    coordinated = plan.get("startup", {}).get("mode") == "coordinated"
    timings = {"planning_s": plan.get("planning_s", 0), "images_s": plan.get("image_acquisition_s", 0),
               "call_inspection_s": plan.get("call_inspection_s", 0)}
    phase_started = time.monotonic()
    eprint("rig replay: verifying source and launch inputs")
    _verify_launch_inputs(plan)
    if not args.dry_run and not args.export_calls:
        replay._guard_clean_host(manifest, args.force)
    source = plan["source"]
    bag = source["bag"]
    if source_fingerprint(source["path"]) != source["fingerprint"]:
        raise RigError("Source changed since planning; check/prepare again")
    for binding in plan["recordings"].values():
        verify_binding(binding)
    if not args.dry_run:
        planner.verify_recorded_call_evidence(plan, manifest, descriptors, root)
    if plan["playback"]["calls"] and replay.validate_calls(
            Path(plan["playback"]["calls"])) != plan["calls_sha"]:
        raise RigError("Call script changed since planning; check again")
    timings["verify_s"] = time.monotonic() - phase_started
    gate = None
    variables = dict(manifest.vars or {})
    variables.update(replay_source=str(runtime), replay_session=args.session,
                     replay_retime="original" if plan["sim_time"] else "wall",
                     replay_start_at_unix_s=gate, replay_epoch_unix_ns=(bag or {}).get("start_ns"),
                     replay_from_s=plan["window"]["start"], replay_to_s=plan["window"]["end"])
    rows = {r.name: r for r in manifest.sensors}
    selected = []
    # Each execution gets immutable config paths; subsequent prepare/check cannot alter a live mount.
    import uuid
    session_root = root / "var" / "replay" / "sessions" / (uuid.uuid4().hex if coordinated else f"{time.time_ns()}")
    env = dispatch.fleet_env(manifest, descriptors)
    if plan.get("execution", {}).get("architecture"):
        env["RIG_TARGET_ARCH"] = plan["execution"]["architecture"]
    if plan.get("image_pins") and set(plan["selected"]) <= set(plan["image_pins"]):
        env["RIG_RUNTIME_IMAGE_MAP"] = json.dumps(plan["image_pins"])
    elif not args.dry_run:
        raise RigError("Replay runtime images were not validated; check the image inventory before launch")
    env["RIG_REPLAY_SOURCE"] = str(runtime)
    env = dispatch.with_clock(env, provider="replay" if plan["sim_time"] else None)
    env["RIG_REPLAY_FROM_S"] = str(plan["window"]["start"])
    env["RIG_REPLAY_TO_S"] = str(plan["window"]["end"] if plan["window"]["end"] is not None else 0)
    if coordinated:
        env.update(RIG_REPLAY_CONTROL_DIR=str(session_root / "control"),
                   RIG_REPLAY_SESSION_ID=session_root.name, RIG_REPLAY_PLAN_ID=plan["fingerprint"])
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
        config = resolve._layered_dict(row, root,
                                     variables=binding_variables(variables, row, plan["recordings"].get(name)),
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
    if args.export_calls:
        if args.names:
            raise RigError("--export-calls takes no instance names")
        player = next((r for r in selected if r.name == plan["player"]), None)
        if not player:
            raise RigError("--export-calls requires a selected bag and player")
        outcomes = dispatch.run_verb([(player, descriptors[player.service])], env, "export-calls",
                                    dry_run=args.dry_run)
        return int(any(o.returncode for o in outcomes))
    empty = set()
    run_id, output_run = None, None
    phase_started = time.monotonic()
    if not args.dry_run and manifest.data_dir:
        eprint("rig replay: capturing replay run")
        # Capture the resolved configs handed to this session, including v2 overrides.
        # Preserve the deployment's enabled flags: the managed bag-player must remain
        # disabled for ordinary up after reconstruction. The execution snapshot below
        # separately records every row actually launched, including that player.
        capture_manifest = dataclasses.replace(manifest, sensors=[
            dataclasses.replace(row, config=by_name[row.name].config)
            if row.name in by_name else row for row in manifest.sensors])
        run_id = runs.new_run(capture_manifest, root, args.label or re.sub(
            r"[^A-Za-z0-9_-]", "-", "replay-" + source["id"]), force=True,
            replay=dict({"of": source["id"], "source": source["path"], "with": args.names},
                        recipe=plan.get("session_recipe", plan["recipe"]), fingerprint=plan["fingerprint"],
                        use_proxies=bool(plan.get("use_proxies")),
                        source_fingerprint=source["fingerprint"], runtime_source=str(runtime), bag=bag,
                        execution=plan.get("execution"), epoch_unix_ns=(bag or {}).get("start_ns"),
                        skipped={"services": args.skip_services, "instances": args.skip_instances},
                        not_started=plan["omitted"],
                        playback=plan["playback"], window=plan["window"],
                        sources=plan["recordings"], selected=plan["selected"],
                        clock="sim" if plan["sim_time"] else "wall",
                        start_at_unix_s=gate, calls_sha=plan.get("calls_sha")))
        import shutil
        output_run = Path(manifest.data_dir) / "runs" / run_id
        run_dir = output_run / ".rig" / "replay"
        run_dir.mkdir(parents=True, exist_ok=True)
        prepare.write_yaml(run_dir / "resolved.yaml", plan)
        prepare.write_yaml(run_dir / "recipe.yaml", plan.get("session_recipe", plan["recipe"]))
        if plan.get("preparation"):
            shutil.copy2(Path(plan["preparation"]) / "provenance.yaml", run_dir / "provenance.yaml")
        if plan["playback"]["calls"]:
            shutil.copy2(plan["playback"]["calls"], run_dir / "calls.yaml")
    timings["capture_s"] = time.monotonic() - phase_started
    _verify_launch_inputs(plan)
    if plan.get("session_recipe"):
        # Capturing a run may take minutes. The shared session must still launch
        # the same recording generation that was checked before capture.
        if source_fingerprint(source["path"]) != source["fingerprint"]:
            raise RigError("Source changed during replay run capture; no services started")
        for binding in plan["recordings"].values():
            verify_binding(binding)
    if not args.dry_run:
        planner.verify_recorded_call_evidence(plan, manifest, descriptors, root)
    # Capture and source verification can take minutes. Only final rendering/snapshotting
    # remains after the legacy deadline is chosen. Zero delay still releases paused sources.
    gate = None if coordinated else round(time.time() + args.start_delay, 3)
    variables["replay_start_at_unix_s"] = gate
    if gate is not None:
        env["RIG_REPLAY_START_AT_UNIX_S"] = f"{gate:.3f}"
    if not args.dry_run:
        for row, desc in pairs:
            config = resolve._layered_dict(rows[row.name], root,
                variables=binding_variables(variables, rows[row.name], plan["recordings"].get(row.name)),
                extra_overrides=desc.replay_source.overrides if row.name in plan["recordings"] else None)
            prepare.write_yaml(row.config, config)
        _verify_launch_inputs(plan)
        if manifest.data_dir:
            runs.snapshot(execution_manifest, root, stacks=plan["selected"])
    state = dict(session=session_root.name, run_id=run_id, status="starting",
                 startup_mode="coordinated" if coordinated else "legacy",
                 start_at_unix_s=gate, timings=timings, attempted=[])
    lifecycle["execution"] = state
    if "engine_id" in lifecycle:
        from . import session_runtime
        if session_runtime.engine_id(env) != lifecycle["engine_id"]:
            raise RigError("Docker engine changed during replay preparation; no services started")
        lifecycle["path"], lifecycle["state"] = session_runtime.register(
            root, execution_manifest, pairs, env, provider="replay",
            engine_id=lifecycle["engine_id"], launch_surfaces=plan["launch_surfaces"],
            run_id=run_id, output_run=output_run)
        # The player consumes these through the saved environment rather than its
        # row YAML; they are part of the frozen configuration used by teardown.
        for extra in (session_root / "player.json", session_root / "calls.yaml"):
            if extra.is_file():
                lifecycle["state"]["configs"][str(extra)] = recipes.digest(extra.read_bytes().hex())
        session_runtime.save(lifecycle["path"], lifecycle["state"])
    coordinator = None
    if coordinated and not args.dry_run:
        from .replay_coordinator import Coordinator
        coordinator = Coordinator(session_root / "control", session_id=session_root.name,
            plan_id=plan["fingerprint"], participants=plan["startup"]["participants"],
            requirements=plan["startup"]["requirements"], timeout_s=plan["startup"]["timeout_s"],
            ready_timeout_s=plan["startup"]["ready_timeout_s"])
    attempted = []
    owned_ids = set()
    container_owners = {}
    recorded_projects = set()
    def remember_owned():
        if coordinator is not None:
            for row, _ in attempted:
                if row.name not in recorded_projects:
                    ids = _project_container_ids(manifest, row)
                    owned_ids.update(ids)
                    container_owners.update({cid: row.name for cid in ids})
                    recorded_projects.add(row.name)
            state["container_ids"] = sorted(owned_ids)

    last_health_check = [0.0]
    def health():
        if time.monotonic() - last_health_check[0] >= 0.5:
            _check_owned_health(owned_ids, allow_player_exit=coordinator.phase == "commit")
            last_health_check[0] = time.monotonic()
    if coordinator is not None:
        coordinator.health = health

    def started(row, desc):
        # Check immediately before this launcher, after potentially lengthy run capture
        # and preceding launchers. Never clean up a project reused by --force.
        if not args.dry_run:
            empty.update(_empty_projects(manifest, [(row, desc)]))
            if lifecycle.get("path") and row.name not in empty:
                raise RigError(f"{row.name}: project appeared after shared session startup preflight")
            if coordinator is not None:
                remember_owned()
                if row.name not in empty:
                    raise RigError(f"{row.name}: project appeared after coordinated startup preflight")
                coordinator.launched_instance(row.name)
            if not coordinated and not attempted and args.start_delay > 0 and time.time() >= gate:
                raise RigError("Startup inspection outlasted the release deadline; no services started; "
                               "retry with a larger --start-delay")
        attempted.append((row, desc))
        state["attempted"].append(row.name)
        if lifecycle.get("path"):
            from . import session_runtime
            lifecycle["state"]["attempted"] = list(state["attempted"])
            session_runtime.save(lifecycle["path"], lifecycle["state"])
    launch_started = time.time()
    state["launch_started_at_unix_s"] = launch_started
    if not args.dry_run:
        _save_execution(session_root, output_run, state)
        if not coordinated and args.start_delay > 0 and time.time() >= gate:
            state["status"] = "failed"
            state["error"] = "Final configuration capture outlasted the release deadline; no services started"
            _save_execution(session_root, output_run, state)
            raise RigError(state["error"] + "; retry with a larger --start-delay")
    phase_started = time.monotonic()
    if coordinated:
        eprint("rig replay: starting held instances; release follows verified readiness")
    else:
        eprint(f"rig replay: starting instances; legacy release at {gate:.3f} ({args.start_delay:g}s delay)")
    try:
        launch_options = {"timeout": lambda: min(coordinator.remaining(), coordinator.ready_timeout_s)} if coordinator is not None else {}
        outcomes = dispatch.run_verb(pairs, env, "up", dry_run=args.dry_run,
                                    stop_on_failure=True, on_start=started, **launch_options)
        remember_owned()
        failed = [o.sensor.name for o in outcomes if o.returncode]
        if failed:
            state["status"], state["error"] = "failed", "Startup failed for " + ", ".join(failed)
        else:
            timings["launch_s"] = time.monotonic() - phase_started
            if coordinator is not None:
                readiness_started = time.time()
                result = coordinator.release(args.start_delay)
                state.update(result)
                gate = result["start_at_unix_s"]
                timings["readiness_s"] = result["ready_at_unix_s"] - readiness_started
                timings["settling_s"] = args.start_delay
                timings["release_s"] = time.time() - result["ready_at_unix_s"] - args.start_delay
            state["status"] = "started"
    except (RigError, OSError, subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
        state["status"] = "cancelled" if isinstance(exc, KeyboardInterrupt) else "failed"
        state["error"] = str(exc) or "Startup interrupted"
    timings["startup_s"] = time.monotonic() - phase_started
    if not args.dry_run:
        state["release_lateness_s"] = (max(
            (s["actual_release_at_unix_s"] - gate for s in state.get("participants", {}).values()), default=0)
            if coordinated and gate is not None else max(0.0, time.time() - gate) if gate is not None else None)
        if state["status"] != "started":
            eprint("rig replay: " + state["error"])
            _save_execution(session_root, output_run, state, required=False)
            if coordinator is not None:
                try:
                    coordinator.abort()
                except OSError as exc:
                    eprint(f"rig replay: could not publish abort; removing owned containers: {exc}")
                try:
                    remember_owned()
                except RigError as exc:
                    eprint(f"rig replay: cannot refresh owned container IDs: {exc}")
                state["participants"] = coordinator.statuses
                state["cleanup"] = _abort_owned(owned_ids)
            else:
                state["cleanup"] = _abort_start(execution_manifest, attempted, empty, env)
            _save_execution(session_root, output_run, state, required=False)
            return 130 if state["status"] == "cancelled" else 1
        _save_execution(session_root, output_run, state, required=False)
        if not coordinated and args.start_delay > 0 and state["release_lateness_s"] > 0:
            eprint(f"rig replay: warning: startup outlasted the release gate by "
                   f"{state['release_lateness_s']:.1f}s; increase --start-delay to avoid losing early inputs")
        eprint("rig replay: startup complete; " + ", ".join(f"{k.removesuffix('_s')} {v:.1f}s"
                                                         for k, v in timings.items()))
    if args.auto_end is not None and not args.dry_run:
        player = next(r for r in selected if r.name == plan["player"])
        def observe_participants(completion):
            try:
                statuses = coordinator.read_statuses()
                missing = set(coordinator.participants) - set(statuses)
                if missing:
                    return replay.ReplayCompletion("unknown", "Missing replay statuses: " + ", ".join(sorted(missing)), completion.containers)
                completed_instances = {p.rsplit(".", 1)[0] for p, status in statuses.items()
                                       if p.endswith(".camera") and status["state"] == "completed"}
                _check_owned_health(owned_ids, allow_player_exit=True,
                    completed_ids={cid for cid, name in container_owners.items() if name in completed_instances})
                state["participants"] = statuses
                pending = [p for p, status in statuses.items() if status["state"] != "completed"]
                if pending and completion.state == "succeeded":
                    return replay.ReplayCompletion("active", "Waiting for " + ", ".join(pending), completion.containers)
            except (RigError, OSError) as exc:
                return replay.ReplayCompletion("failed", str(exc), completion.containers)
            return None

        monitor_options = {"owned_containers": sorted(owned_ids), "observe_participants": observe_participants} if coordinator is not None else {}
        result = replay.auto_end(execution_manifest, descriptors, root, pairs, env, player, args.auto_end,
                               required_calls=bool(plan["playback"]["calls"]),
                               started_after=launch_started, run_id=run_id, **monitor_options)
        state["status"] = "completed" if result == 0 else "interrupted" if result == 130 else "failed"
        if coordinator is not None:
            try:
                state["participants"] = coordinator.read_statuses()
            except RigError as exc:
                state["terminal_evidence_error"] = str(exc)
            _save_execution(session_root, output_run, state, required=False)
        if result == 0 and lifecycle.get("path"):
            from . import session_runtime
            # Do not retire the frozen teardown state until the launched projects
            # are absent; an auto-end success must not strand running services.
            remaining = [row.name for row, _ in pairs if _project_container_ids(manifest, row, env)]
            if remaining:
                state.update(status="failed", error="Auto-end left replay projects: " + ", ".join(remaining))
                raise RigError(state["error"])
            lifecycle["state"].update(status="completed", attempted=list(state["attempted"]))
            session_runtime.save(lifecycle["path"], lifecycle["state"])
            session_runtime.clear(root, lifecycle["state"]["session"])
            lifecycle["cleared"] = True
        return result
    return 0 if state["status"] == "started" else 1
