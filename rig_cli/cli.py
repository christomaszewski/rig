"""The `rig` command line.

Taxonomy (registry plan, settled 2026-08-12): the DEPLOYMENT is the CLI's implicit noun — verbs whose
object is the deployment itself stay top-level (init/add/fetch/up/down/status/logs/pull/doctor/certify);
everything acting on a subordinate noun groups under it (config/run/registry/pkg/overlay/service/
artifact/image). The noun groups are a thin argv translation over the flat command engine below, so
every pre-registry spelling (`new-run`, `bake`, `build`, `rigify`, bare `config`, …) keeps working as a
permanent alias — docs teach the canonical grouped forms."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import (
    RigError, __version__, bake as bake_mod, build as build_mod, certify as certify_mod,
    doctor as doctor_mod, dispatch, fleet as fleet_mod, init as init_mod, install as install_mod,
    overlay as overlay_mod, pkg as pkg_mod, promote as promote_mod, provision as provision_mod,
    registries as registries_mod,
    registry as registry_mod, registry_scaffold, resolve, rigify as rigify_mod,
    runs as runs_mod, status as status_mod,
    vendor as vendor_mod, workingcopy as workingcopy_mod,
)
from .catalog import ServiceEntry, load_catalog
from .common import eprint
from .descriptor import Descriptor, load_descriptor
from .manifest import Manifest, Sensor, load_manifest, stack_summary


def find_root() -> Path:
    """The deployment dir holds vehicle.yaml. Prefer one detected from the cwd (so `cd <deployment> && rig
    up` works with the tool installed separately); else fall back to the dir alongside this CLI (the classic
    single-repo layout where the tool and the deployment share a dir)."""
    cwd = Path.cwd()
    for d in (cwd, *cwd.parents):
        if (d / "vehicle.yaml").exists():
            return d
    return Path(__file__).resolve().parent.parent


def _load(root: Path, *, identity: bool = True,
          render: bool = True) -> tuple[Manifest, dict[str, ServiceEntry], dict[str, Descriptor]]:
    manifest = load_manifest(root)
    # Most of what routes through _load CONSUMES identity (renders configs, names compose
    # projects, exports fleet env) — the mandatory-marker gate lives here, so management verbs
    # that load the manifest directly (pkg list/remove/…) keep working on an unprovisioned box.
    # `identity=False, render=False` is for commands that need only services + descriptors
    # (rig build: images have no vehicle identity).
    if identity:
        from .manifest import require_identity
        require_identity(manifest, what="this command needs a resolved vehicle identity")
    if render:
        manifest = resolve.materialize_manifest(manifest, root)  # profiles/overlays/vars -> rendered
    catalog = load_catalog(root)
    descriptors: dict[str, Descriptor] = {}
    for sensor in manifest.sensors:
        if sensor.service not in catalog:
            raise RigError(f"sensor '{sensor.name}': service '{sensor.service}' not in services.yaml")
        if sensor.service not in descriptors:
            descriptors[sensor.service] = load_descriptor(sensor.service, catalog[sensor.service].path)
    # Legacy platform-in-the-tag conflation: still honored EXACTLY as before (no composition, no
    # RIG_TARGET_PLATFORM), but deprecated — say so on every deployment-scoped command. Data-driven:
    # rig hardcodes no platform names; the in-use riggings' build matrices define them.
    if manifest.platform is None and manifest.image_tag:
        owners = sorted(s for s, d in descriptors.items() if manifest.image_tag in d.build_platforms)
        if owners:
            eprint(f"rig: DEPRECATED — images.tag '{manifest.image_tag}' is a platform name (declared "
                   f"by {', '.join(owners)}); declare `platform: {manifest.image_tag}` and let "
                   f"images.tag carry the version (pulls compose to <tag>-<platform>)")
    return manifest, catalog, descriptors


def _pairs(
    manifest: Manifest, descriptors: dict[str, Descriptor], names: list[str], *, reverse: bool = False
) -> list[tuple[Sensor, Descriptor]]:
    sensors = manifest.select(names, enabled_only=True)
    if reverse:
        sensors = list(reversed(sensors))
    return [(s, descriptors[s.service]) for s in sensors]


def _summarize(outcomes: list[dispatch.Outcome]) -> int:
    failed = [o for o in outcomes if o.returncode != 0]
    if failed:
        eprint(f"rig: {len(failed)}/{len(outcomes)} failed: {', '.join(o.sensor.name for o in failed)}")
        return 1
    return 0


# --- command handlers -------------------------------------------------------

def cmd_up(args, manifest, catalog, descriptors) -> int:
    blocking = [i for i in doctor_mod.collect(manifest, catalog, descriptors) if i.level == doctor_mod.ERROR]
    if blocking and not args.force:
        eprint("rig: preflight failed (pass --force to override):")
        for issue in blocking:
            eprint(f"  [✗] {issue.message}")
        return 1
    env = dispatch.fleet_env(manifest, descriptors)
    if args.target_state:  # --standby/--active: the up-dispatch-only posture token (fleet_env pops
        env["RIG_TARGET_STATE"] = args.target_state  # it on every other verb; launchers honor it at
        #                                              `up` only, over the config's initial_state)
    if args.run and not manifest.data_dir:  # an EXPLICIT --run must never be silently dropped
        raise RigError("up --run needs `data_dir` in vehicle.yaml (the run registry lives under it)")
    pairs = _pairs(manifest, descriptors, args.names)  # ascending order: producers before consumers
    if not pairs:
        eprint("rig: no enabled stacks to bring up")
        return 0
    if not args.dry_run and manifest.data_dir:  # runs: --run names/rotates; bare up only ENSURES
        if args.run:
            runs_mod.up_run(manifest, args.rig_root, args.run, force=args.force)
        else:
            runs_mod.ensure(manifest, args.rig_root)
        # every up logs the effective config against the run (dedup'd; fail-soft — never blocks up)
        runs_mod.snapshot(manifest, args.rig_root, stacks=[s.name for s, _ in pairs])
    eprint(f"rig up: {manifest.vehicle} — {stack_summary([p[0] for p in pairs])}")
    return _summarize(dispatch.run_verb(pairs, env, "up", dry_run=args.dry_run))


def cmd_down(args, manifest, catalog, descriptors) -> int:
    env = dispatch.fleet_env(manifest, descriptors)
    pairs = _pairs(manifest, descriptors, args.names, reverse=True)  # reverse: consumers before producers
    if not pairs:
        eprint("rig: no enabled stacks to tear down")
        return 0
    eprint(f"rig down: {manifest.vehicle} — {stack_summary([p[0] for p in pairs])}")
    if args.end_run and not args.dry_run and manifest.data_dir:
        # BEFORE the verb: `compose down` removes the containers — their docker logs go with them
        runs_mod.capture_docker_logs(manifest)
    rc = _summarize(dispatch.run_verb(pairs, env, "down", dry_run=args.dry_run))
    if args.purge:
        eprint("rig: purging external volumes (final teardown)")
        for sensor, desc in pairs:
            dispatch.purge_external_volumes(sensor, desc, dry_run=args.dry_run)
    if args.end_run and not args.dry_run:
        if rc != 0:
            eprint("rig: down failed — leaving the run open (cannot seal with stacks possibly live)")
            return rc
        # end_run's own guard re-checks: a PARTIAL down leaves other stacks running -> it refuses.
        runs_mod.end_run(manifest, args.rig_root)
    return rc


def cmd_config(args, manifest, catalog, descriptors) -> int:
    env = dispatch.fleet_env(manifest, descriptors)
    pairs = _pairs(manifest, descriptors, args.names)
    return _summarize(dispatch.run_verb(pairs, env, "config", dry_run=args.dry_run))


def _cmd_state_transition(args, manifest, descriptors, verb: str) -> int:
    """`rig standby` / `rig activate`: fan the operational-state verb out over the stacks that
    DECLARE the trio (rigging `verbs:` — the declaration is the support claim; `verb_args`'s
    bare-token fallback would otherwise hand `standby` to a compose-forwarding launcher as a
    compose subcommand). Undeclared = always active, skipped with a note — "everything that can
    park, parks" is the deployment semantics, so a skip is success, not an error. Ordering mirrors
    up/down: activate producers-first, standby consumers-first. rig adds NO timeouts — launchers
    own their transition budgets (activate can be O(minute+) per device: mode restore + spin-up)
    and exit nonzero on their own deadlines, exactly like `up`."""
    env = dispatch.fleet_env(manifest, descriptors)
    pairs = _pairs(manifest, descriptors, args.names, reverse=(verb == "standby"))
    if not pairs:
        eprint(f"rig: no enabled stacks to {verb}")
        return 0
    partial = sorted({d.service for _, d in pairs if d.declared_state_verbs and not d.supports_states})
    if partial:
        eprint(f"rig: partial operational-state declaration (all three or none — standby/activate/"
               f"state), skipping: {', '.join(partial)}")
    declared = [(s, d) for s, d in pairs if d.supports_states]
    skipped = [s.name for s, d in pairs if not d.declared_state_verbs]
    if skipped:
        eprint(f"rig: no state verbs (always active), skipping: {', '.join(skipped)}")
    if not declared:
        eprint(f"rig {verb}: nothing to do — no selected stack declares the operational-state verbs")
        return 0
    eprint(f"rig {verb}: {manifest.vehicle} — {stack_summary([p[0] for p in declared])}")
    return _summarize(dispatch.run_verb(declared, env, verb, dry_run=args.dry_run))


def cmd_standby(args, manifest, catalog, descriptors) -> int:
    return _cmd_state_transition(args, manifest, descriptors, "standby")


def cmd_activate(args, manifest, catalog, descriptors) -> int:
    return _cmd_state_transition(args, manifest, descriptors, "activate")


def cmd_pull(args, manifest, catalog, descriptors) -> int:
    """Pre-pull every stack's images — no containers created/restarted. The field-ops primer: pull while
    the registry is reachable, then `up` (now or later) starts from the local cache."""
    env = dispatch.fleet_env(manifest, descriptors)
    pairs = _pairs(manifest, descriptors, args.names)
    if not pairs:
        eprint("rig: no enabled stacks to pull for")
        return 0
    eprint(f"rig pull: {manifest.vehicle} — {stack_summary([p[0] for p in pairs])}")
    return _summarize(dispatch.run_verb(pairs, env, "pull", dry_run=args.dry_run))


def cmd_status(args, manifest, catalog, descriptors) -> int:
    env = dispatch.fleet_env(manifest, descriptors)
    pairs = _pairs(manifest, descriptors, args.names)
    rows = status_mod.gather(pairs, env)
    run_line = runs_mod.status_line(manifest)
    if getattr(args, "format", "table") == "json":  # the machine contract fleet tooling parses
        print(status_mod.as_json(manifest, rows, run_line))
        return 0
    if run_line is not None:
        print(run_line)
    print(status_mod.render(rows, verbose=args.verbose))  # stdout: the report
    return 0


def cmd_new_run(args, manifest, catalog, descriptors) -> int:
    runs_mod.new_run(manifest, args.rig_root, args.label, force=args.force)
    return 0


def cmd_end_run(args, manifest, catalog, descriptors) -> int:
    # Snapshot the fleet state into the manifest as part of sealing (best-effort).
    env = dispatch.fleet_env(manifest, descriptors)
    rows = status_mod.gather(_pairs(manifest, descriptors, []), env)
    runs_mod.end_run(manifest, args.rig_root, force=args.force, status_text=status_mod.render(rows))
    return 0


def cmd_run_rm(args, manifest, catalog, descriptors) -> int:
    return runs_mod.remove_runs(manifest, args.runs, force=args.force)


def cmd_run_import(args, manifest, catalog, descriptors) -> int:
    return runs_mod.import_runs(manifest, args.paths, move=args.move)


def cmd_runs(args, manifest, catalog, descriptors) -> int:
    rows = runs_mod.list_runs(manifest)
    if not rows:
        print("no runs recorded")
        return 0
    def _size(kb):
        if kb is None:
            return "—"
        if kb < 1024:
            return f"{kb}K"
        return f"{kb / 1024:.0f}M" if kb < 1024 * 1024 else f"{kb / (1024 * 1024):.1f}G"

    replayed = any(r.replay_of for r in rows)  # the column appears only when a replay run exists
    headers = ("RUN", "LABEL", "STATE", "STARTED", "ENDED", "SIZE") \
        + (("REPLAY-OF",) if replayed else ())
    table = [headers] + [
        (r.run, r.label, r.state + (" (link)" if r.linked and r.state != "dangling" else ""),
         r.started, r.ended, _size(r.disk_kb))
        + ((r.replay_of or "—",) if replayed else ())
        for r in rows
    ]
    widths = [max(len(row[i]) for row in table) for i in range(len(headers))]
    for row in table:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
    return 0


def cmd_replay(args, manifest, catalog, descriptors) -> int:
    from . import replay as replay_mod
    return replay_mod.cmd(manifest, catalog, descriptors, args.rig_root, run_ref=args.run,
                          names=args.names, label=args.label, wall_clock=args.wall_clock,
                          force=args.force, dry_run=args.dry_run, calls=args.calls,
                          export_calls=args.export_calls)


def cmd_graph(args, manifest, catalog, descriptors) -> int:
    from . import graph as graph_mod
    return graph_mod.cmd(manifest, descriptors, run_ref=args.run, do_check=args.check,
                         contract_instance=args.contract, out=args.out)


def cmd_logs(args, manifest, catalog, descriptors) -> int:
    env = dispatch.fleet_env(manifest, descriptors)
    pairs = _pairs(manifest, descriptors, args.names)
    extra: list[str] = []
    if args.follow:
        if len(pairs) != 1:
            raise RigError("`logs -f` follows a single sensor; name exactly one")
        extra.append("-f")
    if args.tail is not None:
        extra += ["--tail", str(args.tail)]
    return _summarize(dispatch.run_verb(pairs, env, "logs", extra=extra))


def cmd_doctor(args, manifest, catalog, descriptors) -> int:
    return doctor_mod.run(manifest, catalog, descriptors, deep=args.deep)


def cmd_image_audit(args, manifest, catalog, descriptors) -> int:
    from . import audit as audit_mod
    env = dispatch.fleet_env(manifest, descriptors)
    return audit_mod.audit(manifest, descriptors, env, names=args.names)


def cmd_cleanup(args, manifest, catalog, descriptors) -> int:
    """Decommission sweep: this deployment's docker images (+ volumes) off the host. Containers
    must already be gone (`rig down`) — cleanup refuses otherwise, it never kills anything."""
    from . import cleanup as cleanup_mod
    env = dispatch.fleet_env(manifest, descriptors)
    return cleanup_mod.cleanup(args.rig_root, manifest, descriptors, env, names=args.names,
                               dry_run=args.dry_run, keep_volumes=args.keep_volumes)


def cmd_certify(args, root: Path) -> int:
    """Launcher-contract conformance. Three modes: --diff compares two --emit files; --repo certifies a
    service repo directly (its CI — no deployment tree needed); default certifies the manifest's entries."""
    import os

    if args.diff:
        return certify_mod.diff_emits(Path(args.diff[0]), Path(args.diff[1]))
    emit = Path(args.emit) if args.emit else None
    if args.repo:
        repo = Path(args.repo).resolve()
        from .descriptor import find_descriptor
        path = find_descriptor(repo)
        if path is None:
            raise RigError(f"certify: no rigging.yaml in {repo}")
        from .common import load_yaml
        service = load_yaml(path).get("service") or repo.name
        desc = load_descriptor(service, repo)
        if args.config:
            config = Path(args.config).resolve()
        elif desc.examples:  # the declared example is the natural default config to certify with
            config = (repo / desc.examples[0]).resolve()
            if not config.is_file():
                raise RigError(f"certify: rigging.yaml declares example '{desc.examples[0]}' but it "
                               f"doesn't exist — fix the declaration or pass --config")
            eprint(f"rig certify: using the declared example {desc.examples[0]}")
        else:
            raise RigError("certify --repo needs --config <file>, or an `examples:` list in rigging.yaml "
                           "(an example/instance config to drive the launcher with)")
        targets = [(service, desc, config)]
    else:
        manifest, catalog, descriptors = _load(root)
        sensors = manifest.select(args.names, enabled_only=True)
        if not sensors:
            eprint("rig certify: no enabled stacks to certify")
            return 0
        targets = [(f"{s.service}/{s.name}", descriptors[s.service], s.config) for s in sensors]
    return certify_mod.run_targets(targets, dict(os.environ), emit=emit)


