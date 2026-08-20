"""``rig pkg`` — package operations consulting the configured registries in priority order.

Search/info land here first; install/upgrade/lock/promote arrive with the lockfile and
working-copy pipeline. Results always show FULLY-QUALIFIED names (``public/siyi-zr30``) — the
qualifier is the consumer-side registry name from registries.yaml. A broken or too-new registry
degrades with a warning and the rest keep answering (OQ-8).
"""
from __future__ import annotations

import fnmatch

from . import RigError
from .common import eprint, load_yaml, print_table
from .refs import parse_ref, unqualified
from .registries import Entry, load_entries, open_registry

_TIER_ORDER = {"exact": 0, "glob": 1, "fallback": 2}


def _entries_or_hint() -> list[Entry]:
    entries = load_entries()
    if not entries:
        raise RigError("no registries configured — `rig setup` adds the default public registry, "
                       "or `rig registry add <name> <url>`")
    return entries


def _each_index(entries):
    """Yield (entry, registry, index) per usable registry, warning-and-skipping the broken ones."""
    for entry in entries:
        try:
            reg, index = open_registry(entry)
        except RigError as exc:
            eprint(f"  (skipping '{entry.name}': {exc})")
            continue
        yield entry, reg, index


def _sensor_hits(index: dict, ident: str) -> list[tuple[str, str]]:
    """Profiles whose match identifiers cover `ident`, as (profile, tier) — exact key first, then
    glob keys that fnmatch it, then the '*' fallback."""
    hits: list[tuple[str, str]] = []
    for key, rows in (index.get("sensors") or {}).items():
        if fnmatch.fnmatch(ident, key):  # exact keys have no glob chars, so this covers ==
            hits.extend((row["profile"], row["tier"]) for row in rows)
    return sorted(set(hits), key=lambda h: (_TIER_ORDER.get(h[1], 3), h[0]))


def _axis_note(pkg) -> str:
    """The catalog row's NOTE: each kind's key discovery axis (what you'd search it by), never
    search-hit attribution — no-arg listings aren't 'hits'."""
    if pkg is None:
        return ""
    m = pkg.manifest
    if pkg.kind == "profile":
        for block in (m.get("provides") or {}).get("sensor") or []:
            if isinstance(block, dict) and block.get("match"):
                return f"match: {block['match'][0]}"
        req = (m.get("requires") or {}).get("service") if isinstance(m.get("requires"), dict) else None
        return f"requires: {req}" if req else ""
    if pkg.kind == "overlay":
        target = next((t for t in m.get("targets") or [] if isinstance(t, dict) and t), None)
        note = "target: {}={}".format(*next(iter(target.items()))) if target else ""
        if m.get("project"):
            note += (", " if note else "") + f"project: {m['project']}"
        return note
    if pkg.kind == "suite":
        members = m.get("members") or {}
        count = sum(len(members.get(plural) or []) for plural in ("services", "profiles", "overlays"))
        return f"{count} member{'s' if count != 1 else ''}"
    if pkg.kind == "service":
        source = m.get("source") if isinstance(m.get("source"), dict) else {}
        if source.get("repo"):
            return f"src: {str(source['repo']).rstrip('/').rsplit('/', 1)[-1]}"
        if isinstance(m.get("image"), dict) and m["image"].get("ref"):
            return "image"
    return ""


