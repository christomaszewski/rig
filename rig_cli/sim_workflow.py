"""Attach selected deployment services to an existing simulator ROS graph.

Isaac owns the scene and ROS bridge. Rig owns the workload, clock choice, run,
and only the Compose projects it launches. Check and dry-run never write state.
"""
from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

from . import RigError, dispatch, resolve, runs, runtime_images, session_recipe
from .common import eprint, load_yaml
from .descriptor import find_descriptor, load_descriptor, resolve_ros_namespace
from .manifest import PLAYER_SERVICE, load_manifest, require_identity
from .replay_recipe import digest
from .replay_prepare import write_yaml
from . import session_runtime
from .session_runtime import (active_session, read_state as _read_state, json_write as _json_write,
                              save as _save, verify_files as _verify_files, engine_id as _engine_id,
                              DOCKER_ENV as _DOCKER_ENV, observe)


def _issue(plan, status, code, message):
    plan["issues"].append(dict(status=status, code=code, message=message))


def _finish(plan):
    plan["ready_to_run"] = not any(i["status"] in ("error", "not_checked") for i in plan["issues"])
    return plan


def _surfaces(descriptors):
    result = {}
    for service, desc in descriptors.items():
        descriptor = find_descriptor(desc.repo)
        raw = load_yaml(descriptor) if descriptor else {}
        files = {desc.launcher_path}
        if descriptor:
            files.add(descriptor)
        for pattern in raw.get("launch_surface", []):
            files.update(p for p in desc.repo.glob(pattern) if p.is_file())
        result[service] = {str(p): digest(p.read_bytes().hex()) for p in files if p.is_file()}
    return result


def probe(simulation, env):
    """Run a bundled, dependency-free entry point in the operator's ROS Python."""
    request = {key: simulation[key] for key in ("ros", "publishes", "subscribes") if key in simulation}
    request["timeout_s"] = simulation.get("timeout_s", 10.0)
    source = Path(__file__).with_name("sim_probe.py").read_text()
    argv = simulation.get("probe", [sys.executable]) + ["-c", source]
    try:
        result = subprocess.run(argv, input=json.dumps(request), capture_output=True, text=True,
                                env=env, timeout=request["timeout_s"] + 10)
        doc = json.loads(result.stdout)
        if (not isinstance(doc, dict) or type(doc.get("version")) is not int or doc["version"] != 1
                or type(doc.get("ready")) is not bool or not isinstance(doc.get("issues"), list)):
            raise ValueError("expected version-1 probe response")
        for issue in doc["issues"]:
            if (not isinstance(issue, dict) or issue.get("status") not in ("ok", "warn", "warning", "error")
                    or not isinstance(issue.get("code"), str) or not isinstance(issue.get("message"), str)):
                raise ValueError("invalid issue in probe response")
        if doc["ready"] and any(i["status"] == "error" for i in doc["issues"]):
            raise ValueError("probe claims readiness with unresolved errors")
        if result.returncode and doc["ready"]:
            raise ValueError("probe exited unsuccessfully despite claiming readiness")
        return doc
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise RigError(f"Isaac ROS probe failed: {exc}. Set simulation.probe to Python in a "
                       "sourced ROS environment (rclpy and rosgraph_msgs required)") from exc


def _apply_probe(plan, observation):
    plan["probe"] = observation
    for issue in observation["issues"]:
        if not isinstance(issue, dict) or issue.get("status") not in ("ok", "warn", "warning", "error"):
            raise RigError("Invalid issue in Isaac probe response")
        _issue(plan, issue["status"], "probe_" + str(issue.get("code", "graph")), str(issue.get("message", "")))
    if not observation["ready"] and not any(i["status"] == "error" for i in observation["issues"]):
        _issue(plan, "error", "probe", "Isaac connection was not verified")


