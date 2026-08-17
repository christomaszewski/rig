"""``rig fleet`` — GCS-side fan-out over the per-vehicle surface (ROADMAP §4). NEVER a control
plane: every verb is the ssh loop an operator writes by hand, plus aggregation, correlated run
labels, and sealed-run harvest.

Invariants (settled):
- Transport is the SYSTEM's ssh/scp — BatchMode (nothing ever prompts), no stored credentials,
  no agent, no daemon. A local row (host absent/localhost) skips ssh entirely — N local rows
  with distinct paths ARE a SIL fleet on one machine.
- The remote end is the deployment's OWN entry points (./run.sh > ./rig > rig on PATH) at the
  roster row's path. rig-on-the-GCS never reaches around them.
- Fail SOFT per vehicle: an unreachable row mars its line, never aborts the sweep (DDIL).
  ssh exit 255 = transport failure (UNREACHABLE); anything else is the remote command's rc.
- Explicit roster only (fleet.yaml); no discovery. No config side-channel: values ride
  ``RIG_VAR_*`` env forwarding and land in each vehicle's run snapshot. The one deliberate
  provenance carve-out: ``fleet up`` pushes fleet.yaml itself to each deployment root so the
  run snapshot captures the roster the run was launched under.

SIL (mode: sil): per-vehicle registries under ``sil.data_root/<name>/`` — the "shared run dir"
is a VIEW, not a shared registry: after up, ``<data_root>/runs/<label>-<date>/<vehicle>`` is a
symlink into each vehicle's open run, one browsable folder with per-vehicle subfolders while
the registry machinery (current symlink, seal guard) stays untouched underneath. `fleet sync`
materializes the IDENTICAL layout from real vehicles, so analysis sees one tree either way.
The optional docker network mirrors the field IP scheme: rig only CREATES/REMOVES it and
exports ``RIG_NETWORK``/``RIG_VEHICLE_IP`` — joining it is the services' side of the contract
(external network + ipv4_address in their composes); rig never writes compose config.
"""
from __future__ import annotations

import concurrent.futures
import datetime
import json
import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import RigError
from .common import eprint, load_yaml, print_table

FLEET_KEYS = {"fleet", "mode", "gcs_ip", "sil", "vehicles"}
ROW_KEYS = {"id", "name", "host", "path", "data_dir", "ip"}
SIL_KEYS = {"data_root", "network"}
_LOCAL_HOSTS = {"", "localhost", "127.0.0.1"}
_SSH_OPTS = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=6")


@dataclass(frozen=True)
class Vehicle:
    id: object                    # int|str — forwarded as RIG_VEHICLE_ID for local rows
    name: str
    host: str                     # "" / localhost / 127.0.0.1 = local (no ssh)
    path: str                     # deployment dir on the row's host
    data_dir: str | None          # runs live here; sync needs it (or sil.data_root derives it)
    ip: str | None                # -> RIG_VEHICLE_IP when the SIL network is on

    @property
    def is_local(self) -> bool:
        return self.host in _LOCAL_HOSTS


@dataclass(frozen=True)
class Fleet:
    name: str
    mode: str                     # sil | field — EXPLICIT, never inferred
    gcs_ip: str | None
    data_root: str | None         # sil.data_root
    network: dict | None          # sil.network {name, subnet}
    vehicles: tuple[Vehicle, ...]
    path: Path                    # the fleet.yaml this was loaded from


def find_fleet(explicit: str | None) -> Path:
    """--fleet flag > $RIG_FLEET > upward walk from cwd. No CLI-adjacent fallback — a wrong
    roster is worse than no roster."""
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise RigError(f"fleet: {path} not found")
        return path
    env = os.environ.get("RIG_FLEET")
    if env:
        path = Path(env).expanduser()
        if not path.is_file():
            raise RigError(f"fleet: $RIG_FLEET={env} not found")
        return path
    cwd = Path.cwd()
    for d in (cwd, *cwd.parents):
        if (d / "fleet.yaml").is_file():
            return d / "fleet.yaml"
    raise RigError("fleet: no fleet.yaml found (cwd upward), $RIG_FLEET unset, no --fleet given "
                   "— the roster convention is CHEATSHEET §1.6")