def cmd_vendor(args, root: Path) -> int:
    """Standalone (no manifest load): copy a service's launch surface into services/<service>/."""
    if args.source:
        source = Path(args.source)
    else:
        entry = load_catalog(root).get(args.service)
        if entry is None:
            raise RigError(f"vendor {args.service}: pass --from <path>, or add it to services.yaml")
        source = entry.path
    vendor_mod.vendor(args.service, source, root)
    return 0


def cmd_init(args) -> int:
    discover = None
    if args.discover is not None:  # flag given; "" = no dir supplied -> scan the target's parent
        discover = Path(args.discover).resolve() if args.discover else Path(args.target).resolve().parent
    init_mod.init(Path(args.target), vehicle_id=args.vehicle_id, infra=args.infra or [],
                  discover=discover, no_git=args.no_git)
    return 0


def route_add(root: Path, token: str, *, as_name: str | None = None, tier: str | None = None,
              locked: bool = False) -> int:
    """ONE add grammar under both spellings (`rig add` = `rig pkg add`): local path | bare
    workspace name | registry ref (`public/zenoh-router`) | `sensor:<id>` | `<service>:<profile>`.
    An existing directory reads as a path; registry grammar reads as a ref; BOTH live at once is a
    hard error (the dependency-confusion posture) — `./` forces the directory, `@<version>` the
    package. `--locked` implies the registry reading (it reproduces registry pins)."""
    if token.startswith("sensor:"):
        return install_mod.install(root, token, as_name=as_name, locked=locked)
    if locked:  # registry semantics by definition — a path can't be --locked
        if Path(token).expanduser().exists():
            raise RigError(f"add: --locked applies to registry installs — '{token}' is a local "
                           f"path (paths are wired, not locked)")
        return install_mod.install(root, token, as_name=as_name, locked=True)
    if ":" in token and "/" not in token and not Path(token).expanduser().exists():
        return install_mod.install(root, token, as_name=as_name)  # <service>:<profile>
    if "/" in token:
        ns = token.split("/", 1)[0]
        ns_known = any(e.name == ns for e in registries_mod.load_entries())
        path_live = Path(token).expanduser().exists()
        if ns_known and path_live:
            raise RigError(f"add: '{token}' is ambiguous — the directory exists AND '{ns}' is a "
                           f"configured registry; use ./{token} for the directory, or "
                           f"{token}@<version> for the registry package")
        if ns_known:
            return install_mod.install(root, token, as_name=as_name)
    try:
        return init_mod.add_service(root, token, tier=tier)
    except RigError as exc:
        if "unknown service" not in str(exc) or "/" in token:
            raise
        try:  # bare name, not in the workspace — fall back to unqualified registry resolution
            return install_mod.install(root, token, as_name=as_name)
        except RigError as reg_exc:
            raise RigError(f"{exc}\n  (registry fallback: {reg_exc})")


