"""``rig certify`` — executable conformance checks for the launcher contract.

`doctor` checks the *vehicle* (is this manifest coherent); certify checks the *service* (is this launcher a
well-behaved rig citizen). It runs the launcher's ``config`` verb under a **poisoned fleet env** — values
that cannot occur in real life (``certify.invalid:5000``, ``certify-tag-x``, instance name
``certifyname0``) — and asserts the contract held by looking for the poison in the resolved compose:

  - discipline       `config` exits 0 and stdout is clean compose YAML (human lines belong on stderr)
  - project-name     the compose project is COMPOSE_PROJECT_NAME (a launcher must not pass `-p`)
  - registry         images are pulled from RIG_IMAGE_REGISTRY (ERROR for images the service builds or
                     mirrors; WARN otherwise — upstream pulls work only on an internet-connected vehicle)
  - tag              any image the service *builds* is pulled as `:RIG_IMAGE_TAG` (build/pull agreement)
  - ros-env          ROS_DOMAIN_ID / RMW_IMPLEMENTATION reach some container (ROS services only)
  - volumes          rigging.yaml `external_volumes` patterns match the compose's external volumes
  - determinism      same config + same env -> byte-identical output (no timestamps, no host probing)
  - identity         rename the instance and the old name vanishes (nothing about identity is hardcoded)
  - binds            warn on bind mounts into the service repo (recordings belong under RIG_DATA_DIR;
                     bake would freeze a repo-relative dir into the artifact)
  - status           the `status` verb emits parseable `ps --format json` output

Certify is per-service and host-agnostic, so it runs in a service repo's CI with no deployment tree
(``rig certify --repo . --config examples/usb.yaml``). One contract property a single machine cannot prove
is host-independence of *fallbacks* (JetPack detection, device probing): for that, ``--emit`` writes the
normalized resolved compose, and ``rig certify --diff a.yaml b.yaml`` compares two emits — run it on the
dev box and on the vehicle; byte-identical means a dev-box bake is correct for the target.
"""
from __future__ import annotations

import difflib
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml

from . import RigError
from .bake import _repo_of, _service_images, _services
from .common import eprint, load_yaml
from .descriptor import Descriptor
from .manifest import project_name
from .status import _parse_ps

ERROR, WARN, INFO, OK = "ERROR", "WARN", "INFO", "OK"
_SYMBOL = {ERROR: "✗", WARN: "!", INFO: "·", OK: "✓"}

# Poison values: unambiguous (can't collide with real config) and unpullable (.invalid is RFC 2606
# reserved), so their presence/absence in the launcher's output is proof, not coincidence.
POISON_REGISTRY = "certify.invalid:5000"
POISON_TAG = "certify-tag-x"
POISON_DATA = "/certify/data"
POISON_VID = "42"
POISON_RMW = "rmw_certify_cpp"
NAME_A, NAME_B = "certifyname0", "certifyname1"

# `docker compose config` output is exactly these top-level keys; anything else on stdout is launcher
# chatter that belongs on stderr (a bare "tool: doing x" line parses as YAML — a mapping with a junk key —
# so a parse check alone would miss it).
_COMPOSE_TOP_KEYS = {"name", "version", "services", "networks", "volumes", "configs", "secrets", "include"}

_LAUNCHER_TIMEOUT = 60  # seconds; a hung launcher must not hang certify (or doctor --deep)


@dataclass
class Check:
    level: str
    name: str
    detail: str = ""


def _poison_env(base_env: dict[str, str], name: str) -> dict[str, str]:
    """The fleet env rig would export, with every contract variable set to its poison value."""
    return {
        **base_env,
        "ROS_DOMAIN_ID": POISON_VID,
        "RMW_IMPLEMENTATION": POISON_RMW,
        "VEHICLE_ID": POISON_VID,
        "RIG_IMAGE_REGISTRY": POISON_REGISTRY,
        "RIG_IMAGE_TAG": POISON_TAG,
        "RIG_DATA_DIR": POISON_DATA,
        "COMPOSE_PROJECT_NAME": project_name(name, POISON_VID),
    }


def _write_named_config(base: dict, name: str, service: str, tmp: Path) -> Path:
    """A copy of the instance config with the poison `name` stamped in (the same injection `rig up`
    performs for profiles). Writing it to a temp dir also exercises 'accept a config at an arbitrary
    host path' from the contract."""
    cfg = dict(base)
    cfg["name"] = name
    cfg.setdefault("service", service)
    out = tmp / f"{name}.yaml"
    out.write_text(yaml.safe_dump(cfg, sort_keys=False))
    return out


