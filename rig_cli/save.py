"""``rig pkg save`` — publish this deployment's local edits as the next version of the package
they belong to, then re-anchor the instance clean (QoL plan F6).

The target rule is **top-of-stack** (settled): an instance with bound overlays saves into the
LAST bound overlay (the layer the edits actually sat on top of — republished under its real
name in its real registry, rebound in place, local cleared); an instance with no overlays saves
into its pinned profile (promote's ``--kind profile --adopt`` flow, auto-bump by provenance);
a routed SERVICE saves its code pointer (the update half of ``promote --kind service``). Save
never writes past the layer it targets, takes no targeting flags (a different registry or kind
is a *fork* or a *new thing* — that's ``promote``), and never pushes.

The governing invariant, both directions: **the render never changes** — save moves tuning
from local state into a versioned package byte-identically (round-trip law), and
``unsave_fixup`` (yank/discard's deployment half) moves it back the same way. Deltas are never
composed patch-onto-patch; they are recomputed against the base surface (null-delete markers
fall out of ``structural_diff`` correctly by construction).
"""
from __future__ import annotations

from pathlib import Path

import yaml

from . import RigError
from .common import eprint, load_yaml
from .lock import (load_lock, record_instance, record_package, record_registry, save_lock,
                   sha256_file)
from .manifest import load_manifest
from .refs import parse_ref, unqualified
from .registries import load_entries
from .resolve import deep_merge, overlay_payload_path, structural_diff
from .workingcopy import _strip_identity, local_delta, pin_path, promote_delta


def _entry_by_alias(alias: str, *, what: str):
    entry = next((e for e in load_entries() if e.name == alias), None)
    if entry is None:
        raise RigError(f"{what}: provenance names registry '{alias}' but it is not configured "
                       f"(rig registry list)")
    return entry


def save(root: Path, spec: str, *, dry_run: bool = False) -> int:
    manifest = load_manifest(root)
    sensor = next((s for s in manifest.sensors if s.name == spec), None)
    if sensor is not None:
        return _save_instance(root, sensor, dry_run=dry_run)
    from .catalog import load_catalog
    try:
        catalog = load_catalog(root)
    except RigError:
        catalog = {}
    lock = load_lock(root)
    lock_services = {unqualified(r) for r, i in (lock.get("packages") or {}).items()
                     if (i or {}).get("kind") == "service"}
    if spec in catalog or spec in lock_services:
        return _save_service(root, spec, dry_run=dry_run)
    raise RigError(f"save: no instance or routed service named '{spec}' — save updates the "
                   f"package something in THIS deployment came from (instances: pkg list; "
                   f"suites have no instance — refresh members with `pkg repin`, re-capture "
                   f"with `pkg promote --all --suite`)")


def _save_instance(root: Path, sensor, *, dry_run: bool) -> int:
    state = local_delta(root, sensor)
    if state is None:
        raise RigError(f"save: '{sensor.name}' has no registry provenance (hand-authored) — "
                       f"nothing to save INTO; a first publish chooses a home and name: "
                       f"`rig pkg promote {sensor.name} --to <registry>`")
    if sensor.overlays:
        return _save_overlay(root, sensor, dry_run=dry_run)
    if not sensor.profile:
        raise RigError(f"save: '{sensor.name}' is anchored to its service's example, not a "
                       f"profile — a service package carries no config payload. Capture the "
                       f"config as a profile (`rig pkg promote {sensor.name} --kind profile "
                       f"--to <registry>`); code updates are `rig pkg save {sensor.service}`")
    file_delta, overrides = state
    if not file_delta and not overrides:
        eprint(f"rig pkg save: '{sensor.name}' is clean — nothing to save (config diff)")
        return 0
    alias, key, _ = parse_ref(str(sensor.profile))
    entry = _entry_by_alias(alias or "", what="save")
    from .promote import _existing_manifest
    if _existing_manifest(entry.root, "profiles", key) is None:
        raise RigError(f"save: '{sensor.profile}' is gone from the '{alias}' registry — "
                       f"republish it first (`rig pkg promote {sensor.name} --kind profile "
                       f"--to {alias}`)")
    if dry_run:
        eprint(f"rig pkg save (dry-run): would publish the next version of {alias}/{key} "
               f"({len(dict(file_delta))} edited top-level key(s)"
               f"{' + overrides' if overrides else ''}), re-pin '{sensor.name}' onto it, and "
               f"reset local state — render identical; nothing written")
        return 0
    from .promote import promote
    return promote(root, [sensor.name], to=str(alias), all_dirty=False, name=None, project=None,
                   kind="profile", suite=None, bump=False, target_instance=False, matches=[],
                   requires=None, adopt=True)


