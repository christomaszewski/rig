"""Client-side registry management — the user-level state under ``~/.rig`` (override: ``$RIG_HOME``).

``registries.yaml`` holds the ORDERED registry list (order = resolution priority); ``cache/registries/``
holds managed full clones of git-type registries. Two registry types, both promotable targets:

- ``git`` — managed clone in the cache; ``rig registry sync`` = ff-only pull of the default branch
  (divergence is an error with guidance, never a merge). All resolution runs on the cache — fully
  offline after sync.
- ``local-dir`` — an existing folder used IN PLACE (shared drives, test fixtures, and the dev loop:
  a higher-priority local-dir checkout shadows the cached public copy). Sync just re-checks it.

Degrade, never brick (OQ-8): a registry whose schema this rig can't read, or whose tree is broken,
is skipped with a loud warning — only operations explicitly targeting it fail. One upgraded/broken
registry must not take down unrelated field operations.

``rig setup`` also lives here: the idempotent first-run initializer that owns ALL user state
(package managers install the program; they never touch ``$HOME`` — the deb/brew story depends on
this split). It creates ``~/.rig``, seeds the default public registry, optionally wires a source
checkout onto PATH via a delimited rc block, and ``--purge`` reverses everything.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

from . import RigError
from .common import eprint, load_yaml
from .registry import Registry, generate_index, load_registry

DEFAULT_PUBLIC = ("public", "https://github.com/christomaszewski/rig-registry-public")

_BLOCK_BEGIN = "# >>> rig >>>"
_BLOCK_END = "# <<< rig <<<"


def rig_home() -> Path:
    return Path(os.environ.get("RIG_HOME") or (Path.home() / ".rig"))


def registries_file() -> Path:
    return rig_home() / "registries.yaml"


def cache_dir(name: str) -> Path:
    return rig_home() / "cache" / "registries" / name


@dataclass(frozen=True)
class Entry:
    name: str            # the local alias AND the namespace qualifier consumers type (`public/…`)
    type: str            # "git" | "local-dir"
    location: str        # url (git) or path (local-dir)

    @property
    def root(self) -> Path:
        """Where the registry tree lives for this client: local-dir in place, git in the cache."""
        return Path(self.location).expanduser().resolve() if self.type == "local-dir" else cache_dir(self.name)


def load_entries() -> list[Entry]:
    path = registries_file()
    if not path.is_file():
        return []
    data = load_yaml(path)
    entries: list[Entry] = []
    for raw in data.get("registries") or []:
        raw = raw or {}
        name, rtype = str(raw.get("name") or ""), str(raw.get("type") or "")
        loc = str(raw.get("url") or raw.get("path") or "")
        if not name or rtype not in ("git", "local-dir") or not loc:
            raise RigError(f"{path}: bad entry {raw!r} (need name, type: git|local-dir, url|path)")
        entries.append(Entry(name=name, type=rtype, location=loc))
    return entries


def save_entries(entries: list[Entry]) -> None:
    registries_file().parent.mkdir(parents=True, exist_ok=True)
    rows = [{"name": e.name, "type": e.type, ("url" if e.type == "git" else "path"): e.location}
            for e in entries]
    registries_file().write_text(
        "# Ordered registry list — ORDER IS PRIORITY for unqualified-name resolution.\n"
        "# Managed by `rig registry add|remove` (hand-editing is fine; keep it a list).\n"
        + yaml.safe_dump({"registries": rows}, sort_keys=False))


def add_entry(name: str, *, url: str | None, path: str | None, front: bool) -> None:
    if bool(url) == bool(path):
        raise RigError("registry add: pass a git URL, or --path <dir> for a local-dir registry")
    entries = load_entries()
    if any(e.name == name for e in entries):
        raise RigError(f"registry add: '{name}' already exists (rig registry remove {name} first)")
    if path is not None:
        target = Path(path).expanduser().resolve()
        if not (target / "registry.yaml").is_file():
            raise RigError(f"registry add: {target} is not a registry (no registry.yaml)")
        entry = Entry(name=name, type="local-dir", location=str(target))
    else:
        entry = Entry(name=name, type="git", location=str(url))
    entries = [entry] + entries if front else entries + [entry]
    save_entries(entries)
    eprint(f"rig registry add: '{name}' ({entry.type}) at priority "
           f"{'1 (front)' if front else len(entries)} — sync with `rig registry sync {name}`"
           if entry.type == "git" else
           f"rig registry add: '{name}' (local-dir, used in place) at priority "
           f"{1 if front else len(entries)}")


def remove_entry(name: str) -> None:
    entries = load_entries()
    if not any(e.name == name for e in entries):
        raise RigError(f"registry remove: no registry named '{name}'")
    save_entries([e for e in entries if e.name != name])
    cache = cache_dir(name)
    note = f" (cache kept at {cache} — delete it if you're done)" if cache.is_dir() else ""
    eprint(f"rig registry remove: '{name}' removed{note}")


def _git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def open_registry(entry: Entry) -> tuple[Registry, dict]:
    """Load an entry's registry tree + its index (from index.json when fresh on disk, else generated
    in memory). Raises RigError when the tree is missing/unreadable/too-new — callers degrade."""
    root = entry.root
    if not (root / "registry.yaml").is_file():
        hint = f"run `rig registry sync {entry.name}`" if entry.type == "git" else f"missing: {root}"
        raise RigError(f"registry '{entry.name}': not synced ({hint})")
    reg = load_registry(root, [])  # non-structural issues don't block lookups
    index_path = root / "index.json"
    if index_path.is_file():
        import json
        try:
            return reg, json.loads(index_path.read_text())
        except (OSError, ValueError):
            pass
    return reg, generate_index(reg)


def resolve_namespace(ns: str) -> Entry | None:
    """The configured entry SERVING `ns`: an entry whose registry declares it as namespace wins
    (manifest-side refs carry namespaces, not consumer aliases); alias match is the fallback.
    Fail-soft per entry (an unreachable registry must not mask the right one); None when nothing
    serves it — reporting callers degrade, publishing callers raise their own error."""
    fallback = None
    for entry in load_entries():
        if entry.name == ns:
            fallback = entry
        try:
            reg, _ = open_registry(entry)
        except RigError:
            continue
        if reg.namespace == ns:
            return entry
    return fallback


def current_version_of(ns: str, name: str) -> str | None:
    """Registry-current version of ns/name via the synced caches — None when unresolvable
    (unknown namespace, unsynced cache, package gone). Fail-soft by design: currency reporting
    must never fail on registry state."""
    entry = resolve_namespace(ns)
    if entry is None:
        return None
    try:
        _, index = open_registry(entry)
    except RigError:
        return None
    return ((index.get("packages") or {}).get(name) or {}).get("version")


def _index_delta(old: dict, new: dict) -> list[str]:
    """Package-level changes between two index `packages` maps — the sync digest's body."""
    lines: list[str] = []
    for name in sorted(set(old) | set(new)):
        before, after = old.get(name) or {}, new.get(name) or {}
        if before and after:
            if before.get("version") != after.get("version"):
                lines.append(f"{name} {before.get('version', '?')} -> {after.get('version', '?')}")
        elif after:
            lines.append(f"+ {name} ({after.get('kind', '?')}) {after.get('version', '?')}")
        else:
            lines.append(f"- {name}")
    return lines