def cmd_add(args, root: Path) -> int:
    return route_add(root, args.service, as_name=args.as_name, tier=args.tier,
                     locked=getattr(args, "locked", False))


def cmd_rigify(args) -> int:
    return rigify_mod.rigify(Path(args.directory), service=args.service, tier=args.tier)


def cmd_fetch(args, root: Path) -> int:
    return init_mod.fetch(root)


def cmd_config_render(args, manifest, catalog, descriptors) -> int:
    """`rig config render` — run the config pipeline (profile/overrides materialization happened at
    load) and print each instance's effective config path. Layer attribution (`config diff`) lands
    with the working-copy pipeline."""
    for sensor in manifest.select(args.names, enabled_only=False):
        print(f"{sensor.name}: {sensor.config}")
    return 0


def cmd_artifact_list(args, root: Path) -> int:
    """`rig artifact list` — the baked artifacts under var/artifacts with their provenance."""
    import tarfile

    import yaml as _yaml

    artifacts = sorted((root / "var" / "artifacts").glob("*.tar.gz"))
    if not artifacts:
        print("no artifacts baked (var/artifacts is empty)")
        return 0
    rows = [("TAG", "VEHICLE", "CREATED", "PINNING", "PARENT", "SIZE")]
    for path in artifacts:
        tag = path.name[:-len(".tar.gz")]
        meta = {}
        try:
            with tarfile.open(path) as tf:
                member = tf.extractfile(f"{tag}/metadata.yaml")
                meta = _yaml.safe_load(member.read()) if member else {}
        except (tarfile.TarError, KeyError, OSError, _yaml.YAMLError):
            pass
        size_mb = path.stat().st_size / 1e6
        rows.append((tag, str(meta.get("vehicle", "?")), str(meta.get("created", "?")),
                     str(meta.get("pinning", "?")), str((meta.get("parent") or {}).get("tag", "—")),
                     f"{size_mb:,.0f}M" if size_mb >= 1 else f"{path.stat().st_size / 1e3:.0f}K"))
    from .common import print_table
    print_table(rows)
    return 0


def _optional_root(args) -> Path | None:
    """The cwd deployment when there is one — for verbs whose deployment half is optional
    (yank/discard fix-ups run only when a deployment is actually present)."""
    try:
        root = (args.root or find_root()).resolve()
        return root if (root / "vehicle.yaml").exists() else None
    except RigError:
        return None


def cmd_registry(args) -> int:
    """Registry verbs — authoring (init/validate/index, on a registry tree) and client management
    (add/remove/list/sync, on ~/.rig). All deployment-independent."""
    if args.registry_cmd == "init":
        return registry_scaffold.registry_init(Path(args.directory), namespace=args.namespace)
    if args.registry_cmd == "add":
        registries_mod.add_entry(args.name, url=args.url, path=args.path, front=args.front)
        return 0
    if args.registry_cmd == "remove":
        registries_mod.remove_entry(args.name)
        return 0
    if args.registry_cmd == "list":
        return registries_mod.list_registries()
    if args.registry_cmd == "sync":
        return registries_mod.sync(args.names or None)
    if args.registry_cmd in ("pending", "push", "discard"):
        from . import publish as publish_mod
        if args.registry_cmd == "pending":
            return publish_mod.pending(args.name)
        if args.registry_cmd == "push":
            return publish_mod.push(args.name, args.branches, all_pending=args.all_pending,
                                    pr=args.pr)
        return publish_mod.discard(args.name, args.branches, all_pending=args.all_pending,
                                   root=_optional_root(args))
    root = Path(args.directory).resolve()
    if args.registry_cmd == "validate":
        return registry_mod.cli_validate(root)
    return registry_mod.cli_index(root)


def cmd_pkg(args) -> int:
    if args.pkg_cmd == "search":
        return pkg_mod.search(args.query, kind=args.kind, registry=args.registry)
    try:  # inside a deployment, info also reports the local install state — outside, it's silent
        root = (args.root or find_root()).resolve()
        root = root if (root / "vehicle.yaml").exists() else None
    except RigError:
        root = None
    return pkg_mod.info(args.ref, root=root, versions=args.versions)


def cmd_build(args, root: Path) -> int:
    # Building images consumes NO vehicle identity (service list + descriptors + registry/tag +
    # ROS_DISTRO) and never renders configs — a fleet deployment builds on any box.
    manifest, catalog, descriptors = _load(root, identity=False, render=False)
    if any(u.startswith("images.") for u in manifest.missing_identity):
        eprint("rig build: note — images.registry/tag are per-vehicle and unresolved here; "
               "building without a push target unless --registry/--tag are passed")
    return build_mod.build(manifest, descriptors, registry=args.registry, tag=args.tag,
                           dry_run=args.dry_run, jobs=args.jobs, root=root,
                           platform=args.platform, no_cache=args.no_cache,
                           base_image=args.base_image)


def cmd_bake(args, root: Path) -> int:
    # Fleet-ness is a property of the deployment, not a flag: {{var}} references anywhere mean
    # bake stages the UNRESOLVED tree (a mandatory-marker deployment can't even load resolved
    # here — bake never reads vehicle.local.yaml/shell vars by design).
    if bake_mod.is_fleet(root):
        bake_mod.bake_fleet(root, args.tag, registry=args.registry,
                            bundle_images=args.bundle_images)
        return 0
    manifest, catalog, descriptors = _load(root)
    env = dispatch.fleet_env(manifest, descriptors)
    bake_mod.bake(root, manifest, catalog, descriptors, env, args.tag, registry=args.registry,
                  bundle_images=args.bundle_images)
    return 0


def cmd_unbake(args, root: Path) -> int:
    artifact = Path(args.artifact)
    into = Path(args.into) if args.into else (root / "var" / "unbaked" / artifact.name.split(".tar")[0])
    bake_mod.unbake(artifact, into)
    return 0


_HANDLERS = {
    "up": cmd_up,
    "down": cmd_down,
    "standby": cmd_standby,
    "activate": cmd_activate,
    "cleanup": cmd_cleanup,
    "config": cmd_config,
    "pull": cmd_pull,
    "status": cmd_status,
    "logs": cmd_logs,
    "doctor": cmd_doctor,
    "image-audit": cmd_image_audit,
    "new-run": cmd_new_run,
    "end-run": cmd_end_run,
    "runs": cmd_runs,
    "run-rm": cmd_run_rm,
    "run-import": cmd_run_import,
    "graph": cmd_graph,
    "replay": cmd_replay,
    "config-render": cmd_config_render,
}


# --- noun-group translation over the flat engine ----------------------------
# (group, verb) -> the flat command it runs. Groups that are real subparsers (registry, and pkg
# when it lands) are NOT here — argparse owns them directly.
_GROUP_VERBS: dict[str, dict[str, str]] = {
    "config": {"show": "config", "render": "config-render", "diff": "config-diff"},
    "run": {"new": "new-run", "end": "end-run", "list": "runs", "retrofit": "run-retrofit",
            "rm": "run-rm", "import": "run-import"},
    "artifact": {"bake": "bake", "unbake": "unbake", "list": "artifact-list"},
    "image": {"build": "build", "pull": "pull", "audit": "image-audit"},
    "service": {"rigify": "rigify", "vendor": "vendor", "certify": "certify"},
}
# Not-yet-implemented grouped verbs get a pointed error instead of an "unknown sensor" mystery.
_GROUP_PENDING: dict[tuple[str, str], str] = {}


