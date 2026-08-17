"""The working-copy layer: `config diff` and `pkg upgrade` — the git-status/git-merge of configs.

The anchor is the PRISTINE BASE copy at ``config/.pins/<instance>.yaml`` (GENERATED, committed):
the exact bytes the working config started as, hash-locked in rig.lock's ``instances`` section.
Everything is derived from one primitive, ``resolve.structural_diff``:

- **local delta** = diff(pin, working file) — what YOU changed, exactly the patch a promoted
  overlay would carry. Row ``overrides:`` are reported separately (they're already a delta).
- **diff** prints per-key attribution: base value → yours, ``[local edit]`` vs ``[override]``
  vs ``[deleted]``. Identity keys (name/service) are excluded — they belong to the row.
- **upgrade** is a three-way merge: new base ⊕ your delta, with conflicts (keys the base ALSO
  changed) surfaced — local wins, loudly. A clean (delta-free) upgrade copies the new payload
  verbatim so its comments survive; a dirty one re-renders YAML and says comments were lost.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from . import RigError
from .common import eprint, load_yaml
from .install import qualified, registry_commit
from .lock import load_lock, record_instance, record_package, record_registry, save_lock, sha256_file
from .manifest import Manifest, Sensor, load_manifest
from .pkg import _each_index, _entries_or_hint
from .refs import unqualified
from .resolve import deep_merge, overlay_payload_path, structural_diff

PINS_DIR = "config/.pins"


def pin_path(root: Path, instance: str) -> Path:
    return root / PINS_DIR / f"{instance}.yaml"


def _strip_identity(data: dict) -> dict:
    return {k: v for k, v in (data or {}).items() if k not in ("name", "service")}


def _flat(patch: dict, prefix: str = ""):
    for key, value in patch.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict) and value:
            yield from _flat(value, path)
        else:
            yield path, value


def _dig(data: dict, path: str):
    cur = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def expected_base(root: Path, sensor: Sensor) -> dict | None:
    """Layers 1+2: the pinned base with the bound overlays merged in order — what `working ⊕
    overrides` is measured against. None without a pin (hand-authored instance)."""
    pin = pin_path(root, sensor.name)
    if not pin.is_file():
        return None
    base = _strip_identity(load_yaml(pin))
    for ref in sensor.overlays:
        payload = overlay_payload_path(root, ref)
        if payload.is_file():
            base = deep_merge(base, _strip_identity(load_yaml(payload)))
    return base


def local_delta(root: Path, sensor: Sensor) -> tuple[dict, dict] | None:
    """(file_delta, overrides) — the FILE delta is measured against the PIN (the surface you
    actually edit; the render pipeline re-applies it after overlays), so binding an overlay never
    makes a pristine file look dirty. None without a pin (hand-authored instance)."""
    pin = pin_path(root, sensor.name)
    if not pin.is_file():
        return None
    base = _strip_identity(load_yaml(pin))
    working = _strip_identity(load_yaml(sensor.config))
    return structural_diff(base, working), dict(sensor.overrides or {})


def promote_delta(root: Path, sensor: Sensor) -> dict | None:
    """THE promote payload: D such that (pin ⊕ overlays) ⊕ D == the final render minus identity —
    i.e. binding D last reproduces current behavior exactly (the round-trip law)."""
    base = expected_base(root, sensor)
    if base is None:
        return None
    state = local_delta(root, sensor)
    file_delta, overrides = state if state else ({}, {})
    final = deep_merge(deep_merge(base, file_delta), overrides)
    return structural_diff(base, final)


def _provenance_note(sensor: Sensor, currents: dict) -> str:
    """`(base: ns/x@1.0.0 — 1.1.0 available)` — the pin, plus an upgrade hint when the synced
    registry has moved past it. Fail-soft on registry state (diff must work offline)."""
    if not sensor.profile:
        return ""
    from .refs import parse_ref
    ns, name, pinned = parse_ref(str(sensor.profile))
    if ns not in currents:
        currents[ns] = None
        from .registries import load_entries, open_registry
        entry = next((e for e in load_entries() if e.name == ns), None)
        if entry is not None:
            try:
                currents[ns] = open_registry(entry)[1].get("packages") or {}
            except RigError:
                pass
    current = ((currents[ns] or {}).get(name) or {}).get("version")
    hint = f" — {current} available" if current and current != pinned else ""
    return f" (base: {sensor.profile}{hint})"


def cmd_diff(args, root: Path) -> int:
    """`rig config diff [names]` — which instances are dirty vs their pinned base, per key."""
    manifest: Manifest = load_manifest(root)  # RAW rows — the working files, not the rendered output
    lock = load_lock(root)
    chosen = manifest.select(args.names, enabled_only=False)
    dirty = 0
    currents: dict = {}  # ns -> index packages, opened once, fail-soft
    for sensor in chosen:
        state = local_delta(root, sensor)
        if state is None:
            if args.names:  # only mention pin-less instances when explicitly asked about
                print(f"{sensor.name}: no pinned base (hand-authored) — nothing to diff against")
            continue
        delta, overrides = state
        anchored = ((lock.get("instances") or {}).get(sensor.name) or {}).get("base_sha256")
        if anchored and anchored != sha256_file(pin_path(root, sensor.name)):
            eprint(f"  WARNING {sensor.name}: config/.pins/{sensor.name}.yaml no longer matches "
                   f"rig.lock — the pin was edited by hand?")
        masked = set(dict(_flat(delta))) | set(dict(_flat(overrides)))
        if not delta and not overrides:
            if args.names:
                print(f"{sensor.name}: clean{_provenance_note(sensor, currents)}")
                _print_overlay_attribution(root, sensor, masked)
            continue
        dirty += 1
        print(f"{sensor.name}: dirty{_provenance_note(sensor, currents)}")
        _print_overlay_attribution(root, sensor, masked)
        base = _strip_identity(load_yaml(pin_path(root, sensor.name)))
        for path, value in sorted(_flat(delta)):
            was = _dig(base, path)
            if value is None:
                print(f"  - {path}: {was!r}  [deleted]")
            elif was is None:
                print(f"  + {path}: {value!r}  [local edit]")
            else:
                print(f"  ~ {path}: {was!r} -> {value!r}  [local edit]")
        for path, value in sorted(_flat(overrides)):
            print(f"  ~ {path}: -> {value!r}  [override]")
    if not args.names:
        print(f"{dirty} instance(s) dirty" if dirty else "all pinned instances clean")
    return 0


def _print_overlay_attribution(root: Path, sensor: Sensor, masked: set) -> None:
    """Which overlay set each layer-2 key's final value (last binding wins) — plus a marker when a
    local edit/override masks it (local beats overlays)."""
    if not sensor.overlays:
        return
    attribution: dict[str, tuple] = {}
    for ref in sensor.overlays:
        payload = overlay_payload_path(root, ref)
        if payload.is_file():
            for path, value in _flat(_strip_identity(load_yaml(payload))):
                attribution[path] = (value, ref)
    for path, (value, ref) in sorted(attribution.items()):
        note = "  (masked by local)" if path in masked else ""
        print(f"  = {path}: {value!r}  [overlay {ref}]{note}")


def _resolve_current(ref: str):
    """A locked `ns/name@ver` -> (entry, reg, pkg) at the registry's CURRENT version."""
    ns, _, rest = ref.partition("/")
    name = rest.split("@", 1)[0]
    entries = [e for e in _entries_or_hint() if e.name == ns]
    if not entries:
        raise RigError(f"upgrade: registry '{ns}' (from the lock) is not configured")
    for entry, reg, _ in _each_index(entries):
        pkg = reg.packages.get(name)
        if pkg is not None:
            return entry, reg, pkg
    raise RigError(f"upgrade: '{ref.split('@')[0]}' no longer exists in registry '{ns}'")


