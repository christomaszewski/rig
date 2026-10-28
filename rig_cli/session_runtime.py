"""Frozen lifecycle for shared-recipe sessions, independent of their input provider.

The working deployment may change after startup. Status, logs and teardown use the
exact configuration, environment, Docker engine and project identities captured here.
An external simulator is never a lifecycle-owned resource.
"""
from __future__ import annotations

import json
import contextlib
import os
from pathlib import Path
import re
import subprocess
import uuid

from . import RigError, dispatch, runs
from .common import eprint
from .descriptor import load_descriptor
from .manifest import Manifest, RIG_OWNED_ENV, RosSettings, Sensor
from .replay_recipe import digest

DOCKER_ENV = ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY")


@contextlib.contextmanager
def session_lock(root):
    """Serialize deployment run/pointer transitions even for disjoint selections."""
    import fcntl
    path = Path(root) / "var/sessions/.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RigError("Another session transition is in progress for this deployment") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def pointer(root):
    return Path(root) / "var/sessions/current.json"


def active_session(root):
    return pointer(root).is_file()


def read_state(root):
    if not active_session(root):
        return None
    try:
        token = json.loads(pointer(root).read_text())["session"]
        if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32}", token):
            raise ValueError("invalid session identity")
        path = Path(root) / "var/sessions" / token / "state.json"
        state = json.loads(path.read_text())
        if (not isinstance(state, dict) or state.get("session") != token or type(state.get("version")) is not int or state.get("version") != 1 or
                state.get("provider") not in ("isaac", "replay")):
            raise ValueError("invalid session state")
        _validate_state(state)
        return path, state
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RigError(f"Cannot read saved session: {exc}") from exc


def _validate_state(state):
    """Fail closed with an actionable error before interpreting damaged ownership data."""
    for key in ("environment", "docker_environment", "ros", "repos", "launch_surfaces", "configs"):
        if not isinstance(state.get(key), dict):
            raise ValueError(f"invalid session {key}")
    for key in ("selected", "attempted", "rows"):
        if not isinstance(state.get(key), list):
            raise ValueError(f"invalid session {key}")
    selected, attempted = state["selected"], state["attempted"]
    if (not selected or any(not isinstance(n, str) for n in selected + attempted)
            or len(set(selected)) != len(selected) or not set(attempted) <= set(selected)):
        raise ValueError("invalid session ownership")
    rows = state["rows"]
    if any(not isinstance(r, dict) or not {"name", "service", "config", "tier", "order"} <= r.keys() for r in rows):
        raise ValueError("invalid session rows")
    if [r["name"] for r in rows] != selected or any(
            r["service"] not in state["repos"] or r["config"] not in state["configs"] for r in rows):
        raise ValueError("inconsistent session rows")
    if not {"vehicle", "vehicle_id", "data_dir", "run_id"} <= state.keys():
        raise ValueError("missing session identity")
    if not isinstance(state.get("engine_id"), str) or not state["engine_id"]:
        raise ValueError("missing session Docker identity")
    if (set(state["ros"]) != {"domain_id", "rmw", "distro"}
            or type(state["ros"]["domain_id"]) is not int
            or not isinstance(state["ros"]["rmw"], str)
            or (state["ros"]["distro"] is not None and not isinstance(state["ros"]["distro"], str))):
        raise ValueError("invalid session ROS settings")
    maps = [state["configs"], state["repos"], *state["launch_surfaces"].values()]
    if any(not isinstance(m, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                         for k, v in m.items()) for m in maps):
        raise ValueError("invalid session file inventory")
    if (set(state["docker_environment"]) != set(DOCKER_ENV)
            or any(v is not None and not isinstance(v, str) for v in state["docker_environment"].values())
            or any(not isinstance(v, str) for v in state["environment"].values())):
        raise ValueError("invalid session environment")


def json_write(path, doc):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=2, default=str) + "\n")
    tmp.replace(path)


