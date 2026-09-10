"""``rig catalog`` — every run rig knows about, across registries and harvest trees, searchable
by tag, label, vehicle and date. Tags live in each run's OWN manifest (`rig run tag`), so they
travel with the run and every copy reads them raw.

Roots (``~/.rig/catalog.yaml``): the box's shared registry (the user's, and the machine's —
always, when set),
every registry rig has opened or imported a run into, every ``fleet sync --into`` tree and
reconstruct workspace — remembered as rig touches them — plus ``rig catalog add <dir>``. The
catalog is deliberately NOT what ``rig runs`` or TAB show: those stay the deployment's registry
plus the host's (runs.registries), so a workspace never sees another workspace's runs. The
catalog is where you go to FIND a run; the id/path it prints feeds any run verb.

A raw read every time (a few hundred manifests is sub-second; a root that is gone is listed as
missing, never an error) — no index to fall behind, nothing to rebuild.
"""
from __future__ import annotations

import dataclasses
import datetime
import json
import os
from pathlib import Path

import yaml

from . import RigError
from .common import eprint, load_yaml
from .registries import rig_home

ROOTS_FILE = "catalog.yaml"
_PRUNE = {"exports", ".rig", "bags", "recordings", "graph", "config", "var", "logs", ".git"}


def roots_file() -> Path:
    return rig_home() / ROOTS_FILE


def _read_roots() -> list[dict]:
    path = roots_file()
    if not path.is_file():
        return []
    try:
        doc = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError:
        return []
    out = []
    for row in doc.get("roots") or []:
        if isinstance(row, dict) and row.get("path"):
            out.append({"path": str(row["path"]), "kind": str(row.get("kind") or "registry")})
        elif isinstance(row, str):
            out.append({"path": row, "kind": "registry"})
    return out


def _write_roots(rows: list[dict]) -> None:
    path = roots_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# rig catalog roots — registries and harvest trees `rig catalog` scans.\n"
                    "# rig appends what it touches (a registry it opens/imports into, a `fleet sync`\n"
                    "# tree, a reconstruct workspace); `rig catalog add|remove <dir>` edits by hand.\n"
                    + yaml.safe_dump({"roots": rows}, sort_keys=False))