def _image_report(plan, inspection):
    plan["issues"] = [i for i in plan["issues"] if i["code"] != "images"]
    plan["image_inspection"] = inspection
    plan["image_pins"] = inspection["pins"]
    if inspection["errors"]:
        for message in inspection["errors"]:
            _issue(plan, "error", "images", message)
    elif not inspection["ready"]:
        _issue(plan, "not_checked", "images", "Native runtime images must be acquired: " +
               ", ".join(inspection["proposed_pulls"]))
    else:
        _issue(plan, "ok", "images", "Selected runtime images verified and pinned")


def _quiet(manifest, rows, env=None):
    from .replay_workflow import _project_container_ids
    live = runs.running_projects(manifest, env=env)
    if live:
        raise RigError("Deployment stacks are running: " + ", ".join(live) + "; run rig down first")
    for row in rows:
        if _project_container_ids(manifest, row, env=env):
            raise RigError(f"{row.name}: Compose project already contains containers; run rig down first")


def _verify_running(pairs, env):
    """Check the processes after discovery; a successful launcher can exit before its containers."""
    from .status import _parse_ps, _rollup
    for row, desc in pairs:
        result = dispatch.run(row, desc, env, dispatch.launcher_cmd(row, desc, "status", ["--format", "json"]),
                              dry_run=False, capture=True, timeout=15)
        try:
            state, health, _, _ = _rollup(_parse_ps(result.stdout))
        except (ValueError, TypeError, AttributeError) as exc:
            raise RigError(f"{row.name}: invalid startup status") from exc
        if result.returncode or state != "running" or health == "unhealthy":
            raise RigError(f"{row.name}: startup status is {state}/{health}")