def save(path, state):
    json_write(path, state)
    if state.get("output_run"):
        folder = "simulation" if state["provider"] == "isaac" else "replay"
        json_write(Path(state["output_run"]) / ".rig" / folder / "lifecycle.json", state)


def verify_files(files, what):
    for path, expected in files.items():
        p = Path(path)
        actual = digest(p.read_bytes().hex()) if p.is_file() else None
        if actual != expected:
            raise RigError(f"{what} changed: {path}; restore this session's saved inputs before continuing")


def engine_id(env=None):
    try:
        result = subprocess.run(["docker", "info", "--format", "{{.ID}}"], env=env,
                                capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RigError(f"Cannot verify Docker engine identity: {exc}; no session services changed") from exc
    identity = result.stdout.strip()
    if result.returncode or not re.fullmatch(r"[A-Za-z0-9:_-]+", identity):
        raise RigError("Cannot verify Docker engine identity; no session services changed")
    return identity


def register(root, manifest, pairs, env, *, provider, engine_id, launch_surfaces,
             run_id=None, output_run=None, session_id=None):
    """Publish ownership before the first launcher; callers hold the project locks."""
    if provider not in ("isaac", "replay"):
        raise RigError(f"Unsupported session provider: {provider}")
    if active_session(root):
        raise RigError("A saved session exists; run rig down --end-run before starting another")
    token = session_id or uuid.uuid4().hex
    if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32}", token):
        raise RigError("Invalid saved session identifier")
    fleet_keys = ({k for k in env if k.startswith("RIG_")} |
                  {"ROS_DOMAIN_ID", "RMW_IMPLEMENTATION", "VEHICLE_ID"}) & RIG_OWNED_ENV
    fleet_keys |= set(manifest.extra_env)
    state = dict(version=1, session=token, provider=provider, status="starting", run_id=run_id,
                 output_run=str(output_run) if output_run else None,
                 data_dir=manifest.data_dir, vehicle_id=manifest.vehicle_id, vehicle=manifest.vehicle,
                 ros={"domain_id": manifest.ros.domain_id, "rmw": manifest.ros.rmw, "distro": manifest.ros.distro},
                 engine_id=engine_id, docker_environment={k: env.get(k) for k in DOCKER_ENV},
                 selected=[r.name for r, _ in pairs], attempted=[],
                 environment={k: env[k] for k in fleet_keys if k in env},
                 rows=[dict(name=r.name, service=r.service, config=str(r.config), tier=r.tier, order=r.order)
                       for r, _ in pairs],
                 repos={d.service: str(d.repo) for _, d in pairs}, launch_surfaces=launch_surfaces,
                 configs={str(r.config): digest(r.config.read_bytes().hex()) for r, _ in pairs})
    path = Path(root) / "var/sessions" / token / "state.json"
    if path.exists():
        raise RigError(f"Session identity already exists: {token}")
    save(path, state)
    try:
        with pointer(root).open("x") as handle:
            json.dump({"session": token}, handle)
            handle.write("\n")
    except FileExistsError as exc:
        raise RigError("Another session became active; no services started") from exc
    return path, state


def clear(root, session_id):
    """Remove only the expected pointer; keep immutable session evidence on disk."""
    current = read_state(root)
    if not current:
        return
    if current[1]["session"] != session_id:
        raise RigError("Session changed; refusing to remove another session's pointer")
    pointer(root).unlink()


def saved_context(state):
    descriptors = {s: load_descriptor(s, Path(repo)) for s, repo in state["repos"].items()}
    for files in state["launch_surfaces"].values():
        verify_files(files, "Saved launcher")
    verify_files(state["configs"], "Saved config")
    rows = [Sensor(**{**row, "config": Path(row["config"]), "enabled": True}) for row in state["rows"]]
    env = dict(os.environ)
    for key in RIG_OWNED_ENV:
        env.pop(key, None)
    env.update(state["environment"])
    for key, value in state["docker_environment"].items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    manifest = Manifest(vehicle=state["vehicle"], ros=RosSettings(**state["ros"]),
                        vehicle_id=state["vehicle_id"], data_dir=state["data_dir"], sensors=rows)
    return manifest, descriptors, env


