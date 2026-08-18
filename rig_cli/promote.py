"""``rig pkg promote`` — lift local deployment state into scaffolded, validated packages in a
registry checkout. Promotion emits packages of ANY kind, which is why it lives under ``pkg``:

- default: one **overlay** per named instance — payload = the promote delta ``D`` with the
  round-trip law ``(pin ⊕ overlays) ⊕ D == current render`` (bind D last + ``overlay apply
  --clear-local`` and the render is identical, now versioned).
- ``--kind profile``: the instance's full effective config becomes a new profile (the migration
  path: hand-tuned or pre-registry instances get first-class packages). Without ``--kind``, a
  named hand-authored instance infers profile (an overlay is IMPOSSIBLE with no pinned base —
  the only fact-grounded inference rig makes here).

Update flow (re-promote): the package name defaults from the instance's ``profile:`` provenance
(updating the thing it is PINNED to, never a same-named sibling), ``--bump`` is implied when
provenance proves the target IS that package (alias + name match; a bare name collision still
demands the flag), and the existing manifest is CARRIED FORWARD — ``provides``/``project``/
``overrides_schema`` and any hand-added keys survive a bump; only the generated fields and the
``authored_against`` stamp (re-stamping = the staleness signal) are rewritten.
- ``--all [--suite S]``: every dirty instance → an overlay each, plus a suite referencing the
  deployment's pinned profiles and the new overlays in binding order — the whole-deployment
  capture a fresh vehicle reproduces with ``pkg add <suite>`` (`--locked` for byte-identity).

Publish scope is WRITE + VALIDATE only (settled): into a local-dir registry the files land in
place; into a git registry's managed cache they land as a LOCAL commit on a ``promote/…`` branch
(no push, no credentials — sync only ever ff-pulls the default branch, so the cache never
wedges), and rig prints the push/PR command. Validation failures roll the checkout back.
"""
from __future__ import annotations

import contextlib
import re
import shutil
import subprocess
from pathlib import Path

import yaml

from . import RigError
from .common import eprint, load_yaml
from .lock import load_lock
from .manifest import load_manifest
from .refs import short_name, unqualified
from .registries import Entry, load_entries
from .registry import validate_registry, write_index, load_registry
from .workingcopy import promote_delta

def _target_entry(name: str) -> Entry:
    for entry in load_entries():
        if entry.name == name:
            return entry
    raise RigError(f"promote: no registry named '{name}' configured (rig registry list); "
                   f"`rig registry init <dir>` + `rig registry add {name} --path <dir>` creates one")