def _run_launcher(desc: Descriptor, config: Path, verb_args: list[str], env: dict[str, str]):
    cmd = [str(desc.launcher_path), str(config), *verb_args]
    try:
        return subprocess.run(cmd, env=env, cwd=str(desc.repo), capture_output=True, text=True,
                              timeout=_LAUNCHER_TIMEOUT)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, returncode=124, stdout="",
                                           stderr=f"timed out after {_LAUNCHER_TIMEOUT}s")


def _ref_parts(ref: str) -> tuple[str, str]:
    """(repo-sans-poison-registry, tag) for an image ref. '' tag when none/digest-pinned."""
    ref = ref.split("@", 1)[0]
    repo, tag = _repo_of(ref), ""
    if len(ref) > len(repo):
        tag = ref[len(repo) + 1:]
    if repo.startswith(POISON_REGISTRY + "/"):
        repo = repo[len(POISON_REGISTRY) + 1:]
    return repo, tag


def _env_of(svc: dict) -> dict[str, str]:
    """A service's `environment` as a dict (compose config emits a map; tolerate the list form)."""
    env = svc.get("environment") or {}
    if isinstance(env, list):
        pairs = [str(e).split("=", 1) for e in env]
        return {p[0]: (p[1] if len(p) > 1 else "") for p in pairs}
    return {str(k): ("" if v is None else str(v)) for k, v in env.items()}


def _bind_sources(compose: dict) -> list[tuple[str, str]]:
    """(compose-service, source) for every bind mount (compose config normalizes volumes to long form)."""
    out = []
    for sname, svc in _services(compose):
        for vol in svc.get("volumes") or []:
            if isinstance(vol, dict) and vol.get("type") == "bind" and vol.get("source"):
                out.append((sname, str(vol["source"])))
    return out


def _external_volume_names(compose: dict) -> set[str]:
    names = set()
    for key, vol in (compose.get("volumes") or {}).items():
        if isinstance(vol, dict) and vol.get("external"):
            names.add(vol.get("name") or key)
    return names


# --- the check suite ---------------------------------------------------------------------------------

