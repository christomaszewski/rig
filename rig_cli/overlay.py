"""``rig overlay`` — BINDINGS only (installed ≠ active): apply/remove/reorder/list an instance's
ordered overlay list. Package authoring lives in ``rig pkg promote``.

Applying copies the overlay's delta into ``config/.overlays/`` (deployment-local, so rendering
never needs the registry cache) and appends the fully-qualified ref to the instance row's
``overlays:`` list — ORDER IS MERGE ORDER, last-in-order wins on key conflicts, and local (file
edits + row overrides) always beats every overlay. ``--clear-local`` resets the working file to
its pristine pin and drops the row's ``overrides:`` — the promote round-trip's second half:
identical render, but the tuning now lives in a versioned layer.

Row edits go through a parse→modify→re-serialize of the one generated single-line row (verified
to re-parse; on a hand-authored row shape rig prints the exact edit instead of guessing).
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from . import RigError
from .common import eprint, load_yaml
from .install import qualified, registry_commit, resolve_ref
from .lock import load_lock, record_instance, record_package, record_registry, save_lock, sha256_file
from .manifest import Sensor, load_manifest
from .refs import unqualified
from .resolve import overlay_payload_path

_ROW_KEY_ORDER = ("name", "service", "config", "profile", "overlays", "overrides", "enabled", "order")


def _row_to_line(row: dict) -> str:
    parts = []
    for key in _ROW_KEY_ORDER:
        if key not in row:
            continue
        value = row[key]
        rendered = yaml.safe_dump(value, default_flow_style=True, width=10 ** 6).strip()
        if rendered.endswith("..."):  # plain scalars dump with a document-end marker
            rendered = rendered[:-3].strip()
        parts.append(f"{key}: {rendered}")
    extras = {k: v for k, v in row.items() if k not in _ROW_KEY_ORDER}
    for key, value in extras.items():
        rendered = yaml.safe_dump(value, default_flow_style=True, width=10 ** 6).strip()
        if rendered.endswith("..."):
            rendered = rendered[:-3].strip()
        parts.append(f"{key}: {rendered}")
    return "- { " + ", ".join(parts) + " }"


def edit_row(root: Path, instance: str, mutate) -> None:
    """Load vehicle.yaml, find `instance`'s single-line row, apply ``mutate(rowdict)``, write back —
    verified to re-parse, refusing shapes rig can't safely rewrite."""
    veh = root / "vehicle.yaml"
    text = veh.read_text()
    lines = text.splitlines()
    hits = [i for i, line in enumerate(lines)
            if re.match(r"^\s*- \{.*\bname: " + re.escape(instance) + r"[,}]", line)]
    if len(hits) != 1:
        raise RigError(f"overlay: vehicle.yaml row for '{instance}' "
                       f"{'not found' if not hits else 'is ambiguous'} or not in the generated "
                       f"single-line form — edit its `overlays:` list yourself")
    i = hits[0]
    indent = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
    row = yaml.safe_load(lines[i].lstrip()[2:])  # strip "- "
    if not isinstance(row, dict):
        raise RigError(f"overlay: cannot parse the row for '{instance}'")
    mutate(row)
    lines[i] = indent + _row_to_line(row)
    new = "\n".join(lines) + "\n"
    try:
        yaml.safe_load(new)
    except yaml.YAMLError as exc:
        raise RigError(f"overlay: refusing to write vehicle.yaml — result would not parse ({exc})")
    veh.write_text(new)


def _find_instance(root: Path, name: str) -> Sensor:
    manifest = load_manifest(root)
    for sensor in manifest.sensors:
        if sensor.name == name:
            return sensor
    raise RigError(f"overlay: unknown instance '{name}' (rig status shows them)")


def _targets_cover(pkg_manifest: dict, sensor: Sensor) -> bool:
    for target in pkg_manifest.get("targets") or []:
        if not isinstance(target, dict):
            continue
        if target.get("service") and str(target["service"]).rpartition("/")[-1] == sensor.service:
            return True
        if target.get("instance") == sensor.name:
            return True
    return False


def _record_bindings(lock: dict, root: Path, instance: str, profile, overlays: list[str]) -> None:
    """Refresh the instance anchor's ordered binding record — but NEVER synthesize an anchor for a
    pin-less (hand-authored) instance: an empty base_sha256 would poison `pkg lock` forever.
    Bindings themselves live in vehicle.yaml; the anchor is for pinned bases only."""
    row = (lock.get("instances") or {}).get(instance) or {}
    base = row.get("base_sha256")
    if not base:
        pin = root / "config" / ".pins" / f"{instance}.yaml"
        base = sha256_file(pin) if pin.is_file() else None
    if base:
        record_instance(lock, instance, profile=profile, base_sha256=base, overlays=overlays)
    else:
        (lock.get("instances") or {}).pop(instance, None)


