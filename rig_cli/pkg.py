"""``rig pkg`` — package operations consulting the configured registries in priority order.

Search/info land here first; install/upgrade/lock/promote arrive with the lockfile and
working-copy pipeline. Results always show FULLY-QUALIFIED names (``public/siyi-zr30``) — the
qualifier is the consumer-side registry name from registries.yaml. A broken or too-new registry
degrades with a warning and the rest keep answering (OQ-8).
"""
from __future__ import annotations

import fnmatch

from . import RigError
from .common import eprint
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
        if key == ident or (key != ident and fnmatch.fnmatch(ident, key)):
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
    else:
        needle = query.lower()
        for entry, _, index in _each_index(entries):
            packages = index.get("packages") or {}
            matched = {n for n in packages if needle in n.lower()}
            for key, hit_rows in (index.get("sensors") or {}).items():
                if needle in key.lower():
                    matched.update(row["profile"] for row in hit_rows)
            for name in sorted(matched):
                meta = packages.get(name) or {}
                rows.append((f"{entry.name}/{name}", meta.get("kind", "?"),
                             meta.get("version", "?"), ""))
    if not rows:
        print(f"no matches for '{query}'")
        return 0
    widths = [max(len(r[i]) for r in rows) for i in range(4)]
    for r in rows:  # priority order is preserved: higher-priority registries printed first
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(r)).rstrip())
    return 0


def info(ref: str) -> int:
    entries = _entries_or_hint()
    ns, _, name = ref.rpartition("/")
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
        print(f"  registry: {entry.name} ({entry.type}) at {entry.root}")
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
            print(f"  requires: {(m.get('requires') or {}).get('service')}")
            print(f"  payload: {(m.get('config') or {}).get('payload')}")
        elif pkg.kind == "overlay":
            targets = ["{}={}".format(*next(iter(t.items())))
                       for t in m.get("targets") or [] if isinstance(t, dict) and t]
            print(f"  targets: {', '.join(targets) or '?'}")
            if m.get("project"):
                print(f"  project: {m['project']}")
            print(f"  delta: {(m.get('config') or {}).get('payload')}")
        elif pkg.kind == "suite":
            members = m.get("members") or {}
            for plural in ("services", "profiles", "overlays"):
                for ref_ in members.get(plural) or []:
                    print(f"  member: {ref_}")
        return 0
    raise RigError(f"pkg info: '{ref}' not found in any configured registry "
                   f"(synced? rig registry sync)")
