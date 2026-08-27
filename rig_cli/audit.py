"""``rig image audit`` — deployment-wide image *content* consistency checks.

`doctor` checks the manifest, `certify` checks each launcher; audit checks what actually RUNS: the
images the deployment's stacks resolve to. It renders each enabled stack's compose (the launcher's
`config` verb under the same fleet env `up` exports — so per-instance ``${<SVC>_IMAGE}`` overrides
and composed platform tags are honored), collects the `image:` refs, inspects each unique image with
docker (`ls /opt/ros` + the dpkg ``ros-*`` package list), and cross-checks:

  - distro     every ROS image carries vehicle.yaml ``ros.distro`` (a stray humble image on a
               lyrical vehicle breaks the shared graph at runtime, silently)
  - rmw        the declared ``ros.rmw`` package (``ros-<distro>-rmw-…``) is installed in every
               image matching the declared distro — nodes fall back to another RMW otherwise
  - versions   every ``ros-*`` package present in two or more images has ONE version across them
               (ERROR). Independent images apt-install at different times against a moving ros repo,
               and a skewed rmw package between two images means sessions that can't talk. A shared
               base (RIG_BASE_IMAGE) prevents the skew for base packages — PROVIDED the consumer
               doesn't reinstall them (plain ``apt-get install`` of a package the base already
               carries silently upgrades it; consumers use ``--no-upgrade``). ``rig build
               --no-cache`` re-converges a fleet that already drifted (base first, then every
               consumer, in one invocation). Non-``ros-*`` packages diverging across the ROS images
               are reported as ONE summarized WARN, never an error: ubuntu revision bumps are
               usually benign, but ABI-relevant divergence (libstdc++, boost, an image codec a ROS
               node links against) can still break nodes — the WARN is the clue, not a verdict.

  - msgs      when the deployment exports RIG_MSGS_IMAGE (the fleet-ros-msgs overlay), the
              overlay's baked ``/opt/fleet-msgs/manifest.yaml`` must equal the CURRENT union of
              the riggings' ``msgs:`` declarations (ERROR on drift — the stale-overlay case: a
              declaration added or a pin bumped, ``rig build`` forgotten, ``up`` pulls the old
              image under the same tag and the new types silently vanish from bags), and every
              declared ``apt`` interface package must be installed in it (the same
              ``ros-<distro>-<'_'→'-'>`` mapping the builder uses).
  - pins      each ``msgs.source`` pin against what the declaring service's OWN image actually
              built, via the baked ``/opt/fleet-msgs/provenance.yaml`` (the rig-infra ≥ v1.6.0
              convention: repo/ref/rev per built interface repo — the overlay always bakes it,
              services adopt via provenance-record.sh). Absent → WARN (unadopted, pin skew
              unverifiable); a declared repo missing from a present file, a ref mismatch, or —
              refs equal — service and overlay `rev` SHAs differing (a moved tag, a branch built
              twice: the tier only SHAs give) → ERROR; ``rev: unknown`` (vendored snapshot) or a
              malformed file → WARN, never ERROR.

Non-ROS images (no /opt/ros, no ros-* packages) are excluded from the checks; an image without a
shell/dpkg is reported and skipped (uninspectable this way); a ROS image with zero ros-* dpkg
packages (source-built workspace) gets a WARN — dpkg cannot see inside it. Needs docker and the
images local or pullable: run right after ``rig build`` (or ``rig pull``).
"""
from __future__ import annotations

import re
import shutil
import subprocess

import yaml

from .common import eprint
from .descriptor import Descriptor
from .dispatch import launcher_cmd, run as run_launcher
from .manifest import Manifest

ERROR, WARN, INFO, OK = "ERROR", "WARN", "INFO", "OK"
_SYMBOL = {ERROR: "✗", WARN: "!", INFO: "·", OK: "✓"}