def _warn_authored_against(lock: dict, pkg, service: str) -> None:
    """The consumer half of the v0.1.59 staleness stamp: binding an overlay authored against a
    DIFFERENT service version than this deployment pins deserves a heads-up at apply time."""
    stamp = (pkg.manifest.get("authored_against") or {}).get("service")
    if not stamp:
        return
    mine = next((r for r, info in (lock.get("packages") or {}).items()
                 if (info or {}).get("kind") == "service"
                 and unqualified(r) == service), None)
    if mine and "@" in str(stamp) and mine.split("@")[-1] != str(stamp).split("@")[-1]:
        eprint(f"  WARNING: '{pkg.name}' was authored against {stamp} but this deployment pins "
               f"{mine} — re-verify the delta's keys against the current config surface")


def apply(root: Path, instance: str, ref: str, *, clear_local: bool = False) -> int:
    sensor = _find_instance(root, instance)
    entry, _, pkg = resolve_ref(ref, history="@" in ref)  # an exact @ver may live in git history
    if pkg.kind != "overlay":
        raise RigError(f"overlay apply: '{ref}' is a {pkg.kind}, not an overlay")
    if not _targets_cover(pkg.manifest, sensor):
        raise RigError(f"overlay apply: '{pkg.name}' does not target service '{sensor.service}' "
                       f"or instance '{instance}' (its targets: {pkg.manifest.get('targets')})")
    fq = qualified(entry, pkg)
    if fq in sensor.overlays:
        raise RigError(f"overlay apply: '{fq}' is already bound to '{instance}' "
                       f"(rig overlay reorder changes precedence)")
    if clear_local and not (root / "config" / ".pins" / f"{instance}.yaml").is_file():
        raise RigError(f"overlay apply --clear-local: '{instance}' has no pinned base to reset the "
                       f"working file to — dropping its overrides would be irreversible; apply "
                       f"without --clear-local")
    # Same package already bound at ANOTHER version ⇒ REBIND in place (same position — order is
    # semantic). Two versions of one overlay merging in sequence is never what anyone means.
    replaced = next((o for o in sensor.overlays
                     if o != fq and o.split("@", 1)[0] == fq.split("@", 1)[0]), None)
    payload_rel = (pkg.manifest.get("config") or {}).get("payload")
    src = pkg.pkg_dir / str(payload_rel)
    if not src.is_file():
        raise RigError(f"overlay apply: payload missing in the synced registry: {payload_rel}")

    dest = overlay_payload_path(root, fq)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(src.read_bytes())
    lock = load_lock(root)
    record_package(lock, fq, {"kind": "overlay", "payload_sha256": sha256_file(dest),
                              "project": pkg.manifest.get("project")})
    record_registry(lock, entry.name, rtype=entry.type, location=entry.location,
                    commit=registry_commit(entry))
    _warn_authored_against(lock, pkg, sensor.service)

    def mutate(row: dict) -> None:
        bound = list(row.get("overlays") or [])
        row["overlays"] = ([fq if o == replaced else o for o in bound] if replaced
                           else bound + [fq])
        if clear_local:
            row.pop("overrides", None)

    edit_row(root, instance, mutate)
    if replaced and not any(replaced in s.overlays for s in load_manifest(root).sensors):
        overlay_payload_path(root, replaced).unlink(missing_ok=True)  # last binding moved on
        (lock.get("packages") or {}).pop(replaced, None)
    if clear_local:
        pin = root / "config" / ".pins" / f"{instance}.yaml"
        Path(sensor.config).write_bytes(pin.read_bytes())
        eprint(f"  {instance}: working config reset to the pristine base (pin); row overrides "
               f"dropped — the tuning now lives in '{fq}'")
    sensor = _find_instance(root, instance)  # re-read: the row just changed
    _record_bindings(lock, root, instance, sensor.profile, list(sensor.overlays))
    save_lock(root, lock)
    if replaced:
        eprint(f"rig overlay apply: rebound '{replaced}' -> '{fq}' on '{instance}' "
               f"(same position; last binding wins, local still beats every overlay)")
    else:
        eprint(f"rig overlay apply: '{fq}' bound to '{instance}' at position "
               f"{len(sensor.overlays)} (last binding wins on conflicts; local still beats "
               f"every overlay)")
    return 0


def _bound_match(bound: str, ref: str) -> bool:
    """A bound fq ref matches itself, its unversioned form, or its bare package name."""
    unversioned = bound.split("@", 1)[0]
    return ref in (bound, unversioned, unversioned.rpartition("/")[-1])


