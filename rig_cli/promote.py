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
from .refs import parse_ref, short_name, split_key, unqualified
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


def _faithful_payload_bytes(config_path, payload: dict) -> bytes | None:
    """The working FILE's bytes when they are content-equal to the payload being emitted — so
    COMMENTS survive the promote. PyYAML cannot round-trip comments, so verbatim-when-faithful
    is the mechanism (install's materialize is already a verbatim copy — this closes the loop:
    a profile's commented toggle blocks reach its consumers' working configs intact). A
    top-level `name:` line is commented out (payloads are nameless, same transform as init);
    any content mismatch (overrides folded in, injected keys, a transform-resistant name)
    returns None and the caller dumps the dict as before."""
    try:
        text = Path(config_path).read_text()
    except OSError:
        return None
    lines = []
    for line in text.splitlines():
        if re.match(r"^name:\s", line):  # top-level only — indented name: keys stay
            lines.append(f"# {line}   # (commented by rig promote: the instance row supplies "
                         f"the name)")
        else:
            lines.append(line)
    out = "\n".join(lines) + "\n"
    try:
        parsed = yaml.safe_load(out)
    except yaml.YAMLError:
        return None
    if not isinstance(parsed, dict) or parsed.get("name") is not None:
        return None
    return out.encode() if parsed == payload else None


def _write_pkg(reg_root: Path, kind_dir: str, name: str, manifest: dict,
               payload: dict | bytes | None, written: list[Path],
               backups: dict[Path, Path]) -> None:
    pkg_dir = reg_root / kind_dir / name.replace(":", "/")  # profile keys project svc:short -> svc/short
    if pkg_dir.exists() and pkg_dir not in backups:  # a --bump overwrites: keep the original so
        import tempfile                              # rollback can RESTORE it, never delete it
        backup = Path(tempfile.mkdtemp(prefix="rig-promote-")) / name
        shutil.copytree(pkg_dir, backup)
        backups[pkg_dir] = backup
    (pkg_dir / "config").mkdir(parents=True, exist_ok=True)
    if payload is not None:
        payload_path = pkg_dir / "config" / {"overlays": "delta.yaml",
                                             "vehicles": "vehicle.yaml"}.get(kind_dir, "payload.yaml")
        if isinstance(payload, bytes):  # verbatim: the working file's bytes, comments intact
            payload_path.write_bytes(payload)
        else:
            payload_path.write_text(yaml.safe_dump(payload, sort_keys=False,
                                                   default_flow_style=False))
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