_MARK = "::rig-image-audit::"
# One in-container probe: the /opt/ros distro dirs, a marker, then `pkg version` lines. Runs under
# /bin/sh (every debian/ubuntu ROS image has it); dpkg-query errors (no matches, no dpkg) are
# normalized away — absence is DATA here, not failure.
# The FULL package list, not just ros-*: the cross-image WARN covers system libs too (the ros-*
# subset is split out in parsing — it alone decides ROS-ness and feeds the ERROR checks).
_INSPECT_SH = (f"ls /opt/ros 2>/dev/null; echo '{_MARK}'; "
               "dpkg-query -W -f '${Package} ${Version}\\n' 2>/dev/null; true")
_DOCKER_TIMEOUT = 600  # seconds; covers a cold pull of a multi-GB image, not a hung daemon forever


def _inspect(ref: str) -> tuple[list[str], dict[str, str], dict[str, str], str | None]:
    """(distro dirs under /opt/ros, ros-* pkg -> version, OTHER pkg -> version, error).
    The ros subset alone classifies an image as ROS and feeds the ERROR checks — a plain debian
    image full of system packages must stay non-ROS. error set = uninspectable."""
    cmd = ["docker", "run", "--rm", "--entrypoint", "/bin/sh", ref, "-c", _INSPECT_SH]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=_DOCKER_TIMEOUT)
    except subprocess.TimeoutExpired:
        return [], {}, {}, f"inspect timed out after {_DOCKER_TIMEOUT}s"
    if proc.returncode != 0 or _MARK not in proc.stdout:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        return [], {}, {}, (detail[-1][:160] if detail else f"docker run exited {proc.returncode}")
    head, tail = proc.stdout.split(_MARK, 1)
    distros = [ln.strip() for ln in head.splitlines() if ln.strip()]
    ros_pkgs: dict[str, str] = {}
    sys_pkgs: dict[str, str] = {}
    for ln in tail.splitlines():
        parts = ln.strip().split(None, 1)
        if len(parts) == 2:
            (ros_pkgs if parts[0].startswith("ros-") else sys_pkgs)[parts[0]] = parts[1]
    return distros, ros_pkgs, sys_pkgs, None


# The msgs files a fleet-ros-msgs overlay (and, for provenance, any participating service image)
# bakes. Probed together in one docker run — the script marker also lets test stubs tell this
# probe apart from the dpkg inspection above.
_MSGS_MANIFEST = "/opt/fleet-msgs/manifest.yaml"
_MSGS_PROVENANCE = "/opt/fleet-msgs/provenance.yaml"
_FILE_MARK = "::rig-msgs-file::"
_FILE_ABSENT = "::rig-msgs-absent::"


def _read_msgs_files(ref: str) -> tuple[dict[str, str | None], str | None]:
    """({path: content or None when absent}, error). One probe per image for both msgs files —
    absence is DATA (None), only a docker failure is an error."""
    script = "; ".join(
        f"echo '{_FILE_MARK}{p}'; if [ -f {p} ]; then cat {p}; echo; else echo '{_FILE_ABSENT}'; fi"
        for p in (_MSGS_MANIFEST, _MSGS_PROVENANCE))
    cmd = ["docker", "run", "--rm", "--entrypoint", "/bin/sh", ref, "-c", script]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=_DOCKER_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {}, f"probe timed out after {_DOCKER_TIMEOUT}s"
    if proc.returncode != 0 or _FILE_MARK not in proc.stdout:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        return {}, (detail[-1][:160] if detail else f"docker run exited {proc.returncode}")
    files: dict[str, str | None] = {}
    for chunk in proc.stdout.split(_FILE_MARK)[1:]:
        path, _, body = chunk.partition("\n")
        files[path.strip()] = None if _FILE_ABSENT in body else body
    return files, None


def _norm_repo(url: str) -> str:
    """The provenance-contract join normalization (rig-msgs-provenance-handoff.md §A3): drop the
    scheme (and any userinfo), rewrite the scp form ``git@host:path`` -> ``host/path``, drop ONE
    trailing ``.git``, lowercase the host — https/ssh spellings of one repo match; genuinely
    different remotes do NOT (a mirror names itself in `cloned_from`, which audit ignores)."""
    u = url.strip()
    m = re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://(.*)$", u)
    if m:
        u = re.sub(r"^[^/@]+@", "", m.group(1))
    else:
        m = re.match(r"^[^/@]+@([^:/]+):(.*)$", u)
        if m:
            u = f"{m.group(1)}/{m.group(2)}"
    if u.endswith(".git"):
        u = u[:-4]
    host, sep, path = u.partition("/")
    return host.lower() + sep + path


