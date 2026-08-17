"""Git history as a READ-ONLY version archive for registries (v0.1.63).

The "registries carry ONE current version" model already implies the archive: git-type caches
are FULL clones, so every past version is locally present in history. This module reads it —
`git log`/`git show` only, never a checkout, so the cache/worktree is never disturbed — and is
CAPABILITY-DETECTED: a location without git history returns None everywhere, and callers keep
today's behavior exactly (local-dir folders, shared drives, fixtures). The authoring side is
untouched: no tags, no push; the index still maps name -> current; pins stay exact.

Two consumers:
- ``pkg add ns/name@<non-current>`` — resolve a historical version (checkout_pkg).
- ``--locked`` reproduction — resolve packages at the LOCKED registry commit
  (checkout_pkg_at), turning verify-or-fail into actually-reproduce; the lock's hashes still
  gate the result, so rewritten history fails loudly instead of installing different bytes.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import yaml

from .common import eprint
from .registries import Entry
from .registry import Package


def _git(root: Path, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


def _git_prefix(entry: Entry) -> tuple[Path, str] | None:
    """(git toplevel, registry-root prefix inside it) — None when the registry has no git
    history. The registry may sit BELOW the repo root (a nested registry checkout), so package
    paths must be prefixed when addressing blobs."""
    root = entry.root
    top = _git(root, "rev-parse", "--show-toplevel")
    if top.returncode != 0 or not top.stdout.strip():
        return None
    toplevel = Path(top.stdout.strip())
    try:
        prefix = str(root.resolve().relative_to(toplevel))
    except ValueError:
        return None
    return toplevel, ("" if prefix == "." else prefix + "/")


def _read_blob(toplevel: Path, commit: str, path: str) -> bytes | None:
    proc = subprocess.run(["git", "-C", str(toplevel), "show", f"{commit}:{path}"],
                          capture_output=True)
    return proc.stdout if proc.returncode == 0 else None


def _materialize(toplevel: Path, prefix: str, kind_dir: str, name: str,
                 commit: str) -> Package | None:
    """Extract <kind_dir>/<name>/ as it was at `commit` into a scratch dir and wrap it as a
    Package — the rest of the install machinery then works unchanged."""
    pkg_rel = f"{prefix}{kind_dir}/{name.replace(':', '/')}"  # profile keys project svc:short -> svc/short
    listing = _git(toplevel, "ls-tree", "-r", "--name-only", commit, "--", pkg_rel)
    files = [line for line in listing.stdout.splitlines() if line.strip()]
    if listing.returncode != 0 or not files:
        return None
    scratch = Path(tempfile.mkdtemp(prefix="rig-history-")) / name.replace(":", "--")
    for path in files:
        blob = _read_blob(toplevel, commit, path)
        if blob is None:
            return None
        dest = scratch / Path(path).relative_to(pkg_rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(blob)
    try:
        manifest = yaml.safe_load((scratch / "manifest.yaml").read_text()) or {}
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(manifest, dict):
        return None
    return Package(kind=str(manifest.get("kind") or kind_dir.rstrip("s")),
                   name=name,  # the KEY (profiles: service:short) — manifest `name` is the short half
                   version=str(manifest.get("version") or "?"),
                   pkg_dir=scratch, manifest=manifest)


def checkout_pkg_at(entry: Entry, kind_dir: str, name: str, commit: str) -> Package | None:
    """The package as it was at a specific commit (the --locked reproduction path)."""
    where = _git_prefix(entry)
    if where is None:
        return None
    toplevel, prefix = where
    return _materialize(toplevel, prefix, kind_dir, name, commit)


def checkout_pkg(entry: Entry, kind_dir: str, name: str, version: str) -> Package | None:
    """The package at the commit where its manifest carried `version` — walking the manifest's
    history newest-first (the FIRST hit is the last state published under that version)."""
    where = _git_prefix(entry)
    if where is None:
        return None
    toplevel, prefix = where
    manifest_rel = f"{prefix}{kind_dir}/{name.replace(':', '/')}/manifest.yaml"
    log = _git(toplevel, "log", "--format=%H", "--", manifest_rel)
    if log.returncode != 0:
        return None
    for commit in [line.strip() for line in log.stdout.splitlines() if line.strip()]:
        blob = _read_blob(toplevel, commit, manifest_rel)
        if blob is None:
            continue
        try:
            doc = yaml.safe_load(blob.decode()) or {}
        except (UnicodeDecodeError, yaml.YAMLError):
            continue
        if isinstance(doc, dict) and str(doc.get("version")) == version:
            pkg = _materialize(toplevel, prefix, kind_dir, name, commit)
            if pkg is not None:
                eprint(f"  {entry.name}/{name}@{version}: HISTORICAL version from git history "
                       f"({commit[:7]}) — the registry currently carries something newer")
            return pkg
    return None


def kind_dir_of(kind: str) -> str:
    return {"service": "services", "profile": "profiles",
            "overlay": "overlays", "suite": "suites"}.get(kind, kind + "s")
