"""``rig reconstruct`` / ``rig run retrofit`` — a run dir back into a runnable tree (rig-reconstruct
plan; the design settled 2026-08-31).

Every run opened with ``run_capture`` on carries the tree that ran: ``.rig/artifact.tar.gz`` (the
lean bake — surfaces for all rows, configs, bundled rig) + a manifest ``capture:`` stamp
(sha256 = INTEGRITY, never dedup) + ``.rig/images.yaml`` (local-daemon digests: image IDENTITY,
not bytes — the registry stays the byte store, and an arm64 run still needs an arm64 host or a
platform rebuild). ``reconstruct`` extracts it anywhere — a laptop with no deployment, no
registry — verifies the sha, optionally overlays one of the run's own sealed config snapshots,
LOCALIZES (data_dir → a local path via a tree-local vehicle.local.yaml, which outranks the
machine file), and prints the `rig replay` that comes next. ``--registry HOST`` localizes
``images.registry`` the same way (a bench pulling from its own mirror); ``--enable-replay
<path|ref>`` wires the SIL player into a tree that flew without it — OPT-IN, because the output
is otherwise exactly the tree that ran (the player is harness: never in a with-set, absent from
the config-drift report).

Retrofit (runs that predate capture): the canonical spot doubles as the retrofit spot —
``run retrofit`` copies the DEPLOY artifact matching each run manifest's ``artifact:`` tag into
``.rig/artifact.tar.gz`` and stamps ``capture: {sha256, retrofitted: <date>}``. A deploy
artifact is a strict superset of the capture (and digest-pinned). The ``retrofitted:`` marker is
LOAD-BEARING, not just honesty: a retrofit tarball is AS-SHIPPED, not as-opened, so reconstruct
overlays the run's LAST ``ups:`` snapshot BY DEFAULT for retrofitted captures (between-run
config edits are the overlay's whole job); native captures default to as-opened. Either way the
snapshot's own content-addressing is re-verified (the dir name IS the digest of its files)."""
from __future__ import annotations

import datetime
import shutil
import tarfile
import tempfile
from pathlib import Path

from . import RigError
from .common import eprint, load_yaml


def _run_manifest(run_dir: Path) -> dict:
    mpath = run_dir / "manifest.yaml"
    if not mpath.exists():
        raise RigError(f"{run_dir}: no manifest.yaml — not a run dir")
    return load_yaml(mpath)


def _resolve_run(root: Path | None, ref: str) -> Path:
    """A run-dir PATH (the archive/laptop case), or an id under this deployment's registry when
    one is loadable — reconstruct must work with NO deployment handy, so everything here is
    best-effort except the path form."""
    as_path = Path(ref).expanduser()
    if as_path.is_dir():
        return as_path
    if root is not None and (root / "vehicle.yaml").exists():
        try:
            from .manifest import load_manifest
            from .runs import by_label
            manifest = load_manifest(root)
            if manifest.data_dir:
                if (Path(manifest.data_dir) / "runs" / ref).is_dir():
                    return Path(manifest.data_dir) / "runs" / ref
                labeled = by_label(manifest, ref)  # a LABEL resolves to its newest run
                if labeled is not None:
                    return Path(manifest.data_dir) / "runs" / labeled
        except RigError:
            pass
    raise RigError(f"reconstruct: no run dir at '{ref}' (pass a path to the run directory — "
                   f"an id or label resolves only inside a deployment with a run registry)")


def _sha256(path: Path) -> str:
    from .bake import _sha256 as impl  # one hash spelling across bake/capture/reconstruct
    return impl(path)


def _snapshot_digest_ok(snap_dir: Path) -> bool:
    """Re-verify a sealed config snapshot against its own content-address: the dir NAME is
    _config_digest over its files — recompute from disk, same keys (relative posix paths)."""
    from .runs import _config_digest
    files = {p.relative_to(snap_dir).as_posix(): p.read_bytes()
             for p in sorted(snap_dir.rglob("*")) if p.is_file()}
    return bool(files) and _config_digest(files) == snap_dir.name