def search(query: str = "", *, kind: str | None = None, registry: str | None = None) -> int:
    entries = _entries_or_hint()
    if registry:
        entries = [e for e in entries if e.name == registry]
        if not entries:
            raise RigError(f"pkg search: no registry named '{registry}' (rig registry list)")
    rows: list[tuple[str, str, str, str]] = []
    if not query:  # the catalog: every package, priority order — the intentional "show me everything"
        for entry, reg, index in _each_index(entries):
            packages = index.get("packages") or {}
            for name in sorted(packages):
                meta = packages.get(name) or {}
                rows.append((f"{entry.name}/{name}", meta.get("kind", "?"),
                             meta.get("version", "?"), _axis_note(reg.packages.get(name))))
    elif query.startswith("sensor:"):
        ident = query[len("sensor:"):]
        for entry, _, index in _each_index(entries):
            packages = index.get("packages") or {}
            for profile, tier in _sensor_hits(index, ident):
                version = (packages.get(profile) or {}).get("version", "?")
                rows.append((f"{entry.name}/{profile}", "profile", version, f"match: {tier}"))
    elif query.startswith("project:"):
        tag = query[len("project:"):]
        for entry, _, index in _each_index(entries):
            packages = index.get("packages") or {}
            for name in (index.get("projects") or {}).get(tag, []):
                meta = packages.get(name) or {}
                rows.append((f"{entry.name}/{name}", meta.get("kind", "?"),
                             meta.get("version", "?"), f"project: {tag}"))
    elif ":" in query:  # <service>:[glob] — profiles by required service: "what drives ouster?"
        svc, _, want = query.partition(":")
        for entry, reg, _ in _each_index(entries):
            for key in sorted(reg.packages):
                p = reg.packages[key]
                ksvc, sep, short = key.partition(":")
                if p.kind != "profile" or ksvc != svc:  # the key's service half IS the target
                    continue                            # (placement law: dir == requires.service)
                if want and not fnmatch.fnmatch(short, want):
                    continue
                req = (p.manifest.get("requires") or {}).get("service")
                rows.append((f"{entry.name}/{key}", "profile", p.version, f"requires: {req}"))
    else:
        needle = query.lower()
        for entry, reg, index in _each_index(entries):
            packages = index.get("packages") or {}
            matched = {n: "" for n in packages if needle in n.lower()}
            for key, hit_rows in (index.get("sensors") or {}).items():
                if needle in key.lower():
                    matched.update((row["profile"], f"match: {key}") for row in hit_rows)
            for tag, names in (index.get("projects") or {}).items():
                if needle in tag.lower():
                    matched.update((n, f"project: {tag}") for n in names)
            for pname, pkg in reg.packages.items():  # overlays by TARGET: "what tunes camish?"
                if pkg.kind == "overlay" and any(
                        needle in str(v).lower() for t in (pkg.manifest.get("targets") or [])
                        if isinstance(t, dict) for v in t.values()):
                    matched.setdefault(pname, f"targets: {needle}")
                elif pkg.kind == "profile":  # profiles by SERVICE: "what drives ouster?"
                    req = (pkg.manifest.get("requires") or {}).get("service") \
                        if isinstance(pkg.manifest.get("requires"), dict) else None
                    if isinstance(req, str) and needle in req.lower():
                        matched.setdefault(pname, f"requires: {req}")
            for name in sorted(matched):
                meta = packages.get(name) or {}
                rows.append((f"{entry.name}/{name}", meta.get("kind", "?"),
                             meta.get("version", "?"), matched[name]))
    if kind:
        rows = [r for r in rows if r[1] == kind]
    if not rows:
        what = f"'{query}'" if query else "the catalog"
        filters = "".join(f" (--kind {kind})" if kind else "")
        print(f"no matches for {what}{filters}" if query or kind else
              "no packages in the configured registries (synced? rig registry sync)")
        return 1
    # priority order is preserved: higher-priority registries printed first
    print_table([("PACKAGE", "KIND", "VERSION", "NOTE")] + rows)
    return 0