def _promote_service(root: Path, spec: str, *, to: str, bump: bool,
                     version: str | None, adopt: bool = False) -> int:
    """`promote <instance|service> --kind service`: publish the service's CODE POINTER — the
    dev-loop counterpart of the repo-side registry-release CI job. Everything comes from the
    routed checkout (rigging.yaml is the naming authority; git supplies repo/rev), never from
    deployment config. Guards: clean tree, HEAD present on a remote-tracking ref (consumers
    must be able to clone the pin), route must be a real checkout — not a vendored surface."""
    from .catalog import load_catalog

    entry = _target_entry(to)
    reg_root = entry.root
    if not (reg_root / "registry.yaml").is_file():
        raise RigError(f"promote: registry '{to}' is not synced/reachable at {reg_root}")
    manifest = load_manifest(root)
    svc = next((s.service for s in manifest.sensors if s.name == spec), spec)
    route = load_catalog(root).get(svc)
    if route is None:
        raise RigError(f"promote: no services.yaml route for '{svc}' — pass an instance name or "
                       f"a routed service name (services.yaml keys)")
    checkout = route.path
    try:
        if checkout.resolve().is_relative_to((root / "services").resolve()):
            raise RigError(f"promote: '{svc}' routes to the VENDORED launch surface "
                           f"({checkout}) — publishing a service needs its development checkout "
                           f"(route services.yaml at the repo, or publish from the repo's own "
                           f"registry-release CI)")
    except AttributeError:  # is_relative_to: py3.9+; deployments run 3.9+, belt-and-braces
        pass

    def git(*args):
        return subprocess.run(["git", "-C", str(checkout), *args], capture_output=True, text=True)

    toplevel = git("rev-parse", "--show-toplevel").stdout.strip()
    if not toplevel:
        raise RigError(f"promote: '{svc}' route {checkout} is not a git checkout — a service "
                       f"manifest pins repo+rev; there is nothing to pin here")
    if git("status", "--porcelain").stdout.strip():
        raise RigError(f"promote: the '{svc}' checkout at {checkout} has uncommitted changes — "
                       f"commit (or ignore) them first; the pin must be reproducible")
    head = git("rev-parse", "HEAD").stdout.strip()
    url = git("remote", "get-url", "origin").stdout.strip()
    if not url:
        raise RigError(f"promote: the '{svc}' checkout has no `origin` remote — consumers clone "
                       f"`source.repo`, so push the repo somewhere reachable first")
    if not git("branch", "-r", "--contains", head).stdout.strip():
        raise RigError(f"promote: HEAD {head[:12]}… is not on any remote-tracking ref — push it "
                       f"first (consumers must be able to fetch the pinned rev from {url})")

    from .descriptor import load_descriptor
    desc = load_descriptor(svc, checkout)  # cross-checks rigging.yaml `service:` == the route key
    rel = str(checkout.resolve().relative_to(Path(toplevel).resolve()))
    existing = _existing_manifest(reg_root, "services", svc)
    if version is not None and existing and not bump:
        if str(existing.get("version")) == version:
            raise RigError(f"promote: service '{svc}' already exists at {version} — pass --bump "
                           f"or a new --version")
    ver = version or _next_version(existing, bump, f"service '{svc}'")
    smanifest = {"kind": "service", "name": svc, "version": ver,
                 **_carry_forward(existing, "kind", "name", "version", "source"),
                 "source": {"repo": url, "rev": head,
                            **({"path": rel} if rel not in (".", "") else {})}}
    written: list[Path] = []
    backups: dict[Path, Path] = {}
    with _registry_write_session(entry, to, f"publish-{svc}", written, backups):
        _write_pkg(reg_root, "services", svc, smanifest, None, written, backups)
        eprint(f"  service {_namespace_of(to)}/{svc}@{ver} <- {checkout} "
               f"(rev {head[:12]}…{f', path {rel}' if rel not in ('.', '') else ''})")
    _ = desc  # loaded for its cross-checks; the manifest carries no descriptor fields
    # --adopt: record the published pin in rig.lock — the service branch has no config to
    # re-anchor, so adoption here is PROVENANCE: pkg list stops calling it local/unpublished
    # and the lock names what this deployment corresponds to. The dev route stays (that's the
    # dev loop); `rig pkg upgrade` is the deliberate switch to a vendored copy at the pin.
    # Once adopted, every republish (promote --bump / pkg save) keeps the row current.
    lock = load_lock(root)
    packages = lock.get("packages") or {}
    prior = next((r for r in packages if r.startswith(f"{to}/") and unqualified(r) == svc
                  and (packages[r] or {}).get("kind") == "service"), None)
    if adopt or prior:
        from .install import registry_commit
        from .lock import record_package, record_registry, save_lock
        new_ref = f"{to}/{svc}@{ver}"
        if prior and prior != new_ref:
            packages.pop(prior, None)
        record_package(lock, new_ref, {"kind": "service",
                                       "source": {"repo": url, "rev": head,
                                                  **({"path": rel} if rel not in (".", "")
                                                     else {})}})
        record_registry(lock, entry.name, rtype=entry.type, location=entry.location,
                        commit=registry_commit(entry))
        save_lock(root, lock)
        eprint(f"  {svc}: rig.lock now pins {new_ref}"
               + (" (ADOPTED — was local/unpublished)" if adopt and not prior else
                  " (lock row carried to the new version)")
               + "; the dev route stays — `rig pkg upgrade` vendors at the pin when you want "
                 "a self-contained deployment")
    return 0