def translate_argv(argv: list[str]) -> list[str] | None:
    """Rewrite a canonical grouped spelling (`rig run list`, `rig artifact bake`) onto the flat
    engine. Global flags before the command (--root X) are preserved. Returns None after printing a
    synthesized group help (`rig run`, `rig run --help`) — the caller exits 0. Bare `config` is NOT
    a group here: it stays the legacy alias for `config show`."""
    i = 0
    while i < len(argv):  # skip global flags to find the command token
        tok = argv[i]
        if tok in ("-h", "--help", "--version") or not tok.startswith("-"):
            break
        i += 2 if tok == "--root" else 1  # --root takes a value; --root=X is one token
    if i >= len(argv):
        return argv
    noun = argv[i]
    verbs = _GROUP_VERBS.get(noun)
    if verbs is None:
        return argv
    rest = argv[i + 1:]
    if noun != "config" and (not rest or rest[0] in ("-h", "--help")):
        print(f"usage: rig {noun} <verb> …\nverbs: {', '.join(sorted(verbs))}")
        return None
    if not rest:
        return argv  # bare `config` = legacy show-all
    if (noun, rest[0]) in _GROUP_PENDING:
        raise RigError(f"{noun} {rest[0]}: not yet — {_GROUP_PENDING[(noun, rest[0])]}")
    if rest[0] in verbs:
        return argv[:i] + [verbs[rest[0]]] + rest[1:]
    if noun == "config":
        return argv  # `rig config <sensor…>` — legacy positional names
    raise RigError(f"rig {noun}: unknown verb '{rest[0]}' (expected: {', '.join(sorted(verbs))})")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rig", description="vehicle-level stack orchestrator (infra · sensors · autonomy)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="noun groups (canonical forms; the flat spellings above stay as permanent aliases):\n"
               "  rig config   show | render          rig run      new | end | list | rm | "
               "import | retrofit\n"
               "  rig registry init | add | remove | list | sync | validate | index\n"
               "  rig pkg      search | info | list | outdated | add | remove | upgrade | lock | "
               "save | promote | repin | rebase | yank\n"
               "  rig overlay  apply | remove | reorder | list     rig setup (first-run host setup)\n"
               "  rig completion bash|zsh (TAB completion — deb/brew ship it; `rig setup --shell` wires it)\n"
               "  rig service  rigify | vendor | certify\n"
               "  rig artifact bake | unbake | list   rig image    build | pull | audit\n"
               "  rig fleet    list | status | sync | up | down    (GCS-side fan-out; fleet.yaml)")
    parser.add_argument("--version", action="version", version=f"rig {__version__}")
    parser.add_argument("--root", type=Path, default=None,
                        help="deployment root holding vehicle.yaml (default: detected from the cwd, "
                             "else alongside the CLI)")
    sub = parser.add_subparsers(dest="cmd", required=True, metavar="<command>")

    def add(name, help_text):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("names", nargs="*", help="sensor name(s); default: all enabled")
        return p

    up = add("up", "bring sensors up (producers first)")
    up.add_argument("--dry-run", action="store_true", help="print the exact launcher invocations only")
    up.add_argument("--force", action="store_true", help="bring up even if preflight reports errors")
    up.add_argument("--run", default=None, metavar="LABEL",
                    help="join the open run with this label, or rotate to a new one (bare up never rotates)")
    posture = up.add_mutually_exclusive_group()
    posture.add_argument("--standby", action="store_const", const="standby", dest="target_state",
                         help="come up parked: export RIG_TARGET_STATE=standby (overrides each "
                              "config's initial_state; declared-state services only)")
    posture.add_argument("--active", action="store_const", const="active", dest="target_state",
                         help="come up running: export RIG_TARGET_STATE=active (overrides "
                              "initial_state: standby configs)")

    sb = add("standby", "park declared stacks (reverse order): ready but quiet — lifecycle idle, "
                        "devices in low-power mode; health is unaffected")
    sb.add_argument("--dry-run", action="store_true", help="print the exact launcher invocations only")

    ac = add("activate", "wake declared stacks (producers first): devices to normal, running")
    ac.add_argument("--dry-run", action="store_true", help="print the exact launcher invocations only")

    down = add("down", "tear sensors down (reverse order)")
    down.add_argument("--dry-run", action="store_true")
    down.add_argument("--purge", action="store_true", help="also remove declared external volumes (FINAL teardown)")
    down.add_argument("--end-run", action="store_true", dest="end_run",
                      help="capture every container's docker logs into the run (.rig/logs/), then "
                           "after a successful FULL down, seal it (stamps ended: + size)")

    cl = sub.add_parser("cleanup", help="decommission: remove this deployment's docker images + "
                                        "volumes from the host (after the final `rig down`, "
                                        "before deleting the tree)")
    cl.add_argument("names", nargs="*",
                    help="instance name(s); default: ALL, disabled included — decommission "
                         "covers the whole tree")
    cl.add_argument("--dry-run", action="store_true",
                    help="print every ref/volume that would be removed; touches nothing")
    cl.add_argument("--keep-volumes", action="store_true", dest="keep_volumes",
                    help="images only — leave declared external volumes and project volume residue")

    nr = sub.add_parser("new-run", help="rotate: seal the open run (if any) and open a new one")
    nr.add_argument("label", nargs="?", default=None, help="session label (dir becomes <stamp>_<label>)")
    nr.add_argument("--force", action="store_true",
                    help="rotate even while stacks run (late writes land in the sealed run)")
    nr.add_argument("names", nargs="*", default=[], help=argparse.SUPPRESS)

    er = sub.add_parser("end-run", help="seal the open run: stamp ended + status table, remove `current`")
    er.add_argument("--force", action="store_true", help="seal even while stacks run")
    er.add_argument("names", nargs="*", default=[], help=argparse.SUPPRESS)

    rn = sub.add_parser("runs", help="list the run registry (OPEN / sealed / interrupted)")
    rn.add_argument("names", nargs="*", default=[], help=argparse.SUPPRESS)

    rp = sub.add_parser("replay", help="SIL: play a sealed run's recorded topics back through "
                                       "the named instances (needs the ros2-bag-player row, "
                                       "rig-infra ≥ v1.8.0; selection from the run's graph epochs)")
    rp.add_argument("run", help="SOURCE run id or run-dir path")
    rp.add_argument("names", nargs="*",
                    help="instance(s) under test — brought up live, their recorded inputs played")
    rp.add_argument("--label", default=None,
                    help="label for the NEW replay run (default: replay-<source-run>)")
    rp.add_argument("--export-calls", action="store_true", dest="export_calls",
                    help="print the source run's recorded service calls as a schema-1 call "
                         "script on stdout (redirect to a file, edit, replay with --calls). "
                         "No session: one one-shot player container; takes no instance names")
    rp.add_argument("--calls", default=None, metavar="FILE",
                    help="call-script YAML (schema 1): inject/retime service calls on the sim "
                         "clock — SUPPRESSES verbatim service replay (script XOR verbatim); "
                         "bootstrap one with --export-calls")
    rp.add_argument("--wall-clock", action="store_true", dest="wall_clock",
                    help="no /clock, no use_sim_time (default: RIG_SIM_TIME=1 — player publishes "
                         "/clock, adopted services pace to it)")
    rp.add_argument("--force", action="store_true",
                    help="proceed past preflight errors and the clean-host guard")
    rp.add_argument("--dry-run", action="store_true",
                    help="print selection + the exact launcher invocations; open no run")

    gr = sub.add_parser("graph", help="observed pub/sub/service topology from a run's graph "
                                      "epochs (the bag-logger's graph-snapshotter sidecar, "
                                      "rig-infra ≥ v1.7.0)")
    gr.add_argument("run", nargs="?", default=None,
                    help="run id or run-dir path (default: the open run, else the newest)")
    gr.add_argument("--check", action="store_true",
                    help="compare observed vs the riggings' declared interface: blocks (WARN-only)")
    gr.add_argument("--contract", metavar="INSTANCE", default=None,
                    help="print an interface: scaffold from the instance's observed edges "
                         "(paste into the service repo's rigging.yaml)")
    gr.add_argument("-o", "--out", metavar="FILE", default=None,
                    help="write the materialized union (epoch-shaped YAML) instead of the report")
    gr.add_argument("names", nargs="*", default=[], help=argparse.SUPPRESS)

    rec = sub.add_parser("reconstruct", help="a run dir back into a runnable tree — extract its "
                                             "captured artifact (verified), overlay a config "
                                             "snapshot, localize; works with no deployment handy")
    rec.add_argument("run", help="run dir path (an id resolves only inside a deployment)")
    rec.add_argument("--into", default=None, metavar="DIR",
                     help="destination tree (default: ./<run-id>-tree)")
    rec.add_argument("--config", default=None, metavar="DIGEST12",
                     help="overlay this config snapshot from the run's .rig/config/ (default: "
                          "as-opened for native captures, the LAST ups snapshot for retrofits)")
    rec.add_argument("--copy-run", action="store_true", dest="copy_run",
                     help="COPY the source run into the tree's registry instead of the default "
                          "symlink (portable tree; duplicates the bags)")
    rec.add_argument("--no-import", action="store_true", dest="no_import",
                     help="don't put the source run in the tree's registry at all")

    rrm = sub.add_parser("run-rm", help="remove run(s) from the registry by id (canonical: "
                                        "run rm) — sealed freely, interrupted with --force, "
                                        "the OPEN run never")
    rrm.add_argument("runs", nargs="+", help="run id(s) — see `rig runs`")
    rrm.add_argument("--force", action="store_true",
                     help="also remove unsealed (interrupted/corrupt) runs")

    rim = sub.add_parser("run-import", help="adopt run dir(s) into this registry (canonical: "
                                            "run import) — copied by default, so id-based verbs "
                                            "and completion cover archived runs")
    rim.add_argument("paths", nargs="+", help="path(s) to run dirs (scp'd/downloaded)")
    rim.add_argument("--move", action="store_true",
                     help="move instead of copy (same-disk adoption)")

    rf = sub.add_parser("run-retrofit", help="stamp pre-capture runs with the deploy artifact "
                                             "their manifests name (canonical: run retrofit)")
    rf.add_argument("runs", nargs="+", help="run id(s) or run-dir path(s)")
    rf.add_argument("--artifact", default=None, metavar="TAR",
                    help="explicit tarball for ALL named runs (must match their `artifact:` tag; "
                         "required for dev-tree runs with no tag)")
    rf.add_argument("--from", dest="from_dir", default=None, metavar="DIR",
                    help="artifacts dir to resolve tags against (default: var/artifacts)")

    st = add("status", "fleet status table")
    st.add_argument("-v", "--verbose", action="store_true", help="expand per-container detail")
    st.add_argument("--format", choices=["table", "json"], default="table",
                    help="json = one stable object (vehicle/run/stacks) — the machine contract "
                         "`rig fleet status` parses remotely")

    logs = add("logs", "stream/show a sensor's logs")
    logs.add_argument("-f", "--follow", action="store_true", help="follow (single sensor only)")
    logs.add_argument("--tail", type=int, default=None, help="show only the last N lines")

    add("config", "show each sensor's merged compose (canonical: config show)").add_argument(
        "--dry-run", action="store_true")

    add("config-render", "run the config pipeline; print each instance's effective config path "
                         "(canonical: config render)")

    add("config-diff", "which instances are dirty vs their pinned registry base, per key "
                       "(canonical: config diff)")

    add("pull", "pre-pull each stack's images (no container changes)").add_argument(
        "--dry-run", action="store_true"
    )

    add("image-audit", "inspect every image the deployment runs for ROS distro/rmw/package-version "
                       "consistency (canonical: image audit; run after build/pull)")

    add("doctor", "read-only preflight checks").add_argument(
        "--deep", action="store_true", help="also certify each service's launcher (runs `config` per service)"
    )

    crt = sub.add_parser("certify", help="launcher-contract conformance checks (poison-env)")
    crt.add_argument("names", nargs="*", help="sensor name(s); default: all enabled")
    crt.add_argument("--repo", default=None, help="certify a service repo directly (no deployment needed)")
    crt.add_argument("--config", default=None,
                     help="config to drive the launcher with (with --repo: defaults to the first "
                          "`examples:` entry in rigging.yaml)")
    crt.add_argument("--emit", default=None, metavar="FILE",
                     help="write the normalized resolved compose (run on two hosts, then certify --diff)")
    crt.add_argument("--diff", nargs=2, default=None, metavar=("A", "B"),
                     help="compare two --emit files; identical = host-independent config output")

    ini = sub.add_parser("init", help="scaffold a fresh deployment (vehicle.yaml/services.yaml/config)")
    ini.add_argument("target", help="directory to create the deployment in (its name seeds `vehicle:`)")
    ini.add_argument("--vehicle-id", type=int, default=None, metavar="N",
                     help="pin a LITERAL identity (ROS domain + VEHICLE_ID; the dir name seeds "
                          "`vehicle:`) — a single-vehicle tree. Default: per-host MARKERS, "
                          "supplied per machine by `rig provision` / RIG_VEHICLE_ID (nothing "
                          "comes up as vehicle 1 by accident)")
    ini.add_argument("--infra", action="append", default=[], metavar="NAME|PATH",
                     help="fully wire a shared-infra service (repeatable): a service-dir path, or a bare "
                          "name resolved from the workspace (e.g. --infra zenoh-router finds "
                          "../rig-infra/zenoh-router)")
    ini.add_argument("--no-git", action="store_true", dest="no_git",
                     help="skip the default `git init` + scaffold commit (a deployment is born a "
                          "git repo so rollback is always git)")
    ini.add_argument("--discover", nargs="?", const="", default=None, metavar="DIR",
                     help="scan DIR (default: the target's parent) for service repos (rigging.yaml; one "
                          "level into collection repos like rig-infra): populate services.yaml + copy "
                          "examples + a commented vehicle.yaml menu")

    rgf = sub.add_parser("rigify", help="make an existing software dir rig-compatible "
                                        "(rigging.yaml + launcher + example config, analysis-seeded)")
    rgf.add_argument("directory", help="the service repo to rigify — files are only ADDED, never overwritten")
    rgf.add_argument("--service", default=None,
                     help="service name (default: the directory name, sanitized)")
    rgf.add_argument("--tier", choices=["infra", "sensor", "autonomy"], default=None,
                     help="declare the tier in the generated rigging.yaml (default: a commented hint, "
                          "meaning sensor)")

    ftc = sub.add_parser("fetch", help="materialize example configs for hand-authored manifest rows "
                                       "(`pull` fetches images; `fetch` fetches configs)")

    ad = sub.add_parser("add", help="add a service to THIS deployment — local path, workspace name, "
                                    "registry ref, sensor:<id>, or <service>:<profile>")
    ad.add_argument("service", metavar="NAME|PATH|REF|sensor:ID|SERVICE:PROFILE",
                    help="service dir path or bare workspace name (like init --infra; infra wired "
                         "ENABLED, sensor/autonomy a commented menu row) — or a registry ref "
                         "(public/zenoh-router) / sensor:<id>, which install from the registries")
    ad.add_argument("--tier", choices=["infra", "sensor", "autonomy"], default=None,
                    help="override the service's declared tier for THIS deployment (local forms only)")
    ad.add_argument("--as", dest="as_name", default=None, metavar="NAME",
                    help="instance name for registry installs (ROS-safe; default from the package)")
    ad.add_argument("--locked", action="store_true",
                    help="registry installs only: reproduce rig.lock exactly (same pins/hashes)")

    ven = sub.add_parser("vendor", help="copy a service's launch surface into services/<service>/")
    ven.add_argument("service", help="service name (key in services.yaml / its rigging.yaml)")
    ven.add_argument("--from", dest="source", default=None,
                     help="source repo path (default: the service's services.yaml path)")

    bld = sub.add_parser("build", help="build/push or mirror each service's images into the registry")
    bld.add_argument("--registry", default=None, help="target registry (overrides vehicle.yaml images.registry)")
    bld.add_argument("--tag", default=None, help="tag to pass to each service's build command")
    bld.add_argument("--platform", default=None,
                     help="hardware/OS target (overrides vehicle.yaml `platform:`) — matrix services "
                          "build/push the composed <tag>-<platform> and get RIG_TARGET_PLATFORM")
    bld.add_argument("-j", "--jobs", type=int, default=1, metavar="N",
                     help="build/mirror up to N services concurrently (output grouped per service)")
    bld.add_argument("--no-cache", action="store_true", dest="no_cache",
                     help="full rebuild: export RIG_BUILD_NO_CACHE=1 to every build command (scripts "
                          "opt in, e.g. `docker build ${RIG_BUILD_NO_CACHE:+--no-cache}`)")
    bld.add_argument("--base-image", default=None, metavar="REF", dest="base_image",
                     help="deployment base image ref (overrides vehicle.yaml images.base and any "
                          "`provides: base` service) — exported to builds as RIG_BASE_IMAGE")
    bld.add_argument("--dry-run", action="store_true")

    bk = sub.add_parser("bake", help="freeze the deployment into a tagged, content-addressed artifact")
    bk.add_argument("--tag", required=True, help="artifact tag (names the .tar.gz)")
    bk.add_argument("--registry", default=None,
                    help="registry the vehicle pulls from (overrides vehicle.yaml images.registry); "
                         "images are digest-pinned against it")
    bk.add_argument("--bundle-images", action="store_true",
                    help="docker-save the image set INTO the artifact (multi-GB): zero registry at deploy "
                         "time; refs stay tags and integrity is the artifact's sha256")

    ub = sub.add_parser("unbake", help="extract a baked artifact to an editable tree")
    ub.add_argument("artifact", help="path to the .tar.gz artifact")
    ub.add_argument("--into", default=None, help="destination dir (default: var/unbaked/<tag>)")

    sub.add_parser("artifact-list", help="list baked artifacts with provenance (canonical: artifact list)")

    reg = sub.add_parser("registry", help="package registries — client (add/remove/list/sync) and "
                                          "authoring (init/validate/index)")
    regsub = reg.add_subparsers(dest="registry_cmd", required=True)
    ri = regsub.add_parser("init", help="scaffold a new empty registry in DIR — usable as a local-dir "
                                        "registry at once; CI wrappers (GitHub + GitLab) included")
    ri.add_argument("directory", help="directory to create (must not exist, or be empty)")
    ri.add_argument("--namespace", default=None,
                    help="the namespace consumers see, [a-z][a-z0-9-]* (default: from the dir name)")
    ra = regsub.add_parser("add", help="subscribe this client to a registry (~/.rig/registries.yaml; "
                                       "order = resolution priority)")
    ra.add_argument("name", help="local alias AND the qualifier you type (public/…)")
    ra.add_argument("url", nargs="?", default=None, help="git URL (managed clone + sync)")
    ra.add_argument("--path", default=None, help="use an existing folder IN PLACE (local-dir type)")
    ra.add_argument("--front", action="store_true",
                    help="insert at HIGHEST priority (e.g. a dev checkout shadowing public)")
    rr = regsub.add_parser("remove", help="unsubscribe a registry (cache is kept)")
    rr.add_argument("name")
    regsub.add_parser("list", help="configured registries with sync state")
    rs = regsub.add_parser("sync", help="git: clone/ff-pull into the cache; local-dir: re-check. "
                                        "All resolution is offline afterwards")
    rs.add_argument("names", nargs="*", help="registry name(s); default: all")
    rpn = regsub.add_parser("pending", help="unpublished authoring branches (promote/*) across "
                                            "the git caches — state, carried packages, "
                                            "copy-paste commands")
    rpn.add_argument("name", nargs="?", default=None, help="one registry (default: all git-type)")
    rpu = regsub.add_parser("push", help="push promote/* branches via SYSTEM git (never the "
                                         "default branch — sync's ff-only contract owns it; "
                                         "rig holds no credentials)")
    rpu.add_argument("name", help="the registry whose cache holds the branch(es)")
    rpu.add_argument("branches", nargs="*", help="branch name(s); or --all")
    rpu.add_argument("--all", action="store_true", dest="all_pending",
                     help="every unmerged promote/* branch")
    rpu.add_argument("--pr", action="store_true",
                     help="also CREATE the PR via your own gh/glab when installed "
                          "(capability-detected; URL fallback otherwise — merge stays on "
                          "the forge)")
    rdc = regsub.add_parser("discard", help="delete unpushed promote/* branches — the pre-push "
                                            "undo; the cwd deployment is re-anchored first "
                                            "(save's inverse, render-identical)")
    rdc.add_argument("name", help="the registry whose cache holds the branch(es)")
    rdc.add_argument("branches", nargs="*", help="branch name(s); or --all")
    rdc.add_argument("--all", action="store_true", dest="all_pending",
                     help="every promote/* branch (pushed ones are skipped with a note)")
    rv = regsub.add_parser("validate", help="validate a registry tree (every CI rule + index freshness) "
                                            "— what tools/validate and the CI wrappers call")
    rv.add_argument("directory", nargs="?", default=".", help="registry root (default: cwd)")
    rx = regsub.add_parser("index", help="regenerate index.json (refuses an invalid registry)")
    rx.add_argument("directory", nargs="?", default=".", help="registry root (default: cwd)")

    pkgp = sub.add_parser("pkg", help="package operations across registries (search/info; "
                                      "install/upgrade/lock/promote land with the lockfile)")
    pkgsub = pkgp.add_subparsers(dest="pkg_cmd", required=True)
    ps = pkgsub.add_parser("search", help="search names, sensor:<id>, project:<tag>, or "
                                          "<service>:[glob] (profiles by required service) — "
                                          "no query = the full catalog; results are fully "
                                          "qualified, priority order")
    ps.add_argument("query", nargs="?", default="",
                    help="omit to list every package in every configured registry")
    ps.add_argument("--kind", choices=["service", "profile", "overlay", "suite", "vehicle"],
                    default=None,
                    help="only this package kind (composes with any query form)")
    ps.add_argument("--registry", default=None, metavar="NAME",
                    help="only this registry's packages")
    pi = pkgsub.add_parser("info", help="one package's manifest highlights + provenance")
    pi.add_argument("ref", help="[registry/]name")
    pi.add_argument("--versions", action="store_true",
                    help="list every published version from the registry's git history "
                         "(@old is installable: pkg add ref@version)")
    pkgsub.add_parser("list", help="THIS deployment's installed packages (from rig.lock): kind, "
                                   "which instances use each, upgrades available")
    prm = pkgsub.add_parser("remove", aliases=["rm"],
                            help="undo pkg add: remove instance(s) (row, bindings, clean "
                                 "config, anchors) and GC unused services — bring the "
                                 "instance DOWN first (`rm` is a permanent alias)")
    prm.add_argument("specs", nargs="+", metavar="INSTANCE|PACKAGE",
                     help="instance name(s); a package name works for instance-less dependencies")
    prm.add_argument("--purge-config", action="store_true", dest="purge_config",
                     help="delete the working config even when it carries local edits")
    pin = pkgsub.add_parser("add", aliases=["install"],
                            help="add to THIS deployment — registry ref, sensor:<id>, local path, "
                                 "or workspace name (ONE grammar with `rig add`; `install` is a "
                                 "permanent alias). Registry installs fetch @ pin, vendor, "
                                 "materialize the working config, lock")
    pin.add_argument("spec", metavar="REF|sensor:ID|PATH|NAME",
                     help="[registry/]name[@version] | sensor:<id> | a service-dir path | a bare "
                          "workspace name (local wins; dir + registry both live = hard error)")
    pin.add_argument("--tier", choices=["infra", "sensor", "autonomy"], default=None,
                     help="override the service's declared tier for THIS deployment "
                          "(local forms only)")
    pin.add_argument("--as", dest="as_name", default=None, metavar="NAME",
                     help="instance name (ROS-safe; default: from the package name)")
    pin.add_argument("--locked", action="store_true",
                     help="reproduce rig.lock exactly (same pins, same payload hashes)")
    pu = pkgsub.add_parser("upgrade", help="re-pin profile instances to the registries' current "
                                           "versions — three-way merge, local edits win, conflicts "
                                           "surfaced")
    pu.add_argument("names", nargs="*", help="instance name(s); default: every profile instance")
    pu.add_argument("--dry-run", action="store_true",
                    help="run the REAL sweep (three-ways, conflicts) then roll everything back "
                         "— a full-fidelity preview")
    pkgsub.add_parser("lock", help="re-verify every instance anchor and rewrite rig.lock "
                                   "deterministically")
    psv = pkgsub.add_parser("save", help="publish this deployment's local edits as the next "
                                         "version of the package they CAME FROM, then re-anchor "
                                         "clean (render identical): bound overlay first (top of "
                                         "the stack), else the pinned profile; a routed SERVICE "
                                         "saves its code pointer. No targeting flags — a "
                                         "different registry or kind is `pkg promote`")
    psv.add_argument("spec", metavar="INSTANCE|SERVICE",
                     help="an instance name, or a routed service name (services.yaml key)")
    psv.add_argument("--dry-run", action="store_true",
                     help="print what would be published/re-anchored; write nothing")
    pyk = pkgsub.add_parser("yank", help="retract a package's CURRENT version FROM a registry: "
                                         "restore the previous version from git history (no "
                                         "prior version = remove the package); this deployment "
                                         "re-anchors render-identically (save's inverse)")
    pyk.add_argument("ref", help="the package (profiles: <service>:<short>, or the short half)")
    pyk.add_argument("--from", dest="from_", required=True, metavar="REGISTRY",
                     help="the registry to retract from (retraction takes FROM; publication "
                          "verbs write --to)")
    pyk.add_argument("--dry-run", action="store_true",
                     help="print the retraction plan; write nothing")
    pp = pkgsub.add_parser("promote", help="lift local deltas into scaffolded packages in a "
                                           "registry checkout (write + validate; publishing stays "
                                           "plain git)")
    pp.add_argument("names", nargs="*", help="instance name(s) — or --all for every dirty instance")
    pp.add_argument("--to", required=True, metavar="REGISTRY", help="target registry (from "
                    "`rig registry list`; local-dir written in place, git on a promote/ branch)")
    pp.add_argument("--all", action="store_true", dest="all_dirty",
                    help="promote every dirty instance (one overlay each)")
    pp.add_argument("--name", default=None, help="package name (single instance only). Profiles: "
                    "the SHORT half only — identity is <service>:<short>, service derived from the "
                    "instance; default: the provenance profile's short name, else the instance "
                    "name. Overlays: default <instance>[-<project>]")
    pp.add_argument("--project", default=None, help="project tag (searchable axis; also the "
                    "default name suffix)")
    pp.add_argument("--vehicle", default=None, metavar="NAME",
                    help="(with --suite) also capture vehicle.yaml as a `vehicle` package — the "
                         "suite's instance PLAN: names/order/tiers/bindings reproduce on a fresh "
                         "deployment (identity stays per-host; row refs unversioned)")
    pp.add_argument("--kind", choices=["overlay", "profile", "service"], default=None,
                    help="overlay = the delta; profile = the full effective config; service = "
                         "the routed checkout's CODE POINTER (repo+rev from git — the dev-loop "
                         "counterpart of registry-release CI). Default: overlay for "
                         "registry-based instances, profile for hand-authored "
                         "(no pinned base — an overlay is impossible)")
    pp.add_argument("--suite", default=None, metavar="NAME",
                    help="also emit a suite referencing the deployment's profiles + the new "
                         "overlays in binding order")
    pp.add_argument("--bump", action="store_true",
                    help="the package exists in the target — publish the next patch version "
                         "(implied when updating the exact profile the instance is pinned to)")
    pp.add_argument("--version", default=None, metavar="X.Y.Z", dest="pkg_version",
                    help="(--kind service) explicit version to publish (default: 1.0.0 fresh, "
                         "--bump patch otherwise)")
    pp.add_argument("--target-instance", action="store_true", dest="target_instance",
                    help="scope the overlay to THIS instance name instead of its service")
    pp.add_argument("--match", action="append", default=[], metavar="ID",
                    help="(--kind profile) hardware match identifier (repeatable; REPLACES the "
                         "existing package's match set on a re-promote)")
    pp.add_argument("--requires", default=None, metavar="REF",
                    help="(--kind profile) service requirement override (ns/service@X.Y.Z)")
    pp.add_argument("--adopt", action="store_true",
                    help="after publishing, make THIS deployment consume it. Profiles (one "
                         "instance; implied kind): re-pin the instance onto the new profile — "
                         "working+pin reset, overrides dropped, overlays baked in, render "
                         "identical. Services: record the published pin in rig.lock (no more "
                         "local/unpublished; the dev route stays — `pkg upgrade` vendors at "
                         "the pin). With --all --suite: consent for the capture to profile+"
                         "adopt HAND-AUTHORED instances (without it they are skipped loudly "
                         "and the plan omits them)")
    po = pkgsub.add_parser("outdated", help="dependency-drift report across the registries: "
                                            "profile requires/based_on, overlay authored_against, "
                                            "suite members vs registry-current — report-only, "
                                            "exit 1 on drift (the FIX column names the repair)")
    po.add_argument("refs", nargs="*", help="narrow to specific package name(s); default: every "
                                            "package in the swept registries")
    po.add_argument("--registry", default=None, metavar="NAME|DIR",
                    help="sweep one registry — a configured alias, or a registry directory "
                         "(CI sweeping its own tree); default: all configured")
    po.add_argument("--quiet", action="store_true",
                    help="hide INFO rows (caret-still-covering, unresolvable namespaces)")
    prp = pkgsub.add_parser("repin", help="advance a package's DECLARED dependency pins to "
                                          "registry-current and publish the next patch version "
                                          "(profile requires / overlay authored_against / suite "
                                          "members) — pins only; payload reconciliation is "
                                          "`pkg rebase`")
    prp.add_argument("ref", help="the package in the target registry (profiles: the "
                                 "<service>:<short> key, or the short half when unambiguous)")
    prp.add_argument("--to", required=True, metavar="REGISTRY",
                     help="the registry carrying the package (local-dir in place, git on a "
                          "promote/ branch)")
    prp.add_argument("--dep", default=None, metavar="[ns/]name[@ver]",
                     help="advance the NAMED dependency to this exact version (an explicit "
                          "pin — a stale in-registry pin validates with a warning and installs "
                          "from git history; suite siblings still refresh to current)")
    prp.add_argument("--dry-run", action="store_true",
                     help="print the resulting manifest changes; write nothing")
    pr = pkgsub.add_parser("rebase", help="three-way a FORKED profile onto its parent's current "
                                          "version (based_on lineage; conflicts keep YOURS, "
                                          "loudly) — registry-side, write+validate only")
    pr.add_argument("name", help="the fork's package name in the target registry")
    pr.add_argument("--to", required=True, metavar="REGISTRY",
                    help="the registry carrying the fork (local-dir in place, git on a "
                         "promote/ branch)")
    pr.add_argument("--onto", default=None, metavar="PARENT[@VER]",
                    help="rebase onto this parent version (default: the parent's current)")

    flc = argparse.ArgumentParser(add_help=False)  # shared fleet flags — after the verb
    flc.add_argument("--fleet", default=None, metavar="PATH",
                     help="fleet.yaml (default: $RIG_FLEET, else upward search from the cwd)")
    flc.add_argument("-j", "--jobs", type=int, default=4, help="concurrent vehicles (default 4)")
    fl = sub.add_parser("fleet", help="GCS-side fan-out over the fleet roster (fleet.yaml): the "
                                      "ssh loop, automated — never a control plane")
    flsub = fl.add_subparsers(dest="fleet_cmd", required=True)
    flsub.add_parser("list", parents=[flc], help="roster + reachability table")
    fls = flsub.add_parser("status", parents=[flc],
                           help="aggregate `status --format json` across the fleet")
    fls.add_argument("names", nargs="*", default=[], help="vehicle name(s); default: all")
    fls.add_argument("-v", "--verbose", action="store_true", help="per-stack detail per vehicle")
    flu = flsub.add_parser("up", parents=[flc], help="bring the fleet up with a CORRELATED run "
                                                     "label; SIL: ensures the docker network + "
                                                     "the shared run-dir view")
    flu.add_argument("names", nargs="*", default=[], help="vehicle name(s); default: all")
    flu.add_argument("--run", default=None, metavar="LABEL",
                     help="run label stamped on EVERY vehicle (post-test data groups itself)")
    flu.add_argument("--var", action="append", default=[], metavar="K=V",
                     help="forwarded as RIG_VAR_<k> (the vars tier — lands in each run's "
                          "snapshot; never a config push)")
    flu.add_argument("--force", action="store_true", help="forwarded to each vehicle's up")
    flu.add_argument("--dry-run", action="store_true", help="forwarded; no network/roster/view "
                                                            "side effects")
    fld = flsub.add_parser("down", parents=[flc], help="tear the fleet down (full-fleet success "
                                                       "also removes the SIL network)")
    fld.add_argument("names", nargs="*", default=[], help="vehicle name(s); default: all")
    fld.add_argument("--end-run", action="store_true", dest="end_run",
                     help="forwarded: capture docker logs into each vehicle's run, then seal it "
                          "after a successful down")
    fld.add_argument("--force", action="store_true", help="forwarded to each vehicle's down")
    fld.add_argument("--dry-run", action="store_true")
    fly = flsub.add_parser("sync", parents=[flc], help="harvest SEALED runs (ended: present = "
                                                       "safe to sync) into <into>/<label>/<vehicle>/")
    fly.add_argument("names", nargs="*", default=[], help="vehicle name(s); default: all")
    fly.add_argument("--label", default=None, help="only runs with this label")
    fly.add_argument("--into", default="fleet-runs", metavar="DIR",
                     help="harvest root (default: fleet-runs/) — the same tree a SIL fleet "
                          "view produces live")

    ov = sub.add_parser("overlay", help="overlay BINDINGS on instances (apply/remove/reorder/list) "
                                        "— authoring/publishing is `pkg promote`")
    ovsub = ov.add_subparsers(dest="overlay_cmd", required=True)
    oa = ovsub.add_parser("apply", help="bind an overlay to an instance (appends to the ordered "
                                        "list; last wins, local still beats every overlay)")
    oa.add_argument("instance")
    oa.add_argument("ref", help="[registry/]overlay-name")
    oa.add_argument("--clear-local", action="store_true", dest="clear_local",
                    help="reset the working config to its pristine pin and drop row overrides — "
                         "the promote round-trip's second half (identical render, tuning now "
                         "versioned)")
    orm = ovsub.add_parser("remove", help="unbind an overlay (payload copy dropped with the last "
                                          "binding)")
    orm.add_argument("instance")
    orm.add_argument("ref")
    oro = ovsub.add_parser("reorder", help="set the COMPLETE new binding order (order = merge order)")
    oro.add_argument("instance")
    oro.add_argument("refs", nargs="+")
    ol = ovsub.add_parser("list", help="bindings per instance, in order")
    ol.add_argument("instance", nargs="?", default=None)

    pv = sub.add_parser("provision", help="THIS machine's vehicle identity "
                                          "(/etc/rig/vehicle.local.yaml) — write with sudo; bare "
                                          "form shows + checks against the current deployment")
    pv.add_argument("--id", dest="vehicle_id", default=None, metavar="N",
                    help="vehicle id (numeric: ROS domain + compose-project suffix)")
    pv.add_argument("--name", default=None, help="vehicle name")
    pv.add_argument("--var", action="append", default=[], metavar="k=v",
                    help="per-vehicle var (repeatable; lowercase names)")
    pv.add_argument("--platform", default=None,
                    help="THIS machine's hardware/OS target (e.g. jp7) -> RIG_TARGET_PLATFORM; "
                         "matrix services pull <tag>-<platform>")
    pv.add_argument("--data-dir", dest="data_dir", default=None, metavar="PATH",
                    help="THIS machine's run-registry home (absolute; the registry itself is "
                         "minted lazily by the first up/new-run — nothing else to initialize)")
    pv.add_argument("--force", action="store_true",
                    help="allow CHANGING an existing identity (renames compose projects — "
                         "bring the vehicle down first)")

    cmp_ = sub.add_parser("completion", help="print the TAB-completion script for a shell — "
                                             "`rig setup --shell` wires it, or eval/install it "
                                             "yourself (brew/deb ship it)")
    cmp_.add_argument("shell", choices=["bash", "zsh"], help="target shell")

    st = sub.add_parser("setup", help="first-run host setup: ~/.rig + the default public registry; "
                                      "--shell wires a source checkout onto PATH + shell "
                                      "completion; --purge removes user state")
    st.add_argument("--shell", action="store_true",
                    help="append a delimited PATH block to your shell rc (skipped when `rig` is "
                         "already on PATH — deb/brew/pipx installs need no wiring)")
    st.add_argument("--no-default-registry", action="store_true", dest="no_default_registry",
                    help="don't seed registries.yaml with the public registry")
    st.add_argument("--purge", action="store_true",
                    help="remove ~/.rig and the shell block (run BEFORE uninstalling the package)")
    st.add_argument("--yes", action="store_true", help="confirm --purge non-interactively")
    return parser