def _three_way(root: Path, sensor: Sensor, new_base_path: Path) -> list[str]:
    """New pinned base ⊕ local delta onto the working file + pin; returns the conflict paths.
    Clean (delta-free) working copies take the new base VERBATIM so its comments survive."""
    pin = pin_path(root, sensor.name)
    old_base = _strip_identity(load_yaml(pin))
    new_base = _strip_identity(load_yaml(new_base_path))
    working = _strip_identity(load_yaml(sensor.config))
    delta = structural_diff(old_base, working)
    conflicts = sorted(set(dict(_flat(delta))) & set(dict(_flat(structural_diff(old_base, new_base)))))
    working_path = Path(sensor.config)
    if not delta:
        embedded = str((load_yaml(new_base_path) or {}).get("name") or "")
        if embedded and embedded != sensor.name:  # upstream renamed its example — neutralize it
            from .init import _copy_as_profile
            _copy_as_profile(new_base_path, working_path)
        else:
            working_path.write_bytes(new_base_path.read_bytes())
    else:
        merged = deep_merge(new_base, delta)
        working_path.write_text(yaml.safe_dump(merged, sort_keys=False, default_flow_style=False))
        eprint(f"  {sensor.name}: local edits re-applied — the working file was re-rendered "
               f"(comments lost); review it")
    pin.write_bytes(working_path.read_bytes() if not delta else new_base_path.read_bytes())
    for path in conflicts:
        eprint(f"    CONFLICT {path}: base {_dig(old_base, path)!r} -> "
               f"{_dig(new_base, path)!r}, keeping yours: {_dig(working, path)!r}")
    return conflicts