def _capture_vehicle(root: Path, lock: dict, new_overlay_rows: dict[str, str] | None = None,
                     adopted_rows: dict[str, str] | None = None,
                     skipped_rows: set[str] | None = None) -> dict:
    """The vehicle payload: the origin vehicle.yaml in TEMPLATE form. Identity that must be
    distinct per host (vehicle/vehicle_id) becomes self-markers; fleet DEFAULTS (platform,
    data_dir, images, ros, vars, env) pass through literal — the vehicle-local tier overrides
    them per host. Rows: refs requalified to registry namespaces and VERSION-STRIPPED (the
    suite's members are the only pin authority); config paths canonicalized; rows with no
    registry provenance are warned and omitted (nothing can materialize them). Re-serialized —
    vehicle.yaml comments do not survive capture (documented v1 concession)."""
    raw = load_yaml(root / "vehicle.yaml")
    payload: dict = {}
    for key, value in raw.items():
        if key in ("infra", "sensors", "autonomy"):
            continue  # rows handled below, in tier order
        if key in ("vehicle", "vehicle_id"):
            payload[key] = value if (isinstance(value, str) and f"{{{{{key}}}}}" in value) \
                else f"{{{{{key}}}}}"  # literal identity -> self-marker (mandatory per host)
        else:
            payload[key] = value
    service_pins = {unqualified(r): r for r, info in (lock.get("packages") or {}).items()
                    if (info or {}).get("kind") == "service"}
    for tier in ("infra", "sensors", "autonomy"):
        rows_out = []
        for row in raw.get(tier) or []:
            if not isinstance(row, dict) or not (row.get("name") and row.get("service")):
                continue
            if str(row["name"]) in (skipped_rows or set()):
                continue  # hand-authored without --adopt consent — warned at selection time
            adopted_ref = (adopted_rows or {}).get(str(row["name"]))
            if not adopted_ref and not row.get("profile") and row["service"] not in service_pins:
                eprint(f"  WARNING: row '{row['name']}' ({row['service']}) has no registry "
                       f"provenance (no profile, no service pin) — omitted from the vehicle "
                       f"plan; adopt it first (rig pkg promote {row['service']} --kind service "
                       f"--adopt)")
                continue
            out = dict(row)
            out["config"] = f"config/{tier}/{row['name']}.yaml"  # canonical: where install writes
            if adopted_ref:
                # This promote just profiled + adopted the hand-authored row; the origin row
                # re-anchors only after the publish session, so substitute here — and strip
                # overrides/overlays exactly as adoption does (the payload baked them in;
                # re-applying would double-apply).
                out["profile"] = adopted_ref
                out.pop("overrides", None)
                out.pop("overlays", None)
                rows_out.append(out)
                continue
            if row.get("profile"):
                out["profile"] = _requalify(str(row["profile"])).partition("@")[0]
            overlays = [_requalify(str(o)).partition("@")[0] for o in row.get("overlays") or []]
            # An overlay THIS promote just emitted from the row's dirty delta isn't bound at
            # origin yet — the plan row must still carry it (appended LAST: apply-order parity),
            # or the tuning never lands on a fresh vehicle.
            fresh = (new_overlay_rows or {}).get(str(row["name"]))
            if fresh and fresh not in overlays:
                overlays.append(fresh)
            if overlays:
                out["overlays"] = overlays
            rows_out.append(out)
        if rows_out:
            payload[tier] = rows_out
    return payload