def main(argv=None) -> int:
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    if argv and argv[0] == "_complete":  # hidden, intercepted before ANY parsing: the TAB path
        from . import completions
        return completions.main(argv[1:])
    try:
        argv = translate_argv(argv)
    except RigError as exc:
        eprint(f"rig: {exc}")
        return 1
    if argv is None:  # a synthesized group help was printed
        return 0
    args = build_parser().parse_args(argv)
    # Defaults for flags not present on every subcommand.
    for attr, default in (("verbose", False), ("dry_run", False), ("force", False),
                          ("purge", False), ("follow", False), ("tail", None)):
        if not hasattr(args, attr):
            setattr(args, attr, default)
    try:
        if args.cmd == "init":  # creates a NEW deployment; doesn't read an existing one
            return cmd_init(args)
        if args.cmd == "rigify":  # operates on a service repo — needs no deployment at all
            return cmd_rigify(args)
        if args.cmd == "registry":  # operates on a registry tree / ~/.rig — needs no deployment
            return cmd_registry(args)
        if args.cmd == "fleet":  # GCS-side fan-out — needs fleet.yaml, never a deployment root
            fleet = fleet_mod.load_fleet(args.fleet)
            if args.fleet_cmd == "list":
                return fleet_mod.cmd_list(fleet, jobs=args.jobs)
            if args.fleet_cmd == "status":
                return fleet_mod.cmd_status(fleet, args.names, verbose=args.verbose,
                                            jobs=args.jobs)
            if args.fleet_cmd == "up":
                return fleet_mod.cmd_up(fleet, args.names, run_label=args.run,
                                        set_vars=args.var, force=args.force,
                                        dry_run=args.dry_run, jobs=args.jobs)
            if args.fleet_cmd == "down":
                return fleet_mod.cmd_down(fleet, args.names, end_run=args.end_run,
                                          force=args.force, dry_run=args.dry_run,
                                          jobs=args.jobs)
            return fleet_mod.cmd_sync(fleet, args.names, label=args.label, into=args.into,
                                      jobs=args.jobs)
        if args.cmd == "pkg":
            if args.pkg_cmd in ("add", "install", "remove", "rm", "upgrade", "lock", "promote",
                                "list", "save"):
                root = (args.root or find_root()).resolve()
                if not (root / "vehicle.yaml").exists():
                    raise RigError(f"pkg {args.pkg_cmd}: not in a rig deployment (no vehicle.yaml) — "
                                   f"`rig init` creates one")
                if args.pkg_cmd in ("add", "install"):  # install = permanent alias; ONE grammar
                    return route_add(root, args.spec, as_name=args.as_name,  # with `rig add`
                                     tier=args.tier, locked=args.locked)
                if args.pkg_cmd in ("remove", "rm"):  # rm = permanent alias
                    return install_mod.remove(root, args.specs, purge_config=args.purge_config)
                if args.pkg_cmd == "list":
                    return pkg_mod.list_installed(root)
                if args.pkg_cmd == "upgrade":
                    return workingcopy_mod.upgrade(root, args.names, dry_run=args.dry_run)
                if args.pkg_cmd == "save":
                    from . import save as save_mod
                    return save_mod.save(root, args.spec, dry_run=args.dry_run)
                if args.pkg_cmd == "promote":
                    return promote_mod.promote(
                        root, args.names, to=args.to, all_dirty=args.all_dirty, name=args.name,
                        project=args.project, kind=args.kind, suite=args.suite, bump=args.bump,
                        target_instance=args.target_instance, matches=args.match,
                        requires=args.requires, adopt=args.adopt, version=args.pkg_version,
                        vehicle=args.vehicle)
                return workingcopy_mod.relock(root)
            if args.pkg_cmd == "rebase":  # registry-side: no deployment involved
                return promote_mod.rebase(args.name, to=args.to, onto=args.onto)
            if args.pkg_cmd == "repin":  # registry-side pin advance
                from . import repin as repin_mod
                return repin_mod.repin(args.ref, to=args.to, dep=args.dep, dry_run=args.dry_run)
            if args.pkg_cmd == "outdated":  # registry-side, report-only
                from . import outdated as outdated_mod
                return outdated_mod.outdated(args.refs, registry=args.registry, quiet=args.quiet)
            if args.pkg_cmd == "yank":  # registry-side; the cwd deployment re-anchors if affected
                from . import yank as yank_mod
                return yank_mod.yank(args.ref, from_=args.from_, dry_run=args.dry_run,
                                     root=_optional_root(args))
            return cmd_pkg(args)  # search/info consult ~/.rig only
        if args.cmd == "completion":  # a pure emitter — no deployment, no user state
            from . import completions
            print(completions.script(args.shell), end="")
            return 0
        if args.cmd == "setup":  # host/user environment — the one command whose object is the HOST
            return registries_mod.setup(shell=args.shell, no_default_registry=args.no_default_registry,
                                        purge=args.purge, yes=args.yes)
        if args.cmd == "provision":  # machine identity — works with or without a deployment nearby
            root = (args.root or find_root()).resolve()
            return provision_mod.provision(
                root if (root / "vehicle.yaml").exists() else None,
                vehicle_id=args.vehicle_id, name=args.name, set_vars=args.var,
                platform=args.platform, data_dir=args.data_dir, force=args.force)
        root = (args.root or find_root()).resolve()
        if args.cmd == "add":  # edits the deployment files themselves — routes its own manifest load
            return cmd_add(args, root)
        if args.cmd == "fetch":  # runs BEFORE the manifest is loadable — reads vehicle.yaml raw
            return cmd_fetch(args, root)
        if args.cmd == "vendor":  # operates on a source repo, not the manifest
            return cmd_vendor(args, root)
        if args.cmd == "unbake":  # operates on an artifact, not the manifest
            return cmd_unbake(args, root)
        if args.cmd == "reconstruct":  # a run dir + nothing else — no deployment, no registry
            from . import reconstruct as reconstruct_mod
            return reconstruct_mod.cmd_reconstruct(
                root if (root / "vehicle.yaml").exists() else None,
                run_ref=args.run, into=args.into, config=args.config,
                copy_run=args.copy_run, no_import=args.no_import)
        if args.cmd == "run-retrofit":  # reads run dirs + var/artifacts, not the manifest
            from . import reconstruct as reconstruct_mod
            return reconstruct_mod.cmd_retrofit(root, run_refs=args.runs,
                                                artifact=args.artifact, from_dir=args.from_dir)
        if args.cmd == "artifact-list":  # reads var/artifacts, not the manifest
            return cmd_artifact_list(args, root)
        if args.cmd == "config-diff":  # needs the RAW rows (working files), not the rendered output
            return workingcopy_mod.cmd_diff(args, root)
        if args.cmd == "overlay":  # bindings edit vehicle.yaml rows + config/.overlays
            if args.overlay_cmd == "apply":
                return overlay_mod.apply(root, args.instance, args.ref, clear_local=args.clear_local)
            if args.overlay_cmd == "remove":
                return overlay_mod.remove(root, args.instance, args.ref)
            if args.overlay_cmd == "reorder":
                return overlay_mod.reorder(root, args.instance, args.refs)
            return overlay_mod.list_bindings(root, args.instance)
        if args.cmd == "build":
            return cmd_build(args, root)
        if args.cmd == "bake":
            return cmd_bake(args, root)
        if args.cmd == "certify":  # may run repo-standalone (--repo/--diff) — routes its own manifest load
            return cmd_certify(args, root)
        args.rig_root = root  # handlers that touch the run registry need the deployment root (provenance)
        manifest, catalog, descriptors = _load(root)
        return _HANDLERS[args.cmd](args, manifest, catalog, descriptors)
    except RigError as exc:
        eprint(f"rig: {exc}")
        return 1
    except KeyboardInterrupt:
        eprint("rig: interrupted")
        return 130
