"""``rig pkg install`` — resolve a package from the configured registries and wire it into THIS
deployment. The porcelain `rig add` routes registry refs and ``sensor:<id>`` specs here.

What install produces is a SELF-CONTAINED deployment (the offline/bake story depends on it):

- **service**: the code repo is fetched at the pinned rev into ``~/.rig/cache/src/`` and its launch
  surface VENDORED into ``services/<name>/`` (services.yaml routes to the vendored copy, not the
  cache) — the deployment never needs the cache again.
- **profile**: its required service is installed transitively (constraint resolved to the exact
  in-registry version), then the payload is materialized as the instance's EDITABLE working config
  (``config/<tier>/<instance>.yaml`` — copy verbatim, comments preserved) with the payload hash
  anchored in rig.lock. Edit the file directly; ``config diff`` and ``pkg promote`` diff against
  the anchored base (the git-working-copy mental model).
- **profile-less service** (``rig add public/zenoh-router``): the working config comes from the
  service's declared ``examples:`` at the pinned rev — the same convention ``init --discover`` and
  ``fetch`` use — hash-anchored exactly like a profile payload.

Instance names stay operator-chosen, ROS-safe (never ``service@profile`` — `name` keys the compose
project/volumes/ROS namespace): default = the package name sanitized, ``--as`` on collision.

``--locked`` reproduces: resolved version and payload hash must equal rig.lock's records — the
content hash is the guarantee, so a moved registry HEAD with identical payload still passes.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from . import RigError
from .common import eprint, load_yaml
from .descriptor import load_descriptor
from .init import _append_services_line, _append_tier_row, _safe_name
from .lock import load_lock, record_instance, record_package, record_registry, save_lock, sha256_file
from .manifest import load_manifest
from .pkg import _each_index, _entries_or_hint, _sensor_hits
from .registries import Entry, rig_home
from .registry import Package, Registry, _CONSTRAINT, constraint_satisfied
from .vendor import vendor

_INSTANCE_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def qualified(entry: Entry, pkg: Package) -> str:
    return f"{entry.name}/{pkg.name}@{pkg.version}"


def registry_commit(entry: Entry) -> str | None:
    if entry.type != "git":
        return None
    proc = subprocess.run(["git", "-C", str(entry.root), "rev-parse", "HEAD"],
                          capture_output=True, text=True)
    return proc.stdout.strip() or None


def resolve_ref(ref: str) -> tuple[Entry, Registry, Package]:
    """`[registry/]name[@version]` -> the package, priority order for unqualified names. An explicit
    @version must equal the registry's current version (history lives in git; `--locked` is the
    reproduction path)."""
    want_version = None
    if "@" in ref:
        ref, want_version = ref.split("@", 1)
    ns, _, name = ref.rpartition("/")
    entries = _entries_or_hint()
    if ns:
        entries = [e for e in entries if e.name == ns]
        if not entries:
            raise RigError(f"install: no registry named '{ns}' (rig registry list)")
    for entry, reg, _ in _each_index(entries):
        pkg = reg.packages.get(name)
        if pkg is None:
            continue
        if want_version and pkg.version != want_version:
            raise RigError(f"install: {entry.name}/{name} is at {pkg.version}, not {want_version} "
                           f"(registries carry ONE current version; use --locked to reproduce pins)")
        return entry, reg, pkg
    raise RigError(f"install: '{ref}' not found in any configured registry (rig registry sync?)")


def resolve_sensor(ident: str) -> tuple[Entry, Registry, Package]:
    """`sensor:<id>` -> the matching profile: precedence exact → glob → fallback; the first registry
    (priority order) with any hit answers; same-tier ambiguity inside it demands an explicit choice."""
    for entry, reg, index in _each_index(_entries_or_hint()):
        hits = _sensor_hits(index, ident)
        if not hits:
            continue
        best_tier = hits[0][1]
        best = [profile for profile, tier in hits if tier == best_tier]
        if len(best) > 1:
            raise RigError(f"install sensor:{ident}: ambiguous at tier '{best_tier}' in "
                           f"'{entry.name}': {', '.join(sorted(best))} — install one by name")
        pkg = reg.packages.get(best[0])
        if pkg is not None:
            return entry, reg, pkg
    raise RigError(f"install: no profile matches sensor:{ident} in any configured registry")


def _resolve_required_service(entry: Entry, reg: Registry, profile: Package) -> tuple[Entry, Package]:
    req = (profile.manifest.get("requires") or {}).get("service") or ""
    match = _CONSTRAINT.match(str(req))
    if not match:
        raise RigError(f"install: profile {profile.name} has an invalid requires.service: {req!r}")
    if match["ns"] and match["ns"] != entry.name:  # cross-registry requires — resolve by its ns
        svc_entry, _, service = resolve_ref(f"{match['ns']}/{match['name']}")
    else:
        svc_entry, service = entry, reg.packages.get(match["name"])
    if service is None or service.kind != "service":
        raise RigError(f"install: requires.service '{req}' does not resolve to a service")
    if not constraint_satisfied(match["ver"], bool(match["caret"]), service.version):
        raise RigError(f"install: {profile.name} requires {req} but the registry carries "
                       f"{service.name}@{service.version}")
    return svc_entry, service


def _fetch_source(name: str, source: dict) -> Path:
    """Clone the service's code repo at the pinned rev into the src cache (reused when already
    present at that rev — offline after first fetch). Returns the service dir (source.path aware)."""
    repo_url, rev = str(source.get("repo") or ""), str(source.get("rev") or "")
    dest = rig_home() / "cache" / "src" / f"{name}-{rev[:12]}"

    def git(*args):
        return subprocess.run(["git", "-C", str(dest), *args], capture_output=True, text=True)

    if not (dest / ".git").is_dir():
        dest.parent.mkdir(parents=True, exist_ok=True)
        clone = subprocess.run(["git", "clone", "-q", repo_url, str(dest)],
                               capture_output=True, text=True)
        if clone.returncode != 0:
            detail = (clone.stderr or "").strip().splitlines()
            raise RigError(f"install {name}: clone failed: {repo_url} "
                           f"({detail[-1] if detail else 'git error'})")
        if git("checkout", "-q", "--detach", rev).returncode != 0:
            raise RigError(f"install {name}: rev {rev[:12]}… not found in {repo_url}")
    head = git("rev-parse", "HEAD").stdout.strip()
    if head != rev:  # the cache dir is rev-keyed, so this only fires on tampering/corruption
        raise RigError(f"install {name}: source cache at {dest} is at {head[:12]}…, expected "
                       f"{rev[:12]}… — remove the dir and retry")
    service_dir = dest / str(source["path"]) if source.get("path") else dest
    if not service_dir.is_dir():
        raise RigError(f"install {name}: source.path '{source.get('path')}' not in the repo")
    return service_dir


def _check_locked(lock: dict, ref: str, *, locked: bool, payload_sha: str | None = None) -> None:
    """--locked: what we resolved must BE what the lock records (version via the ref key; content
    via the payload hash — the hash is the guarantee, commits are provenance)."""
    if not locked:
        return
    name = ref.split("@")[0]
    recorded = [r for r in (lock.get("packages") or {}) if r.split("@")[0] == name]
    if ref not in (lock.get("packages") or {}):
        have = f" (lock has {', '.join(recorded)})" if recorded else ""
        raise RigError(f"install --locked: {ref} is not the locked pin{have} — sync the registry "
                       f"to the locked state or install without --locked to re-pin")
    want = (lock["packages"][ref] or {}).get("payload_sha256")
    if want and payload_sha and want != payload_sha:
        raise RigError(f"install --locked: {ref} payload hash differs from the lock "
                       f"({payload_sha[:12]}… vs {want[:12]}…) — registry content changed under "
                       f"the pin")


def _next_order(root: Path, tier: str) -> int:
    manifest = load_manifest(root)
    orders = [s.order for s in manifest.sensors if s.tier == tier]
    return 0 if tier == "infra" and not orders else max(orders, default=0) + (5 if tier == "infra" else 10)


def _wire_row(root: Path, *, row: str, section: str) -> None:
    """Append one row to vehicle.yaml's tier section (generated block form only — same belt-and-braces
    contract as `rig add`: on a hand-authored shape, print the paste-ready line, touch nothing)."""
    veh = root / "vehicle.yaml"
    new = _append_tier_row(veh.read_text(), section, row)
    if new is None:
        eprint(f"install: vehicle.yaml isn't in the generated block form — paste under `{section}:`:"
               f"\n  {row}")
        return
    veh.write_text(new)


def _route_service(root: Path, svc: str) -> None:
    svc_path = root / "services.yaml"
    line = f"  {svc}: {{ path: services/{svc} }}"
    text = svc_path.read_text() if svc_path.exists() else "services:\n"
    routes = (load_yaml(svc_path) or {}).get("services") or {} if svc_path.exists() else {}
    if svc in routes:
        return  # already routed (vendored refresh is fine; a foreign route is respected)
    new = _append_services_line(text, line)
    if new is None:
        eprint(f"install: services.yaml isn't in the generated block form — paste under `services:`:"
               f"\n{line}")
        return
    svc_path.write_text(new)


def _install_service(root: Path, entry: Entry, pkg: Package, lock: dict, *, locked: bool):
    """Fetch + vendor + route one service package; returns its Descriptor (from the vendored copy).
    Idempotent: an already-vendored service is refreshed only when the pin changed."""
    ref = qualified(entry, pkg)
    source = pkg.manifest.get("source")
    if not isinstance(source, dict) or not source.get("repo"):
        raise RigError(f"install: {ref} has no source (image-only services carry no launcher — "
                       f"not installable as a stack)")
    _check_locked(lock, ref, locked=locked)
    already = (lock.get("packages") or {}).get(ref)
    vendored = root / "services" / pkg.name
    if not (already and (vendored / ".vendored.yaml").exists()):
        service_dir = _fetch_source(pkg.name, source)
        from .descriptor import find_descriptor
        desc_path = find_descriptor(service_dir)
        declared = load_yaml(desc_path).get("service") if desc_path else None
        if declared and declared != pkg.name:
            raise RigError(
                f"install: package '{ref}' points at a repo whose rigging.yaml declares service "
                f"'{declared}' — registry package names MUST equal the declared service name (it "
                f"keys the services.yaml route, the vehicle.yaml row, and services/<name>/). "
                f"Rename the package to '{declared}' (fix the workflow that publishes it), then "
                f"re-sync")
        vendor(pkg.name, service_dir, root)
    _route_service(root, pkg.name)
    record_registry(lock, entry.name, rtype=entry.type, location=entry.location,
                    commit=registry_commit(entry))
    record_package(lock, ref, {"kind": "service", "source": {
        "repo": source.get("repo"), "rev": source.get("rev"),
        **({"path": source["path"]} if source.get("path") else {})}})
    return load_descriptor(pkg.name, vendored)


def _materialize_instance(root: Path, *, svc: str, desc, instance: str | None, base_src: Path,
                          profile_ref: str | None, lock: dict, enabled: bool) -> str:
    """Write the working config + the vehicle.yaml row + the lock anchor for one new instance."""
    manifest = load_manifest(root)
    base_data = load_yaml(base_src) if base_src.suffix == ".yaml" else {}
    embedded = str(base_data.get("name") or "") if isinstance(base_data, dict) else ""
    if instance:
        name = instance
    elif profile_ref:  # default: the profile name, made ROS-safe (siyi-zr30 -> siyi_zr30)
        name = _safe_name(profile_ref.rpartition("/")[-1].split("@", 1)[0])
    elif embedded:  # profile-less service whose example is a NAMED config — honor its name
        name = embedded
    else:
        name = _safe_name(base_src.name.replace(".example.yaml", "").replace(".yaml", ""))
    # ROS-safety is enforced on names RIG derives; a service author's own embedded example name
    # (e.g. rig-infra's `zenoh-router`) is accepted verbatim — the existing shipped convention,
    # and the manifest cross-check requires the row to match it anyway.
    if name != embedded and not _INSTANCE_RE.match(name):
        raise RigError(f"install: instance name '{name}' must be ROS-safe ([a-z][a-z0-9_]*) — "
                       f"pass --as <name>")
    if any(s.name == name for s in manifest.sensors):
        raise RigError(f"install: instance name '{name}' already exists in vehicle.yaml — "
                       f"pass --as <name> (duplicate hardware needs distinct names; updating an "
                       f"existing instance is `rig pkg upgrade {name}`)")
    tier = desc.tier if desc.tier in ("infra", "autonomy") else "sensor"
    sub = {"infra": "infra", "sensor": "sensors", "autonomy": "autonomy"}[tier]
    dest = root / "config" / sub / f"{name}.yaml"
    if dest.exists():
        raise RigError(f"install: {dest} already exists — pass --as <name>")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if embedded and embedded != name:  # a NAMED example under a different instance name would fail
        from .init import _copy_as_profile  # the manifest cross-check — neutralize its name line
        survived = _copy_as_profile(base_src, dest)
        if survived and survived != name:
            dest.unlink()
            raise RigError(f"install: the example config pins name '{survived}' in a form rig can't "
                           f"neutralize — use --as {survived}, or author the config by hand")
    else:
        dest.write_bytes(base_src.read_bytes())  # VERBATIM — comments survive; this is the working copy
    pin = root / "config" / ".pins" / f"{name}.yaml"
    pin.parent.mkdir(parents=True, exist_ok=True)
    pin.write_bytes(dest.read_bytes())  # the PRISTINE base copy — config diff/pkg upgrade anchor on it
    base_sha = sha256_file(dest)
    order = _next_order(root, tier)
    profile_part = f"profile: {profile_ref}, " if profile_ref else ""
    row = (f"- {{ name: {name}, service: {svc}, config: config/{sub}/{name}.yaml, "
           f"{profile_part}enabled: {str(bool(enabled)).lower()}, order: {order} }}")
    _wire_row(root, row=row, section=sub)
    record_instance(lock, name, profile=profile_ref, base_sha256=base_sha)
    eprint(f"  instance '{name}' ({svc}, {tier}) — working config config/{sub}/{name}.yaml, "
           f"base pinned {base_sha[:12]}…")
    return name


def _snapshot(root: Path) -> dict:
    """Everything a rollback needs: the three mutable texts + the set of files under the dirs
    install creates into (services/, config/) — created files are exactly the after-minus-before."""
    files = {p for d in ("services", "config") for p in (root / d).rglob("*") if p.is_file()}
    return {"texts": {n: (root / n).read_text() if (root / n).exists() else None
                      for n in ("vehicle.yaml", "services.yaml", "rig.lock")},
            "files": files}


def _rollback(root: Path, snap: dict) -> None:
    import shutil
    for rel, text in snap["texts"].items():
        path = root / rel
        if text is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(text)
    now = {p for d in ("services", "config") for p in (root / d).rglob("*") if p.is_file()}
    for created in now - snap["files"]:
        created.unlink(missing_ok=True)
    for d in ("services", "config"):  # prune dirs the deletions emptied
        for sub in sorted((root / d).rglob("*"), reverse=True):
            if sub.is_dir() and not any(sub.iterdir()):
                shutil.rmtree(sub, ignore_errors=True)


def _install_suite(root: Path, entry: Entry, pkg: Package, *, locked: bool) -> int:
    """Atomic (OQ-9, plan-validate-then-write approximated as all-or-rollback): resolve and install
    every member; ANY failure restores vehicle.yaml/services.yaml/rig.lock and removes every file
    this install created — the deployment is untouched or fully installed, never half."""
    from .overlay import apply as overlay_apply
    members = pkg.manifest.get("members") or {}
    ref = qualified(entry, pkg)
    eprint(f"rig install: suite {ref}")
    snap = _snapshot(root)
    before_names = {s.name for s in load_manifest(root).sensors}
    try:
        for member in members.get("services") or []:
            m_entry, _, m_pkg = resolve_ref(str(member).split("@", 1)[0])
            if m_pkg.version != str(member).rpartition("@")[-1]:
                raise RigError(f"suite member {member}: registry carries {m_pkg.version} — the "
                               f"exact pin is uninstallable at this sync state")
            lock = load_lock(root)
            desc = _install_service(root, m_entry, m_pkg, lock, locked=locked)
            save_lock(root, lock)
            examples = [root / "services" / m_pkg.name / e for e in desc.examples]
            examples = [e for e in examples if e.is_file()]
            if examples and not any(s.service == m_pkg.name for s in load_manifest(root).sensors):
                lock = load_lock(root)
                _materialize_instance(root, svc=m_pkg.name, desc=desc, instance=None,
                                      base_src=examples[0], profile_ref=None, lock=lock,
                                      enabled=True)
                save_lock(root, lock)
        for member in members.get("profiles") or []:
            m_entry, m_reg, m_pkg = resolve_ref(str(member).split("@", 1)[0])
            if m_pkg.version != str(member).rpartition("@")[-1]:
                raise RigError(f"suite member {member}: registry carries {m_pkg.version} — the "
                               f"exact pin is uninstallable at this sync state")
            install(root, f"{m_entry.name}/{m_pkg.name}", locked=locked)
        created = [s for s in load_manifest(root).sensors if s.name not in before_names]
        for member in members.get("overlays") or []:
            m_entry, _, m_pkg = resolve_ref(str(member).split("@", 1)[0])
            if m_pkg.version != str(member).rpartition("@")[-1]:
                raise RigError(f"suite member {member}: registry carries {m_pkg.version} — the "
                               f"exact pin is uninstallable at this sync state")
            targets = [s.name for s in created if _overlay_covers(m_pkg.manifest, s)]
            if not targets:
                raise RigError(f"suite member {member}: no instance created by this suite matches "
                               f"its targets — the suite is inconsistent with its members")
            for instance in targets:
                overlay_apply(root, instance, f"{m_entry.name}/{m_pkg.name}")
    except BaseException:
        _rollback(root, snap)
        eprint(f"rig install: suite {ref} FAILED — deployment rolled back untouched")
        raise
    eprint(f"rig install: suite {ref} complete")
    return 0


def _overlay_covers(manifest: dict, sensor) -> bool:
    for target in manifest.get("targets") or []:
        if not isinstance(target, dict):
            continue
        if target.get("service") and str(target["service"]).rpartition("/")[-1] == sensor.service:
            return True
        if target.get("instance") == sensor.name:
            return True
    return False


def install(root: Path, spec: str, *, as_name: str | None = None, locked: bool = False) -> int:
    """One spec: `sensor:<id>` | `[registry/]profile` | `[registry/]service` | `[registry/]suite`."""
    lock = load_lock(root)
    if spec.startswith("sensor:"):
        entry, reg, pkg = resolve_sensor(spec[len("sensor:"):])
    else:
        entry, reg, pkg = resolve_ref(spec)
    if pkg.kind == "suite":
        return _install_suite(root, entry, pkg, locked=locked)
    if pkg.kind == "overlay":
        raise RigError(f"install: '{pkg.name}' is an overlay — overlays are BOUND to an instance "
                       f"(rig overlay apply), not installed standalone")

    # Updating, not installing? Catch it BEFORE anything mutates (a failed re-add used to leave a
    # re-vendored services/ dir behind) and point at the right verb.
    if as_name is None:
        rows = load_manifest(root).sensors
        if pkg.kind == "profile":
            held = [s.name for s in rows
                    if s.profile and s.profile.rpartition("/")[-1].split("@")[0] == pkg.name]
        else:
            held = [s.name for s in rows if s.service == pkg.name]
        if held:
            raise RigError(f"install: '{pkg.name}' is already installed (instance"
                           f"{'s' if len(held) > 1 else ''} {', '.join(held)}) — update with "
                           f"`rig pkg upgrade {held[0]}`, or pass --as <name> for a SECOND instance")

    if pkg.kind == "profile":
        ref = qualified(entry, pkg)
        payload_rel = (pkg.manifest.get("config") or {}).get("payload")
        payload_path = pkg.pkg_dir / str(payload_rel)
        if not payload_path.is_file():
            raise RigError(f"install: {ref} payload missing in the synced registry: {payload_rel}")
        _check_locked(lock, ref, locked=locked, payload_sha=sha256_file(payload_path))
        svc_entry, service = _resolve_required_service(entry, reg, pkg)
        desc = _install_service(root, svc_entry, service, lock, locked=locked)
        eprint(f"rig install: {ref} (requires {qualified(svc_entry, service)})")
        record_package(lock, ref, {"kind": "profile", "payload_sha256": sha256_file(payload_path),
                                   "requires": qualified(svc_entry, service)})
        record_registry(lock, entry.name, rtype=entry.type, location=entry.location,
                        commit=registry_commit(entry))
        _materialize_instance(root, svc=service.name, desc=desc, instance=as_name,
                              base_src=payload_path, profile_ref=ref, lock=lock, enabled=True)
    else:  # bare service: base config = its declared example at the pinned rev
        ref = qualified(entry, pkg)
        eprint(f"rig install: {ref}")
        desc = _install_service(root, entry, pkg, lock, locked=locked)
        examples = [root / "services" / pkg.name / e for e in desc.examples]
        examples = [e for e in examples if e.is_file()]
        if examples:
            _materialize_instance(root, svc=pkg.name, desc=desc, instance=as_name,
                                  base_src=examples[0], profile_ref=None, lock=lock, enabled=True)
        else:
            eprint(f"  no example config DECLARED at this pin — route + vendor done; author "
                   f"config/<tier>/<name>.yaml and its vehicle.yaml row yourself. (To automate "
                   f"this: declare `examples:` in the repo's rigging.yaml and cut a release)")
    save_lock(root, lock)
    # The real gate: the deployment must still LOAD (name uniqueness, cross-checks) — surface now.
    load_manifest(root)
    eprint(f"  locked: rig.lock updated — commit it")
    return 0
