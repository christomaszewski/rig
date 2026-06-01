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

from .descriptor import Descriptor
from .dispatch import launcher_cmd
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


def gather(pairs: list[tuple[Sensor, Descriptor]], env: dict[str, str]) -> list[Row]:
    rows: list[Row] = []
    for sensor, desc in pairs:
        cmd = launcher_cmd(sensor, desc, "status", ["--format", "json"])
        try:
            proc = subprocess.run(cmd, env=env, cwd=str(desc.repo), capture_output=True, text=True)
            containers = _parse_ps(proc.stdout)
        except Exception:
            containers = []
        state, health, running, total = _rollup(containers)
        rows.append(Row(sensor, state, health, running, total, containers))
    return rows


def render(rows: list[Row], *, verbose: bool = False) -> str:
    headers = ("SENSOR", "SERVICE", "STATE", "HEALTH", "CONTAINERS")
    table = [headers]
    for row in rows:
        table.append(
            (row.sensor.name, row.sensor.service, row.state, row.health, f"{row.running}/{row.total}")
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
