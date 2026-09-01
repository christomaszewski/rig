"""Run directories — one session, one folder (ROADMAP §3c).

rig owns a tiny registry under ``data_dir``: ``runs/<UTCstamp>_<label|auto>/`` with a ``manifest.yaml``
each, and a ``current`` symlink marking the (at most one) OPEN run. Services write via
``<data_dir>/current/<kind>/<name>``, pinning the run at process start (a live symlink flip would ENOENT
a recorder's next segment — so rotation refuses while this manifest's stacks are running).

The two traps this design dodges (see ROADMAP §3c): rotation is NEVER a side effect of `up` (partial
re-ups would split-brain the data tree), and the run id lives in the FILESYSTEM, not the fleet env (an
env id would be frozen into baked composes and break certify's determinism).

Failure posture, deliberately asymmetric:
- the rotation/seal GUARD fails CLOSED: if ``docker compose ls`` can't answer (timeout, parse, non-zero),
  rig refuses to rotate/seal rather than risk sealing under a live recorder — ``--force`` is the human
  override. Only a missing docker binary reads as "nothing running" (our containers need docker).
- READ paths fail SOFT: a corrupt run manifest must never wedge `rig up`/`status`/`runs` — it's reported
  (state ``corrupt``) and worked around; sealing a corrupt-manifest run rewrites a minimal document.

The ``current`` symlink target is RELATIVE (``runs/<id>``) on purpose: containers bind-mount ``data_dir``
and resolve the link inside their own mount namespace. ``current_run`` enforces CONTAINMENT (the target
must be a direct child of ``runs/``) so a mis-pointed link is a loud error, never a basename collision
that seals the wrong run. ``manifest.yaml`` is the machine contract: ``ended:`` present ⇔ sealed.

Config snapshots: every non-dry-run `up` captures the EFFECTIVE config (vehicle.yaml + lock + resolved
vars + rendered per-instance configs) into ``runs/<id>/.rig/config/<digest12>/`` — content-addressed,
so an unchanged config writes nothing — and appends an ``ups:`` event to the manifest. The capture
point is `up`, not open/seal: every config that governed recorded data was live at some `up`, while the
tree at seal time may contain edits that never ran — so sealing only DIRTY-CHECKS (flags, never copies).
The check is gated on a per-deployment-INSTANCE id (``var/deployment-id``, minted lazily; var/ is never
staged by bake, so every untar/clone is a fresh instance): sealing run A from a different deployment
tree must not read as "A's config was edited". Snapshot writes fail SOFT — provenance must never wedge
`up`. Compose-only up.sh does not snapshot (resolved artifacts are tag-determined; fleet artifacts
route every verb through the bundled rig anyway).

Docker log capture: ``down --end-run`` saves every container's ``docker logs`` into the sealing run
(``.rig/logs/<sensor>/<container>.log``) BEFORE the down verb dispatches — ``compose down`` REMOVES
the containers and their stdout/stderr goes with them, so seal time is too late. Fail-soft like
snapshots; compose-only down.sh doesn't capture (same rig-only boundary as the flagged forms).
"""
from __future__ import annotations

import datetime
import json
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

import yaml

from . import RigError, __version__
from .common import eprint, load_yaml
from .lock import sha256_bytes
from .manifest import Manifest, project_name

_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def _root(manifest: Manifest) -> Path:
    if not manifest.data_dir:
        raise RigError("runs need `data_dir` in vehicle.yaml (the host dir the registry lives under)")
    data = Path(manifest.data_dir)
    if not data.is_absolute():
        raise RigError(f"data_dir must be an ABSOLUTE host path, not '{manifest.data_dir}' — a relative "
                       f"path would fork the registry per working directory")
    return data