def _norm_msgs_manifest(data: dict) -> tuple[list[str], dict[str, tuple[str, tuple[str, ...]]]]:
    """(sorted apt names, normalized-repo -> (ref, sorted packages)) — order- and
    spelling-insensitive, so a hand-authored baked manifest compares fairly against rig's
    rendered union."""
    apt = sorted(str(a) for a in (data.get("apt") or []))
    src: dict[str, tuple[str, tuple[str, ...]]] = {}
    for entry in (data.get("source") or []):
        if isinstance(entry, dict) and entry.get("repo"):
            src[_norm_repo(str(entry["repo"]))] = (
                str(entry.get("ref")), tuple(sorted(str(p) for p in (entry.get("packages") or []))))
    return apt, src


def _msgs_stale_issues(manifest: Manifest, descriptors: dict[str, Descriptor], msgs_ref: str,
                       msgs_files: dict[str, str | None],
                       pkgs_by_ref: dict[str, dict[str, str]]) -> list[tuple[str, str]]:
    """The stale-overlay check: the overlay in the registry is whatever the LAST `rig build` baked
    — a `msgs:` declaration added or a pin bumped since then leaves `up` pulling the old image
    under the same tag, and the new types silently vanish from bags. The baked manifest is the
    build's declared union verbatim, so diffing it against the CURRENT union is exact."""
    from .build import msgs_union
    issues: list[tuple[str, str]] = []
    union, _ = msgs_union(descriptors)  # fleet_env exported RIG_MSGS_IMAGE, so the union is valid
    label = f"msgs overlay {msgs_ref}"

    baked_text = msgs_files.get(_MSGS_MANIFEST)
    if baked_text is None:
        issues.append((WARN, f"{label}: no baked {_MSGS_MANIFEST} — not a fleet-ros-msgs build? "
                             f"staleness unverifiable"))
    else:
        try:
            baked = yaml.safe_load(baked_text)
        except yaml.YAMLError:
            baked = None
        if not isinstance(baked, dict):
            issues.append((WARN, f"{label}: baked {_MSGS_MANIFEST} is not valid YAML — "
                                 f"staleness unverifiable"))
        else:
            b_apt, b_src = _norm_msgs_manifest(baked)
            c_apt, c_src = _norm_msgs_manifest(union or {})
            diffs: list[str] = []
            for name in c_apt:
                if name not in b_apt:
                    diffs.append(f"apt +{name}")
            for name in b_apt:
                if name not in c_apt:
                    diffs.append(f"apt -{name}")
            for repo in c_src:
                if repo not in b_src:
                    diffs.append(f"source +{repo}")
                elif b_src[repo][0] != c_src[repo][0]:
                    diffs.append(f"{repo}: baked ref '{b_src[repo][0]}' vs declared "
                                 f"'{c_src[repo][0]}'")
                elif b_src[repo][1] != c_src[repo][1]:
                    diffs.append(f"{repo}: baked packages {list(b_src[repo][1])} vs declared "
                                 f"{list(c_src[repo][1])}")
            for repo in b_src:
                if repo not in c_src:
                    diffs.append(f"source -{repo}")
            if diffs:
                issues.append((ERROR, f"{label} is STALE: its baked manifest differs from the "
                                      f"current `msgs:` declarations ({'; '.join(diffs)}) — "
                                      f"`rig build` rebuilds it; until then bags silently miss "
                                      f"the changed types"))
            else:
                issues.append((OK, f"{label}: baked manifest matches the current `msgs:` "
                                   f"declarations"))

    # Declared apt interface packages must be INSTALLED in the overlay — same name mapping the
    # builder uses, so builder and checker agree by construction (the rmw-check pattern).
    distro = manifest.ros.distro
    apt_names = sorted((union or {}).get("apt") or [])
    if distro and apt_names:
        pkgs = pkgs_by_ref.get(msgs_ref)
        if pkgs is None:
            _, pkgs, _, err = _inspect(msgs_ref)
            if err:
                issues.append((WARN, f"{label}: uninspectable for the apt check — {err}"))
                pkgs = None
        if pkgs is not None:
            missing = [n for n in apt_names
                       if f"ros-{distro}-{n.replace('_', '-')}" not in pkgs]
            if missing:
                issues.append((ERROR, f"{label}: declared apt interface package(s) not installed: "
                                      f"{', '.join(missing)} — the recorder cannot record their "
                                      f"types; `rig build` rebuilds the overlay"))
            else:
                issues.append((OK, f"{label}: all {len(apt_names)} declared apt interface "
                                   f"package(s) installed"))
    return issues