def certify_target(desc: Descriptor, config_path: Path, base_env: dict[str, str],
                   *, emit: Path | None = None) -> list[Check]:
    """Run the conformance suite against one launcher + one config. Returns the per-check results."""
    checks: list[Check] = []
    fail = lambda name, detail: checks.append(Check(ERROR, name, detail))  # noqa: E731
    ok = lambda name: checks.append(Check(OK, name))  # noqa: E731

    lp = desc.launcher_path
    if not lp.exists():
        return [Check(ERROR, "launcher", f"missing: {lp}")]
    if not lp.stat().st_mode & 0o111:
        return [Check(ERROR, "launcher", f"not executable: {lp}")]

    base_cfg = load_yaml(config_path)
    with tempfile.TemporaryDirectory(prefix="rig-certify-") as td:
        tmp = Path(td)
        cfg_a = _write_named_config(base_cfg, NAME_A, desc.service, tmp)
        env_a = _poison_env(base_env, NAME_A)
        run_a = _run_launcher(desc, cfg_a, desc.verb_args("config"), env_a)

        # discipline: rc 0, stdout parses as compose YAML, no junk top-level keys (= chatter on stdout).
        if run_a.returncode != 0:
            fail("discipline", f"`config` exited {run_a.returncode}: {(run_a.stderr or '').strip()[:200]}")
            _cleanup(desc)
            return checks
        try:
            compose = yaml.safe_load(run_a.stdout)
        except yaml.YAMLError as exc:
            compose = None
            fail("discipline", f"`config` stdout is not YAML ({str(exc).splitlines()[0]})")
        if compose is not None and not (isinstance(compose, dict) and compose.get("services")):
            compose = None
            fail("discipline", "`config` stdout is not a compose document (no `services:`)")
        if compose is not None:
            junk = [k for k in compose if k not in _COMPOSE_TOP_KEYS and not str(k).startswith("x-")]
            if junk:
                compose = None
                fail("discipline", f"non-compose top-level keys on stdout (human lines go to stderr): {junk}")
        if compose is None:
            _cleanup(desc)
            return checks  # everything below reads the compose; one failure at a time
        ok("discipline")

        # project-name: rig owns the compose project; the launcher must not override it with -p.
        want = project_name(NAME_A, POISON_VID)
        got = str(compose.get("name", ""))
        if got == want:
            ok("project-name")
        else:
            fail("project-name", f"compose project is '{got or '(unset)'}', not COMPOSE_PROJECT_NAME"
                                 f" ('{want}') — is the launcher passing -p?")

        # registry / tag: where images come from, and build/pull tag agreement.
        owned = set(desc.build_images) | {_repo_of(m) for m in desc.mirror}
        unprefixed_owned, unprefixed_other, mistagged = [], [], []
        for ref in _service_images(compose).values():
            repo, tag = _ref_parts(ref)
            if not ref.startswith(POISON_REGISTRY + "/"):
                (unprefixed_owned if repo in owned else unprefixed_other).append(ref)
            if repo in set(desc.build_images) and tag != POISON_TAG:
                mistagged.append(ref)
        if unprefixed_owned:
            fail("registry", f"images the service builds/mirrors are not pulled from RIG_IMAGE_REGISTRY: "
                             f"{unprefixed_owned}")
        else:
            ok("registry")
        if unprefixed_other:
            checks.append(Check(WARN, "registry", f"upstream image(s) not under RIG_IMAGE_REGISTRY (needs "
                                                  f"an internet-connected vehicle): {unprefixed_other}"))
        if mistagged:
            fail("tag", f"built image(s) pulled with a tag other than RIG_IMAGE_TAG ('{POISON_TAG}') — "
                        f"`rig build` pushes what the compose won't find: {mistagged}")
        elif desc.build_images:
            ok("tag")

        # ros-env: a ROS service must pass the fleet DDS/zenoh graph env into its containers — but WHICH
        # containers run ROS nodes is the config's business (a plugin-less camera runs none; ros1 has no
        # RMW), and rig is schema-opaque. The poison values still give a sharp rule: a WRONG value anywhere
        # is hardcoding (ERROR); both vars arriving somewhere is conformance (OK); total absence is only
        # suspicious (WARN — fine when this config runs no ROS node).
        if desc.ros_distro:
            wrong, seen = [], {"ROS_DOMAIN_ID": False, "RMW_IMPLEMENTATION": False}
            poison = {"ROS_DOMAIN_ID": POISON_VID, "RMW_IMPLEMENTATION": POISON_RMW}
            for sname, svc in _services(compose):
                env = _env_of(svc)
                for var, want in poison.items():
                    if var in env:
                        if env[var] == want:
                            seen[var] = True
                        else:
                            wrong.append(f"{sname}: {var}={env[var]!r}")
            if wrong:
                fail("ros-env", f"fleet ROS env reaches containers with the wrong value (hardcoded, not "
                                f"taken from the env): {wrong}")
            elif all(seen.values()):
                ok("ros-env")
            else:
                checks.append(Check(WARN, "ros-env", "no container env carries the fleet "
                                                     "ROS_DOMAIN_ID/RMW_IMPLEMENTATION — fine only if this "
                                                     "config runs no ROS node"))

        # volumes: the rigging.yaml external_volumes patterns must match reality (down --purge relies on them).
        if desc.external_volumes:
            expected = {p.format(name=NAME_A) for p in desc.external_volumes}
            actual = _external_volume_names(compose)
            missing = expected - actual
            if missing:
                checks.append(Check(WARN, "volumes", f"declared external_volumes not in the compose "
                                                     f"(stale pattern? profile-gated?): {sorted(missing)}"))
            else:
                ok("volumes")

        # binds: a bind into the service repo bakes the repo path into the artifact. Files are fine
        # (rendered params/config); directories (or not-yet-existing paths) are usually misplaced outputs.
        suspicious, datadir = [], []
        for sname, src in _bind_sources(compose):
            sp = Path(src)
            if src.startswith(POISON_DATA):
                datadir.append(src)
            try:
                under_repo = sp.is_relative_to(desc.repo)
            except (ValueError, OSError):
                under_repo = False
            if under_repo and not sp.is_file():
                suspicious.append(f"{sname}: {src}")
        if suspicious:
            checks.append(Check(WARN, "binds", f"directory bind(s) into the service repo — bake freezes "
                                               f"these into the artifact (fine for config inputs; outputs "
                                               f"belong under RIG_DATA_DIR): {suspicious}"))
        if datadir:
            checks.append(Check(INFO, "data-dir", f"binds under RIG_DATA_DIR: {sorted(set(datadir))}"))

        # determinism: same config + same env -> byte-identical. Catches timestamps, $RANDOM, host probing.
        run_a2 = _run_launcher(desc, cfg_a, desc.verb_args("config"), env_a)
        if run_a2.stdout == run_a.stdout:
            ok("determinism")
        else:
            diff = list(difflib.unified_diff(run_a.stdout.splitlines(), run_a2.stdout.splitlines(),
                                             "run-1", "run-2", lineterm="", n=0))
            fail("determinism", "two identical `config` runs differ: " + "; ".join(diff[2:6]))

        # identity: rename the instance -> every trace of the old name disappears from the output.
        cfg_b = _write_named_config(base_cfg, NAME_B, desc.service, tmp)
        run_b = _run_launcher(desc, cfg_b, desc.verb_args("config"), _poison_env(base_env, NAME_B))
        if run_b.returncode != 0:
            fail("identity", f"`config` failed for a renamed instance (exit {run_b.returncode})")
        elif NAME_A in run_b.stdout:
            lines = [ln.strip() for ln in run_b.stdout.splitlines() if NAME_A in ln]
            fail("identity", f"renamed to '{NAME_B}' but '{NAME_A}' persists — identity is hardcoded "
                             f"somewhere, not derived from the config name: {lines[:3]}")
        else:
            ok("identity")

        # status: rig parses `status` as `docker compose ps --format json` (empty = no containers, fine).
        run_s = _run_launcher(desc, cfg_a, [*desc.verb_args("status"), "--format", "json"], env_a)
        if run_s.returncode != 0:
            fail("status", f"`status` exited {run_s.returncode}: {(run_s.stderr or '').strip()[:160]}")
        else:
            try:
                _parse_ps(run_s.stdout)
                ok("status")
            except Exception:  # noqa: BLE001
                fail("status", f"`status --format json` stdout is not JSON: {run_s.stdout.strip()[:120]!r}")

        if emit is not None:
            normalized = run_a.stdout.replace(str(desc.repo), "${REPO}").replace(str(tmp), "${TMP}")
            emit.write_text(normalized)
            checks.append(Check(INFO, "emit", f"normalized compose -> {emit} (diff one from the dev box "
                                              f"against one from the vehicle to prove host-independence)"))

    _cleanup(desc)
    return checks