def _overlay(tree: Path, snap_dir: Path) -> list[str]:
    """Drop a sealed snapshot's config state onto the extracted tree: vehicle.yaml verbatim +
    each rendered instance config over the row's config path (baked trees are profile-stripped
    and the merge is idempotent, so rendered bytes ARE what the launchers received). services.yaml
    is NEVER overlaid — routing belongs to the tarball (a dev-tree snapshot's sibling paths would
    break an extracted tree). Returns the overlaid file list."""
    written: list[str] = []
    snap_veh = snap_dir / "vehicle.yaml"
    if snap_veh.exists():
        shutil.copy2(snap_veh, tree / "vehicle.yaml")
        written.append("vehicle.yaml")
    rows = {}
    try:
        doc = load_yaml(tree / "vehicle.yaml")
        for tier in ("infra", "sensors", "autonomy"):
            for row in doc.get(tier) or []:
                if isinstance(row, dict) and row.get("name") and row.get("config"):
                    rows[str(row["name"])] = str(row["config"])
    except RigError:
        pass
    for rendered in sorted((snap_dir / "rendered").glob("*.yaml")):
        rel = rows.get(rendered.stem, f"config/sensors/{rendered.stem}.yaml")
        dest = tree / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(rendered, dest)
        written.append(rel)
    return written


def _player_rows(tree: Path) -> list[str]:
    """Instance names of the tree's ros2-bag-player rows — a RAW vehicle.yaml read (the tree may
    not load yet, and reconstruct works with no deployment or registry around)."""
    from .replay import PLAYER_SERVICE
    try:
        doc = load_yaml(tree / "vehicle.yaml")
    except RigError:
        return []
    return [str(row.get("name")) for tier in ("infra", "sensors", "autonomy")
            for row in (doc.get(tier) or [])
            if isinstance(row, dict) and row.get("service") == PLAYER_SERVICE]


def _enable_replay_target(token: str) -> tuple[str, Path | str]:
    """--enable-replay's argument, validated BEFORE anything is extracted: ("path", dir) for the
    ros2-bag-player service dir itself or a rig-infra checkout containing it; ("ref", token) for
    a registry ref naming the player. Anything else refuses."""
    from .descriptor import find_descriptor
    from .refs import unqualified
    from .replay import PLAYER_SERVICE
    p = Path(token).expanduser()
    if p.is_dir():
        for cand in (p, p / PLAYER_SERVICE):
            dp = find_descriptor(cand)
            if dp and load_yaml(dp).get("service") == PLAYER_SERVICE:
                return "path", cand.resolve()
        raise RigError(f"reconstruct: --enable-replay {token}: not the {PLAYER_SERVICE} service "
                       f"dir (nor a rig-infra checkout containing one) — its rigging.yaml must "
                       f"declare service: {PLAYER_SERVICE}")
    if unqualified(token).partition(":")[0] != PLAYER_SERVICE:  # `[ns/]name[@ver]` -> name
        raise RigError(f"reconstruct: --enable-replay {token}: neither a directory nor a "
                       f"{PLAYER_SERVICE} registry ref (e.g. public/{PLAYER_SERVICE}@1.10.0)")
    return "ref", token


