"""``rig run diff A B`` — what differs between two runs' RECORDS. Read-only.

A run directory already says what ran: ``manifest.yaml`` (rig version, vehicle, stacks, the
simulation block), the config snapshot of each `up` under ``.rig/config/<digest>/`` (vehicle.yaml,
vars.yaml with each value's source tier, the machine file, rig.lock with packages and image
digests, the rendered per-instance configs) and ``.rig/images.yaml``. This compares two of them,
section by section, and sorts every difference into one of three kinds:

- **differs** — a difference of substance: a package version, an image, a seed, a setting.
- **index digest only** — an image whose multi-architecture INDEX digest differs while its
  per-architecture digests are the same. A build attestation makes the index new on every
  build, so the index says nothing about whether the image changed; the per-architecture
  digests in the captured rig.lock (`build_catalog.<ref>.children`) do. Reported, not counted.
- **per run** — what is different for any two runs: id, label, times, the snapshot's name,
  the capture hash, the session fingerprint, a replay's startup timings. Reported apart, not
  counted.

A run with several snapshots is compared at its LAST `up`. What a run recorded while it ran
(bags, recordings, graph, container logs, simulation probes) is data, not configuration, and
is not compared. Exit status follows diff: 0 no difference of substance, 1 a difference,
2 trouble.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from . import RigError
from .common import eprint, load_yaml

# manifest.yaml keys that are different for any two runs
_PER_RUN = ("run", "label", "started", "ended", "disk_kb", "tags", "deployment", "config",
            "capture.sha256", "simulation.fingerprint", "config_dirty_at_seal")
_LOGS = ("ups", "docker_logs")  # timestamped logs of what was done to the run: not compared
_RIG_AND_VEHICLE = ("rig_version", "vehicle", "vehicle_id", "artifact")
_TREE_FILES = ("vehicle.yaml", "services.yaml", "fleet.yaml", "vehicle.local.yaml",
               "machine.local.yaml")
NOT_COMPARED = ("the log of ups and container-log captures", "recorded data (bags, recordings, "
                "graph)", "container logs", "simulation probes", "the captured tree "
                "(.rig/artifact.tar.gz)")
_BLOCKS = ("simulation", "replay")  # what a session was: held whole by one kind of run only
_MISSING = object()
_DIGEST = re.compile(r"\b(sha256:)?([0-9a-f]{12})[0-9a-f]{28,52}\b")


def _per_run(key: str) -> bool:
    """Manifest keys that differ for any two runs: identity, timestamps, a session's
    fingerprint, and a replay's startup timings and verification counters."""
    last = key.rsplit(".", 1)[-1]
    return (key in _PER_RUN or last in ("at", "fingerprint") or last.endswith("_unix_s")
            or key.startswith(("replay.startup.", "replay.verification.")))


def _flat(value, prefix: str = "") -> dict:
    """A document as {dotted.key: leaf}. A list of mappings that all carry a distinct `name`
    is keyed by it (`sensors[cam_front].config`), so one changed row reads as one changed row;
    any other list is a leaf and is compared whole."""
    if isinstance(value, dict) and value:
        out: dict = {}
        for key, sub in value.items():
            out.update(_flat(sub, f"{prefix}.{key}" if prefix else str(key)))
        return out
    if isinstance(value, list) and value and all(isinstance(v, dict) and "name" in v for v in value):
        names = [str(v["name"]) for v in value]
        if len(set(names)) == len(names):
            out = {}
            for name, sub in zip(names, value):
                out.update(_flat({k: v for k, v in sub.items() if k != "name"} or {"name": name},
                                 f"{prefix}[{name}]"))
            return out
    return {prefix: value}


class _Report:
    def __init__(self) -> None:
        self.differs: list[dict] = []
        self.index_only: dict[str, dict] = {}
        self.per_run: list[dict] = []
        self.sections: list[str] = []   # every section compared, in order
        self.notes: list[str] = []

    def section(self, name: str) -> None:
        if name not in self.sections:
            self.sections.append(name)

    def change(self, section: str, key: str, a, b) -> None:
        self.section(section)
        self.differs.append({"section": section, "key": key,
                             "a": None if a is _MISSING else a, "b": None if b is _MISSING else b})

    def compare(self, section: str, a, b) -> None:
        """Every leaf of two documents, by key."""
        self.section(section)
        fa, fb = _flat(a) if a else {}, _flat(b) if b else {}
        for key in sorted(set(fa) | set(fb)):
            va, vb = fa.get(key, _MISSING), fb.get(key, _MISSING)
            if va != vb:
                self.change(section, key, va, vb)

    @property
    def same(self) -> list[str]:
        changed = {c["section"] for c in self.differs}
        return [name for name in self.sections if name not in changed]