def _cleanup(desc: Descriptor) -> None:
    """A launcher's `config` may eagerly create its external volumes; sweep the poison-named ones."""
    if shutil.which("docker") is None:
        return
    for pattern in desc.external_volumes:
        for name in (NAME_A, NAME_B):
            subprocess.run(["docker", "volume", "rm", pattern.format(name=name)],
                           capture_output=True, text=True)


# --- reporting / entry points --------------------------------------------------------------------------

def report(label: str, checks: list[Check]) -> tuple[int, int]:
    """Print one target's results (doctor-style); returns (errors, warnings)."""
    passed = [c.name for c in checks if c.level == OK]
    eprint(f"rig certify: {label}")
    if passed:
        eprint(f"  [✓] {len(passed)} ok: {', '.join(passed)}")
    for c in checks:
        if c.level != OK:
            eprint(f"  [{_SYMBOL[c.level]}] {c.name}: {c.detail}")
    return (sum(1 for c in checks if c.level == ERROR), sum(1 for c in checks if c.level == WARN))


def run_targets(targets: list[tuple[str, Descriptor, Path]], base_env: dict[str, str],
                *, emit: Path | None = None) -> int:
    """Certify each (label, descriptor, config) target; summarize; exit code 1 on any ERROR."""
    if emit is not None and len(targets) != 1:
        raise RigError("--emit compares one launcher run across hosts; name exactly one target")
    errors = warnings = 0
    for label, desc, config in targets:
        e, w = report(label, certify_target(desc, config, base_env, emit=emit))
        errors += e
        warnings += w
    eprint(f"rig certify: {len(targets)} target(s) — {errors} error(s), {warnings} warning(s)")
    return 1 if errors else 0


def diff_emits(path_a: Path, path_b: Path) -> int:
    """Compare two --emit outputs (e.g. dev box vs vehicle). Identical = the launcher's config output is a
    pure function of (config, env) — a dev-box bake is correct for the target."""
    a, b = Path(path_a), Path(path_b)
    for p in (a, b):
        if not p.exists():
            raise RigError(f"certify --diff: no such file: {p}")
    diff = list(difflib.unified_diff(a.read_text().splitlines(), b.read_text().splitlines(),
                                     str(a), str(b), lineterm=""))
    if not diff:
        eprint("rig certify: emits identical — host-independent ✓")
        return 0
    for line in diff:
        print(line)
    eprint(f"rig certify: emits differ ({sum(1 for l in diff if l.startswith(('+', '-')))} lines) — the "
           f"launcher's config output depends on the host")
    return 1