def list_installed(root) -> int:
    """`rig pkg list` — the FULL deployment inventory: registry packages (from rig.lock) with
    upgrade state, PLUS locally-routed services (path/workspace adds, vendored checkouts) marked
    `local`/`unpublished` — the promotion worklist. Never fails on registry state (unreachable
    registries just blank the upgrade column; a registry-less deployment lists its local rows)."""
    import os as _os

    from .catalog import load_catalog
    from .lock import load_lock
    from .manifest import load_manifest

    lock = load_lock(root)
    packages = lock.get("packages") or {}
    manifest = load_manifest(root)
    try:
        catalog = load_catalog(root)
    except RigError:
        catalog = {}
    tracked = {unqualified(ref) for ref, info in packages.items()
               if (info or {}).get("kind") == "service"}
    local = sorted(name for name in catalog if name not in tracked)
    if not packages and not local:
        print("no packages in this deployment (rig.lock is empty and services.yaml routes "
              "nothing) — `rig add <path|ref|sensor:id>` adds one")
        return 0
    entries = {e.name: e for e in load_entries()}
    from .workingcopy import local_delta
    dirty = set()
    for s in manifest.sensors:  # `*` = local edits — exactly what upgrade will three-way
        state = local_delta(root, s)
        if state and (state[0] or state[1]):
            dirty.add(s.name)

    def users(ref: str, kind: str) -> str:
        name = unqualified(ref)
        if kind == "service":
            hits = [s.name for s in manifest.sensors if s.service == name]
        elif kind == "profile":
            hits = [s.name for s in manifest.sensors
                    if s.profile and unqualified(s.profile) == name]
        else:  # overlay: bound instances
            hits = [s.name for s in manifest.sensors if any(o == ref for o in s.overlays)]
        marked = [h + ("*" if h in dirty else "") for h in hits]
        return ", ".join(marked) or ("(dependency)" if kind == "service" else "—")

    def registry_current(ref: str) -> str:
        ns, name, pinned = parse_ref(ref)
        entry = entries.get(ns or "")
        if entry is None:
            return "registry gone"
        try:
            reg, index = open_registry(entry)
        except RigError:
            return ""
        current = ((index.get("packages") or {}).get(name) or {}).get("version")
        if current is None:
            return "gone from registry"
        return "" if current == pinned else f"{current} available"

    def role(ref: str, kind: str) -> str:
        """active = something you CHOSE (a pinned profile, a bound overlay, an installed suite,
        a bare-service instance's service); dependency = pulled in by a profile's requires."""
        if kind != "service":
            return "active"
        bare = unqualified(ref)
        if any(s.service == bare and not s.profile for s in manifest.sensors):
            return "active"
        holder = next((r for r, info in packages.items()
                       if (info or {}).get("kind") == "profile"
                       and unqualified(str(info.get("requires") or "")) == bare), None)
        return f"dependency of {holder}" if holder else "active"

    def local_origin(name: str) -> str:
        vendored = root / "services" / name / ".vendored.yaml"
        if vendored.is_file():
            try:
                ref = str(load_yaml(vendored).get("ref") or "")[:7]
            except RigError:
                ref = ""
            return f"vendored{f' @ {ref}' if ref else ''}"
        try:
            return _os.path.relpath(catalog[name].path, root)
        except ValueError:  # different drive (Windows) — absolute is still honest
            return str(catalog[name].path)

    rows = [("PACKAGE", "KIND", "ROLE", "USED BY", "UPGRADE")]
    body = [(ref, str((packages[ref] or {}).get("kind", "?"))) for ref in sorted(packages)]
    for ref, kind in sorted(body, key=lambda rk: (role(*rk).startswith("dependency"), rk[0])):
        rows.append((ref, kind, role(ref, kind), users(ref, kind), registry_current(ref)))
    for name in local:  # local rows sort together, after the registry ones — the promotion worklist
        rows.append((name, "service", f"local ({local_origin(name)})",
                     users(name, "service"), "unpublished"))
    print_table(rows)
    notes = []
    if any("*" in r[3] for r in rows[1:]):
        notes.append("* = local edits (upgrade three-way-merges them, local wins)")
    if any(r[4] and r[4] != "unpublished" for r in rows[1:]):
        notes.append("`rig registry sync && rig pkg upgrade` updates — profiles/services "
                     "three-way, bound overlays rebound in place")
    if local:
        notes.append("local = not tracked in any registry — `rig pkg promote --kind service` "
                     "publishes the code pointer; `--kind profile` captures an instance's config")
    if notes:
        print("\n" + "\n".join(f"({n})" for n in notes))
    return 0