def _load(path: Path):
    return load_yaml(path) if path.is_file() else None


def _read_run(run_dir: Path) -> dict:
    mpath = run_dir / "manifest.yaml"
    if not mpath.is_file():
        raise RigError(f"run diff: {run_dir} has no manifest.yaml — not a run directory")
    manifest = load_yaml(mpath)
    ups = [u for u in (manifest.get("ups") or []) if isinstance(u, dict)]
    digest = manifest.get("config") or (ups[-1].get("config") if ups else None)
    snap = run_dir / ".rig" / "config" / str(digest) if digest else None
    if snap is not None and not snap.is_dir():
        snap = None
    images = _load(run_dir / ".rig" / "images.yaml") or {}
    return {
        "dir": run_dir, "manifest": manifest, "snap": snap, "digest": digest,
        "snapshots": len({str(u.get("config")) for u in ups if u.get("config")}),
        "last_stacks": ups[-1].get("stacks") if ups else None,
        "lock": (_load(snap / "rig.lock") if snap else None) or {},
        "images": images.get("images") or {},
    }


def _packages(lock: dict) -> dict[str, tuple[str, dict]]:
    """rig.lock's `packages` ({`registry/name@version`: detail}) by name."""
    out: dict[str, tuple[str, dict]] = {}
    for ref, detail in (lock.get("packages") or {}).items():
        name, _, version = str(ref).rpartition("@")
        out[name or str(ref)] = (version, detail if isinstance(detail, dict) else {})
    return out


def _children(lock: dict, ref: str) -> dict:
    entry = (lock.get("build_catalog") or {}).get(ref)
    children = entry.get("children") if isinstance(entry, dict) else None
    return children if isinstance(children, dict) else {}


def _compare_lock(report: _Report, a: dict, b: dict) -> None:
    la, lb = a["lock"], b["lock"]
    if bool(la) != bool(lb):
        report.change("rig.lock", "(file)", "yes" if la else "no", "yes" if lb else "no")
        return
    if not la:
        return
    report.section("packages")
    pa, pb = _packages(la), _packages(lb)
    for name in sorted(set(pa) | set(pb)):
        (va, da), (vb, db) = pa.get(name, (_MISSING, {})), pb.get(name, (_MISSING, {}))
        if va != vb:
            report.change("packages", name, va, vb)
        fa, fb = _flat(da) if da else {}, _flat(db) if db else {}
        for key in sorted(set(fa) | set(fb)):
            if fa.get(key, _MISSING) != fb.get(key, _MISSING):
                report.change("packages", f"{name} {key}", fa.get(key, _MISSING), fb.get(key, _MISSING))
    report.section("images")
    ca, cb = la.get("build_catalog") or {}, lb.get("build_catalog") or {}
    for ref in sorted(set(ca) | set(cb)):
        ea, eb = ca.get(ref), cb.get(ref)
        if not isinstance(ea, dict) or not isinstance(eb, dict):
            if ea != eb:
                report.change("images", ref, "built" if ea else _MISSING, "built" if eb else _MISSING)
            continue
        kids_a, kids_b = _children(la, ref), _children(lb, ref)
        for arch in sorted(set(kids_a) | set(kids_b)):  # the image itself, per architecture
            if kids_a.get(arch, _MISSING) != kids_b.get(arch, _MISSING):
                report.change("images", f"{ref} {arch}", kids_a.get(arch, _MISSING),
                              kids_b.get(arch, _MISSING))
        if ea.get("digest") != eb.get("digest"):
            if kids_a and kids_a == kids_b:  # a new index over the same image
                report.index_only.setdefault(ref, {"section": "images", "key": ref,
                                                   "a": ea.get("digest"), "b": eb.get("digest")})
            elif not (kids_a and kids_b):  # nothing recorded says what is under the index
                report.change("images", f"{ref} digest", ea.get("digest"), eb.get("digest"))
        fa = _flat({k: v for k, v in ea.items() if k not in ("children", "digest")})
        fb = _flat({k: v for k, v in eb.items() if k not in ("children", "digest")})
        for key in sorted(set(fa) | set(fb)):
            if fa.get(key, _MISSING) != fb.get(key, _MISSING):
                report.change("images", f"{ref} {key}", fa.get(key, _MISSING), fb.get(key, _MISSING))
    for key in sorted((set(la) | set(lb)) - {"packages", "build_catalog", "schema"}):
        report.compare("registries" if key == "registries" else f"lock {key}",
                       {key: la.get(key)}, {key: lb.get(key)})