def load_fleet(explicit: str | None) -> Fleet:
    path = find_fleet(explicit)
    data = load_yaml(path)
    unknown = set(data) - FLEET_KEYS
    if unknown:
        raise RigError(f"{path}: unknown key(s): {', '.join(sorted(unknown))} — fleet.yaml "
                       f"carries only: {', '.join(sorted(FLEET_KEYS))}")
    mode = str(data.get("mode") or "field")
    if mode not in ("sil", "field"):
        raise RigError(f"{path}: mode must be sil or field, not '{mode}'")
    sil = data.get("sil") or {}
    if sil and mode != "sil":
        raise RigError(f"{path}: a sil: block requires `mode: sil` — a field fleet must not "
                       f"carry SIL wiring (explicit beats inferred)")
    if set(sil) - SIL_KEYS:
        raise RigError(f"{path}: sil: carries only: {', '.join(sorted(SIL_KEYS))}")
    network = sil.get("network")
    if network is not None and not (isinstance(network, dict) and network.get("name")
                                    and network.get("subnet")):
        raise RigError(f"{path}: sil.network needs {{name, subnet}}")
    vehicles: list[Vehicle] = []
    for raw in data.get("vehicles") or []:
        raw = raw or {}
        if set(raw) - ROW_KEYS:
            raise RigError(f"{path}: vehicle row {raw.get('name', raw)!r}: unknown key(s) "
                           f"{', '.join(sorted(set(raw) - ROW_KEYS))} — rows carry: "
                           f"{', '.join(sorted(ROW_KEYS))}")
        if raw.get("id") is None or not raw.get("name") or not raw.get("path"):
            raise RigError(f"{path}: vehicle row {raw!r} needs id, name, path")
        vehicles.append(Vehicle(id=raw["id"], name=str(raw["name"]),
                                host=str(raw.get("host") or ""), path=str(raw["path"]),
                                data_dir=(str(raw["data_dir"]) if raw.get("data_dir") else None),
                                ip=(str(raw["ip"]) if raw.get("ip") else None)))
    if not vehicles:
        raise RigError(f"{path}: no vehicles — the roster is the whole point")
    names = [v.name for v in vehicles]
    if len(set(names)) != len(names):
        raise RigError(f"{path}: duplicate vehicle names")
    return Fleet(name=str(data.get("fleet") or path.parent.name), mode=mode,
                 gcs_ip=(str(data["gcs_ip"]) if data.get("gcs_ip") else None),
                 data_root=(str(sil["data_root"]) if sil.get("data_root") else None),
                 network=network, vehicles=tuple(vehicles), path=path)


def _select(fleet: Fleet, names: list[str]) -> list[Vehicle]:
    if not names:
        return list(fleet.vehicles)
    by_name = {v.name: v for v in fleet.vehicles}
    missing = [n for n in names if n not in by_name]
    if missing:
        raise RigError(f"fleet: unknown vehicle(s): {', '.join(missing)} "
                       f"(roster: {', '.join(by_name)})")
    return [by_name[n] for n in names]


def _pathexpr(path: str) -> str:
    """Shell-safe path that still honors a leading ~ on the REMOTE side (quoting would kill
    tilde expansion; $HOME substitution keeps it safe)."""
    if path == "~":
        return '"$HOME"'
    if path.startswith("~/"):
        return '"$HOME"/' + shlex.quote(path[2:])
    return shlex.quote(path)


def _row_data_dir(fleet: Fleet, vehicle: Vehicle) -> str | None:
    if vehicle.data_dir:
        return vehicle.data_dir
    if vehicle.is_local and fleet.data_root:
        return str(Path(fleet.data_root).expanduser() / vehicle.name)
    return None