def remove(root: Path, instance: str, ref: str) -> int:
    sensor = _find_instance(root, instance)
    matches = [o for o in sensor.overlays if _bound_match(o, ref)]
    if not matches:
        raise RigError(f"overlay remove: '{ref}' is not bound to '{instance}' "
                       f"(bound: {', '.join(sensor.overlays) or 'none'})")
    if len(matches) > 1:
        raise RigError(f"overlay remove: '{ref}' is ambiguous on '{instance}' "
                       f"({', '.join(matches)}) — use the fully-qualified ref")
    fq = matches[0]

    def mutate(row: dict) -> None:
        row["overlays"] = [o for o in row.get("overlays") or [] if o != fq]
        if not row["overlays"]:
            row.pop("overlays")

    edit_row(root, instance, mutate)
    lock = load_lock(root)
    remaining = [o for o in sensor.overlays if o != fq]
    _record_bindings(lock, root, instance, sensor.profile, remaining)
    still_bound = any(fq in s.overlays for s in load_manifest(root).sensors)
    if not still_bound:
        overlay_payload_path(root, fq).unlink(missing_ok=True)  # last binding gone -> drop the copy
        (lock.get("packages") or {}).pop(fq, None)
    save_lock(root, lock)
    eprint(f"rig overlay remove: '{fq}' unbound from '{instance}'")
    return 0


def reorder(root: Path, instance: str, refs: list[str]) -> int:
    sensor = _find_instance(root, instance)
    current = list(sensor.overlays)
    full = []
    for ref in refs:  # accept bare names as shorthand for the bound fq refs
        hit = [o for o in current if _bound_match(o, ref)]
        if not hit:
            raise RigError(f"overlay reorder: '{ref}' is not bound to '{instance}'")
        if len(hit) > 1:
            raise RigError(f"overlay reorder: '{ref}' is ambiguous ({', '.join(hit)}) — "
                           f"use the fully-qualified refs")
        full.append(hit[0])
    if sorted(full) != sorted(current):
        raise RigError(f"overlay reorder: give the COMPLETE new order — bound: "
                       f"{', '.join(current)}")

    edit_row(root, instance, lambda row: row.__setitem__("overlays", full))
    lock = load_lock(root)
    _record_bindings(lock, root, instance, sensor.profile, full)
    save_lock(root, lock)
    eprint(f"rig overlay reorder: '{instance}' -> {', '.join(full)} (last wins)")
    return 0


def _binding_status(root: Path, sensor: Sensor, ref: str, currents: dict) -> str:
    """Annotations for one binding: payload copy present? newer version synced? keys masked by
    local state? — `overlay list` as a status view, not a bare enumeration. Fail-soft on
    registry state (listing must work offline)."""
    from .refs import parse_ref
    notes = []
    copy = overlay_payload_path(root, ref)
    if not copy.is_file():
        notes.append("payload copy MISSING (re-apply)")
    ns, name, pinned = parse_ref(ref)
    if ns not in currents:
        from .registries import load_entries, open_registry
        currents[ns] = None
        entry = next((e for e in load_entries() if e.name == ns), None)
        if entry is not None:
            try:
                currents[ns] = open_registry(entry)[1].get("packages") or {}
            except RigError:
                pass
    current = ((currents[ns] or {}).get(name) or {}).get("version")
    if current and current != pinned:
        notes.append(f"{current} available (pkg upgrade rebinds)")
    if copy.is_file():
        from .workingcopy import local_delta
        state = local_delta(root, sensor)
        if state:
            local_paths = set(_flat_paths(state[0])) | set(_flat_paths(state[1]))
            masked = local_paths & set(_flat_paths(_strip(load_yaml(copy))))
            if masked:
                notes.append(f"{len(masked)} key(s) masked by local")
    return f"   [{'; '.join(notes)}]" if notes else ""


def _strip(data: dict) -> dict:
    return {k: v for k, v in (data or {}).items() if k not in ("name", "service")}


def _flat_paths(patch: dict, prefix: str = ""):
    for key, value in (patch or {}).items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict) and value:
            yield from _flat_paths(value, path)
        else:
            yield path


def list_bindings(root: Path, instance: str | None) -> int:
    manifest = load_manifest(root)
    shown = 0
    currents: dict = {}  # ns -> index packages (one registry open per ns, fail-soft)
    for sensor in manifest.sensors:
        if instance and sensor.name != instance:
            continue
        if not sensor.overlays and not instance:
            continue
        shown += 1
        if not sensor.overlays:
            print(f"{sensor.name}: no overlays bound")
            continue
        print(f"{sensor.name}:")
        for i, ref in enumerate(sensor.overlays, 1):
            print(f"  {i}. {ref}{_binding_status(root, sensor, ref, currents)}")
    if not shown:
        print("no overlay bindings in this deployment")
    return 0
