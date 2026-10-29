"""Per-repo bare git mirrors — the one place rig fetches remote git sources from.

Every git source a build needs (a registry service's pinned source, the msgs overlay's `source:`
repos, a rigging's `build.sources`) resolves through ONE bare mirror per repo at
``$RIG_HOME/cache/git/<slug>.git`` (the src cache's ``_repo_slug`` key: one repo spelled two ways
is one mirror). Checkouts and exports are then made from the mirror locally, with no network.

Online, a mirror is fetched at most once per rig process (branches and tags, never pruned), and
again only when a ref is missing. ``offline = True`` (``rig build --offline``) fetches nothing: a
repo or ref the mirror lacks is a named failure, and a branch resolves to the mirror's last fetch,
said out loud. Resolved commits are kept reachable under ``refs/rig/pins/`` so a branch moving
upstream can never let ``git gc`` drop a pin.
"""
from __future__ import annotations

import datetime
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import RigError
from .common import eprint

offline = False             # `rig build --offline`: never fetch
_fetched: set[str] = set()  # mirror paths refreshed by this process
_SHA = re.compile(r"[0-9a-f]{40}")
_HINT_OFFLINE = "`rig build --offline` builds from the local mirror instead"
_HINT_SYNC = "run `rig src sync` while online to fetch it"


def mirror_path(url: str) -> Path:
    from .install import _repo_slug
    from .registries import rig_home
    return rig_home() / "cache" / "git" / f"{_repo_slug(url)}.git"


def _git(*args, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd) if cwd else None,
                          capture_output=True, text=True)


def _detail(proc: subprocess.CompletedProcess) -> str:
    lines = (proc.stderr or proc.stdout or "").strip().splitlines()
    return lines[-1] if lines else f"git exited {proc.returncode}"


def _fetch(url: str, path: Path, *refspecs: str) -> None:
    refspecs = refspecs or ("+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*")
    proc = _git("-C", str(path), "fetch", "-q", url, *refspecs)
    if proc.returncode:
        raise RigError(f"cannot fetch {url}: {_detail(proc)} — {_HINT_OFFLINE}")
    _fetched.add(str(path))


