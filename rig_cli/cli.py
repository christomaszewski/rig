"""The `rig` command line — lifecycle (up/down/status/logs/config/pull), checks (doctor/certify),
authoring (init/vendor), and deployment (build/bake/unbake)."""
from __future__ import annotations

import argparse
from pathlib import Path

from . import (
    RigError, __version__, bake as bake_mod, build as build_mod, certify as certify_mod,
    doctor as doctor_mod, dispatch, init as init_mod, resolve, runs as runs_mod, status as status_mod,
    vendor as vendor_mod,
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


def _load(root: Path) -> tuple[Manifest, dict[str, ServiceEntry], dict[str, Descriptor]]:
    manifest = load_manifest(root)
    manifest = resolve.materialize_manifest(manifest, root)  # render profiles/overrides -> per-instance configs
    catalog = load_catalog(root)
    descriptors: dict[str, Descriptor] = {}
    for sensor in manifest.sensors:
        if sensor.service not in catalog:
            raise RigError(f"sensor '{sensor.name}': service '{sensor.service}' not in services.yaml")
        if sensor.service not in descriptors:
            descriptors[sensor.service] = load_descriptor(sensor.service, catalog[sensor.service].path)
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
    env = dispatch.fleet_env(manifest)
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
    eprint(f"rig up: {manifest.vehicle} — {stack_summary([p[0] for p in pairs])}")
    return _summarize(dispatch.run_verb(pairs, env, "up", dry_run=args.dry_run))


def cmd_down(args, manifest, catalog, descriptors) -> int:
    env = dispatch.fleet_env(manifest)
    pairs = _pairs(manifest, descriptors, args.names, reverse=True)  # reverse: consumers before producers
    if not pairs:
        eprint("rig: no enabled stacks to tear down")
        return 0
    eprint(f"rig down: {manifest.vehicle} — {stack_summary([p[0] for p in pairs])}")
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
        runs_mod.end_run(manifest)
    return rc


def cmd_config(args, manifest, catalog, descriptors) -> int:
    env = dispatch.fleet_env(manifest)
    pairs = _pairs(manifest, descriptors, args.names)
    return _summarize(dispatch.run_verb(pairs, env, "config", dry_run=args.dry_run))


def cmd_pull(args, manifest, catalog, descriptors) -> int:
    """Pre-pull every stack's images — no containers created/restarted. The field-ops primer: pull while
    the registry is reachable, then `up` (now or later) starts from the local cache."""
    env = dispatch.fleet_env(manifest)
    pairs = _pairs(manifest, descriptors, args.names)
    if not pairs:
        eprint("rig: no enabled stacks to pull for")
        return 0
    eprint(f"rig pull: {manifest.vehicle} — {stack_summary([p[0] for p in pairs])}")
    return _summarize(dispatch.run_verb(pairs, env, "pull", dry_run=args.dry_run))


def cmd_status(args, manifest, catalog, descriptors) -> int:
    env = dispatch.fleet_env(manifest)
    pairs = _pairs(manifest, descriptors, args.names)
    rows = status_mod.gather(pairs, env)
    if (run_line := runs_mod.status_line(manifest)) is not None:
        print(run_line)
    print(status_mod.render(rows, verbose=args.verbose))  # stdout: the report
    return 0


def cmd_new_run(args, manifest, catalog, descriptors) -> int:
    runs_mod.new_run(manifest, args.rig_root, args.label, force=args.force)
    return 0


def cmd_end_run(args, manifest, catalog, descriptors) -> int:
    # Snapshot the fleet state into the manifest as part of sealing (best-effort).
    env = dispatch.fleet_env(manifest)
    rows = status_mod.gather(_pairs(manifest, descriptors, []), env)
    runs_mod.end_run(manifest, force=args.force, status_text=status_mod.render(rows))
    return 0


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

    headers = ("RUN", "LABEL", "STATE", "STARTED", "ENDED", "SIZE")
    table = [headers] + [
        (r.run, r.label, r.state, r.started, r.ended, _size(r.disk_kb))
        for r in rows
    ]
    widths = [max(len(row[i]) for row in table) for i in range(len(headers))]
    for row in table:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
    return 0


def cmd_logs(args, manifest, catalog, descriptors) -> int:
    env = dispatch.fleet_env(manifest)
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
                  discover=discover)
    return 0


def cmd_build(args, root: Path) -> int:
    manifest, catalog, descriptors = _load(root)
    return build_mod.build(manifest, descriptors, registry=args.registry, tag=args.tag,
                           dry_run=args.dry_run, jobs=args.jobs)


