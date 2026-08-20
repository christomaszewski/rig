"""``rig pkg repin`` — advance a package's DECLARED dependency pins, registry-side (QoL F3).

The pin-advance half of the maintenance pair: ``rebase`` moves a fork's PAYLOAD onto its
parent's current version; ``repin`` moves a package's declared PINS onto the dependencies'
current versions and publishes the next patch version. Payloads are never touched here.

Kind behavior:
- **profile** — ``requires.service`` advances to the service's registry-current (or the
  ``--dep``-named) version; an exact pin is rewritten, a caret constraint keeps its caret with
  the base advanced (explicit intent wins even when the old caret already covered).
- **overlay** — the ``authored_against`` stamps are re-stamped to current; a missing stamp
  (pre-v0.1.59 packages) is stamped FRESH from the overlay's target (repin is the adoption
  moment). Best-effort key check: delta key paths absent from the new target profile's payload
  surface are WARNINGS listing the paths — the human judges semantics; never a blocker.
- **suite** — every member pin refreshes to its registry-current version (``--dep`` narrows to
  one member). An unresolvable member is a HARD error — a suite must never publish
  half-checked pins.
- **service** — refused: a service's dependency is its code (``promote --kind service`` / CI).

Publish scope is the shared write+validate envelope (local-dir in place; git as a local commit
on a ``promote/…`` branch — push stays explicit). Consumers follow with
``rig registry sync && rig pkg upgrade``.
"""
from __future__ import annotations

from . import RigError
from .common import eprint, load_yaml
from .promote import (_carry_forward, _existing_manifest, _next_version, _registry_write_session,
                      _target_entry, _write_pkg)
from .refs import parse_ref, split_key
from .registries import current_version_of
from .registry import _CONSTRAINT, _QUALIFIED_EXACT, load_registry


def _current_of(reg, ns: str | None, name: str) -> str | None:
    """Dependency's registry-current version: in-registry for bare/self refs, caches otherwise."""
    if ns is None or ns == reg.namespace:
        pkg = reg.packages.get(name)
        return pkg.version if pkg is not None else None
    return current_version_of(ns, name)


def _resolve_pkg(reg, ref: str) -> tuple[str, object]:
    """`[ns/]key[@ver]` (profiles: the short half alone is accepted when unambiguous)."""
    name = parse_ref(ref)[1]
    pkg = reg.packages.get(name)
    if pkg is not None:
        return name, pkg
    hits = [k for k in sorted(reg.packages) if split_key(k)[1] == name]
    if len(hits) == 1:
        return hits[0], reg.packages[hits[0]]
    if hits:
        raise RigError(f"repin: '{name}' is ambiguous: {', '.join(hits)} — use the full "
                       f"<service>:<short> key")
    raise RigError(f"repin: no package '{name}' in the target registry")


def repin(ref: str, *, to: str, dep: str | None = None, dry_run: bool = False) -> int:
    entry = _target_entry(to)
    reg_root = entry.root
    if not (reg_root / "registry.yaml").is_file():
        raise RigError(f"repin: registry '{to}' is not synced/reachable at {reg_root}")
    reg = load_registry(reg_root, [])
    name, pkg = _resolve_pkg(reg, ref)
    dep_ns, dep_name, dep_ver = parse_ref(dep) if dep else (None, None, None)
    if pkg.kind == "service":
        raise RigError("repin: a service's dependency is its CODE — publish a new pin with "
                       "`rig pkg promote --kind service` (or the repo's registry-release CI)")
    if pkg.kind == "vehicle":
        raise RigError("repin: a vehicle plan's rows are UNVERSIONED (its suite's members carry "
                       "the pins) — re-capture it with `rig pkg promote --all --suite <s> "
                       "--vehicle <name>`, and repin the SUITE to refresh member pins")

    existing = _existing_manifest(reg_root, pkg.kind + "s", name) or {}
    changes: list[str] = []       # human lines: "dep old -> new"
    warnings: list[str] = []

    if pkg.kind == "profile":
        manifest, payload = _repin_profile(reg, name, existing, dep_name, dep_ver, changes)
    elif pkg.kind == "overlay":
        manifest, payload = _repin_overlay(reg, pkg, name, existing, dep_name, dep_ver,
                                           changes, warnings)
    else:  # suite
        manifest, payload = _repin_suite(reg, name, existing, dep_name, dep_ver, changes)

    if not changes:
        eprint(f"rig pkg repin: '{to}/{name}' is already current — nothing to repin")
        return 0
    version = _next_version(existing, True, f"{pkg.kind} '{name}'")
    manifest["version"] = version
    for line in changes:
        eprint(f"  {line}")
    for line in warnings:
        eprint(f"  WARNING: {line}")
    if dry_run:
        eprint(f"rig pkg repin (dry-run): would publish {to}/{name}@{version} — nothing written")
        return 0
    written, backups = [], {}
    with _registry_write_session(entry, to, f"repin-{name.replace(':', '-')}", written, backups,
                                 what="repin"):
        _write_pkg(reg_root, pkg.kind + "s", name, manifest, payload, written, backups)
        eprint(f"  {pkg.kind} {to}/{name}@{version}: pins advanced")
    eprint("rig pkg repin: consumers follow with `rig registry sync && rig pkg upgrade`")
    return 0