def _wire_player_path(tree: Path, svc_dir: Path) -> None:
    """Path-routed wiring — init.add_service's belt-and-braces minus its menu-row asymmetry (the
    player row is ALWAYS declared-disabled at order 999): parse the edited files before writing,
    gate on a full catalog + manifest load, restore both files on any failure."""
    import yaml
    from .catalog import load_catalog
    from .descriptor import load_descriptor
    from .init import _append_services_line, _append_tier_row, _repo_examples
    from .manifest import load_manifest
    from .replay import PLAYER_SERVICE
    desc = load_descriptor(PLAYER_SERVICE, svc_dir)
    examples = _repo_examples(svc_dir, desc.examples)
    cfg = tree / "config" / "autonomy" / "bag_player.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    if cfg.exists():
        eprint("  config/autonomy/bag_player.yaml already exists — keeping it")
    elif examples:
        shutil.copy2(examples[0], cfg)
    else:  # under rig the player is env-driven; the standalone knobs are optional
        cfg.write_text(f"service: {PLAYER_SERVICE}\nname: bag_player\n")
    svc_path, veh_path = tree / "services.yaml", tree / "vehicle.yaml"
    svc_line = f"  {PLAYER_SERVICE}: {{ path: {svc_dir} }}"
    row = (f"- {{ name: bag_player, service: {PLAYER_SERVICE}, "
           f"config: config/autonomy/bag_player.yaml, enabled: false, order: 999 }}")
    snippet = (f"    services.yaml, under `services:`:\n    {svc_line}\n"
               f"    vehicle.yaml, under `autonomy:`:\n      {row}")
    orig_svc = svc_path.read_text() if svc_path.exists() else "services:\n"
    orig_veh = veh_path.read_text()
    routes = ((load_yaml(svc_path) or {}).get("services") or {}) if svc_path.exists() else {}
    new_svc = orig_svc if PLAYER_SERVICE in routes else _append_services_line(orig_svc, svc_line)
    new_veh = _append_tier_row(orig_veh, "autonomy", row)
    if new_svc is None or new_veh is None:
        which = "services.yaml" if new_svc is None else "vehicle.yaml"
        eprint(f"rig reconstruct: {which} isn't in the generated block form — config copied, "
               f"files untouched; paste this yourself:\n{snippet}")
        return
    for text, name in ((new_svc, "services.yaml"), (new_veh, "vehicle.yaml")):
        try:
            yaml.safe_load(text)
        except yaml.YAMLError as exc:  # belt: never write a file that will not parse
            raise RigError(f"reconstruct: refusing to write {name} — the edited result would not "
                           f"parse ({exc}); wire it manually:\n{snippet}")
    svc_path.write_text(new_svc)
    veh_path.write_text(new_veh)
    try:  # braces: the real gates (route resolution, name uniqueness, config cross-checks)
        load_catalog(tree)
        load_manifest(tree)
    except Exception as exc:  # noqa: BLE001 — restore, then surface whatever refused
        svc_path.write_text(orig_svc)
        veh_path.write_text(orig_veh)
        raise RigError(f"reconstruct: the wired tree does not load ({exc}) — services.yaml/"
                       f"vehicle.yaml restored; wire it manually:\n{snippet}")


def wire_player(tree: Path, target: tuple[str, Path | str]) -> None:
    """`--enable-replay`: the SIL player into a reconstructed tree that predates it. OPT-IN by
    doctrine — reconstruct's output is otherwise exactly the tree that ran; the player is harness
    (never in a with-set, absent from the config-drift report), so the experiment's isolation
    holds. Registry form = `rig add <ref> --as bag_player` plus the two row values the installer
    cannot know: enabled: false (a plain `up` must never start it) and order: 999 (LAST, forever —
    an autonomy service added later must still land before it)."""
    from .replay import PLAYER_SERVICE
    have = _player_rows(tree)
    if have:
        eprint(f"rig reconstruct: tree already carries a {PLAYER_SERVICE} row "
               f"({', '.join(have)}) — nothing to wire")
        return
    kind, value = target
    if kind == "ref":
        from . import install as install_mod
        install_mod.install(tree, str(value), as_name="bag_player", enabled=False, order=999)
    else:
        _wire_player_path(tree, Path(value))
    if _player_rows(tree):
        eprint(f"rig reconstruct: {PLAYER_SERVICE} wired (bag_player — enabled: false, "
               f"order: 999); harness only: never in a with-set, absent from the config-drift "
               f"report")
    else:
        eprint("rig reconstruct: the player row is NOT wired yet — paste the row printed above")