def cmd_bake(args, root: Path) -> int:
    manifest, catalog, descriptors = _load(root)
    env = dispatch.fleet_env(manifest)
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
    "config": cmd_config,
    "pull": cmd_pull,
    "status": cmd_status,
    "logs": cmd_logs,
    "doctor": cmd_doctor,
    "new-run": cmd_new_run,
    "end-run": cmd_end_run,
    "runs": cmd_runs,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rig", description="vehicle-level sensor-stack orchestrator")
    parser.add_argument("--version", action="version", version=f"rig {__version__}")
    parser.add_argument("--root", type=Path, default=None,
                        help="deployment root holding vehicle.yaml (default: detected from the cwd, "
                             "else alongside the CLI)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add(name, help_text):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("names", nargs="*", help="sensor name(s); default: all enabled")
        return p

    up = add("up", "bring sensors up (producers first)")
    up.add_argument("--dry-run", action="store_true", help="print the exact launcher invocations only")
    up.add_argument("--force", action="store_true", help="bring up even if preflight reports errors")
    up.add_argument("--run", default=None, metavar="LABEL",
                    help="join the open run with this label, or rotate to a new one (bare up never rotates)")

    down = add("down", "tear sensors down (reverse order)")
    down.add_argument("--dry-run", action="store_true")
    down.add_argument("--purge", action="store_true", help="also remove declared external volumes (FINAL teardown)")
    down.add_argument("--end-run", action="store_true", dest="end_run",
                      help="after a successful FULL down, seal the open run (stamps ended: + snapshot)")

    nr = sub.add_parser("new-run", help="rotate: seal the open run (if any) and open a new one")
    nr.add_argument("label", nargs="?", default=None, help="session label (dir becomes <stamp>_<label>)")
    nr.add_argument("--force", action="store_true",
                    help="rotate even while stacks run (late writes land in the sealed run)")
    nr.add_argument("names", nargs="*", default=[], help=argparse.SUPPRESS)

    er = sub.add_parser("end-run", help="seal the open run: stamp ended + snapshot, remove `current`")
    er.add_argument("--force", action="store_true", help="seal even while stacks run")
    er.add_argument("names", nargs="*", default=[], help=argparse.SUPPRESS)

    rn = sub.add_parser("runs", help="list the run registry (OPEN / sealed / interrupted)")
    rn.add_argument("names", nargs="*", default=[], help=argparse.SUPPRESS)

    add("status", "fleet status table").add_argument(
        "-v", "--verbose", action="store_true", help="expand per-container detail"
    )

    logs = add("logs", "stream/show a sensor's logs")
    logs.add_argument("-f", "--follow", action="store_true", help="follow (single sensor only)")
    logs.add_argument("--tail", type=int, default=None, help="show only the last N lines")

    add("config", "render each sensor's merged compose").add_argument("--dry-run", action="store_true")

    add("pull", "pre-pull each stack's images (no container changes)").add_argument(
        "--dry-run", action="store_true"
    )

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
    ini.add_argument("--vehicle-id", type=int, default=1, metavar="N",
                     help="vehicle identity (ROS domain + VEHICLE_ID); default 1")
    ini.add_argument("--infra", action="append", default=[], metavar="TEMPLATE",
                     help="fully wire a bundled templates/ service (repeatable), e.g. "
                          "--infra zenoh-router --infra ros2-bag-logger")
    ini.add_argument("--discover", nargs="?", const="", default=None, metavar="DIR",
                     help="scan DIR (default: the target's parent) for service repos (rigging.yaml): "
                          "populate services.yaml + copy examples + a commented vehicle.yaml menu")

    ven = sub.add_parser("vendor", help="copy a service's launch surface into services/<service>/")
    ven.add_argument("service", help="service name (key in services.yaml / its rigging.yaml)")
    ven.add_argument("--from", dest="source", default=None,
                     help="source repo path (default: the service's services.yaml path)")

    bld = sub.add_parser("build", help="build/push or mirror each service's images into the registry")
    bld.add_argument("--registry", default=None, help="target registry (overrides vehicle.yaml images.registry)")
    bld.add_argument("--tag", default=None, help="tag to pass to each service's build command")
    bld.add_argument("-j", "--jobs", type=int, default=1, metavar="N",
                     help="build/mirror up to N services concurrently (output grouped per service)")
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
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    # Defaults for flags not present on every subcommand.
    for attr, default in (("verbose", False), ("dry_run", False), ("force", False),
                          ("purge", False), ("follow", False), ("tail", None)):
        if not hasattr(args, attr):
            setattr(args, attr, default)
    try:
        if args.cmd == "init":  # creates a NEW deployment; doesn't read an existing one
            return cmd_init(args)
        root = (args.root or find_root()).resolve()
        if args.cmd == "vendor":  # operates on a source repo, not the manifest
            return cmd_vendor(args, root)
        if args.cmd == "unbake":  # operates on an artifact, not the manifest
            return cmd_unbake(args, root)
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