def ensure(url: str) -> Path:
    """The repo's mirror, created and fetched when online; offline it must already exist."""
    path = mirror_path(url)
    if path.is_dir():
        return path
    if offline:
        raise RigError(f"{url} is not in the local mirror ({path}) — {_HINT_SYNC}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=path.name + ".", dir=path.parent))
    try:
        proc = _git("init", "-q", "--bare", str(tmp))
        if proc.returncode:
            raise RigError(f"cannot create a mirror for {url}: {_detail(proc)}")
        _fetch(url, tmp)
        try:
            tmp.rename(path)
        except OSError:  # another rig created it concurrently — theirs is as good
            if not path.is_dir():
                raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    _fetched.add(str(path))
    return path


def fetched_at(path: Path) -> str:
    """When the mirror last fetched (FETCH_HEAD's mtime), for offline notes."""
    stamp = path / "FETCH_HEAD"
    if not stamp.exists():
        return "an unknown time"
    when = datetime.datetime.fromtimestamp(stamp.stat().st_mtime)
    return when.isoformat(timespec="minutes")


def _rev(path: Path, ref: str) -> str | None:
    proc = _git("-C", str(path), "rev-parse", "--verify", "-q", f"{ref}^{{commit}}")
    return proc.stdout.strip() if proc.returncode == 0 else None


def _is_branch(path: Path, ref: str) -> bool:
    def has(name):
        return _git("-C", str(path), "show-ref", "--verify", "-q", name).returncode == 0
    return has(f"refs/heads/{ref}") and not has(f"refs/tags/{ref}")


def resolve(url: str, ref: str) -> str:
    """The commit ``ref`` (tag, branch or SHA) names in ``url``'s mirror. Online: the mirror is
    refreshed once per process — a full SHA already present needs no fetch — and once more on a
    miss, finally asking the remote for the commit by id. Offline: the mirror as last fetched."""
    path = ensure(url)
    immutable = bool(_SHA.fullmatch(ref))
    sha = _rev(path, ref)
    if not offline and (sha is None or (not immutable and str(path) not in _fetched)):
        _fetch(url, path)
        sha = _rev(path, ref)
        if sha is None and immutable:  # a commit no branch/tag reaches: ask for it by id
            proc = _git("-C", str(path), "fetch", "-q", url, ref)
            sha = _rev(path, ref) if proc.returncode == 0 else None
    if sha is None:
        where = (f"the local mirror (fetched {fetched_at(path)}) — {_HINT_SYNC}" if offline
                 else url)
        raise RigError(f"{url}: ref '{ref}' not found in {where}")
    if offline and _is_branch(path, ref):
        eprint(f"rig: {url}: branch '{ref}' resolved offline to {sha[:12]} "
               f"(mirror fetched {fetched_at(path)})")
    _git("-C", str(path), "update-ref", f"refs/rig/pins/{sha}", sha)
    return sha


def clone(url: str, sha: str, dest: Path) -> None:
    """A persistent working tree of ``sha`` at ``dest``, cloned from the mirror (objects
    hardlinked, no network) with ``origin`` reset to ``url``."""
    path = ensure(url)
    for args in (("clone", "-q", "--no-checkout", str(path), str(dest)),
                 ("-C", str(dest), "remote", "set-url", "origin", url)):
        proc = _git(*args)
        if proc.returncode:
            raise RigError(f"cannot check out {url} from its mirror: {_detail(proc)}")
    if _git("-C", str(dest), "checkout", "-q", "--detach", sha).returncode:
        # Not reachable from a cloned branch/tag: bring the pin over from the mirror explicitly.
        _git("-C", str(dest), "fetch", "-q", str(path), f"refs/rig/pins/{sha}")
        proc = _git("-C", str(dest), "checkout", "-q", "--detach", sha)
        if proc.returncode:
            raise RigError(f"cannot check out {url} at {sha[:12]}: {_detail(proc)}")


def export(url: str, sha: str, dest: Path, *, submodules: bool = False) -> None:
    """The tree of ``sha`` at ``dest`` with no ``.git``: a checkout (filters applied, export
    attributes ignored — what a clone would build), made from the mirror with no network."""
    path = ensure(url)
    work = Path(tempfile.mkdtemp(prefix="rig-export-"))
    try:
        # --shared: the mirror's whole object store is borrowed, so any commit in it checks out
        for args in (("clone", "-q", "--shared", "--no-checkout", str(path), str(work / "t")),
                     ("-C", str(work / "t"), "checkout", "-q", "--detach", sha),
                     ("-C", str(work / "t"), "remote", "set-url", "origin", url)):
            proc = _git(*args)
            if proc.returncode:
                raise RigError(f"cannot export {url} at {sha[:12]}: {_detail(proc)}")
        if submodules:
            update_submodules(work / "t", url)
        for meta in (work / "t").rglob(".git"):  # the repo's and any submodule's
            shutil.rmtree(meta) if meta.is_dir() else meta.unlink()
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(work / "t"), str(dest))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _absolute(url: str, base: str) -> str:
    """A submodule URL as git resolves it: ``./x`` and ``../x`` are relative to the
    superproject's remote ``base`` (each ``../`` drops one path component)."""
    if not url.startswith(("./", "../")):
        return url
    base = base.rstrip("/")
    while True:
        if url.startswith("./"):
            url = url[2:]
        elif url.startswith("../"):
            url = url[3:]
            cut = max(base.rfind("/"), base.rfind(":"))
            base = base[:cut] if cut > base.find("://") + 2 else base
        else:
            break
    sep = ":" if re.match(r"^[^/@:]+@[^:/]+$", base) else "/"
    return f"{base}{sep}{url}"