def build(root, path, recipe, *, pull="missing", host=True):
    """Resolve configs and inspect the host without materializing deployment files."""
    from .catalog import load_catalog
    from . import doctor
    original = load_manifest(root, validate_configs=False)
    require_identity(original, what="simulation needs a resolved vehicle identity")
    manifest = session_recipe.apply_session(original, recipe)
    sim = session_recipe.provider_recipe(recipe, "simulation")
    plan = dict(version=1, provider="isaac", mode="attach", recipe=recipe, selected=[], omitted=[],
                configs={}, issues=[], clock="isaac", pull_policy=pull)
    rows = manifest.select([], enabled_only=True)
    plan["selected"] = [r.name for r in rows]
    plan["omitted"] = [r.name for r in manifest.sensors if r.name not in plan["selected"]]
    known = {r.name: r for r in manifest.sensors}
    if recipe["session"].get("clock", "source") != "source":
        _issue(plan, "error", "clock", "Isaac attach requires session.clock: source")
    expected_ros = dict(distro=manifest.ros.distro, rmw=manifest.ros.rmw, domain_id=manifest.ros.domain_id)
    if sim["ros"] != expected_ros:
        _issue(plan, "error", "ros", f"Simulation ROS settings {sim['ros']} do not match deployment {expected_ros}; "
               "use a compatible deployment, including rebuilt images when changing ROS distro/RMW")
    if sim["ros"]["distro"] not in ("humble", "jazzy"):
        _issue(plan, "warning", "experimental_ros", "Isaac documents Humble/Jazzy as tested; verify your "
               "custom bridge build for this ROS distribution")
    for name in sim.get("replace", []):
        if name not in known or known[name].tier != "sensor":
            _issue(plan, "error", "replacement", f"{name}: replacement must name a deployment sensor")
        if not any(p.get("instance") == name for p in sim.get("publishes", [])):
            _issue(plan, "error", "replacement", f"{name}: declare its simulated output in simulation.publishes")
    catalog = load_catalog(root)
    descriptors = {}
    for row in rows:
        if row.tier == "sensor" or row.service == PLAYER_SERVICE:
            _issue(plan, "error", "selection", f"{row.name}: Isaac attach starts infrastructure and autonomy only; "
                   "physical sensors and bag players must be omitted")
        try:
            if row.service not in catalog:
                raise RigError(f"{row.service}: not in services.yaml")
            descriptors[row.service] = load_descriptor(row.service, catalog[row.service].path)
            plan["configs"][row.name] = resolve._layered_dict(row, root, manifest.vars)
            desc = descriptors[row.service]
            if row.tier == "autonomy" and not desc.replay_sim_time:
                _issue(plan, "error", "sim_time", f"{row.name}: launcher must declare replay.sim_time: true "
                       "and honor RIG_SIM_TIME")
            elif not desc.replay_sim_time:
                _issue(plan, "warning", "sim_time", f"{row.name}: no simulation-time capability declared; "
                       "verify timestamp-sensitive infrastructure uses the simulator clock")
            published = {p["topic"]: p["type"] for p in sim.get("publishes", [])}
            for edge in (desc.interface or {}).get("publishes", ()):
                topic = edge.name if edge.name.startswith("/") else resolve_ros_namespace(desc, row.name) + "/" + edge.name
                if topic == "/clock" or topic in published:
                    _issue(plan, "error", "authority", f"{row.name} and Isaac both publish {topic}; "
                           "give each output one authority (shared TF requires explicit scene/config separation)")
            if not desc.interface:
                _issue(plan, "warning", "interface", f"{row.name}: no interface declaration; output "
                       "ownership cannot be fully checked")
        except (RigError, OSError) as exc:
            _issue(plan, "error", "config", str(exc))
    selected_manifest = dataclasses.replace(manifest, sensors=rows)
    for issue in doctor.collect(selected_manifest, catalog, descriptors, configs=plan["configs"]):
        if issue.level == doctor.ERROR:
            _issue(plan, "error", "deployment", issue.message)
    if not manifest.data_dir:
        _issue(plan, "error", "recording", "Set data_dir before simulation so the session has a normal rig run")
    if not any(r.service == "ros2-bag-logger" for r in rows):
        _issue(plan, "warning", "recording", "Select a configured ros2-bag-logger to capture replayable ROS inputs; "
               "run metadata alone is not a recording")
    plan["launch_surfaces"] = _surfaces(descriptors)
    controls = {Path(root) / n for n in ("vehicle.yaml", "vehicle.local.yaml", "services.yaml", "rig.lock", "metadata.yaml")}
    controls.update(r.config for r in rows)
    controls.add(path)
    for row in rows:
        if row.overlays:
            controls.add(Path(root) / "config/.pins" / f"{row.name}.yaml")
            controls.update(resolve.overlay_payload_path(root, ref) for ref in row.overlays)
    plan["control_files"] = {str(p): digest(p.read_bytes().hex()) if p.is_file() else None for p in controls}
    if host and not any(i["status"] == "error" for i in plan["issues"]):
        try:
            from .artifact_runtime import check_target
            from .image_catalog import docker_arch
            from .targets import deployment_target
            check_target(root, manifest)
            architecture = docker_arch()
            if manifest.arch and manifest.arch != architecture:
                raise RigError(f"Deployment requires {manifest.arch}; Docker engine is {architecture}")
            deployment_target(manifest, descriptors, arch=architecture)
            plan["architecture"] = architecture
            env = dispatch.with_clock(dispatch.fleet_env(manifest, descriptors), provider="isaac")
            env["RIG_TARGET_ARCH"] = architecture
            if any(env.get(k) != os.environ.get(k) for k in _DOCKER_ENV):
                raise RigError("Configure the Docker connection in the shell, not vehicle env overrides, "
                               "so image inspection and service launch use the same engine")
            plan["engine_id"] = _engine_id(env)
            _quiet(original, rows, env)
            if active_session(root):
                raise RigError("A saved session exists; run rig down --end-run first")
            metadata = load_yaml(Path(root) / "metadata.yaml") if (Path(root) / "metadata.yaml").exists() else {}
            plan["image_inventory"] = runtime_images.inventory(manifest, descriptors, rows, plan["configs"],
                architecture=architecture, env=env, bundle=metadata.get("bundle"))
            _image_report(plan, runtime_images.inspect_images(plan["image_inventory"], policy=pull))
            _apply_probe(plan, probe(sim, env))
        except (RigError, OSError, subprocess.TimeoutExpired) as exc:
            _issue(plan, "error", "host", str(exc))
    elif not host:
        _issue(plan, "not_checked", "host", "Docker images and the ROS graph were not inspected")
    plan["fingerprint"] = digest(dict(recipe=recipe, configs=plan["configs"], controls=plan["control_files"],
                                     surfaces=plan["launch_surfaces"], architecture=plan.get("architecture")))
    return manifest, descriptors, _finish(plan)


