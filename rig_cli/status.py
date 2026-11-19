"""Fleet status: ask each launcher for `docker compose ps --format json`, roll each project up to one row.

This is the consumer of the launcher stdout/stderr discipline: the human status line goes to stderr, the
JSON to stdout, so we parse cleanly. Health comes from each service's baked Docker HEALTHCHECK; a project
is healthy iff every *healthchecked* container is healthy and all are running (a plugin without a probe
doesn't drag the sensor to "unknown").

A question that could not be answered is never reported as "down". When the launcher's status call
fails (the Docker daemon is unreachable, the socket is not readable, the compose model does not
render) or prints something that is not `ps` output, the row's state is `unknown`, the reason rides
on the row, and `rig status` exits non-zero: an operator who cannot reach Docker must not be told
that a running stack has stopped.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

from . import RigError
from .descriptor import Descriptor, STATE_VOCAB
from .dispatch import instance_env, launcher_cmd
from .manifest import Sensor


def _parse_ps(stdout: str) -> list[dict]:
    """`docker compose ps --format json` is either a JSON array or newline-delimited JSON objects,
    depending on the Compose version. Handle both."""
    stdout = stdout.strip()
    if not stdout:
        return []
    try:
        data = json.loads(stdout)
        return data if isinstance(data, list) else [data]
    except json.JSONDecodeError:
        rows = []
        for line in stdout.splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
        return rows


UNKNOWN = "unknown"  # the state of a row whose status could not be read (see the module docstring)


@dataclass
class Row:
    sensor: Sensor
    state: str
    health: str
    running: int
    total: int
    containers: list[dict]
    # Observed OPERATIONAL state (the launcher's `state` verb), only for services declaring the
    # trio: active|standby|transitioning|down, "unknown" when the probe can't answer, None when
    # undeclared (always active — rendered "-"). Read NEXT TO health: the pair disambiguates
    # (transitioning+healthy = wait, e.g. a post-activate self-reset; transitioning+unhealthy =
    # stuck). rig prints the pair; it never re-interprets it.
    op_state: str | None = None
    # Why `state` is "unknown": the launcher's status call failed, or its output could not be
    # read. None whenever the question was answered (a stopped stack is an answer: "down").
    error: str | None = None


def _rollup(containers: list[dict]) -> tuple[str, str, int, int]:
    if not containers:
        return "down", "-", 0, 0
    states = [c.get("State", "") for c in containers]
    running = sum(1 for s in states if s == "running")
    total = len(containers)
    state = "running" if running == total else ("down" if running == 0 else "partial")

    healths = [c.get("Health", "") for c in containers if c.get("Health")]
    if not healths:
        health = "n/a"
    elif any(h == "unhealthy" for h in healths):
        health = "unhealthy"
    elif all(h == "healthy" for h in healths):
        health = "healthy"
    else:
        health = "starting"
    return state, health, running, total


def _op_state(stdout: str) -> str:
    """The `state` verb's single JSON line -> contract vocabulary. Anything else — chatter on
    stdout, a missing/foreign `state` value — is 'unknown', never a crash (status must render on a
    half-broken vehicle)."""
    try:
        doc = json.loads(stdout.strip())
    except (json.JSONDecodeError, ValueError):
        return "unknown"
    state = doc.get("state") if isinstance(doc, dict) else None
    return state if state in STATE_VOCAB else "unknown"


def gather(pairs: list[tuple[Sensor, Descriptor]], env: dict[str, str]) -> list[Row]:
    rows: list[Row] = []
    for sensor, desc in pairs:
        try:  # one missing launcher (an unchecked-out repo) is that row's problem, not the table's
            cmd = launcher_cmd(sensor, desc, "status", ["--format", "json"])
        except RigError:
            rows.append(Row(sensor, "no launcher", "-", 0, 0, [], None))
            continue
        error = None
        containers: list[dict] = []
        try:  # the SAME per-instance env up/down ran with — platform routing AND the compose
            #     project, or we'd ask the launcher about a stack rig never started
            proc = subprocess.run(cmd, env=instance_env(env, sensor, desc), cwd=str(desc.repo),
                                  capture_output=True, text=True)
        except Exception as exc:  # noqa: BLE001 — status must render on a half-broken vehicle
            error = f"the launcher could not be run: {exc}"
        else:
            if proc.returncode != 0:
                said = (proc.stderr or "").strip().splitlines()
                error = (f"the launcher's status call exited {proc.returncode}"
                         + (f": {said[-1].strip()[:200]}" if said else ""))
            else:
                try:
                    containers = _parse_ps(proc.stdout)
                    if not all(isinstance(c, dict) for c in containers):
                        raise ValueError("not a list of containers")
                except ValueError:  # json.JSONDecodeError is one
                    containers = []
                    error = ("the launcher's status call printed something other than "
                             "`docker compose ps --format json` output")
        if error is not None:  # we could not find out: never "down" (see the module docstring)
            rows.append(Row(sensor, UNKNOWN, "-", 0, 0, [],
                            UNKNOWN if desc.supports_states else None, error))
            continue
        state, health, running, total = _rollup(containers)
        op = None
        if desc.supports_states:  # declared trio only — the declaration is the support claim
            try:
                proc_op = subprocess.run(launcher_cmd(sensor, desc, "state"),
                                         env=instance_env(env, sensor, desc), cwd=str(desc.repo),
                                         capture_output=True, text=True)
                op = _op_state(proc_op.stdout) if proc_op.returncode == 0 else "unknown"
            except Exception:
                op = "unknown"
        rows.append(Row(sensor, state, health, running, total, containers, op))
    return rows


def as_json(manifest, rows: list[Row], run_line: str | None, *, open_run: dict | None = None,
            run_registry_error: str | None = None) -> str:
    """The MACHINE-READABLE status (one stable object) — `rig status --format json`. The remote
    half of `rig fleet status`: fleet tooling parses THIS, never the human table (the same
    contract discipline the run manifests follow)."""
    return json.dumps({
        "vehicle": manifest.vehicle,
        "vehicle_id": manifest.vehicle_id,
        "run": run_line,
        "run_registry": bool(manifest.data_dir),
        "open_run": open_run,
        **({"run_registry_error": run_registry_error} if run_registry_error else {}),
        # op_state is ADDITIVE (v0.2.35): observed operational state, null for services that don't
        # declare the trio. The pre-existing keys — `state` especially (the compose rollup) — are
        # the stable contract older consumers parse; never repurpose them.
        # error is ADDITIVE too: null unless `state` is "unknown" (the status call failed or its
        # output could not be read), then the reason. "unknown" is a new VALUE of `state`: a
        # consumer counting `state == "running"` reads such a stack as not running, as before.
        "stacks": [{"sensor": r.sensor.name, "service": r.sensor.service, "state": r.state,
                    "health": r.health, "op_state": r.op_state,
                    "running": r.running, "total": r.total, "error": r.error} for r in rows],
    }, sort_keys=True)


def unknown(rows: list[Row]) -> list[Row]:
    """The rows whose state could not be read — what makes `rig status` exit non-zero."""
    return [r for r in rows if r.state == UNKNOWN]


def explain(rows: list[Row]) -> list[str]:
    """One sentence per unknown row, for stderr beside the table."""
    return [f"{r.sensor.name}: state unknown — {r.error}" for r in unknown(rows)]


def render(rows: list[Row], *, verbose: bool = False) -> str:
    # OP (operational state) sits NEXT TO health — the contract reads them as a pair.
    headers = ("SENSOR", "SERVICE", "STATE", "HEALTH", "OP", "CONTAINERS")
    table = [headers]
    for row in rows:
        table.append(
            (row.sensor.name, row.sensor.service, row.state, row.health, row.op_state or "-",
             "?" if row.state == UNKNOWN else f"{row.running}/{row.total}")
        )
    widths = [max(len(r[i]) for r in table) for i in range(len(headers))]
    lines = []
    for ri, row in enumerate(table):
        lines.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
        if ri == 0:
            lines.append("  ".join("-" * widths[i] for i in range(len(headers))))
    if verbose:
        for row in rows:
            for c in row.containers:
                name = c.get("Name") or c.get("Service", "?")
                health = c.get("Health") or "-"
                lines.append(f"    └ {name}: {c.get('State', '?')} ({health})")
    return "\n".join(lines)
