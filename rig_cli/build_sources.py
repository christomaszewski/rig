"""The git sources a deployment's builds need, all resolved through the local mirrors (gitmirror).

Three kinds, gathered once by ``requirements``:

  pin    a registry service's pinned source (rig.lock) — checked out when its routed dir is a
         vendored surface without build context (build._resolve_build_cwd)
  msgs   the msgs overlay's ``source:`` repos (build.msgs_union) — exported for the overlay build
  build  a rigging's ``build.sources`` — exported for that service's own build

``rig src sync`` fetches them all (submodules included) while online; ``rig build --offline``
checks them up front with ``offline_problems`` — every missing repo and ref in one report, before
anything builds — and every build receives its trees from ``export`` as ``$RIG_BUILD_SOURCES``:
one directory per source plus ``sources.tsv`` (name, repo, ref, commit), so no build fetches git
itself.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

from . import RigError, gitmirror
from .descriptor import Descriptor
from .manifest import Manifest


@dataclasses.dataclass(frozen=True)
class Requirement:
    kind: str    # pin | msgs | build
    owner: str   # the service, or "msgs overlay"
    repo: str
    ref: str
    name: str = ""  # the export directory (msgs, build)


def build_requirements(service: str, desc: Descriptor) -> list[Requirement]:
    return [Requirement("build", service, s.repo, s.ref, s.name) for s in desc.build_sources]


def msgs_requirements(union: dict | None) -> list[Requirement]:
    return [Requirement("msgs", "msgs overlay", e["repo"], e["ref"], f"m{i}")
            for i, e in enumerate((union or {}).get("source") or [])]


def requirements(manifest: Manifest, descriptors: dict[str, Descriptor],
                 root: Path | None) -> list[Requirement]:
    from .build import msgs_union, pinned_source
    used = {s.service for s in manifest.sensors}
    descs = {s: d for s, d in sorted(descriptors.items()) if s in used}
    reqs: list[Requirement] = []
    for service, desc in descs.items():
        if not desc.build_command:
            continue
        needed, source = pinned_source(service, desc, root)
        if needed and source:
            reqs.append(Requirement("pin", service, str(source["repo"]), str(source["rev"])))
        reqs += build_requirements(service, desc)
    if any(d.msgs_overlay_command for d in descs.values()):
        union, error = msgs_union(descs)
        if error:
            raise RigError(error)
        reqs += msgs_requirements(union)
    return reqs


def _label(req: Requirement) -> str:
    what = {"pin": "pinned source", "msgs": "msgs source", "build": f"build source '{req.name}'"}
    return f"{req.owner}: {what[req.kind]} {req.repo} @ {req.ref}"


def _cached(req: Requirement) -> bool:
    """A pin already checked out in the src cache needs no mirror at all."""
    from .install import _repo_slug
    from .registries import rig_home
    dest = rig_home() / "cache" / "src" / f"{_repo_slug(req.repo)}-{req.ref[:12]}"
    return (dest / ".git").is_dir()


def sync(reqs: list[Requirement]) -> tuple[list[str], list[str]]:
    """Fetch every requirement into its mirror (online). Returns (done lines, problems)."""
    done, problems = [], []
    for req in reqs:
        try:
            sha = gitmirror.resolve(req.repo, req.ref)
            if req.kind in ("pin", "build"):  # both check out submodules; msgs exports don't
                gitmirror.sync_submodules(req.repo, sha)
            done.append(f"{_label(req)} -> {sha[:12]}")
        except RigError as exc:
            problems.append(f"{_label(req)}: {exc}")
    return done, problems


def offline_problems(reqs: list[Requirement]) -> list[str]:
    """Everything an offline build would miss, checked without fetching."""
    was, gitmirror.offline = gitmirror.offline, True
    try:
        problems = []
        for req in reqs:
            if req.kind == "pin" and _cached(req):
                continue
            try:
                sha = gitmirror.resolve(req.repo, req.ref)
                if req.kind in ("pin", "build"):
                    gitmirror.sync_submodules(req.repo, sha)
            except RigError as exc:
                problems.append(f"{_label(req)}: {exc}")
        return problems
    finally:
        gitmirror.offline = was


def export(reqs: list[Requirement], dest: Path) -> Path:
    """Export each msgs/build requirement's tree into ``dest/<name>`` and index them in
    ``dest/sources.tsv`` (name, repo, ref, commit) — the $RIG_BUILD_SOURCES contract."""
    from .common import eprint
    dest.mkdir(parents=True, exist_ok=True)
    rows = []
    for req in reqs:
        sha = gitmirror.resolve(req.repo, req.ref)
        gitmirror.export(req.repo, sha, dest / req.name, submodules=req.kind == "build")
        rows.append("\t".join((req.name, req.repo, req.ref, sha)))
        eprint(f"rig build: {_label(req)} -> {sha[:12]} (from the local mirror)")
    (dest / "sources.tsv").write_text("".join(row + "\n" for row in rows))
    return dest