def _lock_names(lock: dict, ref: str, held) -> bool:
    """Does this run's rig.lock record the very digest its daemon held for `ref`? Only then do
    the lock's per-architecture digests describe the image that ran: the daemon is asked
    independently of the lock, and a lock can be left behind by an image replaced after it."""
    entry = (lock.get("build_catalog") or {}).get(ref)
    digest = entry.get("digest") if isinstance(entry, dict) else None
    return isinstance(held, str) and isinstance(digest, str) and held.rsplit("@", 1)[-1] == digest


def _compare_images(report: _Report, a: dict, b: dict) -> None:
    """.rig/images.yaml: the digest the docker daemon held for each ref when the run opened —
    an INDEX digest for a multi-architecture image. Two different digests are the same image
    only where the record shows it: each run's lock names the digest its daemon held, and the
    two locks list the same per-architecture digests. Anything less is a difference."""
    if not a["images"] and not b["images"]:
        return
    report.section("images")
    for ref in sorted(set(a["images"]) | set(b["images"])):
        va, vb = a["images"].get(ref, _MISSING), b["images"].get(ref, _MISSING)
        if va == vb:
            continue
        kids_a, kids_b = _children(a["lock"], ref), _children(b["lock"], ref)
        unnamed = [name for name, run, held in (("A", a, va), ("B", b, vb))
                   if not _lock_names(run["lock"], ref, held)]
        if not isinstance(va, str) or not isinstance(vb, str) or not (kids_a and kids_b) or unnamed:
            # What the two LOCKS say about this ref is no longer a statement about what ran.
            report.index_only.pop(ref, None)
            report.change("images", ref, va, vb)
        if not isinstance(va, str) or not isinstance(vb, str):  # a run with no digest for it
            continue
        if not (kids_a and kids_b):  # nothing recorded says what is under either index
            continue
        if unnamed:
            who = " and ".join(unnamed)
            report.notes.append(
                f"{ref}: {who}'s rig.lock records another digest for it than the daemon held "
                f"when the run opened, so the lock's per-architecture record does not describe "
                f"the image that ran — counted as a difference")
        elif kids_a == kids_b:
            report.index_only[ref] = {"section": "images", "key": ref, "a": va, "b": vb}
        # else: a per-architecture change, already reported from the lock


def compare(run_a: Path, run_b: Path) -> dict:
    """The report for two run directories, as plain data (the --json form)."""
    a, b = _read_run(run_a), _read_run(run_b)
    report = _Report()
    for name, run in (("A", a), ("B", b)):
        if run["snapshots"] > 1:
            report.notes.append(f"{name} holds {run['snapshots']} config snapshots; it is compared "
                                f"at its last up ({run['digest']})")
    ma, mb = a["manifest"], b["manifest"]
    held_by_one = set()
    for block in _BLOCKS:  # a simulation session against a replay: say that once, not per key
        in_a, in_b = isinstance(ma.get(block), dict), isinstance(mb.get(block), dict)
        if in_a != in_b:
            report.change(block, "(recorded)", "yes" if in_a else "no", "yes" if in_b else "no")
            held_by_one.add(block)
    fa, fb = (_flat({k: v for k, v in m.items() if k not in _LOGS and k not in held_by_one})
              for m in (ma, mb))
    for key in sorted(set(fa) | set(fb)):
        va, vb = fa.get(key, _MISSING), fb.get(key, _MISSING)
        per_run = _per_run(key)
        top = key.split(".", 1)[0]
        section = ("run" if per_run else "rig and vehicle" if top in _RIG_AND_VEHICLE + ("capture",)
                   else "stacks" if top == "stacks" else top if top in ("simulation", "replay")
                   else "run manifest")
        shown = key.split(".", 1)[1] if section in ("simulation", "replay") and "." in key else key
        if per_run:
            if va != vb:
                report.per_run.append({"section": "run", "key": key,
                                       "a": None if va is _MISSING else va,
                                       "b": None if vb is _MISSING else vb})
            continue
        report.section(section)
        if va != vb:
            report.change(section, shown, va, vb)
    report.section("stacks")
    if a["last_stacks"] != b["last_stacks"]:
        report.change("stacks", "at last up", a["last_stacks"], b["last_stacks"])

    if bool(a["snap"]) != bool(b["snap"]):
        report.change("config snapshot", "(recorded)", "yes" if a["snap"] else "no",
                      "yes" if b["snap"] else "no")
    elif a["snap"]:
        sa, sb = a["snap"], b["snap"]
        for name in _TREE_FILES:
            da, db = _load(sa / name), _load(sb / name)
            if da is None and db is None:
                continue
            if (da is None) != (db is None):
                report.change(name, "(file)", "no" if da is None else "yes", "no" if db is None else "yes")
                continue
            report.compare(name, da, db)
        va, vb = _load(sa / "vars.yaml") or {}, _load(sb / "vars.yaml") or {}
        report.compare("vars", va.get("vars") or {}, vb.get("vars") or {})
        report.compare("env", va.get("env") or {}, vb.get("env") or {})
        if ("sources" in va) != ("sources" in vb):  # an older rig recorded values without them
            report.change("value sources", "(recorded)", "yes" if "sources" in va else "no",
                          "yes" if "sources" in vb else "no")
        elif "sources" in va:
            report.compare("value sources", va.get("sources") or {}, vb.get("sources") or {})
        ra = {p.stem: p for p in sorted((sa / "rendered").glob("*.yaml"))}
        rb = {p.stem: p for p in sorted((sb / "rendered").glob("*.yaml"))}
        for stem in sorted(set(ra) | set(rb)):
            if stem not in ra or stem not in rb:
                report.change(f"rendered {stem}", "(file)", "yes" if stem in ra else "no",
                              "yes" if stem in rb else "no")
                continue
            report.compare(f"rendered {stem}", _load(ra[stem]), _load(rb[stem]))
        _compare_lock(report, a, b)
    _compare_images(report, a, b)

    def ident(run: dict) -> dict:
        m = run["manifest"]
        return {"run": str(m.get("run") or run["dir"].name), "vehicle": m.get("vehicle"),
                "label": m.get("label"), "started": m.get("started"), "path": str(run["dir"])}

    def order(change: dict) -> int:  # run, label and times first: what a person looks for
        return _PER_RUN.index(change["key"]) if change["key"] in _PER_RUN else len(_PER_RUN)

    return {"a": ident(a), "b": ident(b), "substantive": bool(report.differs),
            "differs": report.differs, "index_only": list(report.index_only.values()),
            "per_run": sorted(report.per_run, key=order), "same": report.same, "notes": report.notes,
            "not_compared": list(NOT_COMPARED)}