def cmd_reconstruct(root: Path | None, *, run_ref: str, into: str | None,
                    config: str | None, copy_run: bool = False,
                    no_import: bool = False, registry: str | None = None,
                    enable_replay: str | None = None) -> int:
    run_dir = _resolve_run(root, run_ref)
    doc = _run_manifest(run_dir)
    # Flag validation BEFORE extraction: a refused host or player target must not leave a
    # half-built tree behind (the retry would hit the non-empty --into guard).
    reg_host = None
    if registry:
        from .provision import registry_host
        reg_host = registry_host(registry)
    replay_target = _enable_replay_target(enable_replay) if enable_replay else None
    tarpath = run_dir / ".rig" / "artifact.tar.gz"
    if not tarpath.exists():
        tag = doc.get("artifact")
        hint = (f"its manifest names artifact '{tag}' — copy var/artifacts/{tag}.tar.gz from the "
                f"bake machine to {tarpath} (or `rig run retrofit` inside the deployment)"
                if tag else
                "no artifact tag either (a dev-tree run predating capture) — only the config "
                "layer is recoverable, from .rig/config/")
        raise RigError(f"reconstruct: {run_dir.name} has no .rig/artifact.tar.gz; {hint}")

    capture = doc.get("capture") or {}
    if capture.get("sha256"):
        actual = _sha256(tarpath)
        if actual != capture["sha256"]:
            raise RigError(f"reconstruct: {tarpath} sha256 mismatch (manifest "
                           f"{capture['sha256'][:12]}…, file {actual[:12]}…) — the tarball was "
                           f"altered or truncated in the archive; re-fetch it")
    else:
        eprint("rig reconstruct: no capture stamp in the manifest (hand-retrofitted run?) — "
               "extracting unverified")

    dest = Path(into).expanduser() if into else Path.cwd() / f"{run_dir.name}-tree"
    if dest.exists() and any(dest.iterdir()):
        raise RigError(f"reconstruct: {dest} exists and is not empty — pick --into")
    work = Path(tempfile.mkdtemp(prefix="rig-reconstruct-"))
    try:
        with tarfile.open(tarpath) as tf:
            tf.extractall(work)  # noqa: S202 — rig-authored archives; single top-level dir
        tops = [p for p in work.iterdir() if p.is_dir()]
        if len(tops) != 1:
            raise RigError(f"reconstruct: {tarpath} does not contain a single tree "
                           f"({len(tops)} top-level dirs) — not a rig artifact?")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tops[0]), str(dest))
    finally:
        shutil.rmtree(work, ignore_errors=True)

    # The config state: --config wins; retrofitted captures default to the LAST ups: snapshot
    # (the tarball is as-shipped); native captures default to as-opened (no overlay).
    digest = config
    if digest is None and capture.get("retrofitted"):
        ups = doc.get("ups") or []
        digest = (ups[-1] or {}).get("config") if ups and isinstance(ups[-1], dict) else None
        if digest:
            eprint(f"rig reconstruct: retrofitted capture — overlaying the run's last config "
                   f"snapshot ({digest})")
    if digest:
        snap = run_dir / ".rig" / "config" / digest
        if not snap.is_dir():
            have = sorted(p.name for p in (run_dir / ".rig" / "config").glob("*")
                          if p.is_dir()) if (run_dir / ".rig" / "config").is_dir() else []
            raise RigError(f"reconstruct: no config snapshot '{digest}' in {run_dir.name} "
                           f"(present: {', '.join(have) or 'none'})")
        if not _snapshot_digest_ok(snap):
            raise RigError(f"reconstruct: snapshot {digest} fails its own content-address — "
                           f"corrupted in the archive; re-fetch the run dir")
        for rel in _overlay(dest, snap):
            eprint(f"  overlaid {rel}")

    # Localize: a TREE-local vehicle.local.yaml (outranks the machine file) — the snapshot's
    # local file carries the original identity; data_dir is re-pointed off the vehicle path.
    local: dict = {}
    src_local = (run_dir / ".rig" / "config" / digest / "vehicle.local.yaml") if digest else None
    if src_local and src_local.exists():
        try:
            local = load_yaml(src_local)
        except RigError:
            local = {}
    local["data_dir"] = str((dest / "var" / "data").resolve())
    if reg_host:  # the bench's image mirror — per SUBKEY: the run's tag/base pins still apply,
        #           only the host swaps (machine-wide instead: `rig provision --registry`)
        local["images"] = {**(local.get("images") or {}), "registry": reg_host}
    (dest / "var" / "data").mkdir(parents=True, exist_ok=True)
    import yaml
    (dest / "vehicle.local.yaml").write_text(yaml.safe_dump(local, sort_keys=False))

    # The SIL player: every capture since v0.2.36 carries its declared-disabled row; a tree that
    # FLEW without it says so (`rig replay` would refuse in it) and is wired only on request.
    if replay_target is not None:
        wire_player(dest, replay_target)
    elif not _player_rows(dest):
        from .replay import PLAYER_SERVICE
        eprint(f"rig reconstruct: this tree predates the SIL player (no {PLAYER_SERVICE} row) — "
               f"`rig replay` will refuse in it; re-run with --enable-replay <{PLAYER_SERVICE} "
               f"dir | rig-infra checkout | public/{PLAYER_SERVICE}@1.10.0> to wire it (harness "
               f"only — the tree otherwise stays exactly what ran)")

    # Import the source run into the tree's OWN registry so every verb and TAB completion work
    # by id/label inside the experiment workspace. Default = SYMLINK (a reference — the archive
    # stays the canonical home, and multi-GB bags are never silently duplicated; `run rm` on a
    # linked entry unlinks the link, never the target). --copy-run for a fully portable tree;
    # --no-import to opt out.
    run_arg: str | Path = run_dir.resolve()
    if not no_import:
        reg = dest / "var" / "data" / "runs"
        reg.mkdir(parents=True, exist_ok=True)
        entry = reg / run_dir.name
        if copy_run:
            shutil.copytree(run_dir, entry, symlinks=True)
            eprint(f"rig reconstruct: source run COPIED into the tree's registry ({run_dir.name})")
        else:
            entry.symlink_to(run_dir.resolve())
            eprint(f"rig reconstruct: source run LINKED into the tree's registry "
                   f"({run_dir.name} -> archive; `--copy-run` for a portable copy)")
        run_arg = run_dir.name

    images = run_dir / ".rig" / "images.yaml"
    image_note = " · image digests: .rig/images.yaml (fetch by digest, or run on a matching host)" \
        if images.exists() else ""
    registry_note = f" · images.registry -> {reg_host} (vehicle.local.yaml)" if reg_host else ""
    eprint(f"rig reconstruct: {run_dir.name} -> {dest}{image_note}{registry_note}")
    print(f"cd {dest} && ./rig doctor && ./rig replay {run_arg} <names…>")
    return 0