def _chooser(vehicle: Vehicle, argv: list[str], env: dict[str, str]) -> str:
    """The deployment's own entry points, preference order run.sh > ./rig > rig-on-PATH."""
    path = str(Path(vehicle.path).expanduser()) if vehicle.is_local else vehicle.path
    quoted = " ".join(shlex.quote(a) for a in argv)
    prefix = "".join(f"{k}={shlex.quote(str(v))} " for k, v in sorted(env.items()))
    return (f"cd {_pathexpr(path)} && "
            f"if [ -x ./run.sh ]; then {prefix}./run.sh {quoted}; "
            f"elif [ -x ./rig ]; then {prefix}./rig {quoted}; "
            f"else {prefix}rig {quoted}; fi")


@dataclass
class Outcome:
    vehicle: Vehicle
    rc: int
    out: str
    err: str
    reachable: bool


def _run_script(vehicle: Vehicle, script: str, *, timeout: int = 180) -> Outcome:
    cmd = (["sh", "-c", script] if vehicle.is_local
           else ["ssh", *_SSH_OPTS, vehicle.host, script])
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise RigError("fleet: `ssh` not found on PATH — the transport is the system's ssh")
    except subprocess.TimeoutExpired:
        return Outcome(vehicle, 124, "", f"timed out after {timeout}s", reachable=False)
    reachable = vehicle.is_local or proc.returncode != 255  # 255 = ssh transport failure
    return Outcome(vehicle, proc.returncode, proc.stdout, proc.stderr, reachable)


def _fan_out(vehicles: list[Vehicle], make_script, *, jobs: int, timeout: int = 180) -> list[Outcome]:
    """Resolve everything first, then fan out; results return in ROSTER order (stable tables)."""
    scripts = {v.name: make_script(v) for v in vehicles}  # validate all rows before any I/O
    if jobs <= 1 or len(vehicles) == 1:
        return [_run_script(v, scripts[v.name], timeout=timeout) for v in vehicles]
    outcomes: dict[str, Outcome] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as ex:
        futures = {ex.submit(_run_script, v, scripts[v.name], timeout=timeout): v
                   for v in vehicles}
        for fut in concurrent.futures.as_completed(futures):
            out = fut.result()
            outcomes[out.vehicle.name] = out
    return [outcomes[v.name] for v in vehicles]


# --- verbs --------------------------------------------------------------------------------------

def cmd_list(fleet: Fleet, *, jobs: int) -> int:
    def probe(v: Vehicle) -> str:
        path = str(Path(v.path).expanduser()) if v.is_local else v.path
        return (f"test -e {_pathexpr(path)}/vehicle.yaml -o -x {_pathexpr(path)}/run.sh")

    outcomes = _fan_out(list(fleet.vehicles), probe, jobs=jobs, timeout=30)
    rows = [("ID", "NAME", "HOST", "PATH", "STATE")]
    problems = 0
    for o in outcomes:
        if not o.reachable:
            state, problems = "UNREACHABLE", problems + 1
        elif o.rc != 0:
            state, problems = "no deployment at path", problems + 1
        else:
            state = "ok"
        rows.append((str(o.vehicle.id), o.vehicle.name, o.vehicle.host or "localhost",
                     o.vehicle.path, state))
    print(f"fleet: {fleet.name} ({fleet.mode})" + (f"  gcs: {fleet.gcs_ip}" if fleet.gcs_ip else ""))
    print_table(rows)
    return 1 if problems else 0


def cmd_status(fleet: Fleet, names: list[str], *, verbose: bool, jobs: int) -> int:
    vehicles = _select(fleet, names)
    outcomes = _fan_out(vehicles,  # identity env included: SIL trees must answer status too
                        lambda v: _chooser(v, ["status", "--format", "json"],
                                           _vehicle_env(fleet, v, {})),
                        jobs=jobs)
    rows = [("VEHICLE", "REACH", "RUN", "STACKS")]
    detail: list[str] = []
    problems = 0
    for o in outcomes:
        if not o.reachable:
            rows.append((o.vehicle.name, "UNREACHABLE", "—", "—"))
            problems += 1
            continue
        try:
            doc = json.loads(o.out)
            stacks = doc.get("stacks") or []
        except (json.JSONDecodeError, AttributeError):
            rows.append((o.vehicle.name, "ok", "needs rig ≥ 0.1.66 on the vehicle", "—"))
            problems += 1
            continue
        up = sum(1 for s in stacks if s.get("state") == "running")
        run = str(doc.get("run") or "no active run").replace("run: ", "")
        rows.append((o.vehicle.name, "ok", run, f"{up}/{len(stacks)} up"))
        if o.rc != 0:
            problems += 1
        if verbose:
            detail.append(f"{o.vehicle.name}:")
            detail += [f"  {s.get('sensor')}  {s.get('state')}  {s.get('health')}  "
                       f"{s.get('running')}/{s.get('total')}" for s in stacks]
    print_table(rows)
    for line in detail:
        print(line)
    return 1 if problems else 0


