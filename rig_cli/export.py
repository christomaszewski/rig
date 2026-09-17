"""``rig run export`` — a SLIM copy of a sealed run, produced ON the vehicle by the services that
wrote the data, for the trip off it (`rig fleet sync --profile`).

A run dir is mostly two things that are already compressed — the cameras' lossless video and the
bag logger's zstd-chunked mcap — so compressing the directory as a whole gains nothing. What
shrinks a run is leaving data out (video) and having the writer of the rest re-write it smaller
(bags without the point clouds, at the small preset). Division of labor, the replay pattern in
reverse: a service that can slim its own data declares `export: {data}` in its rigging and owns
the mechanics behind its launcher's `export` verb; a deployment names the recipes in vehicle.yaml
`export_profiles:` — rig-level `omit` globs (paths left out entirely) plus per-service option
blocks rig hands over verbatim; rig resolves the run, lays the export tree out, dispatches, and
records what happened. rig stays schema-opaque about what a slim bag is.

Layout: `<run>/exports/<profile>/` mirrors the run. Everything kept is HARDLINKED (no disk cost
on the vehicle; a copy where linking fails), an exporter's `<data>` dir is left for its verb to
fill, and `.rig/export.yaml` names the profile, the source run, what was omitted and each
exporter's outcome — the provenance a harvested slim run carries. An export is a plain run dir
(manifest.yaml included), so `rig run import`, `rig runs` and the analysis tools take it as-is.
"""
from __future__ import annotations

import dataclasses
import datetime
import os
import re
import shutil
from pathlib import Path

import yaml

from . import RigError, dispatch, runs as runs_mod
from .common import eprint, load_yaml

EXPORTS_DIR = "exports"


# ---- the run ------------------------------------------------------------------------------------

def resolve_run(manifest, ref: str) -> tuple[str, Path]:
    """(run-id, run-dir): an id under the registry (the host's too), a label (newest run), or a
    path to a run dir. Refuses the OPEN run (a recorder may still be writing it); WARNs on an
    unsealed one."""
    run_id, run_dir = runs_mod.resolve_ref(manifest, ref, verb="run export")
    if runs_mod.is_open(manifest, run_dir):
        raise RigError(f"export: {run_id} is the OPEN run — a recorder may still be writing "
                       f"it; `rig down --end-run` first")
    if not (run_dir / "manifest.yaml").exists():
        raise RigError(f"export: {run_dir} is not a run dir (no manifest.yaml)")
    try:
        doc = load_yaml(run_dir / "manifest.yaml")
    except RigError:
        doc = {}
    if not doc.get("ended"):
        eprint(f"rig run export: warning: {run_id} is not sealed (`ended:` absent) — the "
               f"recording may be incomplete")
    return run_id, run_dir


# ---- the profile --------------------------------------------------------------------------------

def profile_for(manifest, name: str) -> dict:
    profiles = manifest.export_profiles or {}
    if name in profiles:
        return profiles[name]
    if not profiles:
        raise RigError("export: vehicle.yaml declares no `export_profiles:` — add one, e.g.\n"
                       "  export_profiles:\n"
                       "    review:\n"
                       "      omit: ['recordings/**/*.mkv']    # rig: paths left out entirely\n"
                       "      ros2-bag-logger: { preset: zstd_small, exclude: ['.*/points$'] }")
    raise RigError(f"export: no profile '{name}' — vehicle.yaml declares: "
                   f"{', '.join(sorted(profiles))}")


def options_for(profile: dict, row, data_name: str | None = None) -> dict:
    """The profile's block for one exporter: keyed by the data name / instance name (wins) or
    the service."""
    for key in dict.fromkeys((data_name or row.name, row.name, row.service)):
        block = profile.get(key)
        if isinstance(block, dict):
            return block
    return {}


# ---- exporters ----------------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Exporter:
    row: object       # the manifest row (Sensor) whose launcher does the work
    rel: str          # run-relative data dir (the descriptor's `data`, {name} filled)
    path: Path        # <run>/<rel>
    name: str = ""    # the DATA name — the {name} the run's dir carries: the row's own, or a
    #                   FOREIGN one (a run recorded by another deployment, an instance since
    #                   renamed) the service's row exports on its behalf (RIG_EXPORT_NAME)