def info(ref: str, root=None, versions: bool = False) -> int:
    entries = _entries_or_hint()
    ns, name, asked = parse_ref(ref)   # pkg info accepts @version like every other verb
    if ns:
        entries = [e for e in entries if e.name == ns]
        if not entries:
            raise RigError(f"pkg info: no registry named '{ns}' (rig registry list)")
    for entry, reg, _ in _each_index(entries):
        pkg = reg.packages.get(name)
        if pkg is None:
            continue
        m = pkg.manifest
        print(f"{entry.name}/{pkg.name}  {pkg.kind}  {pkg.version}")
        if asked and asked != pkg.version:
            print(f"  (you asked about @{asked} — the registry carries ONE current version; "
                  f"git-backed registries serve @{asked} via `rig pkg add {entry.name}/{name}"
                  f"@{asked}`)")
        print(f"  registry: {entry.name} ({entry.type}) at {entry.root}")
        _print_local_state(root, entry.name, name)
        if pkg.kind == "service":
            source, image = m.get("source") or {}, m.get("image") or {}
            if source:
                path = f" path={source.get('path')}" if source.get("path") else ""
                print(f"  source: {source.get('repo')} @ {str(source.get('rev'))[:12]}{path}")
            if image:
                print(f"  image: {image.get('ref')}")
            if m.get("ros_distro"):
                print(f"  ros_distro: {m['ros_distro']}")
        elif pkg.kind == "profile":
            for block in (m.get("provides") or {}).get("sensor") or []:
                print(f"  sensor: {block.get('model', '?')}  match: {', '.join(block.get('match', []))}")
            if (m.get("requires") or {}).get("service"):
                print(f"  requires: {m['requires']['service']}")
            if isinstance(m.get("based_on"), str):
                print(f"  based_on: {m['based_on']}{_parent_freshness(m['based_on'], name, entry.name)}")
            print(f"  payload: {(m.get('config') or {}).get('payload')}")
        elif pkg.kind == "overlay":
            targets = ["{}={}".format(*next(iter(t.items())))
                       for t in m.get("targets") or [] if isinstance(t, dict) and t]
            print(f"  targets: {', '.join(targets) or '?'}")
            if m.get("project"):
                print(f"  project: {m['project']}")
            stamp = m.get("authored_against") or {}
            if stamp:  # the staleness provenance (v0.1.59) — finally visible to consumers
                print("  authored_against: " + ", ".join(f"{k}: {v}" for k, v in stamp.items()))
            print(f"  delta: {(m.get('config') or {}).get('payload')}")
        elif pkg.kind == "suite":
            members = m.get("members") or {}
            for plural in ("services", "profiles", "overlays"):
                for ref_ in members.get(plural) or []:
                    print(f"  member: {ref_}")
        if versions:  # the v0.1.63 archive made @old INSTALLABLE; this makes it DISCOVERABLE
            from .history import kind_dir_of, list_versions
            rows = list_versions(entry, kind_dir_of(pkg.kind), name)
            if rows is None:
                print("  versions: current only — no git history (git-backed registries keep "
                      "every past version)")
            else:
                print("  versions:")
                for v, date, sha in rows:
                    mark = "  <- current" if v == pkg.version else ""
                    print(f"    {v}  {date}  {sha}{mark}")
        return 0
    raise RigError(f"pkg info: '{ref}' not found in any configured registry "
                   f"(synced? rig registry sync)")


def _parent_freshness(based_on: str, fork_name: str, fork_alias: str) -> str:
    """The consumer half of fork lineage: the parent's ns is a NAMESPACE — resolve it via the
    shared helper (fail-soft: offline/unsynced parents just show the bare stamp) and say when it
    has moved past the stamped version."""
    from .registries import current_version_of
    ns, pname, pinned = parse_ref(based_on)
    current = current_version_of(ns or "", pname)
    if current and current != pinned:
        return f"  ({current} available — rig pkg rebase {fork_name} --to {fork_alias})"
    return ""


def _print_local_state(root, ns: str, name: str) -> None:
    """One line on THIS deployment's relationship to the package (silent outside a deployment)."""
    if root is None:
        return
    from .lock import load_lock
    try:
        packages = load_lock(root).get("packages") or {}
    except RigError:
        return
    mine = [r for r in packages if parse_ref(r)[:2] == (ns, name)]
    if mine:
        print(f"  installed here: {mine[0]}")
