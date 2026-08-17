"""``rig pkg`` — package operations consulting the configured registries in priority order.

Search/info land here first; install/upgrade/lock/promote arrive with the lockfile and
working-copy pipeline. Results always show FULLY-QUALIFIED names (``public/siyi-zr30``) — the
qualifier is the consumer-side registry name from registries.yaml. A broken or too-new registry
degrades with a warning and the rest keep answering (OQ-8).
"""
from __future__ import annotations

import fnmatch

from . import RigError
from .common import eprint, print_table
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


def search(query: str) -> int:
    entries = _entries_or_hint()
    rows: list[tuple[str, str, str, str]] = []
    if query.startswith("sensor:"):
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
    if not rows:
        print(f"no matches for '{query}'")
        return 1
    # priority order is preserved: higher-priority registries printed first
    print_table([("PACKAGE", "KIND", "VERSION", "NOTE")] + rows)
    return 0


def list_installed(root) -> int:
    """`rig pkg list` — this deployment's installed packages (from rig.lock), which instances use
    each, and whether the synced registries carry a newer version (blank when a registry is
    unreachable — listing never fails on registry state)."""
    from .lock import load_lock
    from .manifest import load_manifest

    lock = load_lock(root)
    packages = lock.get("packages") or {}
    if not packages:
        print("no registry packages installed (rig.lock has no packages section) — "
              "`rig add <ref|sensor:id>` installs one")
        return 0
    manifest = load_manifest(root)
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

    rows = [("PACKAGE", "KIND", "ROLE", "USED BY", "UPGRADE")]
    body = [(ref, str((packages[ref] or {}).get("kind", "?"))) for ref in sorted(packages)]
    for ref, kind in sorted(body, key=lambda rk: (role(*rk).startswith("dependency"), rk[0])):
        rows.append((ref, kind, role(ref, kind), users(ref, kind), registry_current(ref)))
    print_table(rows)
    notes = []
    if any("*" in r[3] for r in rows[1:]):
        notes.append("* = local edits (upgrade three-way-merges them, local wins)")
    if any(r[4] for r in rows[1:]):
        notes.append("`rig registry sync && rig pkg upgrade` updates — profiles/services "
                     "three-way, bound overlays rebound in place")
    if notes:
        print("\n" + "\n".join(f"({n})" for n in notes))
    return 0


def info(ref: str, root=None) -> int:
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
        return 0
    raise RigError(f"pkg info: '{ref}' not found in any configured registry "
                   f"(synced? rig registry sync)")


def _parent_freshness(based_on: str, fork_name: str, fork_alias: str) -> str:
    """The consumer half of fork lineage: the parent's ns is a NAMESPACE — find the entry that
    declares it (fail-soft: offline/unsynced parents just show the bare stamp) and say when it
    has moved past the stamped version."""
    ns, pname, pinned = parse_ref(based_on)
    for entry in load_entries():
        try:
            reg, index = open_registry(entry)
        except RigError:
            continue
        if reg.namespace != ns and entry.name != ns:
            continue
        current = ((index.get("packages") or {}).get(pname) or {}).get("version")
        if current and current != pinned:
            return (f"  ({current} available — rig pkg rebase {fork_name} --to {fork_alias})")
        return ""
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