def _parse_provenance(text: str):
    """(normalized repo -> entry dict, malformed-reason | None). The contract (§A4): an
    unparseable file, wrong `version`, a non-list `source`, an entry missing repo/ref/rev, or two
    entries collapsing to one normalized repo are MALFORMED — reported as WARN by the caller,
    never ERROR (the service-side helper appends blindly and never dedupes, so duplicates are the
    expected misuse; a helper mistake must not fail a fleet)."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return {}, "not valid YAML"
    if not isinstance(data, dict):
        return {}, "not a YAML mapping"
    if data.get("version") != 1:
        return {}, f"unknown schema version {data.get('version')!r} (expected 1)"
    source = data.get("source")
    if source is None:
        source = []
    if not isinstance(source, list):
        return {}, "`source` is not a list"
    entries: dict[str, dict] = {}
    for i, entry in enumerate(source):
        if not isinstance(entry, dict) or not all(entry.get(k) for k in ("repo", "ref", "rev")):
            return {}, f"source #{i} lacks repo/ref/rev"
        key = _norm_repo(str(entry["repo"]))
        if key in entries:
            return {}, f"duplicate entries for {key}"
        entries[key] = entry
    return entries, None


def _msgs_provenance_issues(descriptors: dict[str, Descriptor], svc_refs: dict[str, list[str]],
                            prov_by_ref: dict[str, tuple[dict, str | None] | None],
                            overlay: dict[str, dict]) -> list[tuple[str, str]]:
    """The pin-skew tiers (rig-msgs-provenance-handoff.md §A4): each `msgs.source` pin vs what the
    declaring service's OWN image recorded it built. `prov_by_ref` maps every probed image ref to
    (parsed entries, malformed-reason) — or None when the file is absent; `overlay` is the overlay
    image's parsed provenance ({} when absent/malformed — the SHA tier just doesn't fire).
    A drifted pin means the overlay's definitions are wire-incompatible with what the service
    publishes; equal refs with different SHAs (a moved tag, a branch built twice) are the drift
    only this check can see."""
    issues: list[tuple[str, str]] = []
    verified = sha_checked = 0
    for svc in sorted(s for s, d in descriptors.items() if d.msgs_source):
        readable = [(ref, prov_by_ref[ref]) for ref in svc_refs.get(svc, [])
                    if ref in prov_by_ref]  # uninspectable images were already WARNed upstream
        if not readable:
            continue  # no rendered image to check against (config failure already reported)
        present = [(ref, parsed) for ref, parsed in readable if parsed is not None]
        if not present:
            issues.append((WARN, f"{svc}: declares msgs.source but none of its image(s) "
                                 f"({', '.join(r for r, _ in readable)}) bake "
                                 f"{_MSGS_PROVENANCE} — pin skew unverifiable; adopt "
                                 f"msgs/provenance-record.sh (rig-infra >= v1.6.0)"))
            continue
        valid: list[tuple[str, dict[str, dict]]] = []
        for ref, (entries, malformed) in present:
            if malformed:
                issues.append((WARN, f"{svc}: {ref}: malformed provenance ({malformed}) — "
                                     f"unverifiable"))
            else:
                valid.append((ref, entries))
        if not valid:
            continue
        for src in descriptors[svc].msgs_source:
            key = _norm_repo(src.repo)
            matches = [(ref, entries[key]) for ref, entries in valid if key in entries]
            if not matches:
                issues.append((ERROR, f"{svc}: {src.repo} is declared in msgs.source but no image "
                                      f"provenance records building it "
                                      f"({', '.join(r for r, _ in valid)}) — the image didn't "
                                      f"build what the rigging declares"))
                continue
            for ref, entry in matches:
                got_ref, got_rev = str(entry["ref"]), str(entry["rev"])
                if got_ref != src.ref:
                    issues.append((ERROR, f"{svc}: {src.repo} — rigging declares ref '{src.ref}' "
                                          f"but {ref} built '{got_ref}'; a drifted pin is a "
                                          f"silent schema mismatch in the bags — align them in "
                                          f"one change, then `rig build`"))
                    continue
                if got_rev == "unknown":
                    issues.append((WARN, f"{svc}: {src.repo} @ {src.ref} in {ref} — rev unknown "
                                         f"(vendored snapshot): pin verified by ref only"))
                    verified += 1
                    continue
                verified += 1
                over = overlay.get(key)
                over_rev = str(over["rev"]) if over else None
                if over_rev and over_rev != "unknown":
                    sha_checked += 1
                    if over_rev != got_rev:
                        issues.append((ERROR, f"{svc}: {src.repo} @ {src.ref} — same ref, "
                                              f"different tree: {ref} built {got_rev[:12]}, the "
                                              f"overlay baked {over_rev[:12]} (a moved tag, or a "
                                              f"branch built twice) — rebuild the older side"))
    if verified and not any(lvl == ERROR for lvl, _ in issues):
        issues.append((OK, f"{verified} msgs.source pin(s) verified against image provenance "
                           f"({sha_checked} to the SHA tier)"))
    return issues


def _collect_refs(manifest: Manifest, descriptors: dict[str, Descriptor], env: dict[str, str],
                  names: list[str]) -> tuple[dict[str, list[str]], dict[str, list[str]], list[str]]:
    """Render each enabled stack's compose; return (image ref -> stack names,
    service -> its image refs, per-stack failures). The service map keys the provenance check —
    a `msgs.source` pin is audited against the declaring SERVICE's own images."""
    from .bake import _service_images
    refs: dict[str, list[str]] = {}
    svc_refs: dict[str, list[str]] = {}
    failures: list[str] = []
    for sensor in manifest.select(names, enabled_only=True):
        desc = descriptors[sensor.service]
        proc = run_launcher(sensor, desc, env, launcher_cmd(sensor, desc, "config"),
                            dry_run=False, capture=True)
        compose = None
        if proc.returncode == 0 and (proc.stdout or "").strip():
            try:
                compose = yaml.safe_load(proc.stdout)
            except yaml.YAMLError:
                compose = None
        if not (isinstance(compose, dict) and compose.get("services")):
            failures.append(f"{sensor.name}: launcher `config` produced no compose "
                            f"({(proc.stderr or '').strip()[:120] or f'exit {proc.returncode}'})")
            continue
        for ref in _service_images(compose).values():
            stacks = refs.setdefault(ref, [])
            if sensor.name not in stacks:
                stacks.append(sensor.name)
            by_svc = svc_refs.setdefault(sensor.service, [])
            if ref not in by_svc:
                by_svc.append(ref)
    return refs, svc_refs, failures


