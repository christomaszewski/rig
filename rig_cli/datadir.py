"""The registry's HOME and its bytes: `rig setup --show` (one screen of where everything is),
`rig setup|provision --data-dir NEW --migrate` (move a registry, leave a symlink behind), and
`rig run archive <ids> --to <root>` (move a run's bytes to a drive/NAS, keep a linked registry
entry so every verb and TAB still resolve it — `run rm` on a link unlinks, an unmounted drive
shows as `dangling`).

Recording stays LOCAL (symlinks, hardlinks, atomic renames, disk-speed writes — none of which
SMB/NFS honor); an archive can be anywhere. Copies preserve hardlinks (`rsync -aH` — an export's
hardlinked files would otherwise double) and are verified by file count + bytes before anything
is removed.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from . import RigError, __version__
from .common import eprint, load_yaml


# ---- copying ---------------------------------------------------------------------------------------

def _tree_stats(path: Path) -> tuple[int, int]:
    """(files, bytes) — symlinks counted as files, never followed."""
    files = total = 0
    for dirpath, _, filenames in os.walk(path):
        for name in filenames:
            files += 1
            try:
                total += (Path(dirpath) / name).lstat().st_size
            except OSError:
                pass
    return files, total


def copy_tree(src: Path, dest: Path) -> None:
    """src/ -> dest/ preserving hardlinks (rsync -aH) — a plain copy where rsync is missing
    (hardlinks then become separate files: WARNed). dest may exist (empty or partial: rsync
    completes it)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    rsync = shutil.which("rsync")
    if rsync:
        proc = subprocess.run([rsync, "-aH", str(src) + "/", str(dest) + "/"],
                              capture_output=True, text=True)
        if proc.returncode not in (0, 24):
            err = (proc.stderr or "").strip().splitlines()
            raise RigError(f"copy {src} -> {dest} failed — {err[-1] if err else proc.returncode}")
        return
    eprint("rig: no rsync on this host — copying without hardlink preservation (an export's "
           "hardlinked files become separate copies)")
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest, symlinks=True)


def verify_copy(src: Path, dest: Path) -> None:
    sf, sb = _tree_stats(src)
    df, db = _tree_stats(dest)
    if (sf, sb) != (df, db):
        raise RigError(f"copy verification failed: {src} has {sf} files / {sb} bytes, {dest} has "
                       f"{df} / {db} — nothing removed; re-run to complete the copy")