def remember(path: Path | str, *, kind: str = "registry") -> None:
    """Append a root (idempotent by resolved path). Best-effort by contract: callers wrap it."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        return
    key = str(p.resolve())
    from .manifest import host_registry_dir
    shared = host_registry_dir()
    if shared and str(Path(shared).expanduser().resolve()) == key:
        return  # the box's shared registry is always a root — never written down
    rows = _read_roots()
    if any(str(Path(r["path"]).expanduser().resolve()) == key for r in rows):
        return
    rows.append({"path": str(p), "kind": kind})
    _write_roots(rows)


def roots() -> list[tuple[Path, str]]:
    """(path, kind) in scan order: the machine registry first, then the remembered roots, no
    duplicates (by resolved path)."""
    from .manifest import host_registry_dir, machine_data_dir
    from .userconfig import user_data_dir
    out: list[tuple[Path, str]] = []
    seen: set[str] = set()

    def _add(path: Path, kind: str) -> None:
        key = str(path.expanduser().resolve())
        if key in seen:
            return
        seen.add(key)
        out.append((path.expanduser(), kind))

    shared = host_registry_dir()
    if shared and Path(shared).expanduser().is_absolute():
        _add(Path(shared), "user" if user_data_dir() else "machine")
    machine = machine_data_dir()  # both, when the user's registry shadows the machine's
    if machine and Path(machine).expanduser().is_absolute():
        _add(Path(machine), "machine")
    for row in _read_roots():
        _add(Path(row["path"]), row["kind"])
    return out


def cmd_add(path: str, *, kind: str = "registry") -> int:
    p = Path(path).expanduser()
    if not p.is_dir():
        raise RigError(f"catalog add: {p} is not a directory")
    remember(p.resolve(), kind=kind)
    eprint(f"rig catalog: added {p.resolve()} ({kind})")
    return 0


def cmd_remove(path: str) -> int:
    key = str(Path(path).expanduser().resolve())
    rows = _read_roots()
    keep = [r for r in rows if str(Path(r["path"]).expanduser().resolve()) != key]
    if len(keep) == len(rows):
        raise RigError(f"catalog remove: {path} is not a remembered root (see `rig catalog roots`)")
    _write_roots(keep)
    eprint(f"rig catalog: removed {path}")
    return 0


def cmd_roots() -> int:
    rows = roots()
    if not rows:
        print("no catalog roots — `sudo rig provision --data-dir` names the machine registry; "
              "rig remembers registries and harvest trees as it touches them; "
              "`rig catalog add <dir>` adds one by hand")
        return 0
    for path, kind in rows:
        state = "" if path.is_dir() else "  (missing)"
        print(f"{kind:9} {path}{state}")
    return 0


# ---- the scan -----------------------------------------------------------------------------------

@dataclasses.dataclass
class Entry:
    run: str
    label: str
    vehicle: str
    vehicle_id: str
    state: str          # sealed | OPEN | unsealed | corrupt
    started: str
    ended: str
    disk_kb: int | None
    tags: tuple[str, ...]
    path: Path
    root: Path
    kind: str           # full | slim:<profile> | link
    replay_of: str | None = None

    def as_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["path"], d["root"] = str(self.path), str(self.root)
        d["tags"] = list(self.tags)
        return d


def _entry(run_dir: Path, root: Path, *, open_id: str | None) -> Entry | None:
    mpath = run_dir / "manifest.yaml"
    if not mpath.is_file():
        return None
    try:
        doc = load_yaml(mpath)
    except RigError:
        doc = {"corrupt": True}
    if not isinstance(doc, dict) or "run" not in doc and "corrupt" not in doc:
        return None
    from .runs import run_tags
    ended = str(doc.get("ended") or "")
    if doc.get("corrupt"):
        state = "corrupt"
    elif run_dir.name == open_id:
        state = "OPEN"
    else:
        state = "sealed" if ended else "unsealed"
    kind = "link" if run_dir.is_symlink() else "full"
    prov = run_dir / ".rig" / "export.yaml"
    if prov.is_file():
        try:
            profile = (load_yaml(prov).get("export") or {}).get("profile")
            kind = f"slim:{profile}" if profile else "slim"
        except RigError:
            kind = "slim"
    replay = doc.get("replay")
    return Entry(run=run_dir.name, label=str(doc.get("label") or "—"),
                 vehicle=str(doc.get("vehicle") or "?"), vehicle_id=str(doc.get("vehicle_id") or "?"),
                 state=state, started=str(doc.get("started") or "?"), ended=ended or "—",
                 disk_kb=doc.get("disk_kb") if isinstance(doc.get("disk_kb"), int) else None,
                 tags=run_tags(doc), path=run_dir, root=root, kind=kind,
                 replay_of=str(replay["of"]) if isinstance(replay, dict) and replay.get("of") else None)


def _is_run_dir(d: Path) -> bool:
    return (d / "manifest.yaml").is_file()


def scan_root(root: Path, kind: str, *, max_depth: int = 4) -> list[Entry]:
    """A registry (<root>/runs/<id>), a harvest tree (<root>/<label>/<vehicle>/<run>, any
    shape up to max_depth), or a run dir itself. Descent stops at a run dir (its own exports/
    are copies of it, not runs) and never enters data dirs."""
    out: list[Entry] = []
    if not root.is_dir():
        return out
    if _is_run_dir(root):
        e = _entry(root, root, open_id=None)
        return [e] if e else out
    open_id = None
    if (root / "runs").is_dir():
        from .runs import current_run
        try:
            cur = current_run(root)
            open_id = cur[0] if cur else None
        except RigError:
            open_id = None
        for d in sorted(root.glob("runs/*")):
            if d.is_dir() or d.is_symlink():
                e = _entry(d, root, open_id=open_id) if d.is_dir() else None
                if e:
                    out.append(e)
        return out
    base_depth = len(root.resolve().parts)
    for dirpath, dirnames, _ in os.walk(root):
        d = Path(dirpath)
        if d != root and _is_run_dir(d):
            e = _entry(d, root, open_id=None)
            if e:
                out.append(e)
            dirnames[:] = []
            continue
        depth = len(Path(dirpath).resolve().parts) - base_depth
        if depth >= max_depth:
            dirnames[:] = []
            continue
        dirnames[:] = sorted(n for n in dirnames if n not in _PRUNE and not n.startswith("."))
    return out


def scan(all_roots: list[tuple[Path, str]] | None = None) -> tuple[list[Entry], list[Path]]:
    """(entries newest first, missing roots). One run reachable through several roots (a linked
    registry entry, a nested root) appears once — by resolved path."""
    entries: list[Entry] = []
    missing: list[Path] = []
    seen: set[str] = set()
    for root, kind in (roots() if all_roots is None else all_roots):
        if not root.is_dir():
            missing.append(root)
            continue
        for e in scan_root(root, kind):
            key = str(e.path.resolve())
            if key in seen:
                continue
            seen.add(key)
            entries.append(e)
    entries.sort(key=lambda e: (e.started, e.run), reverse=True)
    return entries, missing


# ---- search -------------------------------------------------------------------------------------

def _date(text: str, what: str) -> str:
    """A date filter as a comparable ISO prefix: YYYY, YYYY-MM, YYYY-MM-DD[THH:MM]."""
    t = text.strip()
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            datetime.datetime.strptime(t, fmt)
            return t
        except ValueError:
            continue
    raise RigError(f"catalog: {what} takes a date (YYYY, YYYY-MM, YYYY-MM-DD, YYYY-MM-DDTHH:MM), "
                   f"not '{text}'")


def _tag_hit(entry_tags: tuple[str, ...], wanted: str) -> bool:
    """`site:` (a bare key) matches any tag with that key; otherwise exact."""
    if wanted.endswith(":"):
        return any(t.startswith(wanted) for t in entry_tags)
    return wanted in entry_tags


def select(entries: list[Entry], *, query: list[str], tags: list[str], label: str | None,
           vehicle: str | None, since: str | None, until: str | None,
           state: str | None) -> list[Entry]:
    out = []
    since_p = _date(since, "--since") if since else None
    until_p = _date(until, "--until") if until else None
    for e in entries:
        if tags and not all(_tag_hit(e.tags, t) for t in tags):
            continue
        if label and e.label != label and not e.label.startswith(f"{label}-"):
            continue
        if vehicle and vehicle not in (e.vehicle, e.vehicle_id):
            continue
        if since_p and e.started < since_p:
            continue
        if until_p and e.started[:len(until_p)] > until_p:
            continue
        if state and e.state.lower() != state.lower():
            continue
        if query:
            hay = " ".join([e.run, e.label, e.vehicle, e.vehicle_id, *e.tags, str(e.path)]).lower()
            if not all(q.lower() in hay for q in query):
                continue
        out.append(e)
    return out


def _size(kb: int | None) -> str:
    if kb is None:
        return "—"
    if kb < 1024:
        return f"{kb}K"
    return f"{kb / 1024:.0f}M" if kb < 1024 * 1024 else f"{kb / (1024 * 1024):.1f}G"


def _where(e: Entry) -> str:
    home = str(Path.home())
    p = str(e.path)
    if p.startswith(home + "/"):
        p = "~" + p[len(home):]
    return p


def cmd_search(*, query: list[str], tags: list[str], label: str | None, vehicle: str | None,
               since: str | None, until: str | None, state: str | None, as_json: bool,
               paths: bool) -> int:
    entries, missing = scan()
    for m in missing:
        eprint(f"rig catalog: root {m} is missing (unmounted? `rig catalog remove` to forget it)")
    hits = select(entries, query=query, tags=tags, label=label, vehicle=vehicle, since=since,
                  until=until, state=state)
    if as_json:
        print(json.dumps([e.as_dict() for e in hits], indent=2))
        return 0
    if paths:
        for e in hits:
            print(e.path)
        return 0
    if not hits:
        if not entries:
            print("no runs cataloged — `rig catalog roots` shows where it looks")
        else:
            print(f"no match ({len(entries)} run(s) cataloged)")
        return 0
    tagged = any(e.tags for e in hits)
    headers = ("VEHICLE", "RUN", "LABEL") + (("TAGS",) if tagged else ()) \
        + ("STATE", "STARTED", "SIZE", "WHERE")
    table = [headers]
    for e in hits:
        kind = "" if e.kind == "full" else f" [{e.kind}]"
        table.append((e.vehicle, e.run, e.label) + ((", ".join(e.tags) or "—",) if tagged else ())
                     + (e.state, e.started, _size(e.disk_kb), _where(e) + kind))
    widths = [max(len(row[i]) for row in table) for i in range(len(headers))]
    for row in table:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    eprint(f"{len(hits)} of {len(entries)} run(s)")
    return 0