def observe(args, root):
    from . import status
    saved = read_state(root)
    if saved is None:
        raise RigError("Session ended; retry the command")
    _, state = saved
    manifest, descriptors, env = saved_context(state)
    if engine_id(env) != state["engine_id"]:
        raise RigError("Docker engine differs from the saved session")
    pairs = [(r, descriptors[r.service]) for r in manifest.select(args.names, enabled_only=True)]
    if args.cmd == "status":
        rows = status.gather(pairs, env)
        run_line = runs.status_line(manifest)
        if getattr(args, "format", "table") == "json":
            print(status.as_json(manifest, rows, run_line))
        else:
            if run_line:
                print(run_line)
            print(status.render(rows, verbose=args.verbose))
        return 0
    extra = []
    if args.follow:
        if len(pairs) != 1:
            raise RigError("`logs -f` follows a single sensor; name exactly one")
        extra.append("-f")
    if args.tail is not None:
        extra.extend(["--tail", str(args.tail)])
    return int(any(o.returncode for o in dispatch.run_verb(pairs, env, "logs", extra=extra)))


def down(root, *, dry_run=False, end_run=False, provider=None):
    saved = read_state(root)
    if saved is None:
        eprint("rig: no saved session")
        return 0
    path, state = saved
    if provider and provider != state["provider"]:
        raise RigError(f"Active session uses {state['provider']}; use rig down --end-run")
    manifest, descriptors, env = saved_context(state)
    rows = manifest.sensors
    attempted = set(state["attempted"])
    pairs = [(r, descriptors[r.service]) for r in reversed(rows) if r.name in attempted]
    from .replay_coordinator import project_locks
    from .replay_workflow import _project_container_ids
    def perform():
        current = read_state(root)
        if not current or current[1]["session"] != state["session"]:
            raise RigError("Session changed before teardown; no services stopped")
        if engine_id(env) != state["engine_id"]:
            raise RigError("Docker engine differs from the saved session; restore its Docker context before teardown")
        if end_run and not dry_run and state.get("data_dir"):
            current_run = runs.current_run(Path(state["data_dir"]))
            if current_run and current_run[0] != state["run_id"]:
                raise RigError("Another run is current; refusing to capture or seal it as this session's run")
            if current_run:
                runs.capture_docker_logs(manifest, env=env)
        failed = []
        for row, desc in pairs:
            try:
                outcomes = dispatch.run_verb([(row, desc)], env, "down", dry_run=dry_run)
                failed.extend(o.sensor.name for o in outcomes if o.returncode)
            except (OSError, RigError, subprocess.TimeoutExpired) as exc:
                eprint(f"rig: teardown failed for {row.name}: {exc}")
                failed.append(row.name)
        if dry_run:
            return int(bool(failed))
        # A successful launcher exit is not evidence that every container is gone.
        for row in rows:
            try:
                if _project_container_ids(manifest, row, env=env):
                    failed.append(row.name)
            except RigError as exc:
                eprint(f"rig: cannot verify teardown for {row.name}: {exc}")
                failed.append(row.name)
        state["status"] = "stop_failed" if failed else "stopped"
        state["cleanup_failed"] = sorted(set(failed))
        save(path, state)
        if failed:
            return 1
        if end_run and state.get("data_dir"):
            current_run = runs.current_run(Path(state["data_dir"]))
            if current_run and current_run[0] != state["run_id"]:
                raise RigError("Another run is current; refusing to seal it as this session's run")
            if current_run:
                if runs.running_projects(manifest, env=env):
                    raise RigError("Session services are still running; run remains open")
                runs.end_run(manifest, root, force=True)
        clear(root, state["session"])
        eprint("rig: selected session services stopped" +
               ("; external Isaac remains running" if state["provider"] == "isaac" else ""))
        return 0
    if dry_run:
        return perform()
    with session_lock(root), project_locks(manifest, [r.name for r in rows], env):
        return perform()