def audit(manifest: Manifest, descriptors: dict[str, Descriptor], env: dict[str, str], *,
          names: list[str] | None = None) -> int:
    if shutil.which("docker") is None:
        eprint("rig image audit: docker not found on PATH — audit inspects images with `docker run`")
        return 1
    refs, svc_refs, failures = _collect_refs(manifest, descriptors, env, names or [])
    if not refs and not failures:
        eprint("rig image audit: no enabled stack resolves any image — nothing to audit")
        return 0

    issues: list[tuple[str, str]] = [(WARN, f) for f in failures]
    distro = manifest.ros.distro
    rmw_pkg = (f"ros-{distro}-{manifest.ros.rmw.replace('_', '-')}"
               if distro and manifest.ros.rmw else None)
    ros_images: list[tuple[str, list[str], dict[str, str], dict[str, str]]] = []
    distro_bad = rmw_bad = 0
    for ref, stacks in refs.items():
        distros, pkgs, sys_pkgs, err = _inspect(ref)
        label = f"{ref} [{', '.join(stacks)}]"
        if err:
            issues.append((WARN, f"{label}: uninspectable (no shell/dpkg? unpullable?) — {err}"))
            continue
        if not distros and not pkgs:  # ROS-ness keys on /opt/ros + ros-* ONLY — a plain debian
            #                           image is full of system packages and must stay excluded
            issues.append((INFO, f"{label}: non-ROS image — excluded from the ROS checks"))
            continue
        ros_images.append((ref, distros, pkgs, sys_pkgs))
        if distro and distros and distro not in distros:
            distro_bad += 1
            issues.append((ERROR, f"{label}: carries ROS distro {distros}, but vehicle.yaml "
                                  f"ros.distro is '{distro}' — rebuild it for the fleet's distro"))
        if distros and not pkgs:
            issues.append((WARN, f"{label}: /opt/ros/{{{', '.join(distros)}}} exists but zero "
                                 f"ros-* dpkg packages — source-built ROS? the package checks "
                                 f"cannot see it"))
        elif rmw_pkg and (not distros or distro in distros) and rmw_pkg not in pkgs:
            rmw_bad += 1
            issues.append((ERROR, f"{label}: declared rmw '{manifest.ros.rmw}' is not installed "
                                  f"({rmw_pkg} missing) — its nodes will run a different RMW than "
                                  f"the fleet graph"))

    # Cross-image agreement: any ros-* package that appears with two different versions is skew —
    # exactly the "two images, two rmw_zenoh_cpp builds, no session" failure this exists to catch.
    by_pkg: dict[str, dict[str, list[str]]] = {}  # pkg -> version -> image refs
    for ref, _, pkgs, _sys in ros_images:
        for pkg, ver in pkgs.items():
            by_pkg.setdefault(pkg, {}).setdefault(ver, []).append(ref)
    shared = {p: v for p, v in by_pkg.items() if sum(len(r) for r in v.values()) > 1}
    skewed = {p: v for p, v in shared.items() if len(v) > 1}
    base_ref = env.get("RIG_BASE_IMAGE")  # fleet_env resolved it (images.base or the provider)
    for pkg, versions in sorted(skewed.items()):
        detail = "; ".join(f"{ver} in {', '.join(sorted(refs_))}"
                           for ver, refs_ in sorted(versions.items()))
        hint = ""
        if base_ref and any(base_ref in refs_ for refs_ in versions.values()):
            # rig KNOWS which image is the base, so it can diagnose, not just advise: an image
            # built FROM the base that apt-installs this package explicitly has upgraded it
            # (plain `apt-get install` of an already-installed package pulls the repo's current
            # candidate). `--no-upgrade` keeps base-pinned packages pinned.
            hint = (f" — {base_ref} is the deployment base; an image built FROM it that installs "
                    f"this package explicitly should use `apt-get install --no-upgrade` "
                    f"(then `rig build --no-cache` to re-converge)")
        issues.append((ERROR, f"version skew: {pkg} — {detail}{hint}"))

    # Non-ros-* divergence across the SAME ROS images: one summarized WARN, never an error, never
    # per-package lines. Ubuntu revision bumps (3ubuntu4 -> 3ubuntu5) are usually benign — but a
    # diverging libstdc++/boost/codec that ROS nodes link against is a real ABI hazard the ros-*
    # check cannot see, and "versions agree" must not certify it as clean silently.
    by_sys: dict[str, dict[str, list[str]]] = {}
    for ref, _, _pkgs, sys_pkgs in ros_images:
        for pkg, ver in sys_pkgs.items():
            by_sys.setdefault(pkg, {}).setdefault(ver, []).append(ref)
    sys_skewed = {p: v for p, v in by_sys.items() if len(v) > 1}
    if sys_skewed:
        sample = ", ".join(f"{p} ({' vs '.join(sorted(v))})" for p, v in sorted(sys_skewed.items())[:3])
        more = f", +{len(sys_skewed) - 3} more" if len(sys_skewed) > 3 else ""
        issues.append((WARN, f"{len(sys_skewed)} non-ROS package(s) differ across the ROS images "
                             f"(e.g. {sample}{more}) — usually benign revision drift, but "
                             f"ABI-relevant divergence can still break nodes; `rig build "
                             f"--no-cache` re-converges"))

    # The msgs overlay: exported by fleet_env only when services declare `msgs:` AND a base
    # provider wires the overlay build — so its presence in the env IS the trigger. Audited even
    # when no rendered compose pulls it (a BAG_LOGGER_IMAGE override, say): the export makes it
    # the deployment's overlay, and any consumer that appears next pulls exactly this ref.
    msgs_ref = env.get("RIG_MSGS_IMAGE")
    overlay_prov: dict[str, dict] = {}
    if msgs_ref:
        msgs_files, msgs_err = _read_msgs_files(msgs_ref)
        if msgs_err:
            issues.append((WARN, f"msgs overlay {msgs_ref}: uninspectable (unpullable? no shell?) "
                                 f"— {msgs_err}"))
        else:
            pkgs_by_ref = {r: p for r, _, p, _ in ros_images}
            issues += _msgs_stale_issues(manifest, descriptors, msgs_ref, msgs_files, pkgs_by_ref)
            any_source = any(d.msgs_source for d in descriptors.values())
            prov_text = msgs_files.get(_MSGS_PROVENANCE)
            if prov_text is None:
                # rig-infra >= v1.6.0 overlays ALWAYS bake the file (`source: []` when apt-only) —
                # absence means a pre-provenance build, never a source-less union (contract §A2).
                if any_source:
                    issues.append((WARN, f"msgs overlay {msgs_ref}: no baked {_MSGS_PROVENANCE} — "
                                         f"pre-provenance overlay build (rig-infra < v1.6.0); "
                                         f"`rig build` re-bakes it and enables the SHA tier"))
            else:
                overlay_prov, malformed = _parse_provenance(prov_text)
                if malformed:
                    issues.append((WARN, f"msgs overlay {msgs_ref}: malformed provenance "
                                         f"({malformed}) — SHA tier unavailable"))

    # The pin-skew tiers: probe provenance on each msgs.source-declaring service's own image(s).
    prov_svcs = sorted(s for s, d in descriptors.items() if d.msgs_source and s in svc_refs)
    if prov_svcs:
        prov_by_ref: dict[str, tuple[dict, str | None] | None] = {}
        for svc in prov_svcs:
            for ref in svc_refs[svc]:
                if ref in prov_by_ref:
                    continue
                files, err = _read_msgs_files(ref)
                if err:
                    issues.append((WARN, f"{svc}: {ref}: provenance unreadable — {err}"))
                    continue
                text = files.get(_MSGS_PROVENANCE)
                prov_by_ref[ref] = None if text is None else _parse_provenance(text)
        issues += _msgs_provenance_issues(descriptors, svc_refs, prov_by_ref, overlay_prov)

    if ros_images and distro and not distro_bad:
        issues.append((OK, f"every ROS image carries /opt/ros/{distro}"))
    if ros_images and rmw_pkg and not rmw_bad:
        issues.append((OK, f"{rmw_pkg} present in every matching ROS image"))
    if shared and not skewed:
        issues.append((OK, f"{len(shared)} ros-* package(s) shared across images — versions agree"))
    elif len(ros_images) == 1:
        # One ROS image has nothing to disagree with. Say it explicitly: a missing "versions agree"
        # line is the contract working (a shared base collapses the fleet to one ROS layer), not a
        # check that silently didn't run.
        issues.append((INFO, "one ROS image in this deployment — no cross-image comparison to make "
                             "(nothing to skew against)"))

    errors = sum(1 for lvl, _ in issues if lvl == ERROR)
    eprint(f"rig image audit: {manifest.vehicle} — {len(refs)} image ref(s), "
           f"{len(ros_images)} ROS, {errors} error(s)")
    for lvl, msg in issues:
        eprint(f"  [{_SYMBOL[lvl]}] {msg}")
    if not issues:
        eprint("  [✓] nothing to check")
    return 1 if errors else 0