def _current(data: Path) -> Path:
    return data / "current"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _iso_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def running_projects(manifest: Manifest) -> list[str]:
    """This manifest's compose projects that are currently up — the rotation/seal guard's evidence.
    Raises RigError when docker CAN'T ANSWER (the guard must fail closed, not open); a missing docker
    binary alone reads as [] (nothing of ours can run without it)."""
    expected = {project_name(s.name, manifest.vehicle_id) for s in manifest.sensors if s.enabled}
    try:
        proc = subprocess.run(["docker", "compose", "ls", "-a", "--format", "json"],
                              capture_output=True, text=True, timeout=15)
    except FileNotFoundError:
        return []
    except subprocess.TimeoutExpired:
        raise RigError("`docker compose ls` timed out — cannot tell whether stacks are running")
    if proc.returncode != 0:
        raise RigError(f"`docker compose ls` failed (exit {proc.returncode}): "
                       f"{(proc.stderr or '').strip()[:160]}")
    try:
        rows = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        rows = None
    if rows is None or not isinstance(rows, list):  # NDJSON fallback (compose has drifted before)
        try:
            rows = [json.loads(line) for line in (proc.stdout or "").splitlines() if line.strip()]
        except json.JSONDecodeError:
            raise RigError("`docker compose ls` output was not parseable JSON — cannot tell whether "
                           "stacks are running")
    live = []
    for row in rows:
        if isinstance(row, dict) and row.get("Name") in expected \
                and "running" in str(row.get("Status", "")).lower():
            live.append(row["Name"])
    return sorted(live)


def _guard(manifest: Manifest, force: bool, action: str) -> None:
    """Refuse rotation/seal while our stacks run — and refuse when we CANNOT TELL (fail closed)."""
    if force:
        return
    try:
        live = running_projects(manifest)
    except RigError as exc:
        raise RigError(f"{action}: {exc} — retry, or --force to rotate anyway (late writes would land "
                       f"in the sealed run)")
    if live:
        raise RigError(f"{action}: stacks are running ({', '.join(live)}) — a running recording belongs "
                       f"to the run it started in. `rig down` first, or --force to accept late writes "
                       f"landing in the sealed run")


def current_run(data: Path) -> tuple[str, Path, dict] | None:
    """(run-id, run-dir, manifest dict) for the OPEN run, or None. Dangling `current` reads as None
    (self-heals on the next ensure). A `current` that is NOT a symlink, or points anywhere but a direct
    child of runs/, is a loud error — never a guess (a basename collision could seal the wrong run)."""
    cur = _current(data)
    if not cur.is_symlink():
        if cur.exists():
            raise RigError(f"{cur} exists but is not a symlink — remove it (the registry owns this path)")
        return None
    target = cur.resolve()
    if not target.is_dir():
        return None  # dangling
    if target.parent != (data / "runs").resolve():
        raise RigError(f"{cur} points outside the registry ({target}) — remove or fix it")
    mpath = target / "manifest.yaml"
    doc: dict = {"run": target.name}
    if mpath.exists():
        try:
            doc = load_yaml(mpath)
        except RigError:
            eprint(f"rig: warning: {mpath} is not parseable — treating the run as corrupt")
            doc = {"run": target.name, "corrupt": True}
    return target.name, target, doc


def _artifact_tag(root: Path) -> str | None:
    """The artifact tag when rig runs inside an extracted artifact (bake's provenance chain plugs in)."""
    meta = root / "metadata.yaml"
    if meta.exists():
        try:
            return load_yaml(meta).get("tag")
        except RigError:
            return None
    return None


def deployment_id(root: Path) -> str | None:
    """The per-deployment-INSTANCE id (var/deployment-id), minted lazily. Lives under var/ on purpose:
    bake never stages var/ and git ignores it, so every untar / fresh clone / re-extract is a NEW
    instance, while a dev tree keeps its id across config edits — exactly the boundary the seal
    dirty-check needs. Fail-soft: an unwritable tree costs the id, never the command."""
    path = root / "var" / "deployment-id"
    try:
        if path.is_file():
            if got := path.read_text().strip():
                return got
        path.parent.mkdir(parents=True, exist_ok=True)
        minted = uuid.uuid4().hex[:12]
        path.write_text(minted + "\n")
        return minted
    except OSError as exc:
        eprint(f"rig: warning: cannot read/mint {path}: {exc}")
        return None


