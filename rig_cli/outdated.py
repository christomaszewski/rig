"""``rig pkg outdated`` — the registry-authoring currency report (QoL plan F2).

For every package in the swept registries, resolve each DECLARED dependency against the
registries' current state and report drift: profile ``requires.service`` (exact pin behind,
caret base behind, caret no longer satisfiable) and ``based_on`` parent movement, overlay
``authored_against`` stamps, suite member pins. The FIX column names the verb that repairs the
row — ``pkg repin`` for pin/stamp advances, ``pkg rebase`` for fork-payload reconciliation.

Report-only and fail-soft by charter: cross-registry refs an unconfigured/unsynced namespace
can't serve print ``unresolvable`` (INFO) and never fail the sweep; ``registry validate`` stays
the correctness gate and never grows currency errors. Exit 1 on drift (CI-able as an advisory
hygiene report), 0 all-current; ``--quiet`` hides INFO rows (caret-still-covering,
unresolvable) so a cron sweep stays readable.
"""
from __future__ import annotations

from pathlib import Path

from . import RigError
from .common import print_table
from .refs import parse_ref, split_key
from .registries import Entry, current_version_of, load_entries, open_registry
from .registry import _CONSTRAINT, _QUALIFIED_EXACT, constraint_satisfied

_DRIFT, _INFO = "drift", "info"


def _target_entries(registry: str | None) -> list[Entry]:
    if registry is None:
        entries = load_entries()
        if not entries:
            raise RigError("pkg outdated: no registries configured — `rig setup` or "
                           "`rig registry add`")
        return entries
    for entry in load_entries():
        if entry.name == registry:
            return [entry]
    as_dir = Path(registry).expanduser()
    if (as_dir / "registry.yaml").is_file():  # an ad-hoc checkout — CI sweeps its own tree
        return [Entry(name=as_dir.resolve().name, type="local-dir", location=str(as_dir.resolve()))]
    raise RigError(f"pkg outdated: '{registry}' is neither a configured registry "
                   f"(rig registry list) nor a registry directory")


def _current(reg, self_ns: str, alias: str, dep_ns: str | None, dep_name: str) -> str | None:
    """Registry-current version of a dependency: in-registry when the ref is bare or names this
    registry (by namespace OR consumer alias), the synced caches otherwise."""
    if dep_ns is None or dep_ns in (self_ns, alias):
        pkg = reg.packages.get(dep_name)
        return pkg.version if pkg is not None else None
    return current_version_of(dep_ns, dep_name)


def _sweep_package(qual: str, pkg, reg, alias: str) -> list[tuple]:
    """Rows for one package: (level, PACKAGE, KIND, DEPENDENCY, PINNED, CURRENT, FIX)."""
    rows: list[tuple] = []
    m = pkg.manifest
    ns = reg.namespace

    def dep_row(kind_of_dep: str, ref_ns: str | None, ref_name: str, pinned: str,
                fix: str, *, caret: bool = False) -> None:
        shown = f"{ref_ns + '/' if ref_ns else ''}{ref_name}"
        current = _current(reg, ns, alias, ref_ns, ref_name)
        if current is None:
            rows.append((_INFO, qual, pkg.kind, shown, pinned,
                         "?", "unresolvable (registry not configured/synced)"))
            return
        if caret:
            base = pinned.lstrip("^")
            if constraint_satisfied(base, True, current):
                if current != base:  # still covering, base behind — floats fine, worth knowing
                    rows.append((_INFO, qual, pkg.kind, shown, pinned, current,
                                 "(caret covers — repin advances the base)"))
                return
            rows.append((_DRIFT, qual, pkg.kind, shown, pinned, current,
                         f"UNSATISFIABLE — rig {fix}"))
            return
        if current != pinned:
            rows.append((_DRIFT, qual, pkg.kind, shown, pinned, current, f"rig {fix}"))

    if pkg.kind == "profile":
        req = (m.get("requires") or {}).get("service") if isinstance(m.get("requires"), dict) else None
        match = _CONSTRAINT.match(str(req)) if isinstance(req, str) else None
        if match:
            pinned = f"{'^' if match['caret'] else ''}{match['ver']}"
            dep_row("service", match["ns"], match["name"], pinned,
                    f"pkg repin {pkg.name} --to {alias}", caret=bool(match["caret"]))
        based_on = m.get("based_on")
        bmatch = _QUALIFIED_EXACT.match(str(based_on)) if isinstance(based_on, str) else None
        if bmatch:
            dep_row("parent", bmatch["ns"], bmatch["name"], bmatch["ver"],
                    f"pkg rebase {pkg.name} --to {alias}")
    elif pkg.kind == "overlay":
        stamps = m.get("authored_against") if isinstance(m.get("authored_against"), dict) else {}
        for stamp_kind, ref in sorted(stamps.items()):
            smatch = _QUALIFIED_EXACT.match(str(ref))
            if smatch:
                dep_row(stamp_kind, smatch["ns"], smatch["name"], smatch["ver"],
                        f"pkg repin {pkg.name} --to {alias}")
    elif pkg.kind == "suite":
        members = m.get("members") if isinstance(m.get("members"), dict) else {}
        for plural in ("vehicles", "services", "profiles", "overlays"):
            for ref in members.get(plural) or []:
                mmatch = _QUALIFIED_EXACT.match(str(ref))
                if mmatch:
                    dep_row(plural.rstrip("s"), mmatch["ns"], mmatch["name"], mmatch["ver"],
                            f"pkg repin {pkg.name} --to {alias}")
    return rows


def outdated(refs: list[str], *, registry: str | None = None, quiet: bool = False) -> int:
    entries = _target_entries(registry)
    want = {parse_ref(r)[1] for r in refs}  # narrowing is by package name; ns rides --registry
    rows: list[tuple] = []
    unknown = set(want)
    for entry in entries:
        try:
            reg, _ = open_registry(entry)
        except RigError as exc:
            from .common import eprint
            eprint(f"  (skipping '{entry.name}': {exc})")
            continue
        for name in sorted(reg.packages):
            short = split_key(name)[1]  # profiles: `svc:short` — accept the short half too
            if want and name not in want and short not in want:
                continue
            unknown.discard(name)
            unknown.discard(short)
            rows.extend(_sweep_package(f"{entry.name}/{name}", reg.packages[name], reg, entry.name))
    if unknown:
        raise RigError(f"pkg outdated: not found in the swept registr"
                       f"{'y' if len(entries) == 1 else 'ies'}: {', '.join(sorted(unknown))}")
    shown = [r for r in rows if not (quiet and r[0] == _INFO)]
    drift = [r for r in rows if r[0] == _DRIFT]
    if not shown:
        print("everything current" + (" (INFO rows hidden)" if quiet and rows else ""))
        return 1 if drift else 0
    print_table([("PACKAGE", "KIND", "DEPENDENCY", "PINNED", "CURRENT", "FIX")]
                + [r[1:] for r in shown])
    print(f"\n({len(drift)} drift row(s); consumers follow a repair with "
          f"`rig registry sync && rig pkg upgrade`)" if drift else
          "\n(no drift — INFO rows only)")
    return 1 if drift else 0