def _fmt(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ---- migrate ----------------------------------------------------------------------------------------

def migrate_registry(old: Path, new: Path, *, keep_old: bool = False) -> None:
    """Move a registry: copy (hardlinks kept), verify, remove the old tree and leave a SYMLINK at
    the old path (workspace links into it, scripts and containers holding the old path keep
    working), rewrite the catalog root. Refuses with an OPEN run (a recorder may be writing)."""
    old, new = old.expanduser(), new.expanduser()
    if not old.is_dir():
        raise RigError(f"migrate: {old} is not a directory (nothing to move — just set the new dir)")
    if old.resolve() == new.resolve():
        raise RigError(f"migrate: {new} already is the registry")
    if (old / "current").exists() or (old / "current").is_symlink():
        raise RigError(f"migrate: {old} has an OPEN run (`current`) — `rig down --end-run` first")
    if new.exists() and not new.is_dir():
        raise RigError(f"migrate: {new} exists and is not a directory")
    if new.is_dir() and any(new.iterdir()):
        if not (new / "runs").is_dir() or not all(p.name == "runs" for p in new.iterdir()):
            raise RigError(f"migrate: {new} is not empty — pick an empty (or absent) directory")
    if str(new.resolve()).startswith(str(old.resolve()) + "/"):
        raise RigError(f"migrate: {new} is inside {old}")
    files, size = _tree_stats(old)
    eprint(f"rig migrate: {old} -> {new} ({files} file(s), {_fmt(size)})")
    copy_tree(old, new)
    verify_copy(old, new)
    if keep_old:
        eprint(f"rig migrate: copied; {old} kept as-is (--keep-old) — no symlink left behind")
    else:
        shutil.rmtree(old)
        old.symlink_to(new.resolve())
        eprint(f"rig migrate: moved; {old} is now a symlink to {new} (links into the old path "
               f"keep resolving)")
    try:  # the catalog follows (best-effort)
        from . import runcatalog
        rows = runcatalog._read_roots()
        key = str(old.resolve()) if keep_old else None
        changed = False
        for r in rows:
            if str(Path(r["path"]).expanduser()) == str(old) or \
                    (key and str(Path(r["path"]).expanduser().resolve()) == key):
                r["path"] = str(new)
                changed = True
        if changed:
            runcatalog._write_roots(rows)
    except Exception:  # noqa: BLE001
        pass


# ---- archive -----------------------------------------------------------------------------------------

def archive_runs(manifest, run_ids: list[str], to: str, *, link: bool = True,
                 force: bool = False) -> int:
    """`run archive`: move sealed runs' BYTES from this deployment's registry to
    <to>/<vehicle>/<run-id> (a drive, a NAS) and leave a symlinked registry entry (unless
    --no-link) so `rig runs`, replay and TAB still find them; the archive root joins the
    catalog. Sealed runs move freely; interrupted need --force; the OPEN run never."""
    from . import runs as runs_mod
    data = runs_mod._root(manifest)
    registry = (data / "runs").resolve()
    root = Path(to).expanduser()
    if not root.is_absolute():
        raise RigError("run archive: --to must be an absolute path")
    if not root.is_dir():
        raise RigError(f"run archive: {root} is not a directory (mounted?)")
    try:
        open_id = (runs_mod.current_run(data) or (None,))[0]
    except RigError:
        open_id = None
    rc = 0
    moved_bytes = 0
    for rid in run_ids:
        src = data / "runs" / rid
        if "/" in rid or not src.exists():
            host = runs_mod.host_root(manifest)
            if host is not None and "/" not in rid and (host / "runs" / rid).is_dir():
                eprint(f"rig run archive: {rid} lives in the HOST registry ({host / 'runs'}) — "
                       f"archive it from a deployment that uses that registry")
            else:
                eprint(f"rig run archive: no run '{rid}' in this registry (ids only)")
            rc = 1
            continue
        if src.is_symlink():
            eprint(f"rig run archive: {rid} is already a link -> {os.readlink(src)}")
            continue
        if src.resolve().parent != registry:
            eprint(f"rig run archive: {rid} is not inside this registry")
            rc = 1
            continue
        if rid == open_id:
            eprint(f"rig run archive: {rid} is the OPEN run — never moved; `rig down --end-run` first")
            rc = 1
            continue
        try:
            doc = load_yaml(src / "manifest.yaml") if (src / "manifest.yaml").exists() else {}
        except RigError:
            doc = {}
        if not doc.get("ended") and not force:
            eprint(f"rig run archive: {rid} is not sealed (interrupted/corrupt) — --force to move "
                   f"it anyway")
            rc = 1
            continue
        vehicle = str(doc.get("vehicle") or manifest.vehicle or "vehicle")
        dest = root / vehicle / rid
        if dest.exists():
            eprint(f"rig run archive: {dest} already exists — not overwritten")
            rc = 1
            continue
        files, size = _tree_stats(src)
        eprint(f"rig run archive: {rid} -> {dest} ({files} file(s), {_fmt(size)})")
        copy_tree(src, dest)
        verify_copy(src, dest)
        shutil.rmtree(src)
        if link:
            src.symlink_to(dest.resolve())
        moved_bytes += size
        eprint(f"rig run archive: {rid} archived" + (" (linked in the registry)" if link
                                                     else " (entry removed — `rig catalog` still lists it)"))
    if moved_bytes:
        eprint(f"rig run archive: {_fmt(moved_bytes)} moved off the registry")
    try:
        from . import runcatalog
        runcatalog.remember(root, kind="archive")
    except Exception:  # noqa: BLE001
        pass
    return rc


# ---- show ------------------------------------------------------------------------------------------

def _count_runs(data: Path) -> str:
    runs = data / "runs"
    if not data.is_dir():
        return "not created yet (the first up/new-run/import mints it)"
    if not runs.is_dir():
        return "0 runs"
    ids = [d for d in runs.iterdir() if d.is_dir() or d.is_symlink()]
    links = sum(1 for d in ids if d.is_symlink())
    open_ = (data / "current").is_symlink()
    return (f"{len(ids)} run(s)" + (f", 1 OPEN" if open_ else "")
            + (f", {links} linked" if links else ""))


def show(root: Path | None) -> int:
    """`rig setup --show`: rig, the user state, the machine identity, the deployment you stand
    in with its EFFECTIVE data_dir and the file that set it, the host registry it reads through,
    the catalog roots — every "where is my stuff" answer on one screen."""
    from .manifest import machine_file, machine_data_dir
    from .registries import rig_home, registries_file, load_entries
    from .userconfig import user_config_file, user_data_dir
    from . import runcatalog

    print(f"rig {__version__}  ({Path(__file__).resolve().parent.parent})")
    home = rig_home()
    print(f"user state: {home}" + ("" if home.is_dir() else "  (absent — `rig setup`)"))
    if registries_file().is_file():
        try:
            names = ", ".join(e.name for e in load_entries()) or "(none)"
        except Exception:  # noqa: BLE001
            names = "(unreadable)"
        print(f"  package registries: {names}")
    ucfg = user_config_file()
    udd = user_data_dir()
    print(f"  {ucfg.name}: " + (f"data_dir {udd}  — {_count_runs(Path(udd))}" if udd else
                                 ("no data_dir (`rig setup --data-dir <dir>`)" if ucfg.is_file()
                                  else "absent (`rig setup --data-dir <dir>` writes it)")))
    mfile = machine_file()
    if mfile.is_file():
        try:
            mdoc = load_yaml(mfile)
        except RigError:
            mdoc = {}
        ident = f"vehicle '{mdoc.get('vehicle', '?')}' id {mdoc.get('vehicle_id', '?')}"
        extras = [f"platform {mdoc['platform']}" if mdoc.get("platform") else "",
                  f"images.registry {mdoc['images']['registry']}"
                  if (mdoc.get("images") or {}).get("registry") else ""]
        print(f"machine identity: {mfile} — {ident}" + "".join(f", {e}" for e in extras if e))
        mdd = machine_data_dir()
        print(f"  data_dir: " + (f"{mdd}  — {_count_runs(Path(mdd))}" if mdd else "none")
              + ("  (shadowed by the user's)" if mdd and udd and Path(udd) != Path(mdd) else ""))
    else:
        print(f"machine identity: NOT PROVISIONED ({mfile} absent — `sudo rig provision`)")

    if root is not None and (root / "vehicle.yaml").is_file():
        from .manifest import load_manifest
        try:
            m = load_manifest(root)
        except RigError as exc:
            print(f"deployment: {root} — NOT loadable ({exc})")
            m = None
        if m is not None:
            print(f"deployment: {root} — vehicle '{m.vehicle}' id {m.vehicle_id}")
            source = _data_dir_source(root, m.data_dir, udd, machine_data_dir())
            if m.data_dir:
                print(f"  data_dir: {m.data_dir}  (from {source}) — {_count_runs(Path(m.data_dir))}")
            else:
                print("  data_dir: none — no run registry (`rig setup --data-dir <dir>`)")
            if m.host_data_dir:
                print(f"  host registry: {m.host_data_dir}  (read-through) — "
                      f"{_count_runs(Path(m.host_data_dir))}")
    else:
        print("deployment: none here (not inside a rig deployment)")

    roots = runcatalog.roots()
    print(f"catalog roots: {len(roots)}" + ("  (`rig catalog roots`)" if roots else
                                             "  (rig remembers registries it touches)"))
    for path, kind in roots[:8]:
        print(f"  {kind:9} {path}" + ("" if path.is_dir() else "  (missing)"))
    if len(roots) > 8:
        print(f"  … {len(roots) - 8} more")
    return 0


def _data_dir_source(root: Path, effective: str | None, user: str | None, machine: str | None) -> str:
    if not effective:
        return "nowhere"
    local = root / "vehicle.local.yaml"
    if local.is_file():
        try:
            if str((load_yaml(local) or {}).get("data_dir") or "").strip():
                return str(local)
        except RigError:
            pass
    if user and Path(user).expanduser() == Path(effective):
        return "~/.rig/config.yaml"
    if machine and Path(machine).expanduser() == Path(effective):
        from .manifest import machine_file
        return str(machine_file())
    return "vehicle.yaml"
