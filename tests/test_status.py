"""status — ps-JSON parsing (both compose output shapes), the per-stack rollup, the machine-readable
JSON contract, the human table, and gather() driving fixture launchers (no daemon).
Run: python3 tests/test_status.py"""
import json
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from rig_cli.descriptor import Descriptor  # noqa: E402
from rig_cli.manifest import Manifest, RosSettings, Sensor  # noqa: E402
from rig_cli.status import Row, _parse_ps, _rollup, as_json, gather, render  # noqa: E402


def _sensor(name: str = "cam", service: str = "camsvc", order: int = 10) -> Sensor:
    return Sensor(name=name, service=service, config=pathlib.Path("/dev/null"),
                  enabled=True, order=order)


def _desc(svc: str, launcher_body: str) -> Descriptor:
    """A Descriptor over a real tmpdir launcher (same construction as test_platform's _desc) —
    gather() shells out to it for real, so the stdout/stderr contract is exercised end to end."""
    repo = pathlib.Path(tempfile.mkdtemp(prefix="status-svc-"))
    launcher = repo / f"{svc}-up"
    launcher.write_text(launcher_body)
    launcher.chmod(0o755)
    return Descriptor(service=svc, repo=repo, launcher=f"{svc}-up", verbs={}, ros_distro=None,
                      external_volumes=[], host_ports=[])


# --- _parse_ps: both real-world `docker compose ps --format json` shapes ------------------------

def test_parse_ps_json_array():
    # Newer compose emits one JSON array.
    rows = _parse_ps('[{"State": "running"}, {"State": "exited"}]')
    assert rows == [{"State": "running"}, {"State": "exited"}]


def test_parse_ps_newline_delimited_objects():
    # Older compose emits newline-delimited objects (json.loads on the whole blob fails -> per line).
    rows = _parse_ps('{"State": "running"}\n\n  {"State": "exited"}  \n')
    assert rows == [{"State": "running"}, {"State": "exited"}]  # blank lines skipped, whitespace ok


def test_parse_ps_single_object_becomes_one_row():
    # A single object parses as a dict, not a list — it must still come back as ONE row.
    assert _parse_ps('{"State": "running"}') == [{"State": "running"}]


def test_parse_ps_empty_and_whitespace_are_no_rows():
    assert _parse_ps("") == []
    assert _parse_ps("   \n  \t ") == []


# --- _rollup: one (state, health, running, total) per project -----------------------------------

def test_rollup_no_containers_is_down():
    assert _rollup([]) == ("down", "-", 0, 0)


def test_rollup_all_running_all_healthy():
    cs = [{"State": "running", "Health": "healthy"}, {"State": "running", "Health": "healthy"}]
    assert _rollup(cs) == ("running", "healthy", 2, 2)


def test_rollup_any_unhealthy_dominates():
    # One unhealthy container must drag the whole project to "unhealthy" — even beside healthy
    # AND starting peers (dominance beats the starting mix rule).
    cs = [{"State": "running", "Health": "healthy"},
          {"State": "running", "Health": "starting"},
          {"State": "running", "Health": "unhealthy"}]
    assert _rollup(cs)[1] == "unhealthy"


def test_rollup_healthy_plus_starting_is_starting():
    cs = [{"State": "running", "Health": "healthy"}, {"State": "running", "Health": "starting"}]
    assert _rollup(cs)[1] == "starting"


def test_rollup_partial_when_some_running_and_counts():
    # Health n/a here too: a stopped container reports no Health, and the running one has no probe.
    cs = [{"State": "running"}, {"State": "exited"}]
    assert _rollup(cs) == ("partial", "n/a", 1, 2)
    # All stopped (but present) is "down" with the total still counted.
    assert _rollup([{"State": "exited"}]) == ("down", "n/a", 0, 1)


def test_rollup_no_healthchecked_containers_is_na():
    # A plugin without a HEALTHCHECK must not drag the sensor to unknown/unhealthy: no probes at
    # all -> health is "n/a", state still rolls up from State alone.
    assert _rollup([{"State": "running"}, {"State": "running"}]) == ("running", "n/a", 2, 2)