def _git(root: Path, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


def _existing_manifest(reg_root: Path, kind_dir: str, name: str) -> dict | None:
    """The target package's CURRENT manifest, or None for a fresh name. Re-promotes seed from it
    (carry-forward) instead of rewriting the manifest wholesale — a bump must not strip hand-added
    fields like `provides` match identifiers or `config.overrides_schema`."""
    manifest = reg_root / kind_dir / name.replace(":", "/") / "manifest.yaml"
    return load_yaml(manifest) if manifest.is_file() else None


def _next_version(existing: dict | None, bump: bool, what: str) -> str:
    if existing is None:
        return "1.0.0"
    current = str(existing.get("version"))
    if not bump:
        raise RigError(f"promote: {what} already exists at {current} — pass --bump to publish "
                       f"a new version")
    try:
        major, minor, patch = (int(x) for x in current.split("."))
    except ValueError:
        raise RigError(f"promote: {what} has a malformed version '{current}' in the registry — "
                       f"fix the manifest before bumping")
    return f"{major}.{minor}.{patch + 1}"


def _namespace_of(alias: str) -> str:
    """A locked ref's ns segment is the CONSUMER'S alias (registries.yaml); package manifests are
    registry-side documents, so refs written into them must carry the registry's OWN namespace —
    otherwise suite members don't resolve and the authored_against staleness check never fires
    when alias ≠ namespace. Unknown/unreadable alias: keep it, loudly."""
    for entry in load_entries():
        if entry.name == alias:
            try:
                return load_registry(entry.root, []).namespace
            except RigError:
                break
    eprint(f"rig: warning: cannot resolve the namespace behind registry alias '{alias}' — "
           f"emitting the alias as-is (the ref may not resolve registry-side)")
    return alias


def _requalify(ref: str) -> str:
    """alias/name[@ver] -> namespace/name[@ver] for refs emitted into package manifests."""
    alias, sep, rest = ref.partition("/")
    return f"{_namespace_of(alias)}{sep}{rest}" if sep else ref


def _write_pkg(reg_root: Path, kind_dir: str, name: str, manifest: dict,
               payload: dict | None, written: list[Path], backups: dict[Path, Path]) -> None:
    pkg_dir = reg_root / kind_dir / name.replace(":", "/")  # profile keys project svc:short -> svc/short
    if pkg_dir.exists() and pkg_dir not in backups:  # a --bump overwrites: keep the original so
        import tempfile                              # rollback can RESTORE it, never delete it
        backup = Path(tempfile.mkdtemp(prefix="rig-promote-")) / name
        shutil.copytree(pkg_dir, backup)
        backups[pkg_dir] = backup
    (pkg_dir / "config").mkdir(parents=True, exist_ok=True)
    if payload is not None:
        payload_path = pkg_dir / "config" / ("delta.yaml" if kind_dir == "overlays" else "payload.yaml")
        payload_path.write_text(yaml.safe_dump(payload, sort_keys=False, default_flow_style=False))
    elif (pkg_dir / "config").exists() and not any((pkg_dir / "config").iterdir()):
        (pkg_dir / "config").rmdir()  # suites: REFERENCES ONLY — no config dir allowed
    (pkg_dir / "manifest.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    written.append(pkg_dir)


def _carry_forward(existing: dict | None, *generated: str) -> dict:
    """Everything from the existing manifest EXCEPT the fields this promote regenerates.
    `authored_against` is always regenerated — re-stamping it is the staleness signal."""
    return {k: v for k, v in (existing or {}).items()
            if k not in generated and k != "authored_against"}


@contextlib.contextmanager
def _registry_write_session(entry: Entry, to: str, branch_name: str, written: list[Path],
                            backups: dict[Path, Path], what: str = "promote"):
    """The write+validate publish envelope shared by promote and rebase: branch (git targets) →
    yield (the caller writes packages) → validate SCOPED to what was written → index →
    commit + switch back (git) or in-place note — with the restore-not-delete rollback on ANY
    failure, and backup scratch dirs cleaned either way."""
    reg_root = entry.root
    prior_branch = branch = None
    if entry.type == "git":
        prior_branch = _git(reg_root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "main"
        if _git(reg_root, "status", "--porcelain").stdout.strip():
            raise RigError(f"{what}: the '{to}' cache at {reg_root} has uncommitted changes — "
                           f"clean it (or remove it and re-sync) first")
        branch = f"promote/{branch_name}"
        if _git(reg_root, "switch", "-c", branch).returncode != 0:
            raise RigError(f"{what}: branch {branch} already exists in the '{to}' cache — "
                           f"push or delete it first")
    try:
        yield
        _, issues = validate_registry(reg_root, check_index=False)
        ours = {str(p.relative_to(reg_root)) for p in written}
        errors = [i for i in issues if i.level == "error"
                  and any(i.where == rel or i.where.startswith(rel + "/") for rel in ours)]
        foreign = [i for i in issues if i.level == "error" and i not in errors]
        if foreign:  # pre-existing breakage is not THIS write's fault — surface, don't block
            eprint(f"rig: note: '{to}' has {len(foreign)} pre-existing validation error(s) "
                   f"unrelated to this {what} (rig registry validate shows them)")
        if errors:
            details = "; ".join(f"{i.where}: {i.message}" for i in errors[:3])
            raise RigError(f"{what}: the emitted packages do not validate — {details}")
        write_index(load_registry(reg_root, []))
        if entry.type == "git":
            _git(reg_root, "add", "-A")
            msg = f"{what}: {', '.join(p.name for p in written)}"
            commit = _git(reg_root, "commit", "-q", "-m", msg)
            if commit.returncode != 0:
                commit = _git(reg_root, "-c", "user.name=rig", "-c", "user.email=rig@localhost",
                              "commit", "-q", "-m", msg)
            if commit.returncode != 0:
                raise RigError(f"{what}: git commit failed in the cache checkout")
            _git(reg_root, "switch", "-q", prior_branch)
            eprint(f"rig pkg {what}: committed on '{branch}' in the '{to}' cache — publish with:")
            eprint(f"  git -C {reg_root} push origin {branch}   (then PR/merge, then "
                   f"rig registry sync)")
        else:
            eprint(f"rig pkg {what}: written + validated in place at {reg_root} — commit/PR "
                   f"with plain git when ready")
    except BaseException:
        if entry.type == "git" and branch:
            _git(reg_root, "reset", "--hard", "-q")
            _git(reg_root, "clean", "-fdq")
            _git(reg_root, "switch", "-q", prior_branch)
            _git(reg_root, "branch", "-D", branch)
        else:
            for pkg_dir in written:  # fresh dirs removed; pre-existing packages RESTORED
                shutil.rmtree(pkg_dir, ignore_errors=True)
                if pkg_dir in backups:
                    shutil.copytree(backups[pkg_dir], pkg_dir)
        raise
    finally:
        for backup in backups.values():
            shutil.rmtree(backup.parent, ignore_errors=True)


def promote(root: Path, names: list[str], *, to: str, all_dirty: bool, name: str | None,
            project: str | None, kind: str | None, suite: str | None, bump: bool,
            target_instance: bool, matches: list[str], requires: str | None,
            adopt: bool = False) -> int:
    if bool(names) == all_dirty:
        raise RigError("promote: name instance(s), or pass --all for every dirty instance")
    if name and (len(names) != 1 or suite):
        raise RigError("promote: --name applies to exactly one instance (and never names a suite)")
    if name and not re.match(r"^[a-z][a-z0-9-]*$", name):
        raise RigError(f"promote: --name '{name}' must match [a-z][a-z0-9-]* — for profiles it is "
                       f"the SHORT half only; the service half is derived from the instance "
                       f"(no '/', ':', or '@')")
    if kind not in (None, "overlay", "profile"):
        raise RigError(f"promote: --kind must be overlay or profile, not '{kind}'")
    if adopt and (all_dirty or suite or len(names) != 1 or kind == "overlay"):
        raise RigError("promote: --adopt re-pins ONE named instance onto its freshly published "
                       "PROFILE (the overlay analogue is `overlay apply --clear-local`)")

    entry = _target_entry(to)
    reg_root = entry.root
    if not (reg_root / "registry.yaml").is_file():
        raise RigError(f"promote: registry '{to}' is not synced/reachable at {reg_root}")
    manifest = load_manifest(root)
    lock = load_lock(root)

    chosen = []
    for sensor in manifest.sensors:
        if names and sensor.name not in names:
            continue
        delta = promote_delta(root, sensor)
        if names:  # explicitly named: a missing base or clean state is an ERROR, not a skip
            skind = kind
            if skind is None and adopt:  # --adopt is profile-only — the flag IS the kind choice
                skind = "profile"
            elif skind is None:  # infer only where it's unambiguous: no pinned base ⇒ overlay is
                skind = "overlay" if delta is not None else "profile"  # IMPOSSIBLE, profile it is
                if skind == "profile":
                    eprint(f"  {sensor.name}: promoting as PROFILE (hand-authored — no registry "
                           f"base to diff an overlay against)")
            if skind == "overlay" and delta is None:
                raise RigError(f"promote: '{sensor.name}' has no pinned registry base — overlays "
                               f"need one (use --kind profile for hand-authored instances)")
            if skind == "overlay" and not delta:
                raise RigError(f"promote: '{sensor.name}' is clean — nothing to promote "
                               f"(config diff shows deltas)")
            chosen.append((sensor, delta, skind))
        elif delta:  # --all: dirty instances only
            chosen.append((sensor, delta, kind or "overlay"))
    unknown = set(names) - {s.name for s in manifest.sensors}
    if unknown:
        raise RigError(f"promote: unknown instance(s): {', '.join(sorted(unknown))}")
    if not chosen:
        eprint("rig pkg promote: nothing dirty — no packages to emit")
        return 0
    if adopt and chosen[0][2] != "profile":
        raise RigError("promote: --adopt needs --kind profile (this promote would emit an "
                       "overlay — its round-trip is `overlay apply --clear-local`)")

    written: list[Path] = []
    backups: dict[Path, Path] = {}  # pre-existing pkg dir -> pristine copy (rollback restores)
    new_overlays: list[str] = []
    adoptions: list[tuple] = []     # (sensor, pkg_name, version, payload, req_lock)
    with _registry_write_session(entry, to, suite or name or chosen[0][0].name,
                                 written, backups):
        target_ns = load_registry(reg_root, []).namespace

        for sensor, delta, skind in chosen:
            if skind == "profile":
                from .workingcopy import expected_base, local_delta
                from .resolve import deep_merge
                base = expected_base(root, sensor) or {}
                state = local_delta(root, sensor)
                file_delta, overrides = state if state else ({}, {})
                if base:
                    payload = deep_merge(deep_merge(base, file_delta), overrides)
                else:  # hand-authored: the working file itself
                    payload = {k: v for k, v in load_yaml(sensor.config).items() if k != "name"}
                    payload = deep_merge(payload, overrides)
                payload.setdefault("service", sensor.service)
                if requires is not None:
                    req_lock = req = requires   # explicit ref: emitted verbatim
                else:
                    req_lock = next((ref for ref, info in (lock.get("packages") or {}).items()
                                     if info.get("kind") == "service"
                                     and unqualified(ref) == sensor.service), None)
                    if req_lock is None:
                        raise RigError(f"promote: no locked service pin for '{sensor.service}' "
                                       f"— pass --requires <ns/service@X.Y.Z>")
                    req = _requalify(req_lock)  # lock refs carry the ALIAS; manifests want ns
                # Identity (schema 2): the key is the (service, short) tuple. The service half is
                # ALWAYS the instance's own service; --name > the provenance profile's short half
                # (updating the thing this instance is PINNED to, not minting a same-named
                # sibling) > the instance name hyphenated supply the short half.
                if name:
                    short = name
                elif sensor.profile:
                    short = short_name(str(sensor.profile))
                else:
                    short = sensor.name.replace("_", "-")
                pkg_name = f"{sensor.service}:{short}"
                existing = _existing_manifest(reg_root, "profiles", pkg_name)
                # Auto-bump ONLY when provenance proves this is the package the instance consumes
                # (alias AND name match the target) — a bare name collision still demands --bump.
                auto = (not bump and existing is not None and sensor.profile
                        and str(sensor.profile).partition("@")[0] == f"{to}/{pkg_name}")
                if auto:
                    eprint(f"  {sensor.name}: updating {sensor.profile} — auto-bump")
                version = _next_version(existing, bump or bool(auto), f"profile '{pkg_name}'")
                pmanifest: dict = {"kind": "profile", "name": short, "version": version,
                                   **_carry_forward(existing, "kind", "name", "version",
                                                   "requires", "config", "provides"),
                                   "requires": {"service": req},
                                   "config": {"payload": "config/payload.yaml"}}
                if matches:  # --match REPLACES the carried-forward set
                    pmanifest["provides"] = {"sensor": [{"model": short, "match": list(matches)}]}
                elif existing and existing.get("provides"):
                    pmanifest["provides"] = existing["provides"]
                if existing and (existing.get("config") or {}).get("overrides_schema"):
                    pmanifest["config"]["overrides_schema"] = existing["config"]["overrides_schema"]
                # Lineage: a FORK (provenance points at a DIFFERENT package than the one being
                # written) records its parent — `pkg rebase` three-ways against it later. Self
                # re-promotes ride _carry_forward (the parent baseline only moves on rebase).
                if sensor.profile and str(sensor.profile).partition("@")[0] != f"{to}/{pkg_name}":
                    pmanifest["based_on"] = _requalify(str(sensor.profile))
                _write_pkg(reg_root, "profiles", pkg_name, pmanifest, payload, written, backups)
                eprint(f"  profile {target_ns}/{pkg_name}@{version} <- {sensor.name}"
                       + (f" (based_on {pmanifest['based_on']})" if pmanifest.get("based_on")
                          else ""))
                if adopt:
                    adoptions.append((sensor, pkg_name, version, payload, req_lock))
            else:
                if delta is None or not delta:
                    continue
                pkg_name = name or (f"{sensor.name.replace('_', '-')}-{project}" if project
                                    else sensor.name.replace("_", "-"))
                existing = _existing_manifest(reg_root, "overlays", pkg_name)
                version = _next_version(existing, bump, f"overlay '{pkg_name}'")
                # Target the service FULLY QUALIFIED unless it lives in the target registry itself —
                # the registry's validator resolves bare names in-registry only.
                svc_target = sensor.service
                if not (reg_root / "services" / sensor.service / "manifest.yaml").is_file():
                    pin = next((ref for ref, info in (lock.get("packages") or {}).items()
                                if info.get("kind") == "service"
                                and unqualified(ref) == sensor.service), None)
                    if pin:
                        svc_target = _requalify(pin.split("@", 1)[0])  # ns/name, registry-side
                # Provenance stamp (staleness tier 1): what this delta was authored against —
                # registry CI warns when the target service moves past it. Refs are requalified:
                # the CI-side check gates on the registry's own namespace, never a consumer alias.
                authored = {}
                svc_pin = next((r for r, info in (lock.get("packages") or {}).items()
                                if info.get("kind") == "service"
                                and unqualified(r) == sensor.service), None)
                if svc_pin:
                    authored["service"] = _requalify(svc_pin)
                if sensor.profile:
                    authored["profile"] = _requalify(str(sensor.profile))
                proj = project or (existing or {}).get("project")
                omanifest = {"kind": "overlay", "name": pkg_name, "version": version,
                             **_carry_forward(existing, "kind", "name", "version",
                                             "targets", "project", "config"),
                             "targets": ([{"instance": sensor.name}] if target_instance
                                         else [{"service": svc_target}]),
                             **({"authored_against": authored} if authored else {}),
                             **({"project": proj} if proj else {}),
                             "config": {"payload": "config/delta.yaml"}}
                _write_pkg(reg_root, "overlays", pkg_name, omanifest, delta, written, backups)
                new_overlays.append(f"{target_ns}/{pkg_name}@{version}")
                eprint(f"  overlay {target_ns}/{pkg_name}@{version} <- {sensor.name} "
                       f"({len(delta)} top-level key(s))")

        if suite:
            existing = _existing_manifest(reg_root, "suites", suite)
            version = _next_version(existing, bump, f"suite '{suite}'")
            profiles = sorted({_requalify(str(s.profile))
                               for s in manifest.sensors if s.profile})
            bound = []
            for sensor in manifest.sensors:  # existing bindings first, manifest order; then new
                bound.extend(r for r in (_requalify(o) for o in sensor.overlays) if r not in bound)
            bound.extend(o for o in new_overlays if o not in bound)
            smanifest = {"kind": "suite", "name": suite, "version": version,
                         **_carry_forward(existing, "kind", "name", "version",
                                         "project", "members"),
                         **({"project": project} if project else {}),
                         "members": {"profiles": profiles, "overlays": bound}}
            _write_pkg(reg_root, "suites", suite, smanifest, None, written, backups)
            eprint(f"  suite {target_ns}/{suite}@{version} ({len(profiles)} profile(s), "
                   f"{len(bound)} overlay(s) in binding order)")

    # Registry write committed/validated — the deployment-side half of the round-trip runs
    # only now (a failed publish must never touch the deployment; a failed adoption leaves
    # the published package published, loudly).
    for sensor, pkg_name, version, payload, req_lock in adoptions:
        _adopt_instance(root, entry, sensor, pkg_name, version, payload, req_lock)
    return 0


def _adopt_instance(root: Path, entry: Entry, sensor, pkg_name: str, version: str,
                    payload: dict, req_lock: str) -> None:
    """--adopt: the profile round-trip's second half (the `--clear-local` of profiles). The
    payload IS the instance's current effective config, so re-pinning the row to the fork,
    resetting working+pin to the payload, dropping overrides, and UNBINDING overlays (their
    content is baked in — bound they would double-apply) provably reproduces the render."""
    from .install import registry_commit
    from .lock import record_instance, record_package, record_registry, save_lock, sha256_file
    from .overlay import edit_row
    from .resolve import overlay_payload_path

    fork_ref = f"{entry.name}/{pkg_name}@{version}"  # the CONSUMER-side (alias) spelling
    body = yaml.safe_dump(payload, sort_keys=False, default_flow_style=False)
    old_overlays = list(sensor.overlays)

    def mutate(row: dict) -> None:
        row["profile"] = fork_ref
        row.pop("overrides", None)
        row.pop("overlays", None)

    try:
        edit_row(root, sensor.name, mutate)
    except RigError as exc:
        eprint(f"  {sensor.name}: --adopt SKIPPED (the package is published) — {exc}")
        return
    Path(sensor.config).write_text(body)  # working copy = the pristine payload, diff-clean
    pin = root / "config" / ".pins" / f"{sensor.name}.yaml"
    pin.parent.mkdir(parents=True, exist_ok=True)
    pin.write_text(body)
    lock = load_lock(root)
    from .manifest import load_manifest
    remaining = load_manifest(root).sensors
    for ref in old_overlays:  # last binding gone -> drop the payload copy + lock row
        if not any(ref in s.overlays for s in remaining):
            overlay_payload_path(root, ref).unlink(missing_ok=True)
            (lock.get("packages") or {}).pop(ref, None)
    record_package(lock, fork_ref, {"kind": "profile", "payload_sha256": sha256_file(pin),
                                    "requires": req_lock})
    record_instance(lock, sensor.name, profile=fork_ref, base_sha256=sha256_file(pin),
                    overlays=[])
    record_registry(lock, entry.name, rtype=entry.type, location=entry.location,
                    commit=registry_commit(entry))
    save_lock(root, lock)
    eprint(f"  {sensor.name}: ADOPTED {fork_ref} — row re-pinned, working+pin reset to the "
           f"payload, overrides dropped"
           + (f", {len(old_overlays)} overlay(s) unbound (content baked in)" if old_overlays
              else "") + "; render identical (round-trip law)")


# --- pkg rebase: move a FORK onto its parent's current version --------------------------------

def _entry_for_namespace(ns: str) -> Entry:
    """based_on refs carry the registry's NAMESPACE (registry-side documents); consumers know
    registries by ALIAS. Map ns -> the configured entry whose registry declares it (fail-soft
    per entry — an unreachable registry must not mask the right one), alias-match fallback."""
    fallback = None
    for entry in load_entries():
        try:
            if load_registry(entry.root, []).namespace == ns:
                return entry
        except RigError:
            pass
        if entry.name == ns:
            fallback = entry
    if fallback is not None:
        return fallback
    raise RigError(f"rebase: no configured registry declares namespace '{ns}' "
                   f"(rig registry list; the parent must be synced/added)")


def _parent_payload(entry: Entry, name: str, version: str) -> tuple[dict, dict]:
    """(manifest, payload) of profiles/<name> at `version` — directly when the registry
    currently carries it, else from git history (v0.1.63; capability-detected)."""
    current = _existing_manifest(entry.root, "profiles", name)
    if current is not None and str(current.get("version")) == version:
        payload_rel = (current.get("config") or {}).get("payload") or "config/payload.yaml"
        path = entry.root / "profiles" / name.replace(":", "/") / str(payload_rel)
        if not path.is_file():
            raise RigError(f"rebase: parent payload missing: {path}")
        return current, load_yaml(path)
    from .history import checkout_pkg
    pkg = checkout_pkg(entry, "profiles", name, version)
    if pkg is None:
        raise RigError(f"rebase: '{entry.name}/{name}' does not carry @{version} and its git "
                       f"history cannot serve it — the three-way needs the OLD parent payload "
                       f"(git-backed registries keep every past version)")
    payload_rel = (pkg.manifest.get("config") or {}).get("payload") or "config/payload.yaml"
    path = pkg.pkg_dir / str(payload_rel)
    if not path.is_file():
        raise RigError(f"rebase: parent payload missing at the historical version: {payload_rel}")
    return pkg.manifest, load_yaml(path)


def rebase(name: str, *, to: str, onto: str | None) -> int:
    """`rig pkg rebase <name> --to <registry> [--onto parent[@ver]]` — three-way the fork onto
    its parent's current (or named) version: D = diff(old parent, fork payload), result =
    new parent ⊕ D, conflicts (parent ALSO changed the key) kept OURS, loudly. Publishes a new
    version with `based_on` advanced and `requires` adopted from the new parent. Registry-side
    only (no deployment); consumers follow with `registry sync && pkg upgrade`."""
    from .refs import parse_ref
    from .resolve import deep_merge, structural_diff
    from .workingcopy import _dig, _flat, _strip_identity

    entry = _target_entry(to)
    reg_root = entry.root
    if not (reg_root / "registry.yaml").is_file():
        raise RigError(f"rebase: registry '{to}' is not synced/reachable at {reg_root}")
    name = parse_ref(name)[1]  # tolerate ns/name[@ver] spellings — the --to registry is the home
    existing = _existing_manifest(reg_root, "profiles", name)
    if existing is None:
        raise RigError(f"rebase: no profile '{name}' in '{to}'")
    based_on = str(existing.get("based_on") or "")
    if not based_on:
        raise RigError(f"rebase: '{to}/{name}' has no based_on lineage — not a fork (promote "
                       f"from an instance pinned to its parent records it, or add "
                       f"`based_on: <ns>/<parent>@<ver>` to the manifest by hand)")
    parent_ns, parent_name, old_ver = parse_ref(based_on)
    if not parent_ns or not old_ver:
        raise RigError(f"rebase: based_on '{based_on}' is not a fully-qualified exact ref")
    parent_entry = _entry_for_namespace(parent_ns)

    # the three payloads
    payload_rel = (existing.get("config") or {}).get("payload") or "config/payload.yaml"
    my_path = reg_root / "profiles" / name.replace(":", "/") / str(payload_rel)
    if not my_path.is_file():
        raise RigError(f"rebase: payload missing: {my_path}")
    mine = _strip_identity(load_yaml(my_path))
    _, old_parent = _parent_payload(parent_entry, parent_name, old_ver)
    old_parent = _strip_identity(old_parent)
    if onto is not None:
        onto_ns, onto_name, onto_ver = parse_ref(onto)
        if (onto_ns and onto_ns != parent_ns) or (onto_name != parent_name):
            raise RigError(f"rebase: --onto must name the parent '{parent_ns}/{parent_name}', "
                           f"not '{onto}'")
    else:
        onto_ver = None
    new_parent_manifest = _existing_manifest(parent_entry.root, "profiles", parent_name)
    if new_parent_manifest is None:
        raise RigError(f"rebase: parent '{parent_ns}/{parent_name}' is gone from its registry")
    new_ver = onto_ver or str(new_parent_manifest.get("version"))
    if new_ver == old_ver:
        eprint(f"rig pkg rebase: '{to}/{name}' is already based on "
               f"{parent_ns}/{parent_name}@{old_ver} — nothing to do")
        return 0
    new_parent_manifest, new_parent = _parent_payload(parent_entry, parent_name, new_ver)
    new_parent = _strip_identity(new_parent)

    # three-way: MINE wins on conflicts, loudly (the upgrade discipline, dict-level)
    mine_delta = structural_diff(old_parent, mine)
    parent_delta = structural_diff(old_parent, new_parent)
    conflicts = sorted(set(dict(_flat(mine_delta))) & set(dict(_flat(parent_delta))))
    merged = deep_merge(new_parent, mine_delta)
    for path in conflicts:
        eprint(f"    CONFLICT {path}: parent {_dig(old_parent, path)!r} -> "
               f"{_dig(new_parent, path)!r}, keeping yours: {_dig(mine, path)!r}")

    version = _next_version(existing, True, f"profile '{name}'")
    new_req = (new_parent_manifest.get("requires") or {}).get("service")
    if new_req and "/" not in str(new_req):  # parent-registry-RELATIVE constraint: qualify it,
        new_req = f"{parent_ns}/{new_req}"   # or it cannot resolve from the fork's registry
    old_req = (existing.get("requires") or {}).get("service")
    if new_req and str(new_req) != str(old_req):
        eprint(f"  requires adopted from the parent: {old_req} -> {new_req}")
    from .refs import split_key
    pmanifest = {"kind": "profile", "name": split_key(name)[1], "version": version,
                 **_carry_forward(existing, "kind", "name", "version",
                                 "requires", "config", "based_on"),
                 **({"requires": {"service": str(new_req)}} if new_req
                    else ({"requires": existing["requires"]} if existing.get("requires")
                          else {})),
                 "based_on": f"{parent_ns}/{parent_name}@{new_ver}",
                 "config": existing.get("config") or {"payload": "config/payload.yaml"}}
    written: list[Path] = []
    backups: dict[Path, Path] = {}
    with _registry_write_session(entry, to, f"rebase-{name.replace(':', '-')}", written, backups,
                                 what="rebase"):  # git branch names cannot carry ':'
        _write_pkg(reg_root, "profiles", name, pmanifest, merged, written, backups)
        eprint(f"  profile {to}/{name}@{version}: rebased "
               f"{parent_ns}/{parent_name}@{old_ver} -> @{new_ver}"
               + (f" ({len(conflicts)} conflict(s), yours kept)" if conflicts else ""))
    eprint("rig pkg rebase: consumers follow with `rig registry sync && rig pkg upgrade`")
    return 0