def cmd_retrofit(root: Path, *, run_refs: list[str], artifact: str | None,
                 from_dir: str | None) -> int:
    """Stamp old runs with the deploy artifact their manifests name. Tag→tarball resolution
    against --from (default: <root>/var/artifacts); --artifact overrides for ALL named runs
    (single-tag campaigns). Refuses tag mismatches; never guesses."""
    import yaml
    src_dir = Path(from_dir).expanduser() if from_dir else root / "var" / "artifacts"
    rc = 0
    for ref in run_refs:
        try:
            run_dir = _resolve_run(root, ref)
            doc = _run_manifest(run_dir)
            if (run_dir / ".rig" / "artifact.tar.gz").exists():
                eprint(f"rig retrofit: {run_dir.name}: already has .rig/artifact.tar.gz — skipped")
                continue
            tag = doc.get("artifact")
            if artifact:
                tarpath = Path(artifact).expanduser()
                if tag and tarpath.name != f"{tag}.tar.gz":
                    raise RigError(f"{run_dir.name} ran artifact '{tag}' but --artifact is "
                                   f"{tarpath.name} — a mismatched tree would assert false "
                                   f"provenance (pass the matching tarball)")
            elif tag:
                tarpath = src_dir / f"{tag}.tar.gz"
            else:
                raise RigError(f"{run_dir.name}: no `artifact:` tag (a dev-tree run) — retrofit "
                               f"needs --artifact with a tree KNOWN unchanged since the run")
            if not tarpath.is_file():
                raise RigError(f"{run_dir.name}: {tarpath} not found — copy it from the bake "
                               f"machine (var/artifacts/) or pass --artifact/--from")
            rig_dir = run_dir / ".rig"
            rig_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(tarpath, rig_dir / "artifact.tar.gz")
            doc["capture"] = {"sha256": _sha256(rig_dir / "artifact.tar.gz"),
                              "rig_version": doc.get("rig_version"),
                              "retrofitted": datetime.date.today().isoformat()}
            (run_dir / "manifest.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
            eprint(f"rig retrofit: {run_dir.name} <- {tarpath.name} "
                   f"(capture stamped, retrofitted)")
        except RigError as exc:
            eprint(f"rig retrofit: {exc}")
            rc = 1
    return rc
