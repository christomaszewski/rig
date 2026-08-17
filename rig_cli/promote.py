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

import shutil
import subprocess
from pathlib import Path

import yaml

from . import RigError
from .common import eprint, load_yaml
from .lock import load_lock
from .manifest import load_manifest
from .refs import unqualified
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
    manifest = reg_root / kind_dir / name / "manifest.yaml"
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
    pkg_dir = reg_root / kind_dir / name
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


def promote(root: Path, names: list[str], *, to: str, all_dirty: bool, name: str | None,
            project: str | None, kind: str | None, suite: str | None, bump: bool,
            target_instance: bool, matches: list[str], requires: str | None) -> int:
    if bool(names) == all_dirty:
        raise RigError("promote: name instance(s), or pass --all for every dirty instance")
    if name and (len(names) != 1 or suite):
        raise RigError("promote: --name applies to exactly one instance (and never names a suite)")
    if kind not in (None, "overlay", "profile"):
        raise RigError(f"promote: --kind must be overlay or profile, not '{kind}'")

    entry = _target_entry(to)
    reg_root = entry.root
    if not (reg_root / "registry.yaml").is_file():
        raise RigError(f"promote: registry '{to}' is not synced/reachable at {reg_root}")
    manifest = load_manifest(root)
    lock = load_lock(root)

    # Git-type target: work on a promote branch in the cache; NEVER leave it checked out.
    prior_branch = None
    if entry.type == "git":
        prior_branch = _git(reg_root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "main"
        if _git(reg_root, "status", "--porcelain").stdout.strip():
            raise RigError(f"promote: the '{to}' cache at {reg_root} has uncommitted changes — "
                           f"clean it (or remove it and re-sync) first")

    chosen = []
    for sensor in manifest.sensors:
        if names and sensor.name not in names:
            continue
        delta = promote_delta(root, sensor)
        if names:  # explicitly named: a missing base or clean state is an ERROR, not a skip
            skind = kind
            if skind is None:  # infer only where it's unambiguous: no pinned base ⇒ overlay is
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

    written: list[Path] = []
    backups: dict[Path, Path] = {}  # pre-existing pkg dir -> pristine copy (rollback restores)
    new_overlays: list[str] = []
    branch = None
    try:
        if entry.type == "git":
            branch = f"promote/{suite or name or chosen[0][0].name}"
            if _git(reg_root, "switch", "-c", branch).returncode != 0:
                raise RigError(f"promote: branch {branch} already exists in the '{to}' cache — "
                               f"push or delete it first")
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
                req = requires
                if req is None:
                    pin = next((ref for ref, info in (lock.get("packages") or {}).items()
                                if info.get("kind") == "service"
                                and unqualified(ref) == sensor.service), None)
                    req = _requalify(pin) if pin else None  # lock refs carry the ALIAS
                if req is None:
                    raise RigError(f"promote: no locked service pin for '{sensor.service}' — pass "
                                   f"--requires <ns/service@X.Y.Z>")
                # Name: --name > the provenance profile (updating the thing this instance is
                # PINNED to, not minting a same-named sibling) > the instance name hyphenated.
                if name:
                    pkg_name = name
                elif sensor.profile:
                    pkg_name = unqualified(str(sensor.profile))
                else:
                    pkg_name = sensor.name.replace("_", "-")
                existing = _existing_manifest(reg_root, "profiles", pkg_name)
                # Auto-bump ONLY when provenance proves this is the package the instance consumes
                # (alias AND name match the target) — a bare name collision still demands --bump.
                auto = (not bump and existing is not None and sensor.profile
                        and str(sensor.profile).partition("@")[0] == f"{to}/{pkg_name}")
                if auto:
                    eprint(f"  {sensor.name}: updating {sensor.profile} — auto-bump")
                version = _next_version(existing, bump or bool(auto), f"profile '{pkg_name}'")
                pmanifest: dict = {"kind": "profile", "name": pkg_name, "version": version,
                                   **_carry_forward(existing, "kind", "name", "version",
                                                   "requires", "config", "provides"),
                                   "requires": {"service": req},
                                   "config": {"payload": "config/payload.yaml"}}
                if matches:  # --match REPLACES the carried-forward set
                    pmanifest["provides"] = {"sensor": [{"model": pkg_name, "match": list(matches)}]}
                elif existing and existing.get("provides"):
                    pmanifest["provides"] = existing["provides"]
                if existing and (existing.get("config") or {}).get("overrides_schema"):
                    pmanifest["config"]["overrides_schema"] = existing["config"]["overrides_schema"]
                _write_pkg(reg_root, "profiles", pkg_name, pmanifest, payload, written, backups)
                eprint(f"  profile {target_ns}/{pkg_name}@{version} <- {sensor.name}")
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

        _, issues = validate_registry(reg_root, check_index=False)
        ours = {str(p.relative_to(reg_root)) for p in written}
        errors = [i for i in issues if i.level == "error"
                  and any(i.where == rel or i.where.startswith(rel + "/") for rel in ours)]
        foreign = [i for i in issues if i.level == "error" and i not in errors]
        if foreign:  # pre-existing breakage is not THIS promote's fault — surface, don't block
            eprint(f"rig: note: '{to}' has {len(foreign)} pre-existing validation error(s) "
                   f"unrelated to this promote (rig registry validate shows them)")
        if errors:
            details = "; ".join(f"{i.where}: {i.message}" for i in errors[:3])
            raise RigError(f"promote: the emitted packages do not validate — {details}")
        write_index(load_registry(reg_root, []))

        if entry.type == "git":
            _git(reg_root, "add", "-A")
            commit = _git(reg_root, "commit", "-q", "-m", f"promote: {', '.join(p.name for p in written)}")
            if commit.returncode != 0:
                commit = _git(reg_root, "-c", "user.name=rig", "-c", "user.email=rig@localhost",
                              "commit", "-q", "-m", f"promote: {', '.join(p.name for p in written)}")
            if commit.returncode != 0:
                raise RigError("promote: git commit failed in the cache checkout")
            _git(reg_root, "switch", "-q", prior_branch)
            eprint(f"rig pkg promote: committed on '{branch}' in the '{to}' cache — publish with:")
            eprint(f"  git -C {reg_root} push origin {branch}   (then PR/merge, then rig registry sync)")
        else:
            eprint(f"rig pkg promote: written + validated in place at {reg_root} — commit/PR with "
                   f"plain git when ready")
    except BaseException:
        if entry.type == "git" and branch:
            _git(reg_root, "reset", "--hard", "-q")
            _git(reg_root, "clean", "-fdq")
            _git(reg_root, "switch", "-q", prior_branch)
            _git(reg_root, "branch", "-D", branch)
        else:
            for pkg_dir in written:  # fresh dirs are removed; pre-existing packages are RESTORED
                shutil.rmtree(pkg_dir, ignore_errors=True)  # (a failed --bump must not delete them)
                if pkg_dir in backups:
                    shutil.copytree(backups[pkg_dir], pkg_dir)
        raise
    finally:
        for backup in backups.values():
            shutil.rmtree(backup.parent, ignore_errors=True)
    return 0