def _need_current(reg, ns: str | None, name: str, *, what: str) -> str:
    current = _current_of(reg, ns, name)
    if current is None:
        raise RigError(f"repin: {what} '{f'{ns}/' if ns else ''}{name}' does not resolve in the "
                       f"configured registries — publishing needs a resolvable target "
                       f"(synced? rig registry sync)")
    return current


def _repin_profile(reg, name, existing, dep_name, dep_ver, changes) -> tuple[dict, None]:
    req = (existing.get("requires") or {}).get("service")
    match = _CONSTRAINT.match(str(req)) if isinstance(req, str) else None
    if not match:
        raise RigError(f"repin: profile '{name}' has no parseable requires.service ({req!r})")
    if dep_name and dep_name != match["name"]:
        raise RigError(f"repin: --dep '{dep_name}' does not match the profile's required "
                       f"service '{match['name']}'")
    target = dep_ver or _need_current(reg, match["ns"], match["name"], what="requires.service")
    prefix = f"{match['ns']}/" if match["ns"] else ""
    new_req = f"{prefix}{match['name']}@{match['caret']}{target}"
    if new_req != req:
        changes.append(f"requires: {req} -> {new_req}")
    manifest = {"kind": "profile", "name": split_key(name)[1], "version": "?",
                **_carry_forward(existing, "kind", "name", "version", "requires"),
                "requires": {"service": new_req}}
    return manifest, None  # payload untouched — pin advance only


def _repin_overlay(reg, opkg, name, existing, dep_name, dep_ver, changes,
                   warnings) -> tuple[dict, None]:
    stamps = existing.get("authored_against") if isinstance(existing.get("authored_against"),
                                                            dict) else {}
    new_stamps: dict = {}
    if not stamps:  # OQ-D: pre-stamp packages adopt the stamp here, from the declared target
        target = next((t for t in existing.get("targets") or []
                       if isinstance(t, dict) and t.get("service")), None)
        if target is None:
            raise RigError(f"repin: overlay '{name}' has neither authored_against stamps nor a "
                           f"service target — nothing to advance")
        ns, svc, _ = parse_ref(str(target["service"]))
        ns = ns or reg.namespace
        current = _need_current(reg, ns, svc, what="target service")
        new_stamps["service"] = f"{ns}/{svc}@{current}"
        changes.append(f"authored_against: (none) -> service {ns}/{svc}@{current} "
                       f"(fresh stamp — this overlay predates the provenance stamp)")
    for kind_of, ref in sorted(stamps.items()):
        smatch = _QUALIFIED_EXACT.match(str(ref))
        if not smatch:
            new_stamps[kind_of] = ref  # unparseable: carried verbatim, loudly
            warnings.append(f"authored_against.{kind_of} '{ref}' is not ns/name@X.Y.Z — kept as-is")
            continue
        if dep_name and smatch["name"] != dep_name and split_key(smatch["name"])[1] != dep_name:
            new_stamps[kind_of] = ref
            continue
        target = dep_ver or _need_current(reg, smatch["ns"], smatch["name"],
                                          what=f"authored_against.{kind_of}")
        new_ref = f"{smatch['ns']}/{smatch['name']}@{target}"
        new_stamps[kind_of] = new_ref
        if new_ref != ref:
            changes.append(f"authored_against.{kind_of}: {ref} -> {new_ref}")
    if changes:
        _overlay_key_check(reg, opkg, stamps, new_stamps, warnings)
    manifest = {"kind": "overlay", "name": name, "version": "?",
                **{k: v for k, v in existing.items()
                   if k not in ("kind", "name", "version", "authored_against")},
                "authored_against": new_stamps}
    return manifest, None