def _collect_config(manifest: Manifest, root: Path) -> dict[str, bytes]:
    """The effective-config capture set, relpath -> bytes. vehicle.yaml is mandatory (no deployment
    tree ⇒ a snapshot is meaningless); the rest joins when present. vars.yaml is generated: the
    RESOLVED var/env context, which is the only place machine-local identity (/etc/rig) and RIG_VAR_*
    shell contributions — sources outside the tree — leave a trace. Rendered configs come off the
    MATERIALIZED manifest rows, never a var/rendered glob (stale files linger for removed instances)."""
    files = {"vehicle.yaml": (root / "vehicle.yaml").read_bytes()}
    for name in ("vehicle.local.yaml", "services.yaml", "rig.lock",
                 "fleet.yaml"):  # `fleet up` pushes the roster here — the run records its fleet
        if (path := root / name).is_file():
            files[name] = path.read_bytes()
    files["vars.yaml"] = yaml.safe_dump(
        {"vars": manifest.vars, "env": manifest.extra_env}, sort_keys=True).encode()
    for sensor in manifest.sensors:
        if sensor.enabled:
            files[f"rendered/{sensor.name}.yaml"] = Path(sensor.config).read_bytes()
    return files


def _config_digest(files: dict[str, bytes]) -> str:
    """12-hex content address over the capture set (sorted `relpath sha256` lines)."""
    lines = "".join(f"{name} {sha256_bytes(files[name])}\n" for name in sorted(files))
    return sha256_bytes(lines.encode())[:12]


def _live_state(manifest: Manifest, root: Path) -> tuple[str | None, str | None]:
    """(config digest, deployment id) of the CURRENT tree, for the seal dirty-check. Fail-soft: a
    tree that can't be collected (not a deployment, unreadable) just skips the check."""
    try:
        digest = _config_digest(_collect_config(manifest, root))
    except Exception:  # noqa: BLE001 — the dirty-check is advisory, never load-bearing
        return None, None
    return digest, deployment_id(root)