def upgrade(root: Path, names: list[str]) -> int:
    """`rig pkg upgrade [instance…]` — re-pin EVERY registry package this deployment uses: profile
    instances (three-way payload merge, local wins), bare service instances (three-way against the
    new vendored example), bound overlays (payload rebound in place — order is semantic), the
    services themselves (refetch + re-vendor at the new pin, including a profile's required
    service), and instance-less service dependencies. All-or-nothing: any failure mid-sweep rolls
    the tree AND the lock back (a half-upgraded deployment fails `pkg lock` on every instance)."""
    from .install import _install_service, _rollback, _snapshot

    manifest = load_manifest(root)
    lock = load_lock(root)
    packages = lock.setdefault("packages", {})
    unknown = set(names) - {s.name for s in manifest.sensors}
    if unknown:
        raise RigError(f"upgrade: unknown instance(s): {', '.join(sorted(unknown))}")
    anchors = lock.get("instances") or {}
    targets = [s for s in manifest.sensors
               if (s.profile or s.name in anchors) and (not names or s.name in names)]
    for skipped in [n for n in names
                    if not any(s.name == n and (s in targets or s.overlays)
                               for s in manifest.sensors)]:
        eprint(f"  {skipped}: no registry provenance (hand-wired) — nothing to upgrade")
    repinned: dict[str, str] = {}  # old service ref -> current ref (re-vendored at most once)
    snap = _snapshot(root)

    def repin_service(old_ref: str) -> str:
        if old_ref in repinned:
            return repinned[old_ref]
        entry, _, pkg = _resolve_current(old_ref)
        new_ref = qualified(entry, pkg)
        if new_ref != old_ref or not (root / "services" / pkg.name / ".vendored.yaml").exists():
            _install_service(root, entry, pkg, lock, locked=False,  # refetch + re-vendor + record
                             allow_repin=True)  # upgrade IS the deliberate pin-moving path
            if new_ref != old_ref:
                packages.pop(old_ref, None)
                eprint(f"  service: {old_ref} -> {new_ref} (re-vendored at the new pin)")
        repinned[old_ref] = new_ref
        return new_ref

    def service_ref_for(service_name: str) -> str | None:
        hits = [r for r, info in packages.items()
                if info.get("kind") == "service" and unqualified(r) == service_name]
        return hits[0] if hits else None

    try:
        changed = _upgrade_sweep(root, manifest, lock, packages, anchors, targets, names,
                                 repin_service, service_ref_for, repinned)
    except BaseException:
        _rollback(root, snap)
        eprint("rig pkg upgrade: failed mid-sweep — rolled back (configs, pins, vehicle.yaml, "
               "services/, rig.lock all restored)")
        raise
    if changed:
        save_lock(root, lock)
        load_manifest(root)  # the gate: the deployment must still load
        eprint(f"rig pkg upgrade: {changed} change(s) — rig.lock updated, commit it")
    else:
        eprint("rig pkg upgrade: everything current")
    return 0