def _ensure_network(fleet: Fleet, *, dry_run: bool) -> None:
    net = fleet.network
    if not net:
        return
    name, subnet = str(net["name"]), str(net["subnet"])
    if dry_run:
        eprint(f"fleet: (dry-run) would ensure docker network {name} ({subnet})")
        return
    probe = subprocess.run(["docker", "network", "inspect", name], capture_output=True, text=True)
    if probe.returncode == 0:
        return
    made = subprocess.run(["docker", "network", "create", "--subnet", subnet, name],
                          capture_output=True, text=True)
    if made.returncode != 0:
        raise RigError(f"fleet: docker network create {name} failed: "
                       f"{(made.stderr or '').strip()[:160]}")
    eprint(f"fleet: created docker network {name} ({subnet}) — services join it via their own "
           f"composes (RIG_NETWORK/RIG_VEHICLE_IP are exported; rig never writes compose config)")


def _remove_network(fleet: Fleet, *, dry_run: bool) -> None:
    net = fleet.network
    if not net or dry_run:
        return
    gone = subprocess.run(["docker", "network", "rm", str(net["name"])],
                          capture_output=True, text=True)
    if gone.returncode == 0:
        eprint(f"fleet: removed docker network {net['name']}")
    else:  # containers still attached etc. — a note, never an error (best-effort by design)
        eprint(f"fleet: note — docker network {net['name']} not removed "
               f"({(gone.stderr or '').strip().splitlines()[-1][:120] if gone.stderr else 'in use?'})")


def _push_roster(fleet: Fleet, vehicles: list[Vehicle], *, dry_run: bool) -> None:
    """The provenance carve-out: fleet.yaml lands beside each deployment so the v0.1.60 run
    snapshot captures the roster the run was launched under. Fail-soft per vehicle."""
    if dry_run:
        return
    for v in vehicles:
        try:
            if v.is_local:
                shutil.copyfile(fleet.path, Path(v.path).expanduser() / "fleet.yaml")
            else:
                scp = subprocess.run(["scp", "-q", *_SSH_OPTS, str(fleet.path),
                                      f"{v.host}:{v.path}/fleet.yaml"],
                                     capture_output=True, text=True, timeout=60)
                if scp.returncode != 0:
                    raise OSError((scp.stderr or "").strip()[:120] or "scp failed")
        except (OSError, subprocess.TimeoutExpired) as exc:
            eprint(f"  {v.name}: warning — roster push failed ({exc}); the run snapshot will "
                   f"miss fleet.yaml")


def _identity_file(fleet: Fleet, vehicle: Vehicle) -> Path | None:
    if not (vehicle.is_local and fleet.data_root):
        return None
    return Path(fleet.data_root).expanduser() / ".identity" / f"{vehicle.name}.yaml"


