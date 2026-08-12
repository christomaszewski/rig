"""``rig pkg promote`` — lift local deployment state into scaffolded, validated packages in a
registry checkout. Promotion emits packages of ANY kind, which is why it lives under ``pkg``:

- default: one **overlay** per named instance — payload = the promote delta ``D`` with the
  round-trip law ``(pin ⊕ overlays) ⊕ D == current render`` (bind D last + ``overlay apply
  --clear-local`` and the render is identical, now versioned).
- ``--kind profile``: the instance's full effective config becomes a new profile (the migration
  path: hand-tuned or pre-registry instances get first-class packages).
- ``--all [--suite S]``: every dirty instance → an overlay each, plus a suite referencing the
  deployment's pinned profiles and the new overlays in binding order — the whole-deployment
  capture a fresh vehicle reproduces with ``pkg install <suite> `` (`--locked` for byte-identity).

Publish scope is WRITE + VALIDATE only (settled): into a local-dir registry the files land in
place; into a git registry's managed cache they land as a LOCAL commit on a ``promote/…`` branch
(no push, no credentials — sync only ever ff-pulls the default branch, so the cache never
wedges), and rig prints the push/PR command. Validation failures roll the checkout back.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from . import RigError
from .common import eprint, load_yaml
from .lock import load_lock
from .manifest import load_manifest
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


def _existing_version(reg_root: Path, kind_dir: str, name: str) -> str | None:
    manifest = reg_root / kind_dir / name / "manifest.yaml"
    return str(load_yaml(manifest).get("version")) if manifest.is_file() else None


def _next_version(existing: str | None, bump: bool, what: str) -> str:
    if existing is None:
        return "1.0.0"
    if not bump:
        raise RigError(f"promote: {what} already exists at {existing} — pass --bump to publish "
                       f"a new version")
    major, minor, patch = (int(x) for x in existing.split("."))
    return f"{major}.{minor}.{patch + 1}"


def _write_pkg(reg_root: Path, kind_dir: str, name: str, manifest: dict,
               payload: dict | None, written: list[Path]) -> None:
    pkg_dir = reg_root / kind_dir / name
    (pkg_dir / "config").mkdir(parents=True, exist_ok=True)
    if payload is not None:
        payload_path = pkg_dir / "config" / ("delta.yaml" if kind_dir == "overlays" else "payload.yaml")
        payload_path.write_text(yaml.safe_dump(payload, sort_keys=False, default_flow_style=False))
    elif (pkg_dir / "config").exists() and not any((pkg_dir / "config").iterdir()):
        (pkg_dir / "config").rmdir()  # suites: REFERENCES ONLY — no config dir allowed
    (pkg_dir / "manifest.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    written.append(pkg_dir)


def promote(root: Path, names: list[str], *, to: str, all_dirty: bool, name: str | None,
            project: str | None, kind: str, suite: str | None, bump: bool,
            target_instance: bool, matches: list[str], requires: str | None) -> int:
    if bool(names) == all_dirty:
        raise RigError("promote: name instance(s), or pass --all for every dirty instance")
    if name and (len(names) != 1 or suite):
        raise RigError("promote: --name applies to exactly one instance (and never names a suite)")
    if kind not in ("overlay", "profile"):
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
            if kind == "overlay" and delta is None:
                raise RigError(f"promote: '{sensor.name}' has no pinned registry base — overlays "
                               f"need one (use --kind profile for hand-authored instances)")
            if kind == "overlay" and not delta:
                raise RigError(f"promote: '{sensor.name}' is clean — nothing to promote "
                               f"(config diff shows deltas)")
            chosen.append((sensor, delta))
        elif delta:  # --all: dirty instances only
            chosen.append((sensor, delta))
    unknown = set(names) - {s.name for s in manifest.sensors}
    if unknown:
        raise RigError(f"promote: unknown instance(s): {', '.join(sorted(unknown))}")
    if not chosen:
        eprint("rig pkg promote: nothing dirty — no packages to emit")
        return 0

    written: list[Path] = []
    new_overlays: list[str] = []
    branch = None
    try:
        if entry.type == "git":
            branch = f"promote/{suite or name or chosen[0][0].name}"
            if _git(reg_root, "switch", "-c", branch).returncode != 0:
                raise RigError(f"promote: branch {branch} already exists in the '{to}' cache — "
                               f"push or delete it first")
        target_ns = load_registry(reg_root, []).namespace

        for sensor, delta in chosen:
            if kind == "profile":
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
                    req = next((ref for ref, info in (lock.get("packages") or {}).items()
                                if info.get("kind") == "service"
                                and ref.rpartition("/")[-1].split("@")[0] == sensor.service), None)
                if req is None:
                    raise RigError(f"promote: no locked service pin for '{sensor.service}' — pass "
                                   f"--requires <ns/service@X.Y.Z>")
                pkg_name = name or f"{sensor.name.replace('_', '-')}"
                version = _next_version(_existing_version(reg_root, "profiles", pkg_name), bump,
                                        f"profile '{pkg_name}'")
                pmanifest: dict = {"kind": "profile", "name": pkg_name, "version": version,
                                   "requires": {"service": req},
                                   "config": {"payload": "config/payload.yaml"}}
                if matches:
                    pmanifest["provides"] = {"sensor": [{"model": pkg_name, "match": list(matches)}]}
                _write_pkg(reg_root, "profiles", pkg_name, pmanifest, payload, written)
                eprint(f"  profile {target_ns}/{pkg_name}@{version} <- {sensor.name}")
            else:
                if delta is None or not delta:
                    continue
                pkg_name = name or (f"{sensor.name.replace('_', '-')}-{project}" if project
                                    else sensor.name.replace("_", "-"))
                version = _next_version(_existing_version(reg_root, "overlays", pkg_name), bump,
                                        f"overlay '{pkg_name}'")
                # Target the service FULLY QUALIFIED unless it lives in the target registry itself —
                # the registry's validator resolves bare names in-registry only.
                svc_target = sensor.service
                if not (reg_root / "services" / sensor.service / "manifest.yaml").is_file():
                    pin = next((ref for ref, info in (lock.get("packages") or {}).items()
                                if info.get("kind") == "service"
                                and ref.rpartition("/")[-1].split("@")[0] == sensor.service), None)
                    if pin:
                        svc_target = pin.split("@", 1)[0]  # ns/name
                omanifest = {"kind": "overlay", "name": pkg_name, "version": version,
                             "targets": ([{"instance": sensor.name}] if target_instance
                                         else [{"service": svc_target}]),
                             **({"project": project} if project else {}),
                             "config": {"payload": "config/delta.yaml"}}
                _write_pkg(reg_root, "overlays", pkg_name, omanifest, delta, written)
                new_overlays.append(f"{to}/{pkg_name}@{version}")
                eprint(f"  overlay {target_ns}/{pkg_name}@{version} <- {sensor.name} "
                       f"({len(delta)} top-level key(s))")

        if suite:
            version = _next_version(_existing_version(reg_root, "suites", suite), bump,
                                    f"suite '{suite}'")
            profiles = sorted({s.profile for s, _ in chosen if s.profile}
                              | {s.profile for s in manifest.sensors if s.profile})
            bound = []
            for sensor in manifest.sensors:  # existing bindings first, manifest order; then new
                bound.extend(o for o in sensor.overlays if o not in bound)
            bound.extend(o for o in new_overlays if o not in bound)
            smanifest = {"kind": "suite", "name": suite, "version": version,
                         **({"project": project} if project else {}),
                         "members": {"profiles": profiles, "overlays": bound}}
            _write_pkg(reg_root, "suites", suite, smanifest, None, written)
            eprint(f"  suite {target_ns}/{suite}@{version} ({len(profiles)} profile(s), "
                   f"{len(bound)} overlay(s) in binding order)")

        _, issues = validate_registry(reg_root, check_index=False)
        if issues:
            details = "; ".join(f"{i.where}: {i.message}" for i in issues[:3])
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
        import shutil
        if entry.type == "git" and branch:
            _git(reg_root, "reset", "--hard", "-q")
            _git(reg_root, "clean", "-fdq")
            _git(reg_root, "switch", "-q", prior_branch)
            _git(reg_root, "branch", "-D", branch)
        else:
            for pkg_dir in written:
                shutil.rmtree(pkg_dir, ignore_errors=True)
        raise
    return 0