def report(plan, *, as_json=False):
    if as_json:
        print(json.dumps(plan, indent=2, default=str))
        return
    eprint("rig sim: attach Isaac; selected " + ", ".join(plan.get("selected", [])))
    for issue in plan["issues"]:
        eprint(f"  [{issue['status']}] {issue['message']}")
    eprint(f"  ready_to_run: {str(plan['ready_to_run']).lower()}")


def execute(root, path, manifest, descriptors, plan, *, label=None, capture_output=False):
    from .replay_coordinator import project_locks
    from .replay_workflow import _project_container_ids
    sim = plan["recipe"]["simulation"]
    rows = manifest.select(plan["selected"], enabled_only=False)
    env = dispatch.with_clock(dispatch.fleet_env(manifest, descriptors), provider="isaac")
    env["RIG_TARGET_ARCH"] = plan["architecture"]
    env["RIG_RUNTIME_IMAGE_MAP"] = json.dumps(plan["image_pins"])
    with session_runtime.session_lock(root), project_locks(manifest, [r.name for r in manifest.sensors], env):
        if _engine_id(env) != plan["engine_id"]:
            raise RigError("Docker engine changed since simulation planning")
        _quiet(manifest, rows, env)
        if active_session(root):
            raise RigError("A saved session exists; run rig down --end-run first")
        _verify_files(plan["control_files"], "Deployment")
        for files in plan["launch_surfaces"].values():
            _verify_files(files, "Launcher")
        # Image acquisition/capture may be slow; a cached successful probe is not a launch gate.
        fresh = probe(sim, env)
        if not fresh["ready"]:
            raise RigError("Isaac connection changed before launch: " + "; ".join(
                i["message"] for i in fresh["issues"] if i["status"] == "error"))
        token = uuid.uuid4().hex
        folder = Path(root) / "var/sim/sessions" / token
        pairs = []
        for row in rows:
            config_path = folder / "config" / (row.name + ".yaml")
            write_yaml(config_path, plan["configs"][row.name])
            pairs.append((dataclasses.replace(row, config=config_path, enabled=True), descriptors[row.service]))
        selected = {r.name: r for r, _ in pairs}
        execution_manifest = dataclasses.replace(manifest, sensors=[selected.get(r.name,
            dataclasses.replace(r, enabled=False)) for r in manifest.sensors])
        run_id = runs.new_run(execution_manifest, root, label or "sim-" + path.stem.replace(".", "-"),
            simulation=dict(provider="isaac", mode="attach", scene=sim["scene"],
                            recipe=plan["recipe"], fingerprint=plan["fingerprint"], clock="isaac"))
        output_run = Path(manifest.data_dir) / "runs" / run_id
        evidence = output_run / ".rig/simulation"
        write_yaml(evidence / "recipe.yaml", plan["recipe"])
        write_yaml(evidence / "resolved.yaml", plan)
        _json_write(evidence / "probe.json", fresh)
        state_path, state = session_runtime.register(root, execution_manifest, pairs, env,
            provider="isaac", engine_id=plan["engine_id"], launch_surfaces=plan["launch_surfaces"],
            run_id=run_id, output_run=output_run, session_id=token)
        runs.snapshot(execution_manifest, root, stacks=plan["selected"])
        attempted = []
        def started(row, desc):
            if _project_container_ids(manifest, row, env=env):
                raise RigError(f"{row.name}: Compose project appeared during startup")
            attempted.append((row, desc))
            state["attempted"].append(row.name)
            _save(state_path, state)
        try:
            _verify_files(plan["control_files"], "Deployment")
            for files in plan["launch_surfaces"].values():
                _verify_files(files, "Launcher")
            gate = probe(sim, env)
            _json_write(evidence / "probe-launch.json", gate)
            if not gate["ready"]:
                raise RigError("Isaac connection changed during run capture; no services started")
            if _engine_id(env) != plan["engine_id"]:
                raise RigError("Docker engine changed during run capture; no services started")
            outcomes = dispatch.run_verb(pairs, env, "up", stop_on_failure=True, on_start=started,
                                         capture=capture_output)
            failed = [o.sensor.name for o in outcomes if o.returncode]
            if failed:
                raise RigError("Startup failed for " + ", ".join(failed))
            after = probe(sim, env)
            _json_write(evidence / "probe-after.json", after)
            if not after["ready"]:
                raise RigError("Isaac graph failed verification after startup: " + "; ".join(
                    i["message"] for i in after["issues"] if i["status"] == "error"))
            _verify_running(pairs, env)
            state["status"] = "running"
            _save(state_path, state)
            eprint(f"rig sim: run {run_id}; stop with rig sim down --end-run")
            return 0
        except (Exception, KeyboardInterrupt) as exc:
            state["status"], state["error"] = "failed", str(exc) or "interrupted"
            def save_failure():
                try:
                    _save(state_path, state)
                except OSError as write_error:
                    eprint(f"rig sim: could not save failure evidence: {write_error}")
            save_failure()
            state["cleanup_failed"] = []
            # A failed launcher may already have created containers. Attempt every owned
            # instance even if a down launcher raises or the evidence disk is unwritable.
            for row, desc in reversed(attempted):
                try:
                    cleanup = dispatch.run_verb([(row, desc)], env, "down", capture=capture_output)
                    state["cleanup_failed"].extend(o.sensor.name for o in cleanup if o.returncode)
                except (Exception, KeyboardInterrupt) as cleanup_error:
                    state["cleanup_failed"].append(row.name)
                    state.setdefault("cleanup_errors", {})[row.name] = str(cleanup_error)
            save_failure()
            eprint("rig sim: startup failed; saved evidence and run retained. Use rig sim down --end-run to finish.")
            raise


