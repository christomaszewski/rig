"""Fleet status: ask each launcher for `docker compose ps --format json`, roll each project up to one row.

This is the consumer of the launcher stdout/stderr discipline: the human status line goes to stderr, the
JSON to stdout, so we parse cleanly. Health comes from each service's baked Docker HEALTHCHECK; a project
is healthy iff every *healthchecked* container is healthy and all are running (a plugin without a probe
doesn't drag the sensor to "unknown").
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

from .descriptor import Descriptor, STATE_VOCAB
from .dispatch import launcher_cmd, service_env
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
        cmd = launcher_cmd(sensor, desc, "status", ["--format", "json"])
        try:  # platform routing: same per-service env view as up/config
            proc = subprocess.run(cmd, env=service_env(env, desc), cwd=str(desc.repo),
                                  capture_output=True, text=True)
            containers = _parse_ps(proc.stdout)
        except Exception:
            containers = []
        state, health, running, total = _rollup(containers)
        op = None
        if desc.supports_states:  # declared trio only — the declaration is the support claim
            try:
                proc_op = subprocess.run(launcher_cmd(sensor, desc, "state"),
                                         env=service_env(env, desc), cwd=str(desc.repo),
                                         capture_output=True, text=True)
                op = _op_state(proc_op.stdout) if proc_op.returncode == 0 else "unknown"
            except Exception:
                op = "unknown"
        rows.append(Row(sensor, state, health, running, total, containers, op))
    return rows


def as_json(manifest, rows: list[Row], run_line: str | None) -> str:
    """The MACHINE-READABLE status (one stable object) — `rig status --format json`. The remote
    half of `rig fleet status`: fleet tooling parses THIS, never the human table (the same
    contract discipline the run manifests follow)."""
    return json.dumps({
        "vehicle": manifest.vehicle,
        "vehicle_id": manifest.vehicle_id,
        "run": run_line,
        # op_state is ADDITIVE (v0.2.35): observed operational state, null for services that don't
        # declare the trio. The pre-existing keys — `state` especially (the compose rollup) — are
        # the stable contract older consumers parse; never repurpose them.
        "stacks": [{"sensor": r.sensor.name, "service": r.sensor.service, "state": r.state,
                    "health": r.health, "op_state": r.op_state,
                    "running": r.running, "total": r.total} for r in rows],
    }, sort_keys=True)


def render(rows: list[Row], *, verbose: bool = False) -> str:
    # OP (operational state) sits NEXT TO health — the contract reads them as a pair.
    headers = ("SENSOR", "SERVICE", "STATE", "HEALTH", "OP", "CONTAINERS")
    table = [headers]
    for row in rows:
        table.append(
            (row.sensor.name, row.sensor.service, row.state, row.health, row.op_state or "-",
             f"{row.running}/{row.total}")
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