def _save_overlay(root: Path, sensor, *, dry_run: bool) -> int:
    """Fold the local delta into the LAST bound overlay and rebind clean. The new delta is
    recomputed against the pre-overlay surface (never patch-composed), so render identity holds
    by construction."""
    delta = promote_delta(root, sensor)
    if not delta:
        eprint(f"rig pkg save: '{sensor.name}' is clean — nothing to save (config diff)")
        return 0
    old_ref = str(sensor.overlays[-1])
    alias, okey, over = parse_ref(old_ref)
    entry = _entry_by_alias(alias or "", what="save")
    from .promote import (_carry_forward, _existing_manifest, _next_version, _requalify,
                          _registry_write_session, _write_pkg)
    existing = _existing_manifest(entry.root, "overlays", okey)
    if existing is None:
        raise RigError(f"save: bound overlay '{old_ref}' is gone from the '{alias}' registry — "
                       f"nothing to update (promote publishes new packages)")
    old_copy = overlay_payload_path(root, old_ref)
    if not old_copy.is_file():
        raise RigError(f"save: the payload copy for '{old_ref}' is missing — re-apply the "
                       f"overlay first (rig overlay apply)")
    # pre = pin ⊕ earlier overlays; target = current render surface; new delta diffs pre→target
    pre = _strip_identity(load_yaml(pin_path(root, sensor.name)))
    for ref in sensor.overlays[:-1]:
        payload = overlay_payload_path(root, ref)
        if payload.is_file():
            pre = deep_merge(pre, _strip_identity(load_yaml(payload)))
    full = deep_merge(pre, _strip_identity(load_yaml(old_copy)))
    state = local_delta(root, sensor)
    file_delta, overrides = state if state else ({}, {})
    target = deep_merge(deep_merge(full, file_delta), overrides)
    new_delta = structural_diff(pre, target)
    version = _next_version(existing, True, f"overlay '{okey}'")
    if dry_run:
        eprint(f"rig pkg save (dry-run): would publish {alias}/{okey}@{version} with the local "
               f"delta folded in, rebind '{sensor.name}' at it, and clear local state — render "
               f"identical; nothing written")
        return 0
    lock = load_lock(root)
    authored = {}
    svc_pin = next((r for r, info in (lock.get("packages") or {}).items()
                    if (info or {}).get("kind") == "service"
                    and unqualified(r) == sensor.service), None)
    if svc_pin:
        authored["service"] = _requalify(svc_pin)
    if sensor.profile:
        authored["profile"] = _requalify(str(sensor.profile))
    omanifest = {"kind": "overlay", "name": okey, "version": version,
                 **_carry_forward(existing, "kind", "name", "version", "config"),
                 **({"authored_against": authored} if authored else {}),
                 "config": {"payload": "config/delta.yaml"}}
    written: list[Path] = []
    backups: dict[Path, Path] = {}
    with _registry_write_session(entry, str(alias), f"save-{okey}", written, backups,
                                 what="save"):
        _write_pkg(entry.root, "overlays", okey, omanifest, new_delta, written, backups)
        eprint(f"  overlay {alias}/{okey}@{version} <- {sensor.name} (local delta folded into "
               f"the last bound overlay)")
    # deployment half: rebind at the new version, clear local — render provably identical
    new_ref = f"{alias}/{okey}@{version}"
    new_copy = overlay_payload_path(root, new_ref)
    new_copy.parent.mkdir(parents=True, exist_ok=True)
    new_copy.write_text(yaml.safe_dump(new_delta, sort_keys=False, default_flow_style=False))
    from .overlay import edit_row

    def mutate(row: dict) -> None:
        row["overlays"] = [new_ref if o == old_ref else o for o in row.get("overlays") or []]
        row.pop("overrides", None)

    edit_row(root, sensor.name, mutate)
    Path(sensor.config).write_bytes(pin_path(root, sensor.name).read_bytes())
    still_bound = any(old_ref in s.overlays for s in load_manifest(root).sensors)
    if not still_bound:
        old_copy.unlink(missing_ok=True)
        (lock.get("packages") or {}).pop(old_ref, None)
    record_package(lock, new_ref, {"kind": "overlay", "payload_sha256": sha256_file(new_copy),
                                   "project": omanifest.get("project")})
    row = next(s for s in load_manifest(root).sensors if s.name == sensor.name)
    record_instance(lock, sensor.name, profile=row.profile,
                    base_sha256=sha256_file(pin_path(root, sensor.name)),
                    overlays=list(row.overlays))
    from .install import registry_commit
    record_registry(lock, entry.name, rtype=entry.type, location=entry.location,
                    commit=registry_commit(entry))
    save_lock(root, lock)
    eprint(f"  {sensor.name}: rebound at {new_ref}, working reset to the pristine base, "
           f"overrides dropped — render identical (round-trip law)")
    return 0