def promote(root: Path, names: list[str], *, to: str, all_dirty: bool, name: str | None,
            project: str | None, kind: str | None, suite: str | None, bump: bool,
            target_instance: bool, matches: list[str], requires: str | None,
            adopt: bool = False, version: str | None = None,
            vehicle: str | None = None) -> int:
    if bool(names) == all_dirty:
        raise RigError("promote: name instance(s), or pass --all for every dirty instance")
    if name and (len(names) != 1 or suite):
        raise RigError("promote: --name applies to exactly one instance (and never names a suite)")
    if vehicle and not suite:
        raise RigError("promote: --vehicle rides the suite capture (--suite <name>) — a vehicle "
                       "package is a suite's instance plan, never published alone")
    if vehicle and not re.match(r"^[a-z][a-z0-9-]*$", vehicle):
        raise RigError(f"promote: --vehicle '{vehicle}' must match [a-z][a-z0-9-]*")
    if name and not re.match(r"^[a-z][a-z0-9-]*$", name):
        raise RigError(f"promote: --name '{name}' must match [a-z][a-z0-9-]* — for profiles it is "
                       f"the SHORT half only; the service half is derived from the instance "
                       f"(no '/', ':', or '@')")
    if kind not in (None, "overlay", "profile", "service"):
        raise RigError(f"promote: --kind must be overlay, profile, or service, not '{kind}'")
    if version is not None and not re.match(r"^\d+\.\d+\.\d+$", version):
        raise RigError(f"promote: --version must be exact X.Y.Z, got '{version}'")
    if kind == "service":
        if len(names) != 1 or all_dirty or suite or name or matches or requires \
                or target_instance or project:
            raise RigError("promote --kind service: exactly one instance/service name, plus "
                           "--to/--bump/--version/--adopt only — a service manifest is a CODE "
                           "POINTER (rigging.yaml names it; config flags don't apply)")
        return _promote_service(root, names[0], to=to, bump=bump, version=version, adopt=adopt)
    if version is not None:
        raise RigError("promote: --version applies to --kind service only (overlay/profile "
                       "versions follow --bump)")
    # --adopt: ONE named instance onto its freshly published profile — OR, with a suite capture
    # (--all --suite), consent for the capture to profile+adopt HAND-AUTHORED instances it could
    # not otherwise reproduce (dirty anchored instances still become overlays, un-adopted).
    if adopt and not (all_dirty and suite) and (all_dirty or suite or len(names) != 1
                                                or kind == "overlay"):
        raise RigError("promote: --adopt re-pins ONE named instance onto its freshly published "
                       "PROFILE (the overlay analogue is `overlay apply --clear-local`); with "
                       "--all it composes with --suite only (capture consent for hand-authored "
                       "instances)")

    entry = _target_entry(to)
    reg_root = entry.root
    if not (reg_root / "registry.yaml").is_file():
        raise RigError(f"promote: registry '{to}' is not synced/reachable at {reg_root}")
    manifest = load_manifest(root)
    lock = load_lock(root)

    chosen = []
    auto_adopted: set[str] = set()  # hand-authored instances a suite capture profiles + adopts
    hand_skipped: set[str] = set()  # hand-authored, no --adopt consent: warned, plan omits them
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
        elif delta is None and suite and sensor.profile is None:
            # --all + suite: a HAND-AUTHORED instance (no registry anchor — e.g. its service
            # declares no example, so install never materialized one). An overlay is impossible
            # (no base to diff), and without capture the suite cannot reconstruct the row — or
            # worse, would silently reproduce the service's example instead of the hand config.
            # Same inference as the named form: the FULL config becomes a PROFILE, and the
            # instance is ADOPTED (row gains provenance, render unchanged) so later captures
            # see a normal pinned instance. Adoption MUTATES the origin (row + anchor) and
            # publishes an auto-derived package name, so it takes --adopt as consent; without
            # it the instance is skipped LOUDLY and the plan omits the row.
            has_pin = any((info or {}).get("kind") == "service"
                          and unqualified(r) == sensor.service
                          for r, info in (lock.get("packages") or {}).items())
            if not has_pin:
                eprint(f"  WARNING: '{sensor.name}' ({sensor.service}) is hand-authored with no "
                       f"registry service pin — cannot capture it (adopt the service first: "
                       f"rig pkg promote {sensor.service} --kind service --adopt)")
                continue
            if not adopt:
                hand_skipped.add(sensor.name)
                eprint(f"  WARNING: '{sensor.name}' is hand-authored (no registry anchor) — NOT "
                       f"captured; the suite will not reproduce it. Either re-run with --adopt "
                       f"(captures it as profile '{sensor.service}:"
                       f"{sensor.name.replace('_', '-')}' and adopts the row), or pick the name "
                       f"yourself first: rig pkg promote {sensor.name} --kind profile "
                       f"--name <short> --adopt --to {to}, then re-capture")
                continue
            chosen.append((sensor, None, "profile"))
            auto_adopted.add(sensor.name)
            eprint(f"  {sensor.name}: hand-authored — capturing as PROFILE and adopting "
                   f"(no registry base to diff an overlay against; render unchanged)")
    unknown = set(names) - {s.name for s in manifest.sensors}
    if unknown:
        raise RigError(f"promote: unknown instance(s): {', '.join(sorted(unknown))}")
    if not chosen:
        if not suite:
            eprint("rig pkg promote: nothing dirty — no packages to emit")
            return 0
        eprint("rig pkg promote: nothing dirty — emitting the suite alone "
               "(pinned profiles + existing bindings)")
    if adopt and not all_dirty and chosen and chosen[0][2] != "profile":
        raise RigError("promote: --adopt needs --kind profile (this promote would emit an "
                       "overlay — its round-trip is `overlay apply --clear-local`)")

    written: list[Path] = []
    backups: dict[Path, Path] = {}  # pre-existing pkg dir -> pristine copy (rollback restores)
    new_overlays: list[str] = []
    new_overlay_rows: dict[str, str] = {}  # instance -> UNVERSIONED ref of its just-emitted overlay
    #                                        (the vehicle plan's row must carry the tuning too)
    new_profile_members: list[str] = []  # auto-adopted instances' profiles: suite members too —
    adopted_rows: dict[str, str] = {}    # ...and the vehicle plan's rows reference them (the
    #                                       origin rows re-anchor only AFTER the publish session)
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
                payload_bytes = _faithful_payload_bytes(sensor.config, payload)
                if payload_bytes is None and "#" in Path(sensor.config).read_text():
                    eprint(f"  {sensor.name}: payload re-rendered (overrides/injected keys "
                           f"folded in) — the working file's comments are not preserved")
                _write_pkg(reg_root, "profiles", pkg_name, pmanifest,
                           payload_bytes or payload, written, backups)
                eprint(f"  profile {target_ns}/{pkg_name}@{version} <- {sensor.name}"
                       + (f" (based_on {pmanifest['based_on']})" if pmanifest.get("based_on")
                          else ""))
                if sensor.name in auto_adopted:
                    new_profile_members.append(f"{target_ns}/{pkg_name}@{version}")
                    adopted_rows[sensor.name] = f"{target_ns}/{pkg_name}"
                if adopt or sensor.name in auto_adopted:
                    adoptions.append((sensor, pkg_name, version, payload, payload_bytes,
                                      req_lock))
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
                new_overlay_rows[sensor.name] = f"{target_ns}/{pkg_name}"
                eprint(f"  overlay {target_ns}/{pkg_name}@{version} <- {sensor.name} "
                       f"({len(delta)} top-level key(s))")

        if suite:
            existing = _existing_manifest(reg_root, "suites", suite)
            version = _next_version(existing, bump, f"suite '{suite}'")
            profiles = sorted({_requalify(str(s.profile))
                               for s in manifest.sensors if s.profile}
                              | set(new_profile_members))
            bound = []
            for sensor in manifest.sensors:  # existing bindings first, manifest order; then new
                bound.extend(r for r in (_requalify(o) for o in sensor.overlays) if r not in bound)
            bound.extend(o for o in new_overlays if o not in bound)
            # The vehicle plan's name resolves FIRST — it changes the services-member rules
            # below. A suite already carrying a plan re-captures it under the same name even
            # without --vehicle: carrying a stale pin forward would fail the head law, and
            # silently DROPPING the plan would be lossy.
            vehicle_name = vehicle
            if not vehicle_name:
                prior = ((existing or {}).get("members") or {}).get("vehicles") or []
                if prior:
                    vehicle_name = unqualified(str(prior[0]))
                    eprint(f"  note: suite '{suite}' carries vehicle plan '{vehicle_name}' — "
                           f"re-capturing it from the current vehicle.yaml (pass --vehicle to "
                           f"rename)")
            # Bare (service-backed) instances: nothing above recreates them — a profile member
            # materializes ITS instance at install, so a profile-less row needs a `services:`
            # member (install materializes one from the service's example) or its promoted
            # overlay can never bind on a fresh vehicle ("no instance created by this suite
            # matches its targets"). The member is the row's LOCK service pin, requalified.
            # Plan-less only: skip a service a profile member covers (auto-materialization would
            # duplicate the instance). WITH a plan, rows drive materialization — no duplication
            # hazard, and closure REQUIRES the member for every bare row.
            profile_services = {split_key(parse_ref(p)[1])[0] for p in profiles}
            services: list[str] = []
            for sensor in manifest.sensors:
                if sensor.profile or sensor.name in auto_adopted or sensor.name in hand_skipped:
                    continue  # auto-adopted: its base is the just-emitted profile member;
                    #           hand-skipped: its row is omitted — a member would be dead weight
                pin = next((r for r, info in (lock.get("packages") or {}).items()
                            if info.get("kind") == "service"
                            and unqualified(r) == sensor.service), None)
                if pin is None:
                    eprint(f"  WARNING: '{sensor.name}' ({sensor.service}) has no registry "
                           f"service pin — the suite cannot recreate it on a fresh vehicle "
                           f"(adopt the service first: rig pkg promote {sensor.service} "
                           f"--kind service --adopt)")
                    continue
                if sensor.service in profile_services and not vehicle_name:
                    eprint(f"  note: bare instance '{sensor.name}' — a profile member already "
                           f"covers {sensor.service}; not adding a services: member (it would "
                           f"duplicate the instance on install)")
                    continue
                ref = _requalify(pin)
                if ref not in services:
                    services.append(ref)
            services.sort()
            if not profiles and not bound and not services:
                raise RigError("promote: the suite would be EMPTY — no pinned profiles, bound "
                               "overlays, or service-backed instances to reference "
                               "(pin/install something first)")
            # The vehicle plan: emitted TOGETHER with the suite (the only authoring path — the
            # combined capture is the one moment rows and members are closed by construction).
            vmember = []
            if vehicle_name:
                vpayload = _capture_vehicle(root, lock, new_overlay_rows, adopted_rows,
                                            hand_skipped)
                vexisting = _existing_manifest(reg_root, "vehicles", vehicle_name)
                vversion = _next_version(vexisting, bump, f"vehicle '{vehicle_name}'")
                vmanifest = {"kind": "vehicle", "name": vehicle_name, "version": vversion,
                             **_carry_forward(vexisting, "kind", "name", "version",
                                             "project", "config"),
                             **({"project": project} if project else {}),
                             "config": {"payload": "config/vehicle.yaml"}}
                _write_pkg(reg_root, "vehicles", vehicle_name, vmanifest, vpayload,
                           written, backups)
                vmember = [f"{target_ns}/{vehicle_name}@{vversion}"]
                nrows = sum(len(vpayload.get(t) or []) for t in ("infra", "sensors", "autonomy"))
                eprint(f"  vehicle {target_ns}/{vehicle_name}@{vversion} <- vehicle.yaml "
                       f"(template, {nrows} row(s))")
            smanifest = {"kind": "suite", "name": suite, "version": version,
                         **_carry_forward(existing, "kind", "name", "version",
                                         "project", "members"),
                         **({"project": project} if project else {}),
                         "members": {**({"vehicles": vmember} if vmember else {}),
                                     **({"services": services} if services else {}),
                                     "profiles": profiles, "overlays": bound}}
            _write_pkg(reg_root, "suites", suite, smanifest, None, written, backups)
            eprint(f"  suite {target_ns}/{suite}@{version} ("
                   + (f"vehicle plan, " if vmember else "")
                   + (f"{len(services)} service(s), " if services else "")
                   + f"{len(profiles)} profile(s), {len(bound)} overlay(s) in binding order)")

    # Registry write committed/validated — the deployment-side half of the round-trip runs
    # only now (a failed publish must never touch the deployment; a failed adoption leaves
    # the published package published, loudly).
    for sensor, pkg_name, version, payload, payload_bytes, req_lock in adoptions:
        _adopt_instance(root, entry, sensor, pkg_name, version, payload, req_lock,
                        payload_bytes=payload_bytes)
    return 0


def _adopt_instance(root: Path, entry: Entry, sensor, pkg_name: str, version: str,
                    payload: dict, req_lock: str, payload_bytes: bytes | None = None) -> None:
    """--adopt: the profile round-trip's second half (the `--clear-local` of profiles). The
    payload IS the instance's current effective config, so re-pinning the row to the fork,
    resetting working+pin to the payload, dropping overrides, and UNBINDING overlays (their
    content is baked in — bound they would double-apply) provably reproduces the render."""
    from .install import registry_commit
    from .lock import record_instance, record_package, record_registry, save_lock, sha256_file
    from .overlay import edit_row
    from .resolve import overlay_payload_path

    fork_ref = f"{entry.name}/{pkg_name}@{version}"  # the CONSUMER-side (alias) spelling
    body = payload_bytes.decode() if payload_bytes is not None else \
        yaml.safe_dump(payload, sort_keys=False, default_flow_style=False)  # comments survive
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
    registries by ALIAS. The shared resolver maps ns -> entry (namespace-declaration wins,
    alias fallback, fail-soft per entry); publishing callers raise on a miss."""
    from .registries import resolve_namespace
    entry = resolve_namespace(ns)
    if entry is not None:
        return entry
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