def sync(names: list[str] | None = None) -> int:
    entries = load_entries()
    if not entries:
        raise RigError("no registries configured — `rig setup` adds the default public registry, "
                       "or `rig registry add <name> <url>`")
    chosen = [e for e in entries if not names or e.name in names]
    unknown = set(names or []) - {e.name for e in entries}
    if unknown:
        raise RigError(f"registry sync: unknown registr{'y' if len(unknown) == 1 else 'ies'}: "
                       f"{', '.join(sorted(unknown))}")
    failed = 0

    def _stale_note(entry: Entry, reg: Registry) -> str:
        """A committed index.json that no longer matches the manifests splits the world: index
        consumers (search, sensor: resolution, upgrade hints) disagree with manifest consumers
        (add, info, upgrade). Surface it at sync, where the operator can actually see it."""
        from .registry import render_index
        path = entry.root / "index.json"
        try:
            if path.is_file() and path.read_text() != render_index(generate_index(reg)):
                return (" — WARNING: index.json is STALE (its CI missed a regen; "
                        "`rig registry index` fixes a local-dir)")
        except (OSError, RigError):
            pass
        return ""

    for entry in chosen:
        if entry.type == "local-dir":
            try:
                reg, index = open_registry(entry)
                mismatch = "" if reg.namespace == entry.name else \
                    f" — WARNING: registry declares namespace '{reg.namespace}' but you consume it " \
                    f"as '{entry.name}'"
                eprint(f"  {entry.name}: local-dir, used in place "
                       f"({len(index.get('packages', {}))} packages){mismatch}"
                       f"{_stale_note(entry, reg)}")
            except RigError as exc:
                failed += 1
                eprint(f"  {entry.name}: DEGRADED — {exc}")
            continue
        dest = cache_dir(entry.name)
        before: dict = {}
        if not (dest / ".git").is_dir():
            dest.parent.mkdir(parents=True, exist_ok=True)
            res = _git("clone", "-q", entry.location, str(dest))
            action = "cloned"
        else:
            if (dest / "index.json").is_file():  # the "before" for the delta digest
                import json
                try:
                    before = json.loads((dest / "index.json").read_text()).get("packages") or {}
                except (OSError, ValueError):
                    pass
            res = _git("pull", "--ff-only", "-q", cwd=dest)
            action = "updated"
        if res.returncode != 0:
            failed += 1
            detail = (res.stderr or res.stdout).strip().splitlines()
            eprint(f"  {entry.name}: sync FAILED — {detail[-1] if detail else 'git error'}")
            if action == "updated":
                eprint(f"      (ff-only: local commits in {dest} diverged? push/PR them, or remove "
                       f"the cache dir and re-sync)")
            continue
        commit = _git("rev-parse", "--short", "HEAD", cwd=dest).stdout.strip()
        try:
            reg, index = open_registry(entry)
            mismatch = "" if reg.namespace == entry.name else \
                f" — WARNING: declares namespace '{reg.namespace}', consumed as '{entry.name}'"
            eprint(f"  {entry.name}: {action} @ {commit} "
                   f"({len(index.get('packages', {}))} packages){mismatch}"
                   f"{_stale_note(entry, reg)}")
            if action == "updated" and before:  # the delta digest: what actually changed
                delta = _index_delta(before, index.get("packages") or {})
                for line in delta[:20]:
                    eprint(f"      {line}")
                if len(delta) > 20:
                    eprint(f"      …and {len(delta) - 20} more")
        except RigError as exc:
            failed += 1
            eprint(f"  {entry.name}: {action} @ {commit} but DEGRADED — {exc}")
    return 1 if failed else 0