def _upgrade_sweep(root: Path, manifest, lock: dict, packages: dict, anchors: dict,
                   targets: list, names: list[str], repin_service, service_ref_for,
                   repinned: dict) -> int:
    changed = 0
    for sensor in targets:
        if sensor.profile:  # profile instance: new payload three-way + requires re-pin
            ref = str(sensor.profile)
            entry, reg, pkg = _resolve_current(ref)
            payload_rel = (pkg.manifest.get("config") or {}).get("payload")
            payload_path = pkg.pkg_dir / str(payload_rel)
            if not payload_path.is_file():
                raise RigError(f"upgrade {sensor.name}: payload missing in registry: {payload_rel}")
            new_ref = qualified(entry, pkg)
            pin = pin_path(root, sensor.name)
            if not pin.is_file():
                eprint(f"  {sensor.name}: no pinned base copy ({PINS_DIR}/) — cannot three-way; "
                       f"re-install to re-anchor")
                continue
            old_svc = ((packages.get(ref) or {}).get("requires")
                       or service_ref_for(sensor.service))
            new_svc = repin_service(old_svc) if old_svc else None
            if new_ref == ref and sha256_file(payload_path) == sha256_file(pin):
                if old_svc == new_svc:
                    eprint(f"  {sensor.name}: {ref} is current — up to date")
                    continue
                record_package(lock, ref, {**packages.get(ref, {}), "requires": new_svc})
                changed += 1
                continue
            conflicts = _three_way(root, sensor, payload_path)
            record_package(lock, new_ref, {"kind": "profile",
                                           "payload_sha256": sha256_file(payload_path),
                                           "requires": new_svc})
            if new_ref != ref:
                packages.pop(ref, None)
                _rewrite_row_profile(root, sensor.name, new_ref)
            record_instance(lock, sensor.name, profile=new_ref,
                            base_sha256=sha256_file(pin_path(root, sensor.name)),
                            overlays=list(sensor.overlays))  # the ORDERED binding record survives
            record_registry(lock, entry.name, rtype=entry.type, location=entry.location,
                            commit=registry_commit(entry))
            changed += 1
            eprint(f"  {sensor.name}: {ref} -> {new_ref}"
                   + (f" ({len(conflicts)} conflict(s), local kept)" if conflicts else ""))
        else:  # bare service instance: base = the service's vendored example at the NEW pin
            old_svc = service_ref_for(sensor.service)
            if old_svc is None:
                eprint(f"  {sensor.name}: no locked service package for '{sensor.service}' — "
                       f"hand-wired instance, skipping")
                continue
            new_svc = repin_service(old_svc)
            pin = pin_path(root, sensor.name)
            from .descriptor import load_descriptor
            desc = load_descriptor(sensor.service, root / "services" / sensor.service)
            examples = [root / "services" / sensor.service / e for e in desc.examples]
            examples = [e for e in examples if e.is_file()]
            if not pin.is_file() or not examples:
                if old_svc != new_svc:
                    changed += 1
                    eprint(f"  {sensor.name}: service re-pinned; "
                           + ("no pinned base copy — working config left as-is"
                              if not pin.is_file() else
                              "no declared example at the new pin — working config left as-is"))
                else:
                    eprint(f"  {sensor.name}: {old_svc} is current — up to date")
                continue
            if old_svc == new_svc and sha256_file(examples[0]) == sha256_file(pin):
                eprint(f"  {sensor.name}: {old_svc} is current — up to date")
                continue
            conflicts = _three_way(root, sensor, examples[0])
            row = anchors.get(sensor.name) or {}
            record_instance(lock, sensor.name, profile=None,
                            base_sha256=sha256_file(pin_path(root, sensor.name)),
                            overlays=list(row.get("overlays") or []))
            changed += 1
            eprint(f"  {sensor.name}: base refreshed from {new_svc}"
                   + (f" ({len(conflicts)} conflict(s), local kept)" if conflicts else ""))

    if not names:  # instance-less service dependencies still get re-pinned in a full sweep
        for ref in [r for r, info in list(packages.items()) if info.get("kind") == "service"]:
            if ref in packages and ref not in repinned and ref not in repinned.values():
                new_ref = repin_service(ref)
                if new_ref != ref:
                    changed += 1

    # Bound overlays: rebind IN PLACE at the registry's current version. Same position (order is
    # semantic), payload copy swapped, lock updated — this is what `pkg list`'s upgrade hint
    # promised. Local edits are untouched: local always beats overlays, so there is no merge.
    from .overlay import edit_row
    rebind_names = [s.name for s in manifest.sensors
                    if s.overlays and (not names or s.name in names)]
    for iname in rebind_names:
        sensor = next(s for s in load_manifest(root).sensors if s.name == iname)  # fresh row
        for ref in list(sensor.overlays):
            try:
                entry, _, pkg = _resolve_current(ref)
            except RigError as exc:
                eprint(f"  {iname}: overlay {ref} — {exc}; binding left as-is")
                continue
            new_ref = qualified(entry, pkg)
            if new_ref == ref:
                continue
            payload_rel = (pkg.manifest.get("config") or {}).get("payload")
            src = pkg.pkg_dir / str(payload_rel)
            if not src.is_file():
                raise RigError(f"upgrade {iname}: overlay payload missing in registry: {payload_rel}")
            dest = overlay_payload_path(root, new_ref)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(src.read_bytes())
            edit_row(root, iname, lambda row, old=ref, new=new_ref: row.__setitem__(
                "overlays", [new if o == old else o for o in row.get("overlays") or []]))
            record_package(lock, new_ref, {"kind": "overlay", "payload_sha256": sha256_file(dest),
                                           "project": pkg.manifest.get("project")})
            record_registry(lock, entry.name, rtype=entry.type, location=entry.location,
                            commit=registry_commit(entry))
            if not any(ref in s.overlays for s in load_manifest(root).sensors):
                overlay_payload_path(root, ref).unlink(missing_ok=True)  # last binding moved on
                packages.pop(ref, None)
            changed += 1
            eprint(f"  {iname}: overlay {ref} -> {new_ref} (rebound in place; local still wins)")
        fresh = next(s for s in load_manifest(root).sensors if s.name == iname)
        anchor = anchors.get(iname) or {}
        if anchor.get("base_sha256"):  # never synthesize an anchor for a pin-less instance
            record_instance(lock, iname, profile=fresh.profile,
                            base_sha256=anchor["base_sha256"], overlays=list(fresh.overlays))
    return changed