def _show(value) -> str:
    """A value for a person: digests and commits cut to 12 hex, long text cut short."""
    text = "(none)" if value is None else \
        json.dumps(value, default=str) if isinstance(value, (dict, list)) else str(value)
    text = _DIGEST.sub(lambda m: (m.group(1) or "") + m.group(2), text)
    return text if len(text) <= 96 else text[:95] + "…"


def render(doc: dict) -> str:
    lines = ["rig run diff"]
    for name in ("a", "b"):
        run = doc[name]
        lines.append(f"  {name.upper()}  {run['run']}  (vehicle {run.get('vehicle') or '—'}, "
                     f"label {run['label'] or '—'}, started {run['started'] or '?'})")
    lines += [f"  note: {note}" for note in doc["notes"]]
    lines.append("")
    if doc["differs"]:
        lines.append("differs")
        section = None
        for change in doc["differs"]:
            if change["section"] != section:
                section = change["section"]
                lines.append(f"  {section}")
            lines.append(f"    {change['key']}: {_show(change['a'])} -> {_show(change['b'])}")
    else:
        lines.append("no difference of substance")
    if doc["same"]:
        lines += ["", "same", "  " + ", ".join(doc["same"])]
    if doc["index_only"]:
        lines += ["", "index digest only (the same image for every recorded architecture; a build "
                      "or push made a new index)"]
        lines += [f"  {c['key']}: {_show(c['a'])} -> {_show(c['b'])}" for c in doc["index_only"]]
    if doc["per_run"]:
        lines += ["", "per run, not counted", "  " + ", ".join(c["key"] for c in doc["per_run"])]
    lines += ["", "not compared", "  " + "; ".join(doc["not_compared"])]
    return "\n".join(lines) + "\n"


def _resolve(root: Path | None, ref: str) -> Path:
    """A run-dir PATH anywhere; inside a deployment also an id or a label, as the run verbs
    take them (this registry, then the host's)."""
    path = Path(ref).expanduser()
    if path.is_dir():
        return path
    if "/" in ref:
        raise RigError(f"run diff: no run directory at {path}")
    if root is None:
        raise RigError(f"run diff: no run directory at '{ref}' — an id or a label is looked up "
                       f"in a deployment's run registry: run this inside one, or pass paths")
    from .manifest import load_manifest
    from .runs import resolve_ref
    return resolve_ref(load_manifest(root), ref, verb="run diff")[1]


def cmd(root: Path | None, ref_a: str, ref_b: str, *, as_json: bool = False) -> int:
    try:
        doc = compare(_resolve(root, ref_a), _resolve(root, ref_b))
    except RigError as exc:
        eprint(f"rig: {exc}")
        return 2
    sys.stdout.write(json.dumps(doc, indent=2, default=str) + "\n" if as_json else render(doc))
    return 1 if doc["substantive"] else 0