def list_registries() -> int:
    entries = load_entries()
    if not entries:
        print("no registries configured (`rig setup` adds the default public one)")
        return 0
    rows = [("#", "NAME", "TYPE", "LOCATION", "STATE", "PACKAGES")]
    for i, entry in enumerate(entries, 1):
        try:
            _, index = open_registry(entry)
            state = "in place" if entry.type == "local-dir" else \
                f"synced @ {_git('rev-parse', '--short', 'HEAD', cwd=entry.root).stdout.strip()}"
            count = str(len(index.get("packages", {})))
        except RigError:
            state, count = ("NOT SYNCED" if entry.type == "git" else "DEGRADED"), "—"
        rows.append((str(i), entry.name, entry.type, entry.location, state, count))
    from .common import print_table
    print_table(rows)
    return 0


# --- rig setup --------------------------------------------------------------------------------

def _rc_targets() -> list[Path]:
    """Every rc file a rig block could live in (used by --purge; --shell writes just the one for
    $SHELL)."""
    home = Path.home()
    return [home / ".zshrc", home / ".bashrc",
            home / ".config" / "fish" / "conf.d" / "rig.fish"]


def _shell_rc() -> Path:
    shell = Path(os.environ.get("SHELL") or "/bin/sh").name
    if shell == "zsh":
        return Path.home() / ".zshrc"
    if shell == "fish":
        return Path.home() / ".config" / "fish" / "conf.d" / "rig.fish"
    return Path.home() / ".bashrc"


def _strip_block(text: str) -> str:
    if _BLOCK_BEGIN not in text:
        return text
    pre, _, rest = text.partition(_BLOCK_BEGIN)
    _, _, post = rest.partition(_BLOCK_END)
    return pre.rstrip("\n") + ("\n" + post.lstrip("\n") if post.strip() else "\n")


def setup(*, shell: bool, no_default_registry: bool, purge: bool, yes: bool) -> int:
    home = rig_home()
    if purge:
        targets = [p for p in _rc_targets() if p.is_file() and _BLOCK_BEGIN in p.read_text()]
        eprint(f"rig setup --purge would remove: {home}"
               + (f" and the rig PATH block in {', '.join(str(t) for t in targets)}" if targets else ""))
        if not yes:
            if not sys.stdin.isatty():
                raise RigError("setup --purge: pass --yes to confirm (non-interactive)")
            if input("proceed? [y/N] ").strip().lower() not in ("y", "yes"):
                eprint("rig setup: aborted")
                return 1
        if home.is_dir():
            shutil.rmtree(home)
        for target in targets:
            target.write_text(_strip_block(target.read_text()))
        eprint("rig setup: user state removed (the rig program itself is your package manager's "
               "to uninstall)")
        return 0

    created = not home.is_dir()
    (home / "cache" / "registries").mkdir(parents=True, exist_ok=True)
    eprint(f"  {home}: {'created' if created else 'exists'}")
    if not registries_file().is_file():
        entries = [] if no_default_registry else \
            [Entry(name=DEFAULT_PUBLIC[0], type="git", location=DEFAULT_PUBLIC[1])]
        save_entries(entries)
        eprint(f"  registries.yaml: created"
               + ("" if no_default_registry else f" with the default '{DEFAULT_PUBLIC[0]}' registry "
                                                 f"({DEFAULT_PUBLIC[1]}) — `rig registry sync` next"))
    else:
        names = ", ".join(e.name for e in load_entries()) or "(empty)"
        eprint(f"  registries.yaml: exists ({names}) — left untouched")

    if shell:
        if shutil.which("rig"):
            eprint("  shell: `rig` already resolves on PATH — no rc block needed")
        else:
            tool_dir = Path(__file__).resolve().parent.parent  # the checkout holding the ./rig shim
            rc = _shell_rc()
            rc.parent.mkdir(parents=True, exist_ok=True)
            line = (f'fish_add_path "{tool_dir}"' if rc.suffix == ".fish"
                    else f'export PATH="{tool_dir}:$PATH"')
            block = f"{_BLOCK_BEGIN}\n{line}\n{_BLOCK_END}\n"
            text = _strip_block(rc.read_text()) if rc.is_file() else ""
            rc.write_text((text.rstrip("\n") + "\n\n" if text.strip() else "") + block)
            eprint(f"  shell: PATH block written to {rc} (restart your shell, or `source {rc}`)")
    return 0