def _ensure_identity(fleet: Fleet, vehicle: Vehicle) -> None:
    """A SIL row is a SIMULATED MACHINE: it gets a simulated machine-identity file (the
    /etc/rig tier, pointed at via RIG_VEHICLE_LOCAL) under data_root/.identity/. That is the
    only tier that can satisfy manifest-level self-markers like `data_dir: "{{data_dir}}"` —
    and it keeps the deployment trees themselves untouched."""
    path = _identity_file(fleet, vehicle)
    if path is None:
        return
    import yaml
    body = yaml.safe_dump({"vehicle": vehicle.name, "vehicle_id": vehicle.id,
                           "data_dir": str(Path(fleet.data_root).expanduser() / vehicle.name)},
                          sort_keys=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.is_file() or path.read_text() != body:
        path.write_text(body)


def _vehicle_env(fleet: Fleet, vehicle: Vehicle, extra_vars: dict[str, str]) -> dict[str, str]:
    env: dict[str, str] = {"RIG_VAR_fleet_mode": fleet.mode}
    for key, value in extra_vars.items():
        env[f"RIG_VAR_{key}"] = value
    if vehicle.is_local:  # per-TREE identity for SIL rows: shell tier + simulated machine file
        env["RIG_VEHICLE_ID"] = str(vehicle.id)
        env["RIG_VEHICLE_NAME"] = vehicle.name
        identity = _identity_file(fleet, vehicle)
        if identity is not None and identity.is_file():
            env["RIG_VEHICLE_LOCAL"] = str(identity)
    if fleet.network:  # process env for composes (${RIG_NETWORK}) — not rig vars
        env["RIG_NETWORK"] = str(fleet.network["name"])
        if vehicle.ip:
            env["RIG_VEHICLE_IP"] = vehicle.ip
    return env


def _fleet_view(fleet: Fleet, vehicles: list[Vehicle], label: str | None) -> None:
    """The SIL shared-run-dir VIEW: <data_root>/runs/<label>-<date>/<vehicle> -> each local
    vehicle's open run. Symlinks only — the per-vehicle registries stay the truth."""
    if fleet.mode != "sil" or not fleet.data_root or not label:
        return
    root = Path(fleet.data_root).expanduser()
    stamp = datetime.datetime.now().strftime("%Y%m%d")
    view = root / "runs" / f"{label}-{stamp}"
    linked = 0
    for v in vehicles:
        if not v.is_local:
            continue
        current = root / v.name / "current"
        if not current.is_symlink():
            continue
        run_rel = os.readlink(current)                      # runs/<id> (RELATIVE by design)
        target = Path("..") / ".." / v.name / run_rel       # view/<name> -> ../../<name>/runs/<id>
        link = view / v.name
        view.mkdir(parents=True, exist_ok=True)
        if link.is_symlink():
            if os.readlink(link) == str(target):
                continue
            link.unlink()
        link.symlink_to(target)
        linked += 1
    if linked:
        shutil.copyfile(fleet.path, view / "fleet.yaml")    # the fleet-level provenance record
        eprint(f"fleet: view {view} — one folder, per-vehicle subfolders (live symlinks)")


def _print_outcomes(outcomes: list[Outcome]) -> int:
    problems = 0
    for o in outcomes:
        tag = "✓" if (o.rc == 0 and o.reachable) else ("UNREACHABLE" if not o.reachable else "✗ FAILED")
        eprint(f"\n───── {o.vehicle.name} {tag} ─────")
        body = (o.out + o.err).strip()
        if body:
            eprint(body)
        problems += 0 if (o.rc == 0 and o.reachable) else 1
    return problems


def cmd_up(fleet: Fleet, names: list[str], *, run_label: str | None, set_vars: list[str],
           force: bool, dry_run: bool, jobs: int) -> int:
    vehicles = _select(fleet, names)
    extra: dict[str, str] = {}
    for kv in set_vars:
        key, sep, value = kv.partition("=")
        if not sep or not key:
            raise RigError(f"fleet up: --var takes k=v, not '{kv}'")
        extra[key] = value
    _ensure_network(fleet, dry_run=dry_run)
    _push_roster(fleet, vehicles, dry_run=dry_run)
    for v in vehicles:  # simulated machine identities (SIL rows) — needed even for dry-run render
        _ensure_identity(fleet, v)
    argv = ["up"] + (["--run", run_label] if run_label else []) \
        + (["--force"] if force else []) + (["--dry-run"] if dry_run else [])
    outcomes = _fan_out(vehicles, lambda v: _chooser(v, argv, _vehicle_env(fleet, v, extra)),
                        jobs=jobs, timeout=600)
    problems = _print_outcomes(outcomes)
    if not dry_run:
        _fleet_view(fleet, [o.vehicle for o in outcomes if o.rc == 0 and o.reachable], run_label)
    eprint(f"\nfleet up: {len(outcomes) - problems}/{len(outcomes)} ok")
    return 1 if problems else 0


def cmd_down(fleet: Fleet, names: list[str], *, end_run: bool, force: bool, dry_run: bool,
             jobs: int) -> int:
    vehicles = _select(fleet, names)
    argv = ["down"] + (["--end-run"] if end_run else []) \
        + (["--force"] if force else []) + (["--dry-run"] if dry_run else [])
    outcomes = _fan_out(vehicles, lambda v: _chooser(v, argv, _vehicle_env(fleet, v, {})),
                        jobs=jobs, timeout=600)
    problems = _print_outcomes(outcomes)
    if not problems and not names:  # FULL fleet down succeeded -> the SIL network can go
        _remove_network(fleet, dry_run=dry_run)
    eprint(f"\nfleet down: {len(outcomes) - problems}/{len(outcomes)} ok")
    return 1 if problems else 0


def _enumerate_runs(vehicle: Vehicle, data_dir: str) -> Outcome:
    """One pass reading the MACHINE CONTRACT (grep-able flat manifest keys) — never a table."""
    script = (f'for d in {_pathexpr(data_dir)}/runs/*/; do [ -d "$d" ] || continue; '
              f'printf \'%s\\t%s\\n\' "$(basename "$d")" '
              f'"$(sed -n \'s/^ended: //p\' "$d/manifest.yaml" 2>/dev/null | head -1)"; done')
    return _run_script(vehicle, script, timeout=60)


def cmd_sync(fleet: Fleet, names: list[str], *, label: str | None, into: str, jobs: int) -> int:
    vehicles = _select(fleet, names)
    dest_root = Path(into).expanduser()
    problems = 0
    pulled = skipped = 0
    for v in vehicles:  # sequential: scp -r of data is bandwidth-bound, not latency-bound
        data_dir = _row_data_dir(fleet, v)
        if data_dir is None:
            eprint(f"  {v.name}: no data_dir in the roster row (and no sil.data_root to derive "
                   f"it) — sync needs to know where runs live")
            problems += 1
            continue
        listing = _enumerate_runs(v, data_dir)
        if not listing.reachable or listing.rc != 0:
            eprint(f"  {v.name}: {'UNREACHABLE' if not listing.reachable else 'run enumeration failed'}"
                   f"{(' — ' + listing.err.strip()[:120]) if listing.err.strip() else ''}")
            problems += 1
            continue
        for line in listing.out.splitlines():
            run_id, _, ended = line.partition("\t")
            run_id = run_id.strip()
            if not run_id:
                continue
            run_label = run_id.split("_", 1)[1] if "_" in run_id else run_id
            if label and run_label != label and not run_label.startswith(f"{label}-"):
                continue
            if not ended.strip():
                eprint(f"  {v.name}/{run_id}: skipped (not sealed — `ended:` absent; "
                       f"`fleet down --end-run` seals)")
                skipped += 1
                continue
            dest = dest_root / run_label / v.name / run_id
            if dest.exists():
                skipped += 1
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            src = f"{data_dir}/runs/{run_id}"
            if v.is_local:
                shutil.copytree(Path(src).expanduser(), dest)
            else:
                scp = subprocess.run(["scp", "-r", "-q", *_SSH_OPTS, f"{v.host}:{src}", str(dest)],
                                     capture_output=True, text=True, timeout=3600)
                if scp.returncode != 0:
                    eprint(f"  {v.name}/{run_id}: scp failed — "
                           f"{(scp.stderr or '').strip()[:120]}")
                    problems += 1
                    continue
            eprint(f"  {v.name}/{run_id} -> {dest}")
            pulled += 1
    eprint(f"fleet sync: {pulled} run(s) pulled, {skipped} skipped, into {dest_root}"
           + (f" — {problems} problem(s)" if problems else ""))
    return 1 if problems else 0