def _save_service(root: Path, svc: str, *, dry_run: bool) -> int:
    import subprocess

    from .promote import _existing_manifest, _promote_service
    lock = load_lock(root)
    alias = next((parse_ref(r)[0] for r, i in (lock.get("packages") or {}).items()
                  if (i or {}).get("kind") == "service" and unqualified(r) == svc), None)
    if alias is None:  # not lock-tracked: find the registry that carries it, priority order
        alias = next((e.name for e in load_entries()
                      if _existing_manifest(e.root, "services", svc) is not None), None)
    if alias is None:
        raise RigError(f"save: service '{svc}' is not in any configured registry — a first "
                       f"publish chooses the home: `rig pkg promote {svc} --kind service "
                       f"--to <registry>`")
    entry = _entry_by_alias(alias, what="save")
    existing = _existing_manifest(entry.root, "services", svc)
    if existing is None:
        raise RigError(f"save: service '{svc}' is not in the '{alias}' registry the lock names "
                       f"— publish it there first (`rig pkg promote {svc} --kind service`)")
    from .catalog import load_catalog
    route = load_catalog(root).get(svc)
    if route is not None and route.path.is_dir():
        head = subprocess.run(["git", "-C", str(route.path), "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        if head and head == str((existing.get("source") or {}).get("rev")):
            eprint(f"rig pkg save: '{alias}/{svc}' already pins {head[:12]}… (the checkout's "
                   f"HEAD) — nothing to save")
            _config_note(root, svc)
            return 0
    if dry_run:
        eprint(f"rig pkg save (dry-run): would publish the next version of {alias}/{svc} "
               f"pinning the routed checkout's HEAD (code only); nothing written")
        _config_note(root, svc)
        return 0
    rc = _promote_service(root, svc, to=alias, bump=True, version=None)
    _config_note(root, svc)
    return rc


def _config_note(root: Path, svc: str) -> None:
    """A service save publishes CODE only — say so whenever instance configs carry unsaved
    edits, and name the right follow-up per instance."""
    manifest = load_manifest(root)
    dirty = []
    for s in manifest.sensors:
        if s.service != svc:
            continue
        state = local_delta(root, s)
        if state and (state[0] or state[1]):
            dirty.append(s)
    if not dirty:
        return
    eprint(f"rig pkg save: published CODE only — {len(dirty)} instance config(s) of '{svc}' "
           f"carry unsaved edits:")
    for s in dirty:
        fix = f"rig pkg save {s.name}" if s.profile else \
            f"rig pkg promote {s.name} --kind profile --to <registry>"
        eprint(f"  {s.name}: {fix}")


# --- unsave: the deployment half of yank/discard (save's inverse) -----------------------------

def unsave_fixup(root: Path, alias: str, kind: str, key: str, retracted_version: str,
                 retracted_manifest: dict, restored) -> None:
    """Re-anchor this deployment after `alias/key@retracted_version` was retracted. `restored`
    is None (package removed) or a dict {version, manifest, payload (bytes|None)} — for git
    discards, simply the default-branch state. Invariant: the RENDER never changes; only
    attribution moves (packaged -> local). Fail-soft: a fix-up problem warns, never wedges the
    retraction."""
    try:
        _unsave(root, alias, kind, key, retracted_version, retracted_manifest, restored)
    except RigError as exc:
        eprint(f"  WARNING: deployment fix-up failed ({exc}) — the registry retraction stands; "
               f"re-anchor by hand (pkg upgrade / pkg lock)")


def _unsave(root: Path, alias: str, kind: str, key: str, version: str,
            retracted_manifest: dict, restored) -> None:
    old_ref = f"{alias}/{key}@{version}"
    manifest = load_manifest(root)
    lock = load_lock(root)
    if kind == "service":
        if old_ref not in (lock.get("packages") or {}):
            return
        (lock.get("packages") or {}).pop(old_ref, None)
        if restored is not None:
            info = {"kind": "service"}
            source = (restored["manifest"].get("source") or {})
            if source:
                info["source"] = {"repo": source.get("repo"), "rev": source.get("rev")}
            record_package(lock, f"{alias}/{key}@{restored['version']}", info)
            eprint(f"  {root.name}: lock repointed {old_ref} -> @{restored['version']} — "
                   f"vendored surfaces unchanged (`rig pkg upgrade` re-vendors at the pin)")
        else:
            eprint(f"  {root.name}: {old_ref} dropped from the lock (package removed) — the "
                   f"local route keeps working; republish with promote --kind service")
        save_lock(root, lock)
        return
    if kind == "profile":
        for sensor in manifest.sensors:
            if str(sensor.profile or "") == old_ref:
                _unsave_profile(root, lock, sensor, alias, key, old_ref, restored,
                                retracted_manifest)
        return
    if kind == "overlay":
        for sensor in manifest.sensors:
            if old_ref in sensor.overlays:
                _unsave_overlay(root, lock, sensor, alias, key, old_ref, restored)


def _unsave_profile(root: Path, lock, sensor, alias, key, old_ref, restored,
                    retracted_manifest) -> None:
    from .overlay import edit_row
    pin = pin_path(root, sensor.name)
    if restored is not None and restored.get("payload"):
        new_ref = f"{alias}/{key}@{restored['version']}"
        pin.parent.mkdir(parents=True, exist_ok=True)
        pin.write_bytes(restored["payload"])
        edit_row(root, sensor.name, lambda row: row.__setitem__("profile", new_ref))
        requires = ((lock.get("packages") or {}).get(old_ref) or {}).get("requires")
        (lock.get("packages") or {}).pop(old_ref, None)
        record_package(lock, new_ref, {"kind": "profile", "payload_sha256": sha256_file(pin),
                                       "requires": requires})
        record_instance(lock, sensor.name, profile=new_ref, base_sha256=sha256_file(pin),
                        overlays=list(sensor.overlays))
        save_lock(root, lock)
        eprint(f"  {sensor.name}: re-pinned onto {new_ref}; your changes are LOCAL again "
               f"(config diff shows them) — render identical")
        return
    # package removed — fall back along provenance
    based_on = str(retracted_manifest.get("based_on") or "")
    if based_on:
        from .promote import _entry_for_namespace, _parent_payload
        ns, pkey, pver = parse_ref(based_on)
        try:
            parent_entry = _entry_for_namespace(ns or "")
            _, payload = _parent_payload(parent_entry, pkey, pver or "")
            body = yaml.safe_dump(payload, sort_keys=False, default_flow_style=False)
            parent_ref = f"{parent_entry.name}/{pkey}@{pver}"
            pin.parent.mkdir(parents=True, exist_ok=True)
            pin.write_text(body)
            edit_row(root, sensor.name, lambda row: row.__setitem__("profile", parent_ref))
            requires = ((lock.get("packages") or {}).get(old_ref) or {}).get("requires")
            (lock.get("packages") or {}).pop(old_ref, None)
            record_package(lock, parent_ref,
                           {"kind": "profile", "payload_sha256": sha256_file(pin),
                            "requires": requires})
            record_instance(lock, sensor.name, profile=parent_ref,
                            base_sha256=sha256_file(pin), overlays=list(sensor.overlays))
            save_lock(root, lock)
            eprint(f"  {sensor.name}: fork removed — re-anchored on its parent {parent_ref}; "
                   f"the net delta is LOCAL (config diff) — render identical")
            return
        except RigError:
            pass  # parent unreachable: hand-authored fallback below
    from .overlay import edit_row as _edit
    _edit(root, sensor.name, lambda row: row.pop("profile", None))
    (lock.get("packages") or {}).pop(old_ref, None)
    (lock.get("instances") or {}).pop(sensor.name, None)
    pin.unlink(missing_ok=True)
    save_lock(root, lock)
    eprint(f"  {sensor.name}: package removed with no reachable lineage — back to "
           f"hand-authored (working file kept; render identical)")


def _unsave_overlay(root: Path, lock, sensor, alias, key, old_ref, restored) -> None:
    from .overlay import edit_row
    pin = _strip_identity(load_yaml(pin_path(root, sensor.name)))
    pre = dict(pin)
    for ref in sensor.overlays:
        if ref == old_ref:
            break
        payload = overlay_payload_path(root, ref)
        if payload.is_file():
            pre = deep_merge(pre, _strip_identity(load_yaml(payload)))
    old_copy = overlay_payload_path(root, old_ref)
    retracted_delta = _strip_identity(load_yaml(old_copy)) if old_copy.is_file() else {}
    state = local_delta(root, sensor)
    file_delta, overrides = state if state else ({}, {})
    target = deep_merge(deep_merge(deep_merge(pre, retracted_delta), file_delta), overrides)
    if restored is not None and restored.get("payload") is not None:
        new_ref = f"{alias}/{key}@{restored['version']}"
        restored_delta = _strip_identity(yaml.safe_load(restored["payload"].decode()) or {})
        new_copy = overlay_payload_path(root, new_ref)
        new_copy.parent.mkdir(parents=True, exist_ok=True)
        new_copy.write_bytes(restored["payload"])
        file_target = structural_diff(deep_merge(pre, restored_delta), target)

        def mutate(row: dict) -> None:
            row["overlays"] = [new_ref if o == old_ref else o for o in row.get("overlays") or []]
            row.pop("overrides", None)

        edit_row(root, sensor.name, mutate)
        record_package(lock, new_ref, {"kind": "overlay", "payload_sha256": sha256_file(new_copy)})
        note = f"rebound at {new_ref}"
    else:
        file_target = structural_diff(pre, target)

        def mutate(row: dict) -> None:
            row["overlays"] = [o for o in row.get("overlays") or [] if o != old_ref]
            if not row["overlays"]:
                row.pop("overlays")
            row.pop("overrides", None)

        edit_row(root, sensor.name, mutate)
        note = "overlay removed; its content is local now"
    working = deep_merge(pin, file_target)
    working.setdefault("service", sensor.service)
    Path(sensor.config).write_text(yaml.safe_dump(working, sort_keys=False,
                                                  default_flow_style=False))
    if not any(old_ref in s.overlays for s in load_manifest(root).sensors):
        old_copy.unlink(missing_ok=True)
        (lock.get("packages") or {}).pop(old_ref, None)
    row = next(s for s in load_manifest(root).sensors if s.name == sensor.name)
    record_instance(lock, sensor.name, profile=row.profile,
                    base_sha256=sha256_file(pin_path(root, sensor.name)),
                    overlays=list(row.overlays))
    save_lock(root, lock)
    eprint(f"  {sensor.name}: {note}; your changes are LOCAL again (config diff) — render "
           f"identical")
