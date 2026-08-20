"""``rig registry pending|push|discard`` — the publish tail (QoL plan F7/F10).

Every authoring verb (promote/rebase/repin/save/yank) on a git-type registry ends as a local
commit on a ``promote/*`` branch in the managed cache. This module makes that state visible and
actionable: ``pending`` lists the branches (ahead/behind, merged-detection, copy-paste
commands), ``push`` publishes them via SYSTEM git (rig holds zero credentials; the default
branch is never pushable through rig — sync's ff-only contract owns that ref) with ``--pr``
delegating PR CREATION ONLY to the user's own gh/glab when present, and ``discard`` deletes
unpushed branches — running save's inverse (``unsave_fixup``) for the cwd deployment first, so
regret is cheap and render-preserving. Local-dir registries need none of this: their authoring
writes sit in the operator's own working tree, undone with plain git.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import yaml

from . import RigError
from .common import eprint, print_table
from .registries import Entry, load_entries


def _git(cwd: Path, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)


def _git_entry(name: str) -> Entry:
    entry = next((e for e in load_entries() if e.name == name), None)
    if entry is None:
        raise RigError(f"no registry named '{name}' (rig registry list)")
    if entry.type != "git":
        raise RigError(f"'{name}' is a local-dir registry — its authoring writes sit in "
                       f"{entry.root}; publish/undo with plain git there")
    if not (entry.root / ".git").is_dir():
        raise RigError(f"'{name}' is not synced (rig registry sync {name})")
    return entry


def _default_branch(cache: Path) -> str:
    head = _git(cache, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD") \
        .stdout.strip()
    if head.startswith("origin/"):
        return head[len("origin/"):]
    return _git(cache, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "main"


def _promote_branches(cache: Path) -> list[str]:
    out = _git(cache, "for-each-ref", "--format=%(refname:short)", "refs/heads/promote/")
    return [b for b in out.stdout.split() if b]


def _is_merged(cache: Path, branch: str, default: str) -> bool:
    for base in (f"origin/{default}", default):
        if _git(cache, "merge-base", "--is-ancestor", branch, base).returncode == 0:
            return True
    return False


def _is_pushed(cache: Path, branch: str) -> bool:
    return _git(cache, "rev-parse", "--verify", "-q",
                f"refs/remotes/origin/{branch}").returncode == 0


def pending(name: str | None = None) -> int:
    entries = load_entries()
    if name:
        entries = [e for e in entries if e.name == name]
        if not entries:
            raise RigError(f"registry pending: no registry named '{name}'")
    rows: list[tuple] = []
    hints: list[str] = []
    for entry in entries:
        if entry.type != "git":
            continue  # local-dir: authoring is the operator's own working tree
        cache = entry.root
        if not (cache / ".git").is_dir():
            continue
        default = _default_branch(cache)
        for branch in _promote_branches(cache):
            subject = _git(cache, "log", "-1", "--format=%s", branch).stdout.strip()
            if _is_merged(cache, branch, default):
                state = "merged — safe to delete"
                hints.append(f"rig registry discard {entry.name} {branch}")
            elif _is_pushed(cache, branch):
                state = "pushed (PR/merge pending)"
            else:
                state = "unpushed"
                hints.append(f"rig registry push {entry.name} {branch}")
            rows.append((entry.name, branch, state, subject))
    if not rows:
        print("no pending authoring branches"
              + (f" in '{name}'" if name else " in the git-type registries"))
        return 0
    print_table([("REGISTRY", "BRANCH", "STATE", "CARRIES")] + rows)
    if hints:
        print("\n" + "\n".join(f"  {h}" for h in dict.fromkeys(hints)))
    return 0


def pending_count() -> int:
    """Unpushed, unmerged authoring branches across the git caches — doctor's advisory.
    Fail-soft everywhere (doctor must never break on registry state)."""
    count = 0
    try:
        for entry in load_entries():
            if entry.type != "git" or not (entry.root / ".git").is_dir():
                continue
            default = _default_branch(entry.root)
            count += sum(1 for b in _promote_branches(entry.root)
                         if not _is_merged(entry.root, b, default)
                         and not _is_pushed(entry.root, b))
    except RigError:
        return 0
    return count


def push(name: str, branches: list[str], *, all_pending: bool = False, pr: bool = False) -> int:
    entry = _git_entry(name)
    cache = entry.root
    default = _default_branch(cache)
    available = _promote_branches(cache)
    if all_pending:
        chosen = [b for b in available if not _is_merged(cache, b, default)]
    else:
        chosen = []
        for b in branches:
            full = b if b.startswith("promote/") else f"promote/{b}"
            if full not in available:
                raise RigError(f"registry push: '{b}' is not a promote/* branch in the "
                               f"'{name}' cache (rig registry pending) — the default branch "
                               f"is never pushable through rig")
            chosen.append(full)
    if not chosen:
        eprint(f"rig registry push: nothing to push in '{name}' (rig registry pending)")
        return 0
    rc = 0
    for branch in chosen:
        res = _git(cache, "push", "origin", branch)
        for stream in (res.stdout, res.stderr):
            for line in stream.splitlines():
                eprint(f"  {line}")
        if res.returncode != 0:
            eprint(f"rig registry push: '{branch}' FAILED")
            rc = 1
            continue
        eprint(f"rig registry push: '{branch}' pushed to origin")
        if pr:
            _create_pr(cache, branch)
    if rc == 0:
        eprint("(after PR/merge: `rig registry sync` — consumers then `rig pkg upgrade`)")
    return rc


def _create_pr(cache: Path, branch: str) -> None:
    """PR CREATION ONLY, delegated to the user's own forge tool (the fleet system-ssh
    precedent: rig runs the tool, holds nothing). Tool absent or unrecognized forge -> the
    printed-URL handoff; merge and CI stay on the forge, always."""
    url = _git(cache, "remote", "get-url", "origin").stdout.strip()
    tool = argv = None
    if "github" in url:
        tool, argv = "gh", ["gh", "pr", "create", "--fill", "--head", branch]
    elif "gitlab" in url:
        tool, argv = "glab", ["glab", "mr", "create", "--fill", "--source-branch", branch]
    if tool is None or shutil.which(tool) is None:
        eprint(f"  --pr: no forge tool for this remote ({tool or 'unrecognized host'}"
               f" not on PATH) — open the PR from the URL above")
        return
    res = subprocess.run(argv, cwd=cache, capture_output=True, text=True)
    for stream in (res.stdout, res.stderr):
        for line in stream.splitlines():
            eprint(f"  {line}")
    if res.returncode != 0:
        eprint(f"  --pr: {tool} failed — open the PR from the URL above")


def discard(name: str, branches: list[str], *, all_pending: bool = False,
            root: Path | None = None) -> int:
    entry = _git_entry(name)
    cache = entry.root
    default = _default_branch(cache)
    available = _promote_branches(cache)
    if all_pending:
        chosen = list(available)
        explicit = False
    else:
        if not branches:
            raise RigError("registry discard: name branch(es), or pass --all "
                           "(rig registry pending lists them)")
        chosen = []
        for b in branches:
            full = b if b.startswith("promote/") else f"promote/{b}"
            if full not in available:
                raise RigError(f"registry discard: '{b}' is not a promote/* branch in the "
                               f"'{name}' cache (rig registry pending)")
            chosen.append(full)
        explicit = True
    if not chosen:
        eprint(f"rig registry discard: nothing to discard in '{name}'")
        return 0
    for branch in chosen:
        pushed = _is_pushed(cache, branch)
        if pushed and not explicit:
            eprint(f"  {branch}: PUSHED — skipped by --all (name it explicitly to delete the "
                   f"local branch; the remote needs `git -C {cache} push origin "
                   f"--delete {branch}`)")
            continue
        if root is not None and not _is_merged(cache, branch, default):
            _fixup_from_branch(root, entry, cache, branch, default)
        _git(cache, "branch", "-D", branch)
        eprint(f"rig registry discard: '{branch}' deleted from the '{name}' cache"
               + (f" — the remote copy remains: `git -C {cache} push origin --delete {branch}`"
                  if pushed else ""))
    return 0


def _branch_key(rel: str) -> tuple[str, str] | None:
    """registry-relative manifest path -> (kind, key): profiles/<svc>/<short>/ projects the
    `svc:short` compound; other kinds are flat."""
    parts = rel.split("/")
    if len(parts) == 4 and parts[0] == "profiles" and parts[3] == "manifest.yaml":
        return "profile", f"{parts[1]}:{parts[2]}"
    if len(parts) == 3 and parts[2] == "manifest.yaml" and \
            parts[0] in ("services", "overlays", "suites"):
        return parts[0].rstrip("s"), parts[1]
    return None


def _fixup_from_branch(root: Path, entry: Entry, cache: Path, branch: str, default: str) -> None:
    """The un-save half of a discard: for every package version the branch INTRODUCED, restore
    the cwd deployment onto the default-branch state (the natural 'restored' — what the world
    still sees) with the delta back as local edits. Render-preserving; fail-soft."""
    from .save import unsave_fixup
    changed = _git(cache, "diff", "--name-only", f"{default}...{branch}").stdout.splitlines()
    for rel in [c.strip() for c in changed if c.strip().endswith("manifest.yaml")]:
        keyed = _branch_key(rel)
        if keyed is None:
            continue
        kind, key = keyed
        tip_raw = _git(cache, "show", f"{branch}:{rel}")
        if tip_raw.returncode != 0:
            continue
        try:
            tip = yaml.safe_load(tip_raw.stdout) or {}
        except yaml.YAMLError:
            continue
        tip_version = str(tip.get("version") or "")
        base_raw = _git(cache, "show", f"{default}:{rel}")
        restored = None
        if base_raw.returncode == 0:
            try:
                base = yaml.safe_load(base_raw.stdout) or {}
            except yaml.YAMLError:
                base = {}
            if str(base.get("version") or "") == tip_version:
                continue  # no version introduced — nothing the deployment could have adopted
            payload_rel = (base.get("config") or {}).get("payload")
            payload = None
            if payload_rel:
                blob = subprocess.run(
                    ["git", "-C", str(cache), "show",
                     f"{default}:{str(Path(rel).parent / str(payload_rel))}"],
                    capture_output=True)
                payload = blob.stdout if blob.returncode == 0 else None
            restored = {"version": str(base.get("version")), "manifest": base,
                        "payload": payload}
        unsave_fixup(root, entry.name, kind, key, tip_version, tip, restored)
