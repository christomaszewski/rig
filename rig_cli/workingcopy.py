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


def cmd_diff(args, root: Path) -> int:
    """`rig config diff [names]` — which instances are dirty vs their pinned base, per key."""
    manifest: Manifest = load_manifest(root)  # RAW rows — the working files, not the rendered output
    lock = load_lock(root)
    chosen = manifest.select(args.names, enabled_only=False)
    dirty = 0
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
                print(f"{sensor.name}: clean")
                _print_overlay_attribution(root, sensor, masked)
            continue
        dirty += 1
        provenance = f" (base: {sensor.profile})" if sensor.profile else ""
        print(f"{sensor.name}: dirty{provenance}")
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


def upgrade(root: Path, names: list[str]) -> int:
    """`rig pkg upgrade [instance…]` — three-way: new pinned base ⊕ local delta, conflicts loud."""
    manifest = load_manifest(root)
    lock = load_lock(root)
    targets = [s for s in manifest.sensors if s.profile and (not names or s.name in names)]
    unknown = set(names) - {s.name for s in manifest.sensors}
    if unknown:
        raise RigError(f"upgrade: unknown instance(s): {', '.join(sorted(unknown))}")
    if not targets:
        eprint("rig pkg upgrade: no profile-provenance instances" +
               (" among those named" if names else "") + " — nothing to upgrade")
        return 0
    changed = 0
    for sensor in targets:
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
        if new_ref == ref and sha256_file(payload_path) == sha256_file(pin):
            eprint(f"  {sensor.name}: {ref} is current — up to date")
            continue

        old_base = _strip_identity(load_yaml(pin))
        new_base = _strip_identity(load_yaml(payload_path))
        working = _strip_identity(load_yaml(sensor.config))
        delta = structural_diff(old_base, working)
        base_changes = structural_diff(old_base, new_base)
        conflicts = sorted(set(dict(_flat(delta))) & set(dict(_flat(base_changes))))
        merged = deep_merge(new_base, delta)

        working_path = Path(sensor.config)
        if not delta:  # clean: take the new payload verbatim — its comments survive
            working_path.write_bytes(payload_path.read_bytes())
        else:
            working_path.write_text(yaml.safe_dump(merged, sort_keys=False, default_flow_style=False))
            eprint(f"  {sensor.name}: local edits re-applied — the working file was re-rendered "
                   f"(comments lost); review it")
        pin.write_bytes(payload_path.read_bytes())
        for path in conflicts:
            eprint(f"    CONFLICT {path}: base {_dig(old_base, path)!r} -> "
                   f"{_dig(new_base, path)!r}, keeping yours: {_dig(working, path)!r}")

        # Re-pin the lock: profile, its (possibly newer) required service, the instance anchor.
        svc_ref = ((lock.get("packages") or {}).get(ref) or {}).get("requires")
        record_package(lock, new_ref, {"kind": "profile", "payload_sha256": sha256_file(payload_path),
                                       "requires": svc_ref})
        if new_ref != ref:
            (lock.get("packages") or {}).pop(ref, None)
            _rewrite_row_profile(root, sensor.name, new_ref)
        record_instance(lock, sensor.name, profile=new_ref, base_sha256=sha256_file(pin))
        record_registry(lock, entry.name, rtype=entry.type, location=entry.location,
                        commit=registry_commit(entry))
        changed += 1
        eprint(f"  {sensor.name}: {ref} -> {new_ref}"
               + (f" ({len(conflicts)} conflict(s), local kept)" if conflicts else ""))
    if changed:
        save_lock(root, lock)
        load_manifest(root)  # the gate: the deployment must still load
        eprint(f"rig pkg upgrade: {changed} instance(s) upgraded — rig.lock updated, commit it")
    return 0


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
    """`rig pkg lock` — re-verify every instance anchor against its pin copy and rewrite rig.lock
    deterministically. Catches hand-edited pins and normalizes the file."""
    lock = load_lock(root)
    manifest = load_manifest(root)
    problems = 0
    for name, row in sorted((lock.get("instances") or {}).items()):
        pin = pin_path(root, name)
        if not any(s.name == name for s in manifest.sensors):
            eprint(f"  {name}: in rig.lock but not in vehicle.yaml — removing the stale anchor")
            del lock["instances"][name]
            continue
        if not pin.is_file():
            problems += 1
            eprint(f"  {name}: missing {PINS_DIR}/{name}.yaml (the pristine base copy) — re-install "
                   f"to re-anchor")
            continue
        got = sha256_file(pin)
        if got != row.get("base_sha256"):
            problems += 1
            eprint(f"  {name}: pin copy hash {got[:12]}… != lock {str(row.get('base_sha256'))[:12]}… "
                   f"— the pin was edited; restore it or re-install")
    save_lock(root, lock)
    eprint(f"rig pkg lock: rewritten deterministically"
           + (f" — {problems} problem(s) above" if problems else ", all anchors verified"))
    return 1 if problems else 0