def snapshot(manifest: Manifest, root: Path, *, stacks: list[str]) -> str | None:
    """Capture the effective config into the OPEN run (cmd_up calls this right after ensure/up_run).
    Content-addressed under runs/<id>/.rig/config/<digest12>/ — an unchanged config writes no files,
    but EVERY call appends an `ups:` event (the temporal log data analysis reads). Fail-soft
    throughout: provenance must never wedge `up`."""
    try:
        data = _root(manifest)
        cur = current_run(data)
        if cur is None:
            eprint("rig: warning: no open run to snapshot config into")
            return None
        run_id, run_dir, doc = cur
        files = _collect_config(manifest, root)
        digest = _config_digest(files)
        snap_dir = run_dir / ".rig" / "config" / digest
        if not snap_dir.is_dir():
            tmp = run_dir / ".rig" / f".config.{digest}.tmp"
            if tmp.exists():
                shutil.rmtree(tmp)
            for rel, blob in files.items():
                (tmp / rel).parent.mkdir(parents=True, exist_ok=True)
                (tmp / rel).write_bytes(blob)
            snap_dir.parent.mkdir(parents=True, exist_ok=True)
            tmp.replace(snap_dir)
            eprint(f"rig: run {run_id}: captured config snapshot {digest}")
        entry: dict = {"at": _iso_now(), "stacks": list(stacks), "config": digest}
        if (dep := deployment_id(root)) is not None:
            entry["deployment"] = dep
        entry["root"] = str(root.resolve())
        doc["config"] = digest
        ups = doc.get("ups")
        if not isinstance(ups, list):
            ups = doc["ups"] = []
        ups.append(entry)
        (run_dir / "manifest.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
        return digest
    except Exception as exc:  # noqa: BLE001 — fail SOFT: never wedge `up` on provenance
        eprint(f"rig: warning: config snapshot failed ({exc}) — continuing without it")
        return None


def capture_docker_logs(manifest: Manifest) -> int:
    """Save `docker logs` from every container of this manifest's compose projects into the OPEN run.
    cmd_down calls this BEFORE dispatching the down verb when --end-run was asked: `compose down`
    REMOVES the containers, and their stdout/stderr goes with them — seal time is too late.

    Layout: runs/<id>/.rig/logs/<sensor>/<container>.log — under .rig/ (rig's namespace in the run
    dir), so a data kind literally named `logs` under current/<kind>/ can never collide. stderr is
    merged into stdout with --timestamps, so one file reads like the terminal and stays ordered.
    A partial down retried later composes: each capture writes only the containers that still exist,
    and per-file overwrite keeps earlier captures intact. Stopped containers count too (`ps -a`) —
    a crashed stack's logs are exactly the ones worth keeping. Fail SOFT throughout: like config
    snapshots, provenance must never wedge `down`. Returns the number of containers captured."""
    captured = 0
    try:
        data = _root(manifest)
        cur = current_run(data)
        if cur is None:
            eprint("rig: warning: no open run to capture docker logs into")
            return 0
        run_id, run_dir, doc = cur
        for sensor in manifest.sensors:
            if not sensor.enabled:
                continue
            project = project_name(sensor.name, manifest.vehicle_id)
            try:
                proc = subprocess.run(
                    ["docker", "ps", "-a", "--filter",
                     f"label=com.docker.compose.project={project}", "--format", "{{.Names}}"],
                    capture_output=True, text=True, timeout=15)
            except (FileNotFoundError, subprocess.TimeoutExpired):
                return 0  # no docker / wedged daemon — nothing to save; down itself will surface it
            if proc.returncode != 0:
                eprint(f"rig: warning: `docker ps` failed for {project} — skipping its log capture")
                continue
            for name in (n.strip() for n in proc.stdout.splitlines() if n.strip()):
                dest = run_dir / ".rig" / "logs" / sensor.name / f"{name}.log"
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with open(dest, "wb") as fh:  # stream to disk — a chatty container won't OOM us
                        rc = subprocess.run(["docker", "logs", "--timestamps", name],
                                            stdout=fh, stderr=subprocess.STDOUT,
                                            timeout=120).returncode
                    if rc == 0:
                        captured += 1
                    else:  # the daemon's error lands IN the file — a record of why capture failed
                        eprint(f"rig: warning: `docker logs {name}` exited {rc}")
                except (OSError, subprocess.TimeoutExpired) as exc:
                    eprint(f"rig: warning: could not capture logs for {name}: {exc}")
        if captured:
            doc["docker_logs"] = {"at": _iso_now(), "containers": captured}
            (run_dir / "manifest.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
            eprint(f"rig: run {run_id}: captured docker logs for {captured} container(s) "
                   f"-> runs/{run_id}/.rig/logs/")
    except Exception as exc:  # noqa: BLE001 — fail SOFT: never wedge `down` on provenance
        eprint(f"rig: warning: docker log capture failed ({exc}) — continuing")
    return captured


def _open_run(manifest: Manifest, root: Path, data: Path, label: str | None,
              replay: dict | None = None) -> str:
    runs = data / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    run_id = f"{_now()}_{label or 'auto'}"
    run_dir = runs / run_id
    n = 2
    while run_dir.exists():  # same-second collision
        run_dir = runs / f"{run_id}-{n}"
        n += 1
    run_dir.mkdir()
    doc = {
        "run": run_dir.name,
        "vehicle": manifest.vehicle,
        "vehicle_id": manifest.vehicle_id,
        "rig_version": __version__,
        "started": _iso_now(),
        "stacks": [s.name for s in manifest.sensors if s.enabled],
    }
    if label:
        doc["label"] = label
    if (tag := _artifact_tag(root)) is not None:
        doc["artifact"] = tag
    if (dep := deployment_id(root)) is not None:
        doc["deployment"] = dep  # the OPENING deployment instance; each ups: entry re-attributes
    if replay:  # a SIL replay session: {of: <source-run-id>, source: <abs path>, with: [names]}
        doc["replay"] = replay
    if manifest.run_capture:  # every run dir carries the tree that ran (rig-reconstruct plan) —
        try:                  # fail-soft: capture must NEVER block a field session
            from . import bake as bake_mod
            doc["capture"] = bake_mod.capture_run(root, manifest, run_dir)
        except Exception as exc:  # noqa: BLE001
            eprint(f"rig: warning: run capture failed ({exc}) — opening without "
                   f".rig/artifact.tar.gz (config snapshots still cover the config layer; "
                   f"`rig run retrofit` can supply the tree later)")
    (run_dir / "manifest.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    cur = _current(data)
    if cur.exists() and not cur.is_symlink():
        raise RigError(f"{cur} exists but is not a symlink — remove it (the registry owns this path)")
    tmp = data / ".current.tmp"
    tmp.unlink(missing_ok=True)  # a crash between symlink+replace must not brick every later attempt
    tmp.symlink_to(Path("runs") / run_dir.name)  # RELATIVE — resolvable inside container mounts
    tmp.replace(cur)
    return run_dir.name


def _seal(run_dir: Path, status_text: str | None, *, live_digest: str | None = None,
          live_deployment: str | None = None) -> None:
    mpath = run_dir / "manifest.yaml"
    doc: dict = {"run": run_dir.name}
    if mpath.exists():
        try:
            doc = load_yaml(mpath)
        except RigError:  # seal must not be blocked by a corrupt manifest — rewrite minimally
            eprint(f"rig: warning: {mpath} was corrupt — rewriting a minimal manifest to seal")
            doc = {"run": run_dir.name, "corrupt": True}
    # Dirty-check, not a copy: seal-time edits never ran, so copying them would assert false
    # provenance. Gated on the deployment id — sealing from a DIFFERENT tree (stale run rotated
    # away by a freshly untarred artifact) must not read as "this run's config was edited".
    ups = doc.get("ups")
    last = ups[-1] if isinstance(ups, list) and ups and isinstance(ups[-1], dict) else None
    if (last is not None and live_digest and live_deployment
            and last.get("deployment") == live_deployment
            and last.get("config") and last.get("config") != live_digest):
        doc["config_dirty_at_seal"] = True
        eprint("rig: warning: config changed after this run's last `up` — those edits never "
               "applied to the recorded data (snapshot reflects what ran)")
    doc["ended"] = _iso_now()
    try:
        du = subprocess.run(["du", "-sk", str(run_dir)], capture_output=True, text=True, timeout=120)
        doc["disk_kb"] = int(du.stdout.split()[0])
    except Exception:  # noqa: BLE001 — size is best-effort
        pass
    if status_text:
        doc["status_at_end"] = status_text
    mpath.write_text(yaml.safe_dump(doc, sort_keys=False))


def ensure(manifest: Manifest, root: Path) -> str | None:
    """`up`'s guard: make sure an open run exists — create `_auto` if absent. NEVER rotates. Returns the
    open run id, or None when data_dir is unset (feature inert)."""
    if not manifest.data_dir:
        return None
    data = _root(manifest)
    if (cur := current_run(data)) is not None:
        return cur[0]
    run_id = _open_run(manifest, root, data, None)
    eprint(f"rig: opened run {run_id} (no session was open — label one with `rig new-run <label>`)")
    return run_id


def new_run(manifest: Manifest, root: Path, label: str | None, *, force: bool = False,
            replay: dict | None = None) -> str:
    """Rotate: seal the open run (if any) and open a new one. Guarded while our stacks run.
    `replay` stamps the new run as a SIL replay session (replay.py passes it; see the manifest's
    `replay:` key)."""
    if label and not _LABEL_RE.match(label):
        raise RigError(f"new-run: label '{label}' must match [A-Za-z0-9][A-Za-z0-9_-]* "
                       f"(it becomes a directory name)")
    data = _root(manifest)
    _guard(manifest, force, "new-run")
    if (cur := current_run(data)) is not None:
        digest, dep = _live_state(manifest, root)
        _seal(cur[1], None, live_digest=digest, live_deployment=dep)
        eprint(f"rig: sealed run {cur[0]}")
    run_id = _open_run(manifest, root, data, label, replay=replay)
    eprint(f"rig: opened run {run_id}")
    return run_id


def end_run(manifest: Manifest, root: Path, *, force: bool = False,
            status_text: str | None = None) -> str | None:
    """Seal the open run and remove `current`. Guarded while our stacks run; idempotent when none open.
    `root` (the deployment tree sealing from) feeds the config dirty-check — advisory, fail-soft."""
    data = _root(manifest)
    cur = current_run(data)
    if cur is None:
        eprint("rig: no active run")
        return None
    _guard(manifest, force, "end-run")
    digest, dep = _live_state(manifest, root)
    _seal(cur[1], status_text, live_digest=digest, live_deployment=dep)
    _current(data).unlink(missing_ok=True)
    eprint(f"rig: sealed run {cur[0]}")
    return cur[0]


def up_run(manifest: Manifest, root: Path, label: str, *, force: bool = False) -> str:
    """`up --run <label>`: join the open run when its label matches (idempotent — never mints X-2),
    else rotate to a fresh run with that label (same guard as new-run)."""
    data = _root(manifest)
    cur = current_run(data)
    # str() both sides: a label like `123` round-trips through YAML as an int in hand-written or
    # sh-generated manifests; a type mismatch must not cause a silent rotation.
    if cur is not None and str(cur[2].get("label", "")) == str(label):
        return cur[0]
    return new_run(manifest, root, label, force=force)


@dataclass
class RunRow:
    run: str
    label: str
    state: str  # OPEN | sealed | interrupted | corrupt
    started: str
    ended: str
    disk_kb: int | None
    replay_of: str | None = None  # SIL replay sessions name their source run
    linked: bool = False          # a symlinked registry entry (reconstruct's import) — a
    #                               reference to the archive, not a local copy


def list_runs(manifest: Manifest) -> list[RunRow]:
    data = _root(manifest)
    try:
        open_id = (current_run(data) or (None,))[0]
    except RigError:  # a broken `current` must not hide the registry listing
        open_id = None
    rows = []
    runs = data / "runs"
    for d in sorted(runs.iterdir()) if runs.is_dir() else []:
        if d.is_symlink() and not d.is_dir():  # dangling link: the archive moved — say so,
            rows.append(RunRow(run=d.name, label="—", state="dangling", started="?",
                               ended="—", disk_kb=None, linked=True))  # never silently vanish
            continue
        if not d.is_dir():
            continue
        try:
            doc = load_yaml(d / "manifest.yaml") if (d / "manifest.yaml").exists() else {}
        except RigError:  # open-ness outranks corruption: the operator must still see what's live
            rows.append(RunRow(run=d.name, label="?", state="OPEN" if d.name == open_id else "corrupt",
                               started="?", ended="?", disk_kb=None))
            continue
        ended = str(doc.get("ended") or "")
        state = "OPEN" if d.name == open_id else ("sealed" if ended else "interrupted")
        replay = doc.get("replay")
        rows.append(RunRow(run=d.name, label=str(doc.get("label") or "—"), state=state,
                           started=str(doc.get("started") or "?"), ended=ended or "—",
                           disk_kb=doc.get("disk_kb"), linked=d.is_symlink(),
                           replay_of=str(replay["of"]) if isinstance(replay, dict)
                           and replay.get("of") else None))
    return rows


def by_label(manifest: Manifest, ref: str) -> str | None:
    """The NEWEST run id whose label matches `ref` (ids are `<stamp>_<label>`, so this is a pure
    dirname check — no manifest reads at resolution time). Lets every run-ref verb accept the
    label a human remembers (`rig replay flight1 …`); an exact id always wins upstream."""
    data = _root(manifest)
    runs = data / "runs"
    if not runs.is_dir():
        return None
    matches = sorted(d.name for d in runs.iterdir() if d.is_dir()
                     and d.name.endswith(f"_{ref}"))
    return matches[-1] if matches else None


def remove_runs(manifest: Manifest, run_ids: list[str], *, force: bool = False) -> int:
    """`run rm`: delete run dirs from THIS registry, by id (never by path — containment is the
    registry's own doctrine: a mis-typed path must not delete something outside runs/). Sealed
    runs remove freely; interrupted/corrupt need --force (their recording may be the only copy of
    a crashed session); the OPEN run NEVER removes — even --force (`current` would dangle under a
    possibly-live recorder) — `rig down --end-run` first."""
    data = _root(manifest)
    runs = (data / "runs").resolve()
    try:
        open_id = (current_run(data) or (None,))[0]
    except RigError:
        open_id = None  # a broken `current` must not block cleanup of OTHER runs
    rc = 0
    freed_kb = 0
    for rid in run_ids:
        run_dir = data / "runs" / rid
        if "/" not in rid and run_dir.is_symlink():
            # a LINKED run (reconstruct's registry import): the entry is a reference — remove
            # the link, NEVER the archive target, and no seal gate (unlinking loses nothing)
            run_dir.unlink()
            eprint(f"rig run rm: unlinked {rid} (the archived run itself is untouched)")
            continue
        if "/" in rid or not run_dir.is_dir() or run_dir.resolve().parent != runs:
            eprint(f"rig run rm: no run '{rid}' in this registry (ids only — see `rig runs`)")
            rc = 1
            continue
        if rid == open_id:
            eprint(f"rig run rm: {rid} is the OPEN run — never removed (a recorder may be "
                   f"writing it and `current` would dangle); `rig down --end-run` first")
            rc = 1
            continue
        doc: dict = {}
        if (run_dir / "manifest.yaml").exists():
            try:
                doc = load_yaml(run_dir / "manifest.yaml")
            except RigError:
                pass
        if not doc.get("ended") and not force:
            eprint(f"rig run rm: {rid} is not sealed (interrupted/corrupt) — its recording may "
                   f"be the only copy of that session; --force to remove anyway")
            rc = 1
            continue
        kb = doc.get("disk_kb")
        if not isinstance(kb, int):
            try:
                kb = int(subprocess.run(["du", "-sk", str(run_dir)], capture_output=True,
                                        text=True, timeout=120).stdout.split()[0])
            except Exception:  # noqa: BLE001 — size is display-only
                kb = 0
        shutil.rmtree(run_dir)
        freed_kb += kb
        eprint(f"rig run rm: removed {rid}" + (f" ({kb // 1024} MB)" if kb else ""))
    if freed_kb:
        eprint(f"rig run rm: freed {freed_kb / 1024:.0f} MB")
    return rc


def import_runs(manifest: Manifest, paths: list[str], *, move: bool = False) -> int:
    """`run import`: adopt run dir(s) (scp'd off a vehicle, downloaded from the archive) into
    THIS registry so the id-based verbs (runs/graph/replay/reconstruct, TAB completion) cover
    them. COPY by default — the source stays the archive's; --move for same-disk adoption. Never
    touches `current` (an imported run is history, not the open session)."""
    data = _root(manifest)
    runs = data / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    rc = 0
    for raw in paths:
        src = Path(raw).expanduser()
        if not src.is_dir() or not (src / "manifest.yaml").exists():
            eprint(f"rig run import: {src} is not a run dir (no manifest.yaml)")
            rc = 1
            continue
        target = runs / src.name
        if target.exists():
            eprint(f"rig run import: {src.name} already in the registry — remove it first "
                   f"(`rig run rm`) or rename the source")
            rc = 1
            continue
        try:
            doc = load_yaml(src / "manifest.yaml")
            if not doc.get("ended"):
                eprint(f"rig run import: warning: {src.name} is not sealed (`ended:` absent) — "
                       f"importing an incomplete session")
        except RigError:
            eprint(f"rig run import: warning: {src.name} has an unparseable manifest — "
                   f"importing as-is (it will list as corrupt)")
        if move:
            shutil.move(str(src), str(target))
        else:
            shutil.copytree(src, target, symlinks=True)
        eprint(f"rig run import: {src.name} {'moved' if move else 'copied'} into the registry")
    return rc


def status_line(manifest: Manifest) -> str | None:
    """The one-liner for `rig status`'s header (None when the feature is inert). Never raises — a broken
    registry must not take `status` down with it."""
    if not manifest.data_dir:
        return None
    try:
        cur = current_run(_root(manifest))
    except RigError as exc:
        return f"run: registry problem — {exc}"
    if cur is None:
        return "run: none active"
    run_id, _, doc = cur
    label = doc.get("label") or run_id
    return f"run: {label} (open since {doc.get('started', '?')}) -> runs/{run_id}"