def _profile_pkg(reg, ns: str, name: str):
    """The CURRENT profile package for ns/name — in this registry or a configured one."""
    if ns == reg.namespace:
        return reg.packages.get(name)
    from .registries import resolve_namespace
    entry = resolve_namespace(ns)
    if entry is None:
        return None
    try:
        return load_registry(entry.root, []).packages.get(name)
    except RigError:
        return None


def _payload_surface(pkg) -> set | None:
    if pkg is None or pkg.kind != "profile":
        return None
    from .workingcopy import _flat
    payload_rel = (pkg.manifest.get("config") or {}).get("payload") or "config/payload.yaml"
    path = pkg.pkg_dir / str(payload_rel)
    if not path.is_file():
        return None
    try:
        return {p for p, _ in _flat(load_yaml(path))}
    except RigError:
        return None


def _overlay_key_check(reg, opkg, old_stamps, new_stamps, warnings) -> None:
    """Best-effort delta-surface check (warn-only by charter): delta keys that were ON the old
    profile surface but are GONE from the new one have VANISHED — the tuning no longer lands.
    Absent-from-new keys we can't verify against the old surface may simply be additions, so
    they warn softly. Every resolution failure is silent — the check is advisory."""
    from .workingcopy import _flat
    new_m = _QUALIFIED_EXACT.match(str(new_stamps.get("profile") or ""))
    if new_m is None:
        return
    new_surface = _payload_surface(_profile_pkg(reg, new_m["ns"], new_m["name"]))
    if new_surface is None:
        return
    delta_rel = (opkg.manifest.get("config") or {}).get("payload") or "config/delta.yaml"
    delta_path = opkg.pkg_dir / str(delta_rel)
    if not delta_path.is_file():
        return
    try:
        delta_paths = {p for p, _ in _flat(load_yaml(delta_path))}
    except RigError:
        return
    absent = sorted(p for p in delta_paths if p not in new_surface)
    if not absent:
        return
    old_surface = None
    old_m = _QUALIFIED_EXACT.match(str((old_stamps or {}).get("profile") or ""))
    if old_m is not None:
        old_pkg = _profile_pkg(reg, old_m["ns"], old_m["name"])
        if old_pkg is not None and old_pkg.version != old_m["ver"]:
            from .history import checkout_pkg
            from .registries import resolve_namespace
            entry = resolve_namespace(old_m["ns"])
            old_pkg = checkout_pkg(entry, "profiles", old_m["name"], old_m["ver"]) \
                if entry is not None else None
        old_surface = _payload_surface(old_pkg)
    if old_surface is not None:
        vanished = [p for p in absent if p in old_surface]
        if vanished:
            warnings.append(f"delta key(s) VANISHED from the profile surface: "
                            f"{', '.join(vanished[:6])} — the tuning no longer lands; review "
                            f"the delta before publishing onward")
    else:
        warnings.append(f"delta key(s) not on the new profile surface (old surface "
                        f"unavailable — they may be additions): {', '.join(absent[:6])}")


def _repin_suite(reg, name, existing, dep_name, dep_ver, changes) -> tuple[dict, None]:
    members = existing.get("members") if isinstance(existing.get("members"), dict) else None
    if members is None:
        raise RigError(f"repin: suite '{name}' has no members mapping")
    new_members: dict = {}
    for plural in ("vehicles", "services", "profiles", "overlays"):
        refreshed = []
        for ref in members.get(plural) or []:
            mmatch = _QUALIFIED_EXACT.match(str(ref))
            if not mmatch:
                raise RigError(f"repin: suite member '{ref}' is not ns/name@X.Y.Z")
            # EVERY member refreshes — the registry law pins in-registry members at head, so a
            # narrowed refresh could never validate; --dep only overrides the named member's
            # target version (its real use: cross-registry members, validated format-only).
            named = dep_name and (mmatch["name"] == dep_name
                                  or split_key(mmatch["name"])[1] == dep_name)
            target = dep_ver if (named and dep_ver) else \
                _need_current(reg, mmatch["ns"], mmatch["name"],
                              what=f"suite member ({plural.rstrip('s')})")
            new_ref = f"{mmatch['ns']}/{mmatch['name']}@{target}"
            refreshed.append(new_ref)
            if new_ref != str(ref):
                changes.append(f"member: {ref} -> {new_ref}")
        if refreshed:
            new_members[plural] = refreshed
    manifest = {"kind": "suite", "name": name, "version": "?",
                **_carry_forward(existing, "kind", "name", "version", "members"),
                "members": new_members}
    return manifest, None