def discover_exporters(manifest, descriptors, run_dir: Path) -> tuple[list[Exporter], list[str]]:
    """(exporters, notices). Data presence is the evidence, never the run's age or origin: a row
    whose rigging declares `export` claims its own data dir; any OTHER dir matching the service's
    pattern (`bags/{name}` -> bags/*) is exported by that service's first row under the dir's
    name — so a run recorded before this feature, by another deployment, or under an instance
    name this tree no longer has is still covered."""
    out: list[Exporter] = []
    notes: list[str] = []
    missing: dict[str, list[str]] = {}  # service -> "nothing under" notes, dropped when the
    #                                     service turns out to hold the data under another name
    claimed: set[str] = set()
    by_service: dict[str, tuple[object, list]] = {}
    for row in manifest.sensors:
        es = getattr(descriptors.get(row.service), "export_source", None)
        if es is not None:
            by_service.setdefault(row.service, (es, []))[1].append(row)
    for service, (es, rows) in by_service.items():
        for row in rows:
            rel = es.data_path(row.name)
            path = run_dir / rel
            if path.is_dir() and any(path.iterdir()):
                out.append(Exporter(row=row, rel=rel, path=path, name=row.name))
                claimed.add(rel)
            else:
                missing.setdefault(service, []).append(
                    f"{row.name}: nothing under {rel} in the run — not exported")
    for service, (es, rows) in by_service.items():
        head, marker, tail = es.data.partition("{name}")
        if not marker or tail or (head and not head.endswith("/")):
            continue  # only `<dir>/{name}` patterns enumerate
        parent = run_dir / head
        if not parent.is_dir():
            continue
        executor = next((r for r in rows if r.enabled), rows[0])
        for d in sorted(parent.iterdir()):
            rel = f"{head}{d.name}"
            if d.name.startswith(".") or not d.is_dir() or rel in claimed or not any(d.iterdir()):
                continue
            out.append(Exporter(row=executor, rel=rel, path=d, name=d.name))
            claimed.add(rel)
            missing.pop(service, None)  # the data IS here, under the run's own name
            notes.append(f"{rel}: no instance named '{d.name}' in this deployment — "
                         f"{executor.name} [{service}] handles it (the run's own naming)")
    for lines in missing.values():
        notes += lines
    return out, notes


# ---- omit globs ---------------------------------------------------------------------------------

def glob_to_regex(pattern: str) -> re.Pattern:
    """Run-relative glob: `**` = any number of path segments (including none), `*` / `?` within
    one segment. Anchored at the run root; a pattern with no `/` still has to match the whole
    relative path (say `**/*.mkv` for "anywhere")."""
    out = ""
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif c == "*":
            out += "[^/]*"
            i += 1
        elif c == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(c)
            i += 1
    return re.compile(out + r"\Z")


def omitted(rel: str, patterns: list[re.Pattern]) -> bool:
    return any(p.match(rel) for p in patterns)


# ---- the tree -----------------------------------------------------------------------------------

def plan(run_dir: Path, omit: list[str], data_dirs: list[str]) -> tuple[list[str], list[str], int]:
    """(kept, omitted, omitted_bytes): every file of the run by run-relative path, minus the
    run's own `exports/`, minus the exporters' data dirs (their verbs fill those), minus the
    profile's omit globs (counted, with their bytes, for the provenance)."""
    patterns = [glob_to_regex(g) for g in omit]
    owned = tuple(d.rstrip("/") + "/" for d in data_dirs)
    kept: list[str] = []
    left: list[str] = []
    left_bytes = 0
    for dirpath, dirnames, filenames in os.walk(run_dir):
        rel_dir = os.path.relpath(dirpath, run_dir)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
        if rel_dir == "":
            dirnames[:] = [d for d in dirnames if d != EXPORTS_DIR]  # never export the exports
        dirnames.sort()
        for name in sorted(filenames):
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if owned and rel.startswith(owned):
                continue
            if rel.startswith(".rig/export/"):
                continue  # an in-place rewrite's specs/scripts — the export writes its own there
            if omitted(rel, patterns):
                left.append(rel)
                try:
                    left_bytes += (run_dir / rel).lstat().st_size
                except OSError:
                    pass
                continue
            kept.append(rel)
    return kept, left, left_bytes


def materialize(run_dir: Path, dest: Path, kept: list[str]) -> tuple[int, int]:
    """Lay the kept files out under dest: hardlinks (a run and its export share a disk, so a
    kept file costs nothing), a copy where the link fails (another filesystem), symlinks kept as
    symlinks. Returns (linked, copied) counts."""
    linked = copied = 0
    for rel in kept:
        src = run_dir / rel
        dst = dest / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_symlink():
            os.symlink(os.readlink(src), dst)
            continue
        try:
            os.link(src, dst)
            linked += 1
        except OSError:
            shutil.copy2(src, dst)
            copied += 1
    return linked, copied


def tree_bytes(path: Path) -> int:
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for name in filenames:
            try:
                total += (Path(dirpath) / name).lstat().st_size
            except OSError:
                pass
    return total


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ---- the verb -----------------------------------------------------------------------------------

