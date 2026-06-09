"""The `rig` command line: up / down / status / logs / config / doctor."""
from __future__ import annotations

import argparse
from pathlib import Path

from . import (
    RigError, __version__, bake as bake_mod, build as build_mod, doctor as doctor_mod, dispatch,
    init as init_mod, resolve, status as status_mod, vendor as vendor_mod,
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
    pairs = _pairs(manifest, descriptors, args.names)  # ascending order: producers before consumers
    if not pairs:
        eprint("rig: no enabled stacks to bring up")
        return 0
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
    return rc


def cmd_config(args, manifest, catalog, descriptors) -> int:
    env = dispatch.fleet_env(manifest)
    pairs = _pairs(manifest, descriptors, args.names)
    return _summarize(dispatch.run_verb(pairs, env, "config", dry_run=args.dry_run))


def cmd_status(args, manifest, catalog, descriptors) -> int:
    env = dispatch.fleet_env(manifest)
    pairs = _pairs(manifest, descriptors, args.names)
    rows = status_mod.gather(pairs, env)
    print(status_mod.render(rows, verbose=args.verbose))  # stdout: the report
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
    return doctor_mod.run(manifest, catalog, descriptors)


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
    init_mod.init(Path(args.target))
    return 0


def cmd_build(args, root: Path) -> int:
    manifest, catalog, descriptors = _load(root)
    return build_mod.build(manifest, descriptors, registry=args.registry, tag=args.tag,
                           dry_run=args.dry_run, jobs=args.jobs)


def cmd_bake(args, root: Path) -> int:
    manifest, catalog, descriptors = _load(root)
    env = dispatch.fleet_env(manifest)
    bake_mod.bake(root, manifest, catalog, descriptors, env, args.tag, registry=args.registry)
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
    "status": cmd_status,
    "logs": cmd_logs,
    "doctor": cmd_doctor,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rig", description="vehicle-level sensor-stack orchestrator")
    parser.add_argument("--version", action="version", version=f"rig {__version__}")
    parser.add_argument("--root", type=Path, default=None, help="rig repo root (default: alongside the CLI)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add(name, help_text):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("names", nargs="*", help="sensor name(s); default: all enabled")
        return p

    up = add("up", "bring sensors up (producers first)")
    up.add_argument("--dry-run", action="store_true", help="print the exact launcher invocations only")
    up.add_argument("--force", action="store_true", help="bring up even if preflight reports errors")

    down = add("down", "tear sensors down (reverse order)")
    down.add_argument("--dry-run", action="store_true")
    down.add_argument("--purge", action="store_true", help="also remove declared external volumes (FINAL teardown)")

    add("status", "fleet status table").add_argument(
        "-v", "--verbose", action="store_true", help="expand per-container detail"
    )

    logs = add("logs", "stream/show a sensor's logs")
    logs.add_argument("-f", "--follow", action="store_true", help="follow (single sensor only)")
    logs.add_argument("--tail", type=int, default=None, help="show only the last N lines")

    add("config", "render each sensor's merged compose").add_argument("--dry-run", action="store_true")

    add("doctor", "read-only preflight checks")

    ini = sub.add_parser("init", help="scaffold a fresh deployment (vehicle.yaml/services.yaml/config)")
    ini.add_argument("target", help="directory to create the deployment in")

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
        manifest, catalog, descriptors = _load(root)
        return _HANDLERS[args.cmd](args, manifest, catalog, descriptors)
    except RigError as exc:
        eprint(f"rig: {exc}")
        return 1
    except KeyboardInterrupt:
        eprint("rig: interrupted")
        return 130
