"""``rig pkg yank <ref> --from <registry>`` — version retraction (QoL plan F10, OQ-K settled).

Restores the PREVIOUS version from git history as current (delete the new package dir, restore
the old, regen index, validate — through the shared write+validate envelope: local-dir in
place, git as a local ``promote/yank-…`` branch commit). No prior version ⇒ the package is
REMOVED entirely — "remove a package from a registry" is yank's degenerate case, so a mistaken
first publish undoes as cleanly as a mistaken update. Non-git local-dir (no history): only full
removal is available, stated plainly. Git-backed consumers lose nothing: the yanked version
stays installable from history — it just stops being current.

``--from``, not ``--to``: publication verbs write TO a registry; retraction takes FROM one.

The deployment half is save's inverse (``unsave_fixup``): when the cwd deployment references
the yanked version, it re-anchors onto the restored version with the delta back as LOCAL edits
— render byte-identical. Deployments elsewhere can't be fixed from here (the caveat prints);
for them the retraction is deliberate and ``pkg upgrade`` is the remedy.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from . import RigError
from .common import eprint
from .promote import _registry_write_session, _target_entry
from .refs import parse_ref, split_key
from .registry import load_registry


def _resolve_pkg(reg, ref: str):
    """Same resolution as repin: full key, or the profile short half when unambiguous."""
    name = parse_ref(ref)[1]
    pkg = reg.packages.get(name)
    if pkg is not None:
        return name, pkg
    hits = [k for k in sorted(reg.packages) if split_key(k)[1] == name]
    if len(hits) == 1:
        return hits[0], reg.packages[hits[0]]
    if hits:
        raise RigError(f"yank: '{name}' is ambiguous: {', '.join(hits)} — use the full "
                       f"<service>:<short> key")
    raise RigError(f"yank: no package '{name}' in the '{reg.namespace}' registry")


def yank(ref: str, *, from_: str, dry_run: bool = False, root: Path | None = None) -> int:
    entry = _target_entry(from_)
    reg_root = entry.root
    if not (reg_root / "registry.yaml").is_file():
        raise RigError(f"yank: registry '{from_}' is not synced/reachable at {reg_root}")
    reg = load_registry(reg_root, [])
    name, pkg = _resolve_pkg(reg, ref)
    kind_dir = pkg.kind + "s"
    from .history import checkout_pkg, previous_version
    prev = previous_version(entry, kind_dir, name, pkg.version)
    restored_pkg = checkout_pkg(entry, kind_dir, name, prev) if prev else None
    if prev and restored_pkg is None:
        raise RigError(f"yank: git history names @{prev} but cannot materialize it — "
                       f"the cache may be shallow; re-sync and retry")
    plan = (f"restore @{prev} as current (the yanked @{pkg.version} stays in git history)"
            if restored_pkg is not None else
            f"REMOVE the package (no prior version"
            + (" — no git history to restore from" if prev is None and entry.type == "local-dir"
               else "") + ")")
    eprint(f"  {from_}/{name}@{pkg.version} ({pkg.kind}): {plan}")
    if dry_run:
        eprint("rig pkg yank (dry-run): nothing written")
        return 0

    pkg_dir = reg_root / kind_dir / name.replace(":", "/")
    written: list[Path] = []
    backups: dict[Path, Path] = {}
    with _registry_write_session(entry, from_, f"yank-{name.replace(':', '-')}", written,
                                 backups, what="yank"):
        backup = Path(tempfile.mkdtemp(prefix="rig-yank-")) / name.replace(":", "--")
        shutil.copytree(pkg_dir, backup)
        backups[pkg_dir] = backup
        shutil.rmtree(pkg_dir)
        if restored_pkg is not None:
            shutil.copytree(restored_pkg.pkg_dir, pkg_dir)
        elif not any(pkg_dir.parent.iterdir()) and pkg_dir.parent != reg_root / kind_dir:
            pkg_dir.parent.rmdir()  # profiles/<svc>/ left empty by the removal
        written.append(pkg_dir)
        eprint(f"  {pkg.kind} {from_}/{name}: "
               + (f"current is @{prev} again" if restored_pkg is not None else "removed"))

    # the deployment half: save's inverse for THIS deployment; elsewhere the caveat applies
    restored = None
    if restored_pkg is not None:
        payload_rel = (restored_pkg.manifest.get("config") or {}).get("payload")
        payload = None
        if payload_rel and (restored_pkg.pkg_dir / str(payload_rel)).is_file():
            payload = (restored_pkg.pkg_dir / str(payload_rel)).read_bytes()
        restored = {"version": prev, "manifest": restored_pkg.manifest, "payload": payload}
    if root is not None:
        from .save import unsave_fixup
        unsave_fixup(root, entry.name, pkg.kind, name, pkg.version, pkg.manifest, restored)
    eprint("rig pkg yank: deployments ELSEWHERE that adopted the yanked version keep running "
           "(self-contained) but reference a retracted version — `rig pkg upgrade` moves them "
           "onto current; the yanked bits remain in git history")
    return 0