# --- as_json: the machine contract fleet tooling parses -----------------------------------------

def test_as_json_stable_shape_and_sorted_keys():
    manifest = Manifest(vehicle="boat", ros=RosSettings(domain_id=7, rmw="rmw_fastrtps_cpp",
                                                        distro=None),
                        sensors=[], vehicle_id=7)
    rows = [Row(_sensor("cam", "camsvc"), "running", "healthy", 2, 2,
                [{"State": "running"}, {"State": "running"}]),
            Row(_sensor("gnss", "gnsssvc", order=20), "down", "-", 0, 0, [])]
    out = as_json(manifest, rows, "20260101T000000Z_auto (open)")
    expected = {
        "vehicle": "boat",
        "vehicle_id": 7,
        "run": "20260101T000000Z_auto (open)",
        "stacks": [
            {"sensor": "cam", "service": "camsvc", "state": "running", "health": "healthy",
             "running": 2, "total": 2},
            {"sensor": "gnss", "service": "gnsssvc", "state": "down", "health": "-",
             "running": 0, "total": 0},
        ],
    }
    # Byte-for-byte against a sort_keys dump: proves the exact keys/values AND that the
    # serialization is stable (fleet tooling diffs/parses this string).
    assert out == json.dumps(expected, sort_keys=True)
    # run: None must serialize as null, not vanish (the key is part of the contract).
    assert json.loads(as_json(manifest, [], None))["run"] is None


# --- render: the human table --------------------------------------------------------------------

def test_render_table_and_verbose_container_lines():
    containers = [{"Name": "cam-vehicle-1-core-1", "State": "running", "Health": "healthy"},
                  {"Service": "helper", "State": "exited"}]  # no Name -> falls back to Service
    rows = [Row(_sensor("cam", "camsvc"), "partial", "n/a", 1, 2, containers)]
    basic = render(rows)
    lines = basic.splitlines()
    assert "SENSOR" in lines[0] and "HEALTH" in lines[0] and "CONTAINERS" in lines[0]
    assert set(lines[1]) == {"-", " "}                      # header underline row
    assert "cam" in basic and "partial" in basic and "n/a" in basic
    assert "1/2" in basic                                    # running/total counts
    assert "└" not in basic                                  # per-container detail is verbose-only
    verbose = render(rows, verbose=True)
    assert "└ cam-vehicle-1-core-1: running (healthy)" in verbose
    assert "└ helper: exited (-)" in verbose                 # Service fallback + missing Health -> "-"


# --- gather: real launcher subprocesses, canned ps JSON, no docker ------------------------------

_OK_LAUNCHER = """\
#!/bin/sh
# launcher contract: `<launcher> <config> ps --format json` (default status verb is `ps`);
# human noise goes to stderr, the JSON to stdout — gather must parse cleanly around it.
[ "$2" = ps ] || exit 0
echo "camsvc: 1 container up" >&2
printf '{"Name":"cam-vehicle-1-core-1","State":"running","Health":"healthy"}\\n'
"""

_BROKEN_LAUNCHER = """\
#!/bin/sh
echo "camsvc: cannot reach docker" >&2
exit 1
"""


def test_gather_rolls_up_healthy_and_failed_launchers():
    pairs = [(_sensor("cam", "camsvc"), _desc("camsvc", _OK_LAUNCHER)),
             (_sensor("gnss", "gnsssvc", order=20), _desc("gnsssvc", _BROKEN_LAUNCHER))]
    rows = gather(pairs, {"PATH": os.environ["PATH"]})
    assert [r.sensor.name for r in rows] == ["cam", "gnss"]   # one Row per pair, input order kept
    ok = rows[0]
    assert (ok.state, ok.health, ok.running, ok.total) == ("running", "healthy", 1, 1)
    assert ok.containers[0]["Name"] == "cam-vehicle-1-core-1"  # containers kept for verbose render
    bad = rows[1]  # launcher exits 1 with nothing on stdout -> no containers -> "down", never a crash
    assert (bad.state, bad.health, bad.running, bad.total) == ("down", "-", 0, 0)
    assert bad.containers == []


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print("FAIL", name, "->", exc)
    sys.exit(1 if failures else 0)
