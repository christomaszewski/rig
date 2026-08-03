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
"""
from __future__ import annotations

import datetime
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

from . import RigError, __version__
from .common import eprint, load_yaml
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


def _open_run(manifest: Manifest, root: Path, data: Path, label: str | None) -> str:
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
    (run_dir / "manifest.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    cur = _current(data)
    if cur.exists() and not cur.is_symlink():
        raise RigError(f"{cur} exists but is not a symlink — remove it (the registry owns this path)")
    tmp = data / ".current.tmp"
    tmp.unlink(missing_ok=True)  # a crash between symlink+replace must not brick every later attempt
    tmp.symlink_to(Path("runs") / run_dir.name)  # RELATIVE — resolvable inside container mounts
    tmp.replace(cur)
    return run_dir.name


def _seal(run_dir: Path, status_text: str | None) -> None:
    mpath = run_dir / "manifest.yaml"
    doc: dict = {"run": run_dir.name}
    if mpath.exists():
        try:
            doc = load_yaml(mpath)
        except RigError:  # seal must not be blocked by a corrupt manifest — rewrite minimally
            eprint(f"rig: warning: {mpath} was corrupt — rewriting a minimal manifest to seal")
            doc = {"run": run_dir.name, "corrupt": True}
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


def new_run(manifest: Manifest, root: Path, label: str | None, *, force: bool = False) -> str:
    """Rotate: seal the open run (if any) and open a new one. Guarded while our stacks run."""
    if label and not _LABEL_RE.match(label):
        raise RigError(f"new-run: label '{label}' must match [A-Za-z0-9][A-Za-z0-9_-]* "
                       f"(it becomes a directory name)")
    data = _root(manifest)
    _guard(manifest, force, "new-run")
    if (cur := current_run(data)) is not None:
        _seal(cur[1], None)
        eprint(f"rig: sealed run {cur[0]}")
    run_id = _open_run(manifest, root, data, label)
    eprint(f"rig: opened run {run_id}")
    return run_id


def end_run(manifest: Manifest, *, force: bool = False, status_text: str | None = None) -> str | None:
    """Seal the open run and remove `current`. Guarded while our stacks run; idempotent when none open."""
    data = _root(manifest)
    cur = current_run(data)
    if cur is None:
        eprint("rig: no active run")
        return None
    _guard(manifest, force, "end-run")
    _seal(cur[1], status_text)
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


def list_runs(manifest: Manifest) -> list[RunRow]:
    data = _root(manifest)
    try:
        open_id = (current_run(data) or (None,))[0]
    except RigError:  # a broken `current` must not hide the registry listing
        open_id = None
    rows = []
    runs = data / "runs"
    for d in sorted(runs.iterdir()) if runs.is_dir() else []:
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
        rows.append(RunRow(run=d.name, label=str(doc.get("label") or "—"), state=state,
                           started=str(doc.get("started") or "?"), ended=ended or "—",
                           disk_kb=doc.get("disk_kb")))
    return rows


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
