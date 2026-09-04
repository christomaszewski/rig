"""``rig pkg add`` (alias: install) — resolve a package from the configured registries and wire it into THIS
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

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

import yaml

from . import RigError
from .common import eprint, load_yaml
from .descriptor import load_descriptor
from .init import _append_services_line, _append_tier_row, _route_span, _safe_name
from .lock import load_lock, record_instance, record_package, record_registry, save_lock, sha256_file
from .manifest import load_manifest
from .pkg import _each_index, _entries_or_hint, _sensor_hits
from .refs import short_name, unqualified
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


def resolve_ref(ref: str, *, history: bool = False) -> tuple[Entry, Registry, Package]:
    """`[registry/]name[@version]` -> the package, priority order for unqualified names. An explicit
    @version must equal the registry's current version — except with `history=True` (pkg add),
    where a git-backed registry serves past versions read-only from its history."""
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
            if history:
                from .history import checkout_pkg, kind_dir_of
                hist = checkout_pkg(entry, kind_dir_of(pkg.kind), name, want_version)
                if hist is not None:
                    return entry, reg, hist
                hint = (" — and that version is not in the registry's git history" if
                        checkout_pkg_capable(entry) else
                        " — historical versions need a git-backed registry (this one has no "
                        "git history)")
            else:
                hint = ""
            raise RigError(f"install: {entry.name}/{name} is at {pkg.version}, not {want_version} "
                           f"(registries carry ONE current version{hint})")
        return entry, reg, pkg
    raise RigError(f"install: '{ref}' not found in any configured registry (rig registry sync?)")


def checkout_pkg_capable(entry: Entry) -> bool:
    from .history import _git_prefix
    return _git_prefix(entry) is not None


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


def profiles_for_service(svc: str) -> list[str]:
    """Every configured registry's profiles for <svc>, fully qualified — miss-message fodder.
    Index-only (keys are `service:short`), so broken registries just skip."""
    hits: list[str] = []
    for entry, _, index in _each_index(_entries_or_hint()):
        hits.extend(f"{entry.name}/{key}" for key in (index.get("packages") or {})
                    if key.startswith(f"{svc}:"))
    return sorted(set(hits))


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
        if not match["caret"]:  # EXACT pin behind head: honor it from git history (a snapshot
            full = ".".join((str(match["ver"]).split(".") + ["0", "0", "0"])[:3])  # stays valid)
            try:
                svc_entry, _, service = resolve_ref(f"{svc_entry.name}/{match['name']}@{full}",
                                                    history=True)
                return svc_entry, service
            except RigError as exc:
                raise RigError(f"install: {profile.name} requires {req} but the registry carries "
                               f"{service.name}@{service.version} and the pinned version is "
                               f"unavailable ({exc}) — `rig pkg repin` the profile, or sync a "
                               f"git-backed registry")
        raise RigError(f"install: {profile.name} requires {req} but the registry carries "
                       f"{service.name}@{service.version}")
    return svc_entry, service


def _repo_slug(url: str) -> str:
    """Cache key for a source repo URL: ``<basename>-<8 hex>`` of the NORMALIZED url (scheme,
    ``user@``, ``:`` vs ``/``, trailing ``.git`` — one repo spelled two ways is one key). The src
    cache is keyed by (repo, rev) rather than by the installing service so services released from
    one collection repo (rig-infra) SHARE a checkout: their common ``../base/build.sh`` then
    resolves to ONE file and build's base-provider dedupe holds across services, instead of two
    per-service clones of identical content reading as two different base builds."""
    norm = re.sub(r"^[a-z+]+://", "", url.strip().lower())  # scheme
    norm = re.sub(r"^[^/@:]+@", "", norm)                   # user@ (ssh / scp-like)
    norm = norm.replace(":", "/").rstrip("/")
    norm = norm[:-4] if norm.endswith(".git") else norm
    stem = re.sub(r"[^a-z0-9._-]", "-", norm.rsplit("/", 1)[-1]) or "repo"
    return f"{stem}-{hashlib.sha256(norm.encode()).hexdigest()[:8]}"


def _fetch_source(name: str, source: dict) -> Path:
    """Clone the service's code repo at the pinned rev into the src cache (reused when already
    present at that rev — offline after first fetch). Keyed by (repo, rev) via _repo_slug, so
    every service pinned at one repo rev shares one checkout. Returns the service dir
    (source.path aware)."""
    repo_url, rev = str(source.get("repo") or ""), str(source.get("rev") or "")
    dest = rig_home() / "cache" / "src" / f"{_repo_slug(repo_url)}-{rev[:12]}"
    legacy = dest.parent / f"{name}-{rev[:12]}"  # pre-v0.2.25 per-service key: adopt, don't reclone
    if not dest.exists() and legacy != dest and (legacy / ".git").is_dir():
        legacy.rename(dest)  # same repo at the same rev — the tamper check below still verifies

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
    if (dest / ".gitmodules").is_file():  # driver source via submodules: the superproject commit
        sub = git("submodule", "update", "--init", "--recursive", "--quiet")  # pins their revs —
        if sub.returncode != 0:           # exact-pin transitively. No-op when already initialized
            detail = (sub.stderr or "").strip().splitlines()
            raise RigError(f"install {name}: submodule init failed at {rev[:12]}… "
                           f"({detail[-1] if detail else 'git error'}) — are the submodule URLs "
                           f"reachable from this machine?")
    service_dir = dest / str(source["path"]) if source.get("path") else dest
    if not service_dir.is_dir():
        raise RigError(f"install {name}: source.path '{source.get('path')}' not in the repo")
    return service_dir


def _check_locked(lock: dict, ref: str, *, locked: bool, payload_sha: str | None = None,
                  source: dict | None = None) -> None:
    """--locked: what we resolved must BE what the lock records (version via the ref key; content
    via the payload hash for profiles/overlays and source.repo/rev for services — a registry that
    rewrote a rev under the same version must FAIL, not fetch different code)."""
    if not locked:
        return
    name = ref.split("@")[0]
    recorded = [r for r in (lock.get("packages") or {}) if r.split("@")[0] == name]
    if ref not in (lock.get("packages") or {}):
        have = f" (lock has {', '.join(recorded)})" if recorded else ""
        raise RigError(f"install --locked: {ref} is not the locked pin{have} — sync the registry "
                       f"to the locked state or install without --locked to re-pin")
    entry = lock["packages"][ref] or {}
    want = entry.get("payload_sha256")
    if want and payload_sha and want != payload_sha:
        raise RigError(f"install --locked: {ref} payload hash differs from the lock "
                       f"({payload_sha[:12]}… vs {want[:12]}…) — registry content changed under "
                       f"the pin")
    locked_src = entry.get("source") or {}
    if source and locked_src:
        for field in ("repo", "rev"):
            if locked_src.get(field) and str(source.get(field)) != str(locked_src[field]):
                raise RigError(f"install --locked: {ref} source.{field} differs from the lock "
                               f"({source.get(field)} vs {locked_src[field]}) — the registry "
                               f"rewrote the pin's provenance; refusing to fetch different code")


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
               f"\n  {row}\n"
               f"  (the install is INCOMPLETE until pasted: config + pin + lock anchor are staged, "
               f"and `rig pkg lock` before pasting would drop the anchor)")
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


def _install_service(root: Path, entry: Entry, pkg: Package, lock: dict, *, locked: bool,
                     allow_repin: bool = False):
    """Fetch + vendor + route one service package; returns its Descriptor (from the vendored copy).
    Idempotent: an already-vendored service is refreshed only when the pin changed. A DIFFERENT
    pin for an already-locked service is an ERROR unless allow_repin (pkg upgrade's deliberate
    path) — services are SHARED: silently re-vendoring moves the code under every instance that
    uses it, and the stale ref would linger as a duplicate lock row."""
    ref = qualified(entry, pkg)
    source = pkg.manifest.get("source")
    if not isinstance(source, dict) or not source.get("repo"):
        raise RigError(f"install: {ref} has no source (image-only services carry no launcher — "
                       f"not installable as a stack)")
    _check_locked(lock, ref, locked=locked, source=source)
    other = [r for r, info in (lock.get("packages") or {}).items()
             if (info or {}).get("kind") == "service" and r != ref
             and unqualified(r) == pkg.name]
    if other and not allow_repin:
        raise RigError(f"install: service '{pkg.name}' is locked at {other[0]} but this install "
                       f"needs {ref} — a service is SHARED by every instance using it; move the "
                       f"pin deliberately with `rig pkg upgrade` first")
    if other and allow_repin:
        for stale in other:  # upgrading: the old pin's lock row must not linger as a duplicate
            (lock.get("packages") or {}).pop(stale, None)
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
    if not (locked and entry.name in (lock.get("registries") or {})):
        record_registry(lock, entry.name, rtype=entry.type, location=entry.location,
                        commit=registry_commit(entry))  # --locked reproduces FROM that commit —
    #                                                     never clobber the anchor it used
    record_package(lock, ref, {"kind": "service", "source": {
        "repo": source.get("repo"), "rev": source.get("rev"),
        **({"path": source["path"]} if source.get("path") else {})}})
    return load_descriptor(pkg.name, vendored)


def _materialize_instance(root: Path, *, svc: str, desc, instance: str | None, base_src: Path,
                          profile_ref: str | None, lock: dict, enabled: bool,
                          tier: str | None = None, order: int | None = None) -> str:
    """Write the working config + the vehicle.yaml row + the lock anchor for one new instance.
    `tier`/`order` override the descriptor-derived tier and next-available order — the
    vehicle-plan install path places rows exactly where the plan says."""
    manifest = load_manifest(root)
    base_data = load_yaml(base_src) if base_src.suffix == ".yaml" else {}
    embedded = str(base_data.get("name") or "") if isinstance(base_data, dict) else ""
    if instance:
        name = instance
    elif profile_ref:  # default: the profile's SHORT name, made ROS-safe (siyi-zr30 -> siyi_zr30)
        name = _safe_name(short_name(profile_ref))
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
    if tier not in ("infra", "sensor", "autonomy"):
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
    if order is None:
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
    """Everything a rollback needs: the three mutable texts + the CONTENTS of every file under the
    dirs pkg verbs mutate (services/, config/) — contents, not just the file set, because upgrade
    REWRITES working configs and pins in place (a set-only snapshot can undo creations but not
    modifications). These are small text surfaces; holding them in memory is cheap."""
    files = {p: p.read_bytes()
             for d in ("services", "config") for p in (root / d).rglob("*") if p.is_file()}
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
    for created in now - set(snap["files"]):
        created.unlink(missing_ok=True)
    for path, blob in snap["files"].items():  # restore modified + deleted originals
        if not path.exists() or path.read_bytes() != blob:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(blob)
    for d in ("services", "config"):  # prune dirs the deletions emptied
        for sub in sorted((root / d).rglob("*"), reverse=True):
            if sub.is_dir() and not any(sub.iterdir()):
                shutil.rmtree(sub, ignore_errors=True)


def _member_pkg(member) -> tuple:
    """Resolve one suite member at its EXACT pin. At head: the current package. Behind head: the
    pinned version from the registry's git history (a suite's value IS its exact set — a member's
    later release must not make the suite uninstallable; `pkg outdated` reports the drift and
    `pkg repin` refreshes). A non-git registry, or a version history never carried, fails with
    the pointed hint."""
    ref = str(member)
    m_entry, m_reg, m_pkg = resolve_ref(ref.split("@", 1)[0])
    want = ref.rpartition("@")[-1]
    if m_pkg.version != want:
        head = m_pkg.version
        try:
            m_entry, m_reg, m_pkg = resolve_ref(ref, history=True)
        except RigError as exc:
            raise RigError(f"suite member {ref}: registry carries {head} and the pinned version "
                           f"is unavailable ({exc}) — `rig pkg repin <suite>` to refresh the "
                           f"suite, or sync a git-backed registry")
        eprint(f"  note: suite member {ref} is behind registry-current ({head}) — installing the "
               f"pinned version from history (rig pkg repin <suite> --to <registry> refreshes)")
    return m_entry, m_reg, m_pkg


def _write_vehicle_settings(root: Path, plan: dict) -> None:
    """The plan's vehicle-level settings become the target's vehicle.yaml — IDENTITY excepted:
    the target's pre-install literal `vehicle`/`vehicle_id` fill the plan's marker slots
    (identity belongs to the target/host tiers; the package neither overwrites nor erases it —
    with no target literal the marker stays and the provision hint applies). Row sections are
    omitted here; materialization appends each plan row."""
    existing = load_yaml(root / "vehicle.yaml") if (root / "vehicle.yaml").is_file() else {}
    doc: dict = {}
    for key, value in plan.items():
        if key in ("infra", "sensors", "autonomy"):
            continue
        if key in ("vehicle", "vehicle_id"):
            prior = existing.get(key)
            marker = isinstance(value, str) and f"{{{{{key}}}}}" in value
            prior_literal = prior is not None and not (isinstance(prior, str)
                                                      and f"{{{{{key}}}}}" in prior)
            doc[key] = prior if (marker and prior_literal) else value
        else:
            doc[key] = value
    (root / "vehicle.yaml").write_text(
        "# vehicle.yaml — written by `rig pkg add` from the suite's vehicle plan. Identity stays\n"
        "# THIS machine's (vehicle-local tier / `rig provision` supply it per host).\n"
        + yaml.safe_dump(doc, sort_keys=False, default_flow_style=False))


def _install_planned(root: Path, suite_ref: str, plan: dict, v_entry, v_pkg, v_payload: Path,
                     members: dict, *, locked: bool) -> None:
    """The vehicle-plan install: members are the SOURCES (fetched/vendored/pinned, never
    auto-instantiated), the plan's rows are the INSTANCES — their names, tier placement, order,
    enabled flags, overrides, and per-row overlay bindings in row order."""
    from .overlay import apply as overlay_apply, edit_row
    _write_vehicle_settings(root, plan)
    service_descs: dict[str, object] = {}
    for member in members.get("services") or []:
        m_entry, _, m_pkg = _member_pkg(member)
        lock = load_lock(root)
        service_descs[m_pkg.name] = _install_service(root, m_entry, m_pkg, lock, locked=locked)
        save_lock(root, lock)
    profile_bases: dict[str, tuple] = {}  # member name -> (ref, payload_path, service, desc)
    for member in members.get("profiles") or []:
        m_entry, m_reg, m_pkg = _member_pkg(member)
        lock = load_lock(root)
        prepped = _prep_profile(root, m_entry, m_reg, m_pkg, lock, locked=locked)
        save_lock(root, lock)
        profile_bases[m_pkg.name] = prepped
        service_descs.setdefault(prepped[2], prepped[3])
    overlay_refs: dict[str, str] = {}  # member name -> consumer-alias ref for overlay apply
    for member in members.get("overlays") or []:
        m_entry, _, m_pkg = _member_pkg(member)
        overlay_refs[m_pkg.name] = f"{m_entry.name}/{m_pkg.name}@{m_pkg.version}"  # exact pin
    lock = load_lock(root)  # the plan itself is provenance
    record_package(lock, qualified(v_entry, v_pkg),
                   {"kind": "vehicle", "payload_sha256": sha256_file(v_payload)})
    save_lock(root, lock)

    tier_of = {"infra": "infra", "sensors": "sensor", "autonomy": "autonomy"}
    for section in ("infra", "sensors", "autonomy"):
        for row in plan.get(section) or []:
            if not isinstance(row, dict) or not (row.get("name") and row.get("service")):
                continue
            name = str(row["name"])
            if row.get("profile"):
                pname = unqualified(str(row["profile"]))
                if pname not in profile_bases:
                    raise RigError(f"suite {suite_ref}: plan row '{name}' references profile "
                                   f"'{row['profile']}' with no matching member — the suite is "
                                   f"not closed (registry validate reports this)")
                pref, base_src, svc_name, desc = profile_bases[pname]
            else:
                svc_name = str(row["service"])
                desc = service_descs.get(svc_name)
                if desc is None:
                    raise RigError(f"suite {suite_ref}: bare plan row '{name}' needs a services: "
                                   f"member for '{svc_name}' — the suite is not closed")
                examples = [e for e in (root / "services" / svc_name / x for x in desc.examples)
                            if e.is_file()]
                if not examples:
                    raise RigError(f"suite {suite_ref}: plan row '{name}': service '{svc_name}' "
                                   f"declares no example config at this pin — nothing to "
                                   f"materialize the row from")
                pref, base_src = None, examples[0]
            lock = load_lock(root)
            _materialize_instance(root, svc=svc_name, desc=desc, instance=name,
                                  base_src=base_src, profile_ref=pref, lock=lock,
                                  enabled=bool(row.get("enabled", True)),
                                  tier=tier_of[section],
                                  order=(int(row["order"]) if row.get("order") is not None
                                         else None))
            save_lock(root, lock)
            if isinstance(row.get("overrides"), dict) and row["overrides"]:
                edit_row(root, name, lambda r, o=dict(row["overrides"]): r.update(overrides=o))
            for oref in row.get("overlays") or []:
                oname = unqualified(str(oref))
                if oname not in overlay_refs:
                    raise RigError(f"suite {suite_ref}: plan row '{name}': overlay '{oref}' has "
                                   f"no matching member — the suite is not closed")
                overlay_apply(root, name, overlay_refs[oname])
    load_manifest(root)  # the gate: the planned deployment must load (uniqueness, cross-checks)


def _install_suite(root: Path, entry: Entry, pkg: Package, *, locked: bool) -> int:
    """Atomic (OQ-9, plan-validate-then-write approximated as all-or-rollback): resolve and install
    every member; ANY failure restores vehicle.yaml/services.yaml/rig.lock and removes every file
    this install created — the deployment is untouched or fully installed, never half. A suite
    carrying a vehicle member installs PLAN-DRIVEN (rows decide instances) into an empty
    deployment; without one, the classic path runs (default names, one instance per profile)."""
    from .overlay import apply as overlay_apply
    members = pkg.manifest.get("members") or {}
    ref = qualified(entry, pkg)
    vmembers = members.get("vehicles") or []
    if len(vmembers) > 1:
        raise RigError(f"suite {ref}: carries {len(vmembers)} vehicle members — at most one "
                       f"(the instance plan)")
    plan = v_entry = v_pkg = v_payload = None
    if vmembers:
        v_entry, _, v_pkg = _member_pkg(vmembers[0])
        vrel = (v_pkg.manifest.get("config") or {}).get("payload") or "config/vehicle.yaml"
        v_payload = v_pkg.pkg_dir / str(vrel)
        plan = load_yaml(v_payload)
        if not isinstance(plan, dict):
            raise RigError(f"suite member {vmembers[0]}: vehicle payload is not a mapping")
    eprint(f"rig install: suite {ref}" + (" — vehicle plan drives the rows" if plan else ""))
    snap = _snapshot(root)
    before_names = {s.name for s in load_manifest(root).sensors}
    if plan is not None and before_names:
        raise RigError(f"suite {ref} carries a vehicle plan — it installs into an EMPTY "
                       f"deployment (this one has: {', '.join(sorted(before_names))}); "
                       f"use a fresh `rig init` tree")
    try:
        if plan is not None:
            _install_planned(root, ref, plan, v_entry, v_pkg, v_payload, members, locked=locked)
            eprint(f"rig install: suite {ref} complete")
            return 0
        for member in members.get("services") or []:
            m_entry, _, m_pkg = _member_pkg(member)
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
            m_entry, m_reg, m_pkg = _member_pkg(member)
            install(root, f"{m_entry.name}/{m_pkg.name}@{m_pkg.version}", locked=locked)
        created = [s for s in load_manifest(root).sensors if s.name not in before_names]
        for member in members.get("overlays") or []:
            m_entry, _, m_pkg = _member_pkg(member)
            targets = [s.name for s in created if _overlay_covers(m_pkg.manifest, s)]
            if not targets:
                raise RigError(f"suite member {member}: no instance created by this suite matches "
                               f"its targets — the suite is inconsistent with its members (an "
                               f"overlay from a bare service-backed instance needs a services: "
                               f"member alongside it; re-promote the suite with rig ≥ 0.2.16, "
                               f"which emits and validates that)")
            for instance in targets:
                overlay_apply(root, instance, f"{m_entry.name}/{m_pkg.name}@{m_pkg.version}")
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


def _delete_row(root: Path, instance: str) -> bool:
    """Drop one generated single-line row from vehicle.yaml; False = shape rig can't edit
    (instructions printed, nothing touched)."""
    veh = root / "vehicle.yaml"
    lines = veh.read_text().splitlines()
    hits = [i for i, line in enumerate(lines)
            if re.match(r"^\s*- \{.*\bname: " + re.escape(instance) + r"[,}]", line)]
    if len(hits) != 1:
        eprint(f"remove: vehicle.yaml row for '{instance}' "
               f"{'not found' if not hits else 'ambiguous'} or hand-authored — delete it yourself")
        return False
    del lines[hits[0]]
    new = "\n".join(lines) + "\n"
    import yaml as _yaml
    _yaml.safe_load(new)  # belt: never write a file that will not parse
    veh.write_text(new)
    return True


def _menu_lines(root: Path, svc: str) -> list[tuple[int, str]]:
    """Generated commented MENU rows for a service in vehicle.yaml, as (line index, line)."""
    veh = root / "vehicle.yaml"
    if not veh.is_file():
        return []
    return [(i, line) for i, line in enumerate(veh.read_text().splitlines())
            if re.match(r"^\s*#\s*- \{.*\bservice: " + re.escape(svc) + r"[,}]", line)]


def _drop_menu_lines(root: Path, svc: str) -> None:
    hits = [i for i, _ in _menu_lines(root, svc)]
    if not hits:
        return
    veh = root / "vehicle.yaml"
    lines = veh.read_text().splitlines()
    for i in reversed(hits):
        del lines[i]
    new = "\n".join(lines) + "\n"
    import yaml as _yaml
    _yaml.safe_load(new)  # belt: never write a file that will not parse
    veh.write_text(new)
    eprint(f"  {len(hits)} commented menu row(s) for '{svc}' removed from vehicle.yaml")


def _remove_local_service(root: Path, svc: str, *, purge_config: bool) -> None:
    """Undo a PATH/workspace add for a service with no lock rows: drop the generated route and
    menu comment(s); a vendored copy under services/ is deleted, the checkout the route pointed
    at is NEVER touched. Copied menu configs are kept unless --purge-config (they may carry
    authored content rig can't distinguish from the example)."""
    import shutil
    configs = []
    for _, line in _menu_lines(root, svc):  # find the copied configs before the comments go
        found = re.search(r"\bconfig: ([^,}\s]+)", line)
        if found:
            configs.append(root / found.group(1))
    _drop_menu_lines(root, svc)
    _drop_route(root, svc)
    vendored = root / "services" / svc
    if (vendored / ".vendored.yaml").exists():
        shutil.rmtree(vendored)
        eprint(f"  service '{svc}': vendored copy removed")
    for cfg in configs:
        if cfg.is_file():
            if purge_config:
                cfg.unlink()
                eprint(f"  {cfg.relative_to(root)}: deleted (--purge-config)")
            else:
                eprint(f"  kept {cfg.relative_to(root)} — --purge-config deletes it")
    eprint(f"  service '{svc}': local route removed (re-add with rig pkg add <path>)")


def _drop_route(root: Path, svc: str) -> None:
    svc_path = root / "services.yaml"
    if not svc_path.is_file():
        return
    lines = svc_path.read_text().splitlines()
    span = _route_span(lines, svc)
    if span is None:
        eprint(f"remove: no '{svc}' route found in services.yaml — remove it yourself")
        return
    del lines[span[0]:span[1]]
    svc_path.write_text("\n".join(lines) + "\n")


def _gc_service(root: Path, lock: dict, svc: str) -> None:
    """Drop a service package once NOTHING uses it: no instance rows, no locked profile requiring
    it. Only ever removes a VENDORED services/ dir — a workspace route target is never touched."""
    import shutil
    packages = lock.get("packages") or {}
    if any(s.service == svc for s in load_manifest(root).sensors):
        return
    still_required = any(
        unqualified(str(info.get("requires") or "")) == svc
        for info in packages.values() if info.get("kind") == "profile")
    if still_required:
        return
    refs = [r for r, info in packages.items()
            if info.get("kind") == "service" and unqualified(r) == svc]
    if not refs:
        return  # not a registry package (workspace-wired) — never GC'd
    vendored = root / "services" / svc
    if (vendored / ".vendored.yaml").exists():
        shutil.rmtree(vendored)
        _drop_route(root, svc)
        eprint(f"  service '{svc}': unused — vendored dir + route removed")
    else:
        eprint(f"  service '{svc}': unused, but services/{svc} is not a vendored dir — "
               f"route left for you to review")
    for ref in refs:
        packages.pop(ref, None)


def remove(root: Path, specs: list[str], *, purge_config: bool = False) -> int:
    """`rig pkg remove <instance…|package|service>` — the inverse of `pkg add`, for EVERY add
    form. Instance form removes the row, bindings, anchors, and (when clean vs its pin) the
    working config; dependency services are GC'd; hand-wired rows are removed too (config kept
    — no pin to prove it clean — unless --purge-config). Package form removes an instance-less
    dependency, and a LOCALLY-ROUTED service name (path/workspace add) drops the route, the
    generated menu comments, and any vendored copy — never the checkout itself. Both forms
    refuse (listing the instances) while anything still uses the target. rig only edits files:
    bring the instance DOWN first — a removed row orphans running containers from rig's view."""
    lock = load_lock(root)
    packages = lock.setdefault("packages", {})
    anchors = lock.setdefault("instances", {})
    for spec in specs:
        manifest = load_manifest(root)
        sensor = next((s for s in manifest.sensors if s.name == spec), None)
        if sensor is None:
            bare = unqualified(spec)
            refs = [r for r in packages if unqualified(r) == bare]
            if not refs:
                from .catalog import load_catalog
                try:
                    catalog = load_catalog(root)
                except RigError:
                    catalog = {}
                if bare in catalog:  # a LOCAL route (path/workspace add, no lock rows) — undo it
                    users = [s.name for s in manifest.sensors if s.service == bare]
                    if users:
                        raise RigError(f"remove: '{bare}' is used by instance"
                                       f"{'s' if len(users) > 1 else ''} {', '.join(users)} — "
                                       f"remove those instead (rig pkg remove {users[0]})")
                    _remove_local_service(root, bare, purge_config=purge_config)
                    continue
                raise RigError(f"remove: '{spec}' is neither an instance, an installed package, "
                               f"nor a routed service (rig pkg list shows all three)")
            ref, kind = refs[0], (packages[refs[0]] or {}).get("kind")
            if kind == "service":
                users = [s.name for s in manifest.sensors if s.service == bare]
            elif kind == "profile":
                users = [s.name for s in manifest.sensors
                         if s.profile and unqualified(s.profile) == bare]
            else:
                users = [s.name for s in manifest.sensors if any(o == ref for o in s.overlays)]
            if users:
                raise RigError(f"remove: {ref} is used by instance{'s' if len(users) > 1 else ''} "
                               f"{', '.join(users)} — remove those instead "
                               f"(rig pkg remove {users[0]})")
            if kind == "service":
                _gc_service(root, lock, bare)
            else:
                packages.pop(ref, None)
                eprint(f"  {ref}: removed from rig.lock (nothing was using it)")
            continue

        if sensor.name not in anchors and not sensor.profile:
            eprint(f"  '{spec}' is hand-wired (no registry provenance): removing the row; the "
                   f"working config has no pin to prove it clean, so it is kept unless "
                   f"--purge-config")
        eprint(f"remove: make sure '{spec}' is DOWN first (`rig down {spec}`) — a removed row "
               f"orphans any running containers from rig's view")
        if not _delete_row(root, sensor.name):
            continue
        # overlay bindings: refcount the payload copies against the REMAINING rows
        remaining = load_manifest(root).sensors
        for fq in sensor.overlays:
            if not any(fq in s.overlays for s in remaining):
                from .resolve import overlay_payload_path
                overlay_payload_path(root, fq).unlink(missing_ok=True)
                packages.pop(fq, None)
                eprint(f"  overlay '{fq}': last binding — payload copy + lock entry removed")
        pin = root / "config" / ".pins" / f"{sensor.name}.yaml"
        working = Path(sensor.config)
        if working.is_file():
            clean = pin.is_file() and sha256_file(working) == sha256_file(pin)
            if clean or purge_config:
                working.unlink()
            else:
                try:
                    shown = working.relative_to(root)
                except ValueError:  # a row may point outside the deployment — still a fine path
                    shown = working
                eprint(f"  kept {shown} — it has local edits (--purge-config deletes it anyway)")
        pin.unlink(missing_ok=True)
        (root / "var" / "rendered" / f"{sensor.name}.yaml").unlink(missing_ok=True)  # no stale render
        anchors.pop(sensor.name, None)
        if sensor.profile:
            others = [s for s in remaining if s.profile == sensor.profile]
            if not others:
                packages.pop(str(sensor.profile), None)
        _gc_service(root, lock, sensor.service)
        if not any((packages[r] or {}).get("kind") == "service"
                   and unqualified(r) == sensor.service for r in packages):
            from .catalog import load_catalog
            try:
                routed = sensor.service in load_catalog(root)
            except RigError:
                routed = False
            # LOCAL service (no lock row): GC the route once nothing references it — no rows
            # left and no menu comments still offering it (mirror of the registry GC).
            if routed and not any(s.service == sensor.service
                                  for s in load_manifest(root).sensors) \
                    and not _menu_lines(root, sensor.service):
                _remove_local_service(root, sensor.service, purge_config=purge_config)
        eprint(f"  instance '{sensor.name}' removed")
    save_lock(root, lock)
    load_manifest(root)  # the gate: the deployment must still load
    eprint("rig pkg remove: done — rig.lock updated, commit it")
    return 0


def _route_set(root: Path, svc: str, target: str) -> bool:
    """Re-point an EXISTING generated services.yaml route at `target`. False (with a paste-ready
    line) when the route is absent or hand-authored — the belt-and-braces rule every file edit
    here follows: never rewrite a shape rig didn't write."""
    svc_path = root / "services.yaml"
    line = f"  {svc}: {{ path: {target} }}"
    if not svc_path.is_file():
        eprint(f"swap: no services.yaml — add the route yourself:\n{line}")
        return False
    lines = svc_path.read_text().splitlines()
    span = _route_span(lines, svc)
    if span is None:
        eprint(f"swap: no '{svc}' route in services.yaml to re-point — add it yourself:\n{line}")
        return False
    start, end, style = span
    if style == "flow":
        lines[start:end] = [line]
    else:
        # BLOCK form (every captured/reconstructed tree): rewrite the `path:` value in place and
        # leave the entry's other keys alone — a minimal edit beats normalizing someone's file.
        keys = [i for i in range(start + 1, end) if re.match(r"^\s+path:\s*", lines[i])]
        if len(keys) != 1:
            eprint(f"swap: the '{svc}' route in services.yaml has no single `path:` line — "
                   f"re-point it yourself:\n{line}")
            return False
        indent = len(lines[keys[0]]) - len(lines[keys[0]].lstrip())
        lines[keys[0]] = f"{' ' * indent}path: {target}"
    svc_path.write_text("\n".join(lines) + "\n")
    return True


def _example_config(desc, base: Path) -> Path | None:
    """The service's first DECLARED example config, resolved under `base` (a checkout or the
    vendored copy). None when the repo declares none — the drift report then simply says nothing."""
    for rel in desc.examples:
        candidate = base / rel
        if candidate.is_file():
            return candidate
    return None


def _config_key_drift(example: Path | None, instances) -> list[str]:
    """Top-level key drift between the NEW version's example config and each KEPT working config.
    Report-only, never a gate: rig is schema-opaque about a service's config, and keeping the
    config across a swap is the whole point — but a renamed or retired key is exactly what makes a
    SIL run fail confusingly, so name it (the replay config-drift doctrine)."""
    lines: list[str] = []
    if example is None:
        return lines
    try:
        want = set((load_yaml(example) or {}).keys())
    except RigError:
        return lines
    for sensor in instances:
        try:
            have = set((load_yaml(sensor.config) or {}).keys())
        except RigError:
            continue
        missing = sorted(want - have - {"name"})   # `name` is stamped by the row, never the config's
        extra = sorted(have - want - {"name", "service"})
        if missing or extra:
            parts = []
            if missing:
                parts.append(f"absent from your config: {', '.join(missing)}")
            if extra:
                parts.append(f"not in the new example: {', '.join(extra)}")
            lines.append(f"  {sensor.name}: config key drift — {'; '.join(parts)}")
        else:
            lines.append(f"  {sensor.name}: config keys match the new example")
    return lines


def swap(root: Path, service: str, token: str, *, reset_config: bool = False,
         locked: bool = False) -> int:
    """`rig swap <service> <path|ref>` — re-point an INSTALLED service at a different source,
    keeping the deployment's own wiring. The reconstructed-tree SIL workflow: a run dir rebuilds
    the tree that ran, and the experiment is the same config and the same data against DIFFERENT
    CODE, so exactly three things move — the services.yaml route, the vendored surface, and the
    rig.lock service pin. vehicle.yaml rows (name/order/enabled/config path), the working configs,
    their pins and overlay bindings are NOT touched (`--reset-config` opts one half out).

    Services are SHARED: a swap moves the code under every instance of that service, and the
    affected names are printed. Undo is `rig reconstruct` again — it is non-destructive and cheap."""
    manifest = load_manifest(root)
    instances = [s for s in manifest.sensors if s.service == service]
    if not instances:
        known = sorted({s.service for s in manifest.sensors})
        raise RigError(f"swap: no '{service}' row in vehicle.yaml — swap replaces the source of an "
                       f"INSTALLED service; `rig add` adds a new one. This deployment runs: "
                       f"{', '.join(known) or 'nothing'}")
    if ":" in token and "/" not in token and not Path(token).expanduser().exists():
        raise RigError(f"swap: '{token}' is a profile key — a profile is an INSTANCE's config "
                       f"source, not a service's code; swap takes a service dir path or a service "
                       f"registry ref (`rig pkg upgrade <instance>` re-pins a profile)")

    kind, target = _swap_source(root, token, locked=locked)
    lock = load_lock(root)
    snap = _snapshot(root)
    try:
        if kind == "path":
            from .descriptor import find_descriptor
            dpath = find_descriptor(target)
            declared = str(load_yaml(dpath).get("service") or "") if dpath else ""
            if not dpath:
                raise RigError(f"swap: {target} has no rigging.yaml — not a service checkout")
            if declared and declared != service:
                raise RigError(f"swap: {target} declares service '{declared}', not '{service}' — a "
                               f"swap replaces a service with another version of ITSELF "
                               f"(`rig add` installs a different service)")
            desc = load_descriptor(service, target)
            base = target
            rel = os.path.relpath(target, root)
            if not _route_set(root, service, rel):
                _rollback(root, snap)
                return 1
            vendored = root / "services" / service
            if (vendored / ".vendored.yaml").exists():  # the route left it behind — never a stale twin
                shutil.rmtree(vendored, ignore_errors=True)
                eprint(f"  dropped the vendored copy at services/{service} (the route is the "
                       f"checkout now — edit it in place and re-`up`)")
            for ref in [r for r, info in (lock.get("packages") or {}).items()
                        if (info or {}).get("kind") == "service" and unqualified(r) == service]:
                lock["packages"].pop(ref, None)  # path-routed: no registry pin to reproduce
                eprint(f"  dropped the registry pin {ref} — this service is path-routed now")
            eprint(f"rig swap: {service} -> {rel} (local checkout)")
        else:
            entry, reg, pkg = target
            if pkg.kind != "service":
                raise RigError(f"swap: '{pkg.name}' is a {pkg.kind}, not a service — swap replaces "
                               f"a service's code")
            if pkg.name != service:
                raise RigError(f"swap: {qualified(entry, pkg)} is service '{pkg.name}', not "
                               f"'{service}' — a swap replaces a service with another version of "
                               f"ITSELF (rig add installs a different service)")
            pkg = _at_locked_commit(entry, pkg, lock, locked)
            desc = _install_service(root, entry, pkg, lock, locked=locked, allow_repin=True)
            # _route_service RESPECTS an existing route (a foreign one is not install's to move) —
            # but re-pointing IS the swap, so say it explicitly: a path-routed service coming back
            # to the registry must leave the checkout behind, not keep running from it.
            if not _route_set(root, service, f"services/{service}"):
                _rollback(root, snap)
                return 1
            base = root / "services" / service
            eprint(f"rig swap: {service} -> {qualified(entry, pkg)} (vendored + pinned)")

        if reset_config:
            for sensor in instances:
                if sensor.profile:
                    eprint(f"  {sensor.name}: profile-backed — config left alone "
                           f"(`rig pkg upgrade {sensor.name}` re-bases a profile instance)")
                    continue
                example = _example_config(desc, base)
                if example is None:
                    eprint(f"  {sensor.name}: the new version declares no example config — kept")
                    continue
                from .init import _copy_as_profile
                survived = _copy_as_profile(example, sensor.config)
                if survived and survived != sensor.name:
                    raise RigError(f"swap --reset-config: {example.name} pins name '{survived}' in "
                                   f"a form rig can't neutralize — reset {sensor.name}'s config by hand")
                pin = root / "config" / ".pins" / f"{sensor.name}.yaml"
                pin.parent.mkdir(parents=True, exist_ok=True)
                pin.write_bytes(Path(sensor.config).read_bytes())
                record_instance(lock, sensor.name, profile=None,
                                base_sha256=sha256_file(Path(sensor.config)))
                eprint(f"  {sensor.name}: config RESET from the new example (your edits are gone)")
        else:
            for line in _config_key_drift(_example_config(desc, base), instances):
                eprint(line)

        save_lock(root, lock)
        load_manifest(root)  # the real gate: routes resolve, names hold, configs cross-check
    except BaseException:
        _rollback(root, snap)
        eprint("rig swap: failed — rolled back (services.yaml/vehicle.yaml/rig.lock/configs restored)")
        raise
    eprint(f"  instances now running this code: {', '.join(s.name for s in instances)}"
           + ("" if reset_config else " — configs kept as they were"))
    return 0


def _swap_source(root: Path, token: str, *, locked: bool):
    """`rig add`'s dual grammar, reused: ('path', dir) | ('ref', (entry, registry, package)).
    An existing directory reads as a path; registry grammar as a ref; --locked forces the ref
    reading. A bare name falls back to the one-level workspace scan, then to the registries."""
    from .init import _resolve_service_dir
    if not locked and Path(token).expanduser().is_dir():
        return "path", Path(token).expanduser().resolve()
    if "/" in token or locked:
        return "ref", resolve_ref(token, history=True)
    try:
        return "path", _resolve_service_dir(token, root.parent, label="swap")
    except RigError as exc:
        try:
            return "ref", resolve_ref(token, history=True)
        except RigError as reg_exc:
            raise RigError(f"{exc}\n  (registry fallback: {reg_exc})")


def _at_locked_commit(entry: Entry, pkg: Package, lock: dict, locked: bool) -> Package:
    """--locked + a git-backed registry: resolve the package at the LOCKED registry commit —
    actually reproduce, instead of verify-current-or-fail. The lock's hashes still gate the
    result downstream, so rewritten history fails loudly rather than installing different
    bytes. No git / no recorded commit ⇒ the package as resolved (today's behavior)."""
    if not locked:
        return pkg
    commit = ((lock.get("registries") or {}).get(entry.name) or {}).get("commit")
    if not commit:
        return pkg
    from .history import checkout_pkg_at, kind_dir_of
    hist = checkout_pkg_at(entry, kind_dir_of(pkg.kind), pkg.name, commit)
    if hist is None:
        return pkg
    if hist.version != pkg.version:
        eprint(f"  --locked: {pkg.name} taken from the locked registry commit {commit[:7]} "
               f"(@{hist.version}; the registry currently carries @{pkg.version})")
    return hist


def _prep_profile(root: Path, entry: Entry, reg, pkg: Package, lock: dict,
                  *, locked: bool) -> tuple[str, Path, str, object]:
    """Everything a profile install does EXCEPT materializing an instance: verify the payload,
    fetch/vendor/route the required service, record the lock rows. Returns
    (qualified_ref, payload_path, service_name, descriptor) — the pieces materialization needs.
    Shared by plain install (one default instance) and the vehicle-plan path (N named rows)."""
    ref = qualified(entry, pkg)
    payload_rel = (pkg.manifest.get("config") or {}).get("payload")
    payload_path = pkg.pkg_dir / str(payload_rel)
    if not payload_path.is_file():
        raise RigError(f"install: {ref} payload missing in the synced registry: {payload_rel}")
    _check_locked(lock, ref, locked=locked, payload_sha=sha256_file(payload_path))
    svc_entry, service = _resolve_required_service(entry, reg, pkg)
    service = _at_locked_commit(svc_entry, service, lock, locked)
    desc = _install_service(root, svc_entry, service, lock, locked=locked)
    eprint(f"rig install: {ref} (requires {qualified(svc_entry, service)})")
    record_package(lock, ref, {"kind": "profile", "payload_sha256": sha256_file(payload_path),
                               "requires": qualified(svc_entry, service)})
    if not (locked and entry.name in (lock.get("registries") or {})):
        record_registry(lock, entry.name, rtype=entry.type, location=entry.location,
                        commit=registry_commit(entry))
    return ref, payload_path, service.name, desc


def install(root: Path, spec: str, *, as_name: str | None = None, locked: bool = False,
            enabled: bool = True, order: int | None = None) -> int:
    """One spec: `sensor:<id>` | `[registry/]key[@ver]` — where a profile's key IS the
    `service:short` tuple, so `ouster:generic`, `internal/ouster:generic@1.0.0`, and plain
    service/suite names all resolve through the same ref path. `enabled`/`order` place the new
    row deliberately (reconstruct --enable-replay's declared-disabled, last-ordered player row);
    the defaults are `rig add`'s: enabled, next available order."""
    lock = load_lock(root)
    if spec.startswith("sensor:"):
        entry, reg, pkg = resolve_sensor(spec[len("sensor:"):])
    else:
        try:
            entry, reg, pkg = resolve_ref(spec, history=True)
        except RigError as exc:
            svc, sep, _ = unqualified(spec).partition(":")
            others = profiles_for_service(svc) if sep else []
            if others:  # a profile-key miss: show what DOES exist for that service
                raise RigError(f"{exc}\n  profiles for service '{svc}': {', '.join(others)} "
                               f"(rig pkg search {svc}:)")
            raise
    pkg = _at_locked_commit(entry, pkg, lock, locked)
    if pkg.kind == "suite":
        return _install_suite(root, entry, pkg, locked=locked)
    if pkg.kind == "overlay":
        raise RigError(f"install: '{pkg.name}' is an overlay — overlays are BOUND to an instance "
                       f"(rig overlay apply), not installed standalone")
    if pkg.kind == "vehicle":
        raise RigError(f"install: '{pkg.name}' is a vehicle plan — it installs only through the "
                       f"suite that references it (rig pkg add <ns>/<suite>)")

    # Updating, not installing? Catch it BEFORE anything mutates (a failed re-add used to leave a
    # re-vendored services/ dir behind) and point at the right verb.
    if as_name is None:
        rows = load_manifest(root).sensors
        if pkg.kind == "profile":
            held = [s.name for s in rows
                    if s.profile and unqualified(s.profile) == pkg.name]
        else:
            held = [s.name for s in rows if s.service == pkg.name]
        if held:
            raise RigError(f"install: '{pkg.name}' is already installed (instance"
                           f"{'s' if len(held) > 1 else ''} {', '.join(held)}) — update with "
                           f"`rig pkg upgrade {held[0]}`, or pass --as <name> for a SECOND instance")

    snap = _snapshot(root)  # any failure past here rolls the tree back — no leaked vendor/route
    try:
        if pkg.kind == "profile":
            ref, payload_path, service_name, desc = _prep_profile(root, entry, reg, pkg, lock,
                                                                  locked=locked)
            _materialize_instance(root, svc=service_name, desc=desc, instance=as_name,
                                  base_src=payload_path, profile_ref=ref, lock=lock,
                                  enabled=enabled, order=order)
        else:  # bare service: base config = its declared example at the pinned rev
            ref = qualified(entry, pkg)
            eprint(f"rig install: {ref}")
            desc = _install_service(root, entry, pkg, lock, locked=locked)
            examples = [root / "services" / pkg.name / e for e in desc.examples]
            examples = [e for e in examples if e.is_file()]
            if examples:
                _materialize_instance(root, svc=pkg.name, desc=desc, instance=as_name,
                                      base_src=examples[0], profile_ref=None, lock=lock,
                                      enabled=enabled, order=order)
            else:
                eprint(f"  no example config DECLARED at this pin — route + vendor done; author "
                       f"config/<tier>/<name>.yaml and its vehicle.yaml row yourself. (To automate "
                       f"this: declare `examples:` in the repo's rigging.yaml and cut a release)")
        save_lock(root, lock)
        # The real gate: the deployment must still LOAD (name uniqueness, cross-checks) — surface now.
        load_manifest(root)
    except BaseException:
        _rollback(root, snap)
        eprint("rig install: failed — rolled back (vehicle.yaml/services.yaml/rig.lock/configs "
               "restored)")
        raise
    eprint(f"  locked: rig.lock updated — commit it")
    return 0