def _rewrite_row_profile(root: Path, instance: str, new_ref: str) -> None:
    """Point one row's `profile:` at the new pin (generated single-line row form only)."""
    veh = root / "vehicle.yaml"
    lines = veh.read_text().splitlines()
    for i, line in enumerate(lines):
        if f"name: {instance}," in line and "profile: " in line:
            import re
            lines[i] = re.sub(r"profile: [^,}]+", f"profile: {new_ref}", line)
            veh.write_text("\n".join(lines) + "\n")
            return
    eprint(f"  {instance}: vehicle.yaml row not in the generated form — update its "
           f"`profile:` to {new_ref} yourself")


def relock(root: Path) -> int:
    """`rig pkg lock` — re-verify every instance anchor against its pin copy AND every bound
    overlay's payload copy against its locked hash, then rewrite rig.lock deterministically.
    A report verb: findings go to STDOUT (machine-consumable), like the other list/diff verbs."""
    lock = load_lock(root)
    manifest = load_manifest(root)
    problems = 0
    for name, row in sorted((lock.get("instances") or {}).items()):
        pin = pin_path(root, name)
        if not any(s.name == name for s in manifest.sensors):
            print(f"  {name}: in rig.lock but not in vehicle.yaml — removing the stale anchor")
            del lock["instances"][name]
            continue
        if not row.get("base_sha256"):  # legacy: overlay apply once synthesized pin-less anchors
            print(f"  {name}: anchor has no base hash (pin-less instance) — dropping it "
                  f"(bindings live in vehicle.yaml; anchors are for pinned bases only)")
            del lock["instances"][name]
            continue
        if not pin.is_file():
            problems += 1
            print(f"  {name}: missing {PINS_DIR}/{name}.yaml (the pristine base copy) — re-install "
                  f"to re-anchor")
            continue
        got = sha256_file(pin)
        if got != row.get("base_sha256"):
            problems += 1
            print(f"  {name}: pin copy hash {got[:12]}… != lock {str(row.get('base_sha256'))[:12]}… "
                  f"— the pin was edited; restore it or re-install")
    bound = {o for s in manifest.sensors for o in s.overlays}
    for ref, info_ in sorted((lock.get("packages") or {}).items()):
        if (info_ or {}).get("kind") != "overlay" or ref not in bound:
            continue
        copy = overlay_payload_path(root, ref)
        want = (info_ or {}).get("payload_sha256")
        if not copy.is_file():
            problems += 1
            print(f"  {ref}: bound but its payload copy is missing (config/.overlays/) — re-apply")
        elif want and sha256_file(copy) != want:
            problems += 1
            print(f"  {ref}: payload copy no longer matches rig.lock — it was edited by hand; "
                  f"re-apply the overlay (edits belong in the working config or a new version)")
    save_lock(root, lock)
    print(f"rig pkg lock: rewritten deterministically"
          + (f" — {problems} problem(s) above" if problems else ", all anchors verified"))
    return 1 if problems else 0