def in_place(manifest, descriptors, run_id: str, run_dir: Path, *, profile: str | None,
             recipe: dict, lossy: bool, force: bool, dry_run: bool) -> int:
    """`--in-place`: the exporters REWRITE their data inside the run (the bag logger: each
    session re-written at the preset, verified against the original's per-topic counts, then
    swapped in) — reclaiming disk instead of making a copy. No export tree, nothing omitted
    (`omit` globs never delete in place); options that DROP data need --lossy, and the launcher
    is the one that refuses without it (rig stays opaque about what a lossy option is). The run
    manifest records every rewrite (`rewrites:`), which is also the idempotency check."""
    run_dir = run_dir.resolve()  # an archived (linked) run rewrites where its bytes are
    exporters, notices = discover_exporters(manifest, descriptors, run_dir)
    for n in notices:
        eprint(f"rig run export: {n}")
    if recipe.get("omit"):
        eprint(f"rig run export: --in-place never deletes: the profile's omit globs "
               f"({', '.join(recipe['omit'])}) apply to export copies only")
    if not exporters:
        eprint("rig run export: nothing to rewrite — no service in this deployment declares "
               "`export:` for data this run holds")
        return 0
    mpath = run_dir / "manifest.yaml"
    try:
        doc = load_yaml(mpath)
    except RigError:
        doc = {"run": run_id}
    history = [h for h in (doc.get("rewrites") or []) if isinstance(h, dict)]
    label = profile or "recompress"
    todo = []
    for e in exporters:
        options = options_for(recipe, e.row, e.name)
        done = next((h for h in history if h.get("data") == e.rel and h.get("ok")
                     and (h.get("options") or {}) == options), None)
        if done is not None and not force:
            eprint(f"rig run export: {e.rel}: already rewritten with these options "
                   f"({done.get('at', '?')}) — --force to redo")
            continue
        todo.append((e, options))
    if dry_run or not todo:
        for e, options in todo:
            eprint(f"  {e.rel}: {e.row.name} [{e.row.service}] would rewrite in place "
                   f"({_fmt_bytes(tree_bytes(e.path))} now) with options {options or '{}'}"
                   + (" — LOSSY allowed" if lossy else ""))
        return 0

    env = dispatch.fleet_env(manifest, descriptors)
    env.update({"RIG_EXPORT_SOURCE": str(run_dir), "RIG_EXPORT_DEST": str(run_dir),
                "RIG_EXPORT_PROFILE": label, "RIG_EXPORT_INPLACE": "1"})
    if lossy:
        env["RIG_EXPORT_LOSSY"] = "1"
    if force:
        env["RIG_EXPORT_FORCE"] = "1"
    rc = 0
    records = []
    for e, options in todo:
        opt_path = run_dir / ".rig" / "export" / f"{e.name}.yaml"
        opt_path.parent.mkdir(parents=True, exist_ok=True)
        opt_path.write_text(yaml.safe_dump(options, sort_keys=False) if options else "{}\n")
        before = tree_bytes(e.path)
        e_env = dict(env, RIG_EXPORT_OPTIONS=str(opt_path), RIG_EXPORT_NAME=e.name)
        outcomes = dispatch.run_verb([(e.row, descriptors[e.row.service])], e_env, "export")
        code = outcomes[0].returncode if outcomes else 1
        after = tree_bytes(e.path)
        if code == 0 or after != before:  # a refusal that touched nothing is not history
            records.append({"at": datetime.datetime.now(datetime.timezone.utc)
                            .replace(microsecond=0).isoformat(),
                            "profile": label, "data": e.rel, "by": e.row.name,
                            "service": e.row.service, "options": options, "lossy": bool(lossy),
                            "bytes": {"before": before, "after": after}, "ok": code == 0})
        if code != 0:
            eprint(f"rig run export: {e.rel}: in-place rewrite failed (exit {code}) — the "
                   f"original data stays wherever a session did not verify")
            rc = 1
        else:
            saved = before - after
            eprint(f"rig run export: {e.rel}: {_fmt_bytes(before)} -> {_fmt_bytes(after)}"
                   + (f" ({_fmt_bytes(saved)} reclaimed)" if saved > 0 else ""))
    try:
        doc = load_yaml(mpath)
    except RigError:
        doc = {"run": run_id}
    doc["rewrites"] = [h for h in (doc.get("rewrites") or []) if isinstance(h, dict)] + records
    if "disk_kb" in doc:
        doc["disk_kb"] = (tree_bytes(run_dir) + 1023) // 1024
    mpath.write_text(yaml.safe_dump(doc, sort_keys=False))
    return rc