def _submodules(gitmodules: str) -> dict[str, dict[str, str]]:
    """name -> {path, url} from `git config --get-regexp` output over a .gitmodules."""
    out: dict[str, dict[str, str]] = {}
    for line in gitmodules.splitlines():
        key, _, value = line.partition(" ")
        m = re.fullmatch(r"submodule\.(.+)\.(path|url)", key)
        if m:
            out.setdefault(m.group(1), {})[m.group(2)] = value
    return {n: s for n, s in out.items() if s.get("path") and s.get("url")}


def update_submodules(work: Path, origin_url: str) -> None:
    """``git submodule update --init``, recursively, with every submodule fetched through its
    own mirror (``url.<mirror>.insteadOf``) — so a pinned superproject checks out offline."""
    if not (work / ".gitmodules").is_file():
        return
    listing = _git("config", "-f", str(work / ".gitmodules"), "--get-regexp",
                   r"^submodule\..*\.(path|url)$")
    subs = _submodules(listing.stdout)
    # Submodules already checked out at their pinned commit need nothing — not even a mirror, so
    # a checkout initialized before mirrors existed keeps working offline.
    status = _git("-C", str(work), "submodule", "status").stdout.splitlines()
    current = {line[1:].split()[1] for line in status if line.startswith(" ") and
               len(line[1:].split()) > 1}
    pending: dict[str, dict[str, str]] = {}
    for name, sub in subs.items():
        sub["absolute"] = url = _absolute(sub["url"], origin_url)
        if sub["path"] in current:
            continue
        commit = _git("-C", str(work), "rev-parse", f"HEAD:{sub['path']}").stdout.strip()
        if commit:
            resolve(url, commit)
        pending[name] = sub
    if pending:
        # Point each pending submodule at its mirror for the clone, then back at its upstream.
        # (Not url.<mirror>.insteadOf: git skips URL rewriting for local-path submodule URLs.)
        paths = [sub["path"] for sub in pending.values()]
        steps = [("-C", str(work), "submodule", "init", "--quiet", "--", *paths)]
        steps += [("-C", str(work), "config", f"submodule.{name}.url",
                   str(mirror_path(sub["absolute"]))) for name, sub in pending.items()]
        # The mirrors are local paths: allow the file transport for submodules (git >= 2.38.1
        # refuses it by default, a guard against UNTRUSTED repos pointing at local files).
        steps.append(("-c", "protocol.file.allow=always", "-C", str(work), "submodule", "update",
                      "--quiet", "--", *paths))
        for name, sub in pending.items():
            steps += [("-C", str(work), "config", f"submodule.{name}.url", sub["absolute"]),
                      ("-C", str(work / sub["path"]), "remote", "set-url", "origin",
                       sub["absolute"])]
        for args in steps:
            proc = _git(*args)
            if proc.returncode:
                raise RigError(f"submodule update in {work} failed: {_detail(proc)}")
    for sub in subs.values():
        update_submodules(work / sub["path"], sub["absolute"])


def sync_submodules(url: str, sha: str) -> None:
    """Mirror every submodule ``sha`` pins, recursively, without checking anything out."""
    path = mirror_path(url)
    listing = _git("-C", str(path), "config", "--blob", f"{sha}:.gitmodules", "--get-regexp",
                   r"^submodule\..*\.(path|url)$")
    for sub in _submodules(listing.stdout if listing.returncode == 0 else "").values():
        tree = _git("-C", str(path), "ls-tree", sha, "--", sub["path"]).stdout.split()
        if len(tree) >= 3 and tree[1] == "commit":
            sub_url = _absolute(sub["url"], url)
            sync_submodules(sub_url, resolve(sub_url, tree[2]))


def reset() -> None:
    """Forget this process's fetches and leave offline mode (tests, and long-lived callers)."""
    global offline
    offline = False
    _fetched.clear()