def down(root, *, dry_run=False, end_run=False):
    return session_runtime.down(root, dry_run=dry_run, end_run=end_run, provider="isaac")


def command(args, root):
    if args.cmd == "sim-down":
        return down(root, dry_run=args.dry_run, end_run=args.end_run)
    as_json = getattr(args, "json", False) or getattr(args, "format", "text") == "json"
    try:
        path, recipe = session_recipe.load(root, args.recipe, "simulation")
        manifest, descriptors, plan = build(root, path, recipe, pull=args.pull)
        execute_run = args.cmd == "sim-up" and not args.dry_run
        if execute_run and plan.get("image_inventory") and not any(
                i["status"] in ("error", "not_checked") and i["code"] != "images" for i in plan["issues"]):
            inspection = runtime_images.acquire_images(plan["image_inventory"], policy=args.pull,
                                                       inspection=plan["image_inspection"])
            _image_report(plan, inspection)
            _finish(plan)
        if not (execute_run and as_json and plan["ready_to_run"]):
            report(plan, as_json=as_json)
        if not plan["ready_to_run"]:
            return 1
        if execute_run:
            result = execute(root, path, manifest, descriptors, plan, label=args.label, capture_output=as_json)
            if as_json:
                saved = _read_state(root)
                plan["execution"] = {k: saved[1][k] for k in ("run_id", "status")} if saved else {"status": "finished"}
                report(plan, as_json=True)
            return result
        return 0
    except (RigError, OSError, subprocess.TimeoutExpired) as exc:
        if as_json:
            report(dict(version=1, ready_to_run=False, issues=[dict(status="error", code="input", message=str(exc))]),
                   as_json=True)
            return 1
        raise RigError(str(exc)) from exc