def cmd(manifest, catalog, descriptors, root: Path, *, run_ref: str, profile: str | None,
        force: bool = False, dry_run: bool = False, in_place_mode: bool = False,
        lossy: bool = False) -> int:
    run_id, run_dir = resolve_run(manifest, run_ref)
    if in_place_mode:
        recipe = profile_for(manifest, profile) if profile else {}
        return in_place(manifest, descriptors, run_id, run_dir, profile=profile, recipe=recipe,
                        lossy=lossy, force=force, dry_run=dry_run)
    if lossy:
        raise RigError("run export: --lossy only means something with --in-place (an export "
                       "copy never touches the run)")
    recipe = profile_for(manifest, profile)
    omit = list(recipe.get("omit") or [])
    exporters, notices = discover_exporters(manifest, descriptors, run_dir)
    for n in notices:
        eprint(f"rig run export: {n}")
    unknown = [k for k in recipe if k != "omit"
               and not any(k in (row.name, row.service) for row in manifest.sensors)]
    for k in unknown:
        eprint(f"rig run export: warning: profile '{profile}' names '{k}', which is neither a "
               f"service nor an instance of this deployment — its options apply to nothing")

    dest = run_dir / EXPORTS_DIR / profile
    if dest.exists():
        if not force:
            raise RigError(f"export: {run_id} already has a '{profile}' export "
                           f"({dest}) — --force to redo it")
        if not dry_run:
            shutil.rmtree(dest)
    kept, left, left_bytes = plan(run_dir, omit, [e.rel for e in exporters])

    eprint(f"rig run export: {run_id} -> {profile}: {len(kept)} file(s) kept, {len(left)} "
           f"omitted ({_fmt_bytes(left_bytes)})"
           + (f"; exporters: {', '.join(e.name for e in exporters)}" if exporters else
              "; no exporters"))
    if dry_run:
        for e in exporters:
            eprint(f"  {e.row.name} [{e.row.service}]: export {e.rel} with options "
                   f"{options_for(recipe, e.row, e.name) or '{}'}")
        return 0

    dest.mkdir(parents=True, exist_ok=True)
    linked, copied = materialize(run_dir, dest, kept)

    env = dispatch.fleet_env(manifest, descriptors)
    env["RIG_EXPORT_SOURCE"] = str(run_dir)
    env["RIG_EXPORT_DEST"] = str(dest)
    env["RIG_EXPORT_PROFILE"] = profile
    if force:
        env["RIG_EXPORT_FORCE"] = "1"
    services: dict[str, dict] = {}
    rc = 0
    for e in exporters:
        options = options_for(recipe, e.row, e.name)
        opt_path = dest / ".rig" / "export" / f"{e.name}.yaml"
        opt_path.parent.mkdir(parents=True, exist_ok=True)
        opt_path.write_text(yaml.safe_dump(options, sort_keys=False) if options else "{}\n")
        (dest / e.rel).mkdir(parents=True, exist_ok=True)
        e_env = dict(env)
        e_env["RIG_EXPORT_OPTIONS"] = str(opt_path)
        e_env["RIG_EXPORT_NAME"] = e.name
        outcomes = dispatch.run_verb([(e.row, descriptors[e.row.service])], e_env, "export")
        code = outcomes[0].returncode if outcomes else 1
        services[e.name] = {"service": e.row.service, "data": e.rel, "rc": code,
                            "options": options}
        if code != 0:
            eprint(f"rig run export: {e.name} [{e.row.service}] export failed (exit {code})")
            rc = 1

    src_bytes = tree_bytes(run_dir) - tree_bytes(run_dir / EXPORTS_DIR)
    exp_bytes = tree_bytes(dest)
    rig_dir = dest / ".rig"
    rig_dir.mkdir(parents=True, exist_ok=True)
    (rig_dir / "export.yaml").write_text(yaml.safe_dump({"export": {
        "profile": profile,
        "of": run_id,
        "source": str(run_dir),
        "created": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat(),
        "omit": omit,
        "omitted": {"files": len(left), "bytes": left_bytes},
        "kept": {"files": len(kept), "linked": linked, "copied": copied},
        "services": services,
        "bytes": {"source": src_bytes, "export": exp_bytes},
        "ok": rc == 0,
    }}, sort_keys=False))
    ratio = f"{src_bytes / exp_bytes:.1f}x" if exp_bytes else "∞"
    eprint(f"rig run export: {dest} — {_fmt_bytes(exp_bytes)} from {_fmt_bytes(src_bytes)} "
           f"({ratio}){' — with failures' if rc else ''}")
    return rc
