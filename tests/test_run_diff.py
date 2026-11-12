"""`rig run diff A B` — what differs between two runs' RECORDS. Run: python3 tests/test_run_diff.py

The runs are built here, file by file, as `up` and `sim up` write them: manifest.yaml, the config
snapshot under .rig/config/<digest>/ (vehicle.yaml, vars.yaml, rig.lock, rendered configs) and
.rig/images.yaml. Nothing is started. The command reads and never writes, so every test may
compare the same two directories again.
"""
import contextlib
import copy
import io
import json
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from rig_cli.cli import main, translate_argv  # noqa: E402

os.environ.setdefault("RIG_HOME", str(pathlib.Path(tempfile.mkdtemp()) / "home"))
os.environ["RIG_VEHICLE_LOCAL"] = str(pathlib.Path(tempfile.mkdtemp()) / "absent.yaml")
for _stray in [k for k in os.environ
               if k in ("RIG_VEHICLE_ID", "RIG_VEHICLE_NAME") or k.startswith("RIG_VAR_")]:
    os.environ.pop(_stray)

PLANNER = "localhost:5000/planner:dev"
BASE = "localhost:5000/fleet-ros:dev"


def _sha(seed: str) -> str:
    return "sha256:" + (seed * 64)[:64]


def _spec(**over) -> dict:
    """Everything that makes one run's record, as plain values a test can change one at a time."""
    spec = {
        "stamp": "20260301T100000Z", "label": "a", "rig": "0.2.85", "vehicle": "bench-01",
        "vehicle_id": 7, "stacks": ["zenoh_router", "bag_logger", "planner"],
        "seed": 7, "scene": "scenes/dock.yaml", "simulator": "worldsim", "sim_version": "4.2.0",
        "planner": "0.1.0", "planner_commit": "aaaa00000001", "planner_rev": "bbbb00000001",
        "child": _sha("a1"), "index": _sha("b1"),      # the image, and this build's index of it
        "vars": {"rate_hz": 2, "vehicle": "bench-01", "vehicle_id": 7},
        "sources": None, "machine": None, "horizon_s": 10, "config": "aaa000000001",
    }
    spec.update(over)
    return spec


def _write_run(registry: pathlib.Path, spec: dict) -> pathlib.Path:
    run = registry / "runs" / f"{spec['stamp']}_{spec['label']}"
    snap = run / ".rig" / "config" / spec["config"]
    (snap / "rendered").mkdir(parents=True)
    started = f"2026-03-01T10:{spec['stamp'][11:13]}:00+00:00"
    manifest = {
        "run": run.name, "vehicle": spec["vehicle"], "vehicle_id": spec["vehicle_id"],
        "rig_version": spec["rig"], "started": started, "stacks": spec["stacks"],
        "label": spec["label"], "deployment": "d0d0d0d0d0d0",
        "simulation": {
            "provider": "external", "simulator": {"name": spec["simulator"], "version": spec["sim_version"]},
            "mode": "attach", "scene": spec["scene"],
            "recipe": {"version": 2, "simulation": {"scene": spec["scene"], "seed": spec["seed"]}},
            "fingerprint": (spec["stamp"] + spec["label"]) * 3, "clock": "external"},
        "capture": {"sha256": (spec["label"] + "cafe01") * 8, "rig_version": spec["rig"]},
        "config": spec["config"],
        "ups": [{"at": started, "stacks": spec["stacks"], "config": spec["config"],
                 "deployment": "d0d0d0d0d0d0", "root": "/home/bench/ws/deployment"}],
        "ended": started.replace(":00+", ":30+"), "disk_kb": 800 + len(spec["label"]),
    }
    (run / "manifest.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    (snap / "vehicle.yaml").write_text(yaml.safe_dump(
        {"vehicle": spec["vehicle"], "vehicle_id": spec["vehicle_id"], "data_dir": str(registry),
         "infra": [{"name": "zenoh_router", "service": "zenoh-router", "config": "config/infra/zenoh_router.yaml"}],
         "autonomy": [{"name": "planner", "service": "planner", "config": "config/autonomy/planner.yaml"}]},
        sort_keys=False))
    vars_doc = {"vars": spec["vars"], "env": {}}
    if spec["sources"] is not None:
        vars_doc["sources"] = spec["sources"]
    (snap / "vars.yaml").write_text(yaml.safe_dump(vars_doc, sort_keys=True))
    if spec["machine"] is not None:
        (snap / "machine.local.yaml").write_text(yaml.safe_dump(spec["machine"], sort_keys=False))
    (snap / "rendered" / "planner.yaml").write_text(yaml.safe_dump(
        {"service": "planner", "name": "planner", "plan": {"horizon_s": spec["horizon_s"], "mode": "greedy"}},
        sort_keys=False))
    (snap / "rig.lock").write_text(yaml.safe_dump({
        "build_catalog": {
            BASE: {"architectures": ["amd64"], "children": {"amd64": _sha("c1")},
                   "digest": _sha("d1"), "image": "fleet-ros", "ref": BASE, "service": "zenoh-router",
                   "source_revision": "eeee00000001"},
            PLANNER: {"architectures": ["amd64"], "children": {"amd64": spec["child"]},
                      "digest": spec["index"], "image": "planner", "ref": PLANNER, "service": "planner",
                      "source_revision": spec["planner_rev"]}},
        "instances": {"zenoh_router": {"base_sha256": "ffff00000001"}},
        "packages": {
            f"lab/planner@{spec['planner']}": {
                "commit": spec["planner_commit"], "kind": "service",
                "source": {"path": "planner", "repo": "https://example.org/lab.git", "rev": spec["planner_rev"]}},
            "public/zenoh-router@1.20.1": {
                "commit": "eeee00000002", "kind": "service",
                "source": {"path": "zenoh-router", "repo": "https://example.org/infra.git", "rev": "eeee00000001"}}},
        "registries": {"lab": {"commit": "eeee00000003", "type": "local-dir", "path": "/home/bench/registry"}},
        "schema": 1}, sort_keys=True))
    (run / ".rig" / "images.yaml").write_text(yaml.safe_dump({
        "images": {BASE: f"localhost:5000/fleet-ros@{_sha('e1')}",
                   PLANNER: f"localhost:5000/planner@{spec['index']}"}, "schema": 1}, sort_keys=True))
    return run


def _three_runs() -> tuple[pathlib.Path, pathlib.Path, pathlib.Path, pathlib.Path]:
    """(registry, a, b, a2): a and a2 run the same planner, built twice; b runs the next version."""
    registry = pathlib.Path(tempfile.mkdtemp()) / "data"
    a = _write_run(registry, _spec())
    b = _write_run(registry, _spec(stamp="20260301T100200Z", label="b", config="aaa000000002",
                                   planner="0.2.0", planner_commit="aaaa00000002",
                                   planner_rev="bbbb00000002", child=_sha("a2"), index=_sha("b2")))
    a2 = _write_run(registry, _spec(stamp="20260301T100300Z", label="a2", config="aaa000000003",
                                    index=_sha("b3")))
    return registry, a, b, a2


def _rig(*argv) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = main(list(argv))
        except SystemExit as exc:  # argparse
            rc = int(exc.code or 0)
    return rc, out.getvalue(), err.getvalue()


def _diff(a, b) -> tuple[int, dict]:
    rc, out, err = _rig("run", "diff", str(a), str(b), "--json")
    assert rc in (0, 1), err
    return rc, json.loads(out)


def _found(doc: dict, bucket: str) -> set:
    return {(c["section"], c["key"]) for c in doc[bucket]}


def test_the_same_software_built_twice_differs_in_run_identity_and_index_digest_only():
    """An image's multi-architecture index is new on every build; the image under it is not.
    Reading the index would report a change where there is none."""
    _, a, _, a2 = _three_runs()
    rc, doc = _diff(a, a2)
    assert rc == 0 and doc["substantive"] is False
    assert doc["differs"] == []
    assert _found(doc, "index_only") == {("images", PLANNER)}
    assert {("run", "run"), ("run", "label"), ("run", "started")} <= _found(doc, "per_run")
    assert {"rig and vehicle", "stacks", "simulation", "packages", "vars", "vehicle.yaml",
            "rendered planner"} <= set(doc["same"])
    rc, out, err = _rig("run", "diff", str(a), str(a2))                 # and as a person reads it
    assert rc == 0, err
    assert "no difference of substance" in out
    assert "index digest only" in out
    assert PLANNER in out.split("index digest only", 1)[1]      # named there, not as a difference
    assert a.name in out and a2.name in out


def test_a_changed_package_is_named_with_its_versions_and_its_image():
    _, a, b, _ = _three_runs()
    rc, doc = _diff(a, b)
    assert rc == 1 and doc["substantive"] is True
    by_key = {(c["section"], c["key"]): (c["a"], c["b"]) for c in doc["differs"]}
    assert by_key[("packages", "lab/planner")] == ("0.1.0", "0.2.0")
    assert by_key[("images", f"{PLANNER} amd64")] == (_sha("a1"), _sha("a2"))
    assert {section for section, _ in by_key} == {"packages", "images"}    # and nothing else
    assert doc["index_only"] == []                              # the index moved because the image did
    assert {"rig and vehicle", "stacks", "simulation", "vars", "rendered planner"} <= set(doc["same"])
    rc, out, err = _rig("run", "diff", str(a), str(b))
    assert rc == 1, err
    line = next(ln for ln in out.splitlines() if "lab/planner" in ln)
    assert "0.1.0" in line and "0.2.0" in line
    assert "no difference of substance" not in out


def test_a_seed_a_value_and_a_rendered_setting_are_reported_by_key():
    registry, a, _, _ = _three_runs()
    c = _write_run(registry, _spec(stamp="20260301T100400Z", label="c", config="aaaaaaaaaaaa",
                                   seed=8, horizon_s=20,
                                   vars={"rate_hz": 5, "vehicle": "bench-01", "vehicle_id": 7}))
    rc, doc = _diff(a, c)
    assert rc == 1
    by_key = {(d["section"], d["key"]): (d["a"], d["b"]) for d in doc["differs"]}
    assert by_key[("simulation", "recipe.simulation.seed")] == (7, 8)
    assert by_key[("vars", "rate_hz")] == (2, 5)
    assert by_key[("rendered planner", "plan.horizon_s")] == (10, 20)
    assert len(by_key) == 3
    assert "packages" in doc["same"] and "images" in doc["same"]


def test_value_sources_and_the_machine_file_are_compared_when_runs_hold_them():
    registry, a, _, _ = _three_runs()
    held = {"vars": {"rate_hz": 2, "vehicle": "bench-01", "vehicle_id": 7, "altitude_band": 3},
            "rig": "0.2.86"}
    d = _write_run(registry, _spec(stamp="20260301T101000Z", label="d", config="dddddddddddd", **held,
                                   sources={"altitude_band": "fleet.yaml row uav-03", "rate_hz": "vehicle.yaml"},
                                   machine={"vehicle": "bench-01", "vehicle_id": 7}))
    e = _write_run(registry, _spec(stamp="20260301T101100Z", label="e", config="eeeeeeeeeeee", **held,
                                   sources={"altitude_band": "/etc/rig/vehicle.local.yaml",
                                            "rate_hz": "vehicle.yaml"},
                                   machine={"vehicle": "bench-01", "vehicle_id": 7,
                                            "vars": {"altitude_band": 3}}))
    rc, doc = _diff(d, e)                                       # the same value, from another tier
    assert rc == 1
    by_key = {(c["section"], c["key"]): (c["a"], c["b"]) for c in doc["differs"]}
    assert by_key[("value sources", "altitude_band")] == ("fleet.yaml row uav-03",
                                                          "/etc/rig/vehicle.local.yaml")
    assert by_key[("machine.local.yaml", "vars.altitude_band")] == (None, 3)
    assert "vars" in doc["same"]
    rc, doc = _diff(a, d)                                       # an older run holds neither
    assert rc == 1
    found = _found(doc, "differs")
    assert ("rig and vehicle", "rig_version") in found
    assert ("machine.local.yaml", "(file)") in found and ("value sources", "(recorded)") in found


def test_an_image_with_no_per_architecture_record_counts_as_changed():
    registry, a, _, _ = _three_runs()
    f = _write_run(registry, _spec(stamp="20260301T101200Z", label="f", config="ffffffffffff"))
    images = f / ".rig" / "images.yaml"
    doc = yaml.safe_load(images.read_text())
    doc["images"]["docker.io/library/redis:7"] = "redis@" + _sha("0f0f")
    images.write_text(yaml.safe_dump(doc))
    g = _write_run(registry, _spec(stamp="20260301T101300Z", label="g", config="999999999999"))
    doc["images"]["docker.io/library/redis:7"] = "redis@" + _sha("1e1e")
    (g / ".rig" / "images.yaml").write_text(yaml.safe_dump(doc))
    rc, report = _diff(f, g)
    assert rc == 1                                              # nothing says the image is the same
    assert ("images", "docker.io/library/redis:7") in _found(report, "differs")
    assert report["index_only"] == []


def _held(run: pathlib.Path, ref: str, value="(no entry)") -> None:
    """Rewrite what the daemon held for `ref` when the run opened."""
    path = run / ".rig" / "images.yaml"
    doc = yaml.safe_load(path.read_text())
    if value == "(no entry)":
        doc["images"].pop(ref, None)
    else:
        doc["images"][ref] = value
    path.write_text(yaml.safe_dump(doc, sort_keys=True))


def test_an_image_its_lock_does_not_describe_is_not_called_the_same():
    """The per-architecture record lives in rig.lock; what ran is what the daemon held. The lock
    vouches for an image only when it records the very digest the daemon held. A lock left
    behind by a replaced image, or a ref with no digest at all, is a difference."""
    registry = pathlib.Path(tempfile.mkdtemp()) / "data"
    a = _write_run(registry, _spec())
    b = _write_run(registry, _spec(stamp="20260301T100400Z", label="b", config="aaa000000004"))
    rc, doc = _diff(a, b)
    assert rc == 0 and not _found(doc, "index_only"), doc             # as recorded: the same
    _held(b, PLANNER, f"localhost:5000/planner@{_sha('f9')}")        # another image; B's lock is stale
    rc, doc = _diff(a, b)
    assert rc == 1 and ("images", PLANNER) in _found(doc, "differs"), doc
    assert ("images", PLANNER) not in _found(doc, "index_only"), doc
    assert any("rig.lock" in note and PLANNER in note for note in doc["notes"]), doc["notes"]
    for nothing in (None, "(no entry)"):                             # no digest recorded for it
        _held(b, PLANNER, nothing)
        rc, doc = _diff(a, b)
        assert rc == 1 and ("images", PLANNER) in _found(doc, "differs"), (nothing, doc)
        assert ("images", PLANNER) not in _found(doc, "index_only"), (nothing, doc)
    registry, a, _, a2 = _three_runs()                               # each lock names what ran:
    rc, doc = _diff(a, a2)                                           # a rebuild is still index-only
    assert rc == 0 and ("images", PLANNER) in _found(doc, "index_only"), doc
    _held(a, PLANNER, f"localhost:5000/planner@{_sha('f8')}")        # the locks still agree with
    rc, doc = _diff(a, a2)                                           # each other; A ran something else
    assert rc == 1 and ("images", PLANNER) in _found(doc, "differs"), doc
    assert ("images", PLANNER) not in _found(doc, "index_only"), doc


def test_runs_resolve_by_path_anywhere_and_by_id_or_label_inside_a_deployment():
    registry, a, b, _ = _three_runs()
    root = pathlib.Path(tempfile.mkdtemp())
    (root / "vehicle.yaml").write_text(f"vehicle: bench-01\nvehicle_id: 7\ndata_dir: {registry}\nsensors: []\n")
    (root / "services.yaml").write_text("services: {}\n")
    rc, out, err = _rig("--root", str(root), "run", "diff", a.name, "b")      # an id and a label
    assert rc == 1 and "lab/planner" in out, err
    rc, by_path, err = _rig("run", "diff", str(a), str(b))
    assert rc == 1, err
    assert [ln for ln in by_path.splitlines() if "lab/planner" in ln] == \
        [ln for ln in out.splitlines() if "lab/planner" in ln]
    assert translate_argv(["run", "diff", "a", "b"]) == ["run-diff", "a", "b"]


def test_trouble_exits_2_and_says_what_is_missing():
    registry, a, _, _ = _three_runs()
    rc, _, err = _rig("run", "diff", str(a), str(registry / "runs" / "nope"))
    assert rc == 2 and "nope" in err, err
    empty = registry / "runs" / "20260301T110000Z_empty"
    empty.mkdir()
    rc, _, err = _rig("run", "diff", str(a), str(empty))
    assert rc == 2 and "manifest.yaml" in err, err
    bare = _write_run(registry, _spec(stamp="20260301T110100Z", label="bare", config="bbbbbbbbbbbb"))
    doc = yaml.safe_load((bare / "manifest.yaml").read_text())
    doc.pop("config"), doc.pop("ups")                           # opened, never brought up by rig
    (bare / "manifest.yaml").write_text(yaml.safe_dump(doc))
    rc, out, err = _rig("run", "diff", str(a), str(bare), "--json")
    assert rc == 1, err                                         # still compared, as far as it goes
    report = json.loads(out)
    assert ("config snapshot", "(recorded)") in _found(report, "differs")
    assert "rig and vehicle" in report["same"]


def test_a_run_with_several_snapshots_is_compared_at_its_last_up_and_says_so():
    registry, a, _, _ = _three_runs()
    h = _write_run(registry, _spec(stamp="20260301T101400Z", label="h", config="111111111111", horizon_s=20))
    earlier = h / ".rig" / "config" / "000000000000" / "rendered"
    earlier.mkdir(parents=True)
    (earlier / "planner.yaml").write_text("service: planner\nname: planner\nplan: {horizon_s: 10, mode: greedy}\n")
    doc = yaml.safe_load((h / "manifest.yaml").read_text())
    doc["ups"].insert(0, dict(copy.deepcopy(doc["ups"][0]), config="000000000000"))
    (h / "manifest.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    rc, out, err = _rig("run", "diff", str(a), str(h))
    assert rc == 1, err
    assert "horizon_s" in out
    assert "2 config snapshots" in out and "last" in out


def _as_replay(run: pathlib.Path, *, of: str, launch_s: float, session: int) -> None:
    """Turn a written run into a replay session's record: no simulation block, a replay one."""
    doc = yaml.safe_load((run / "manifest.yaml").read_text())
    doc.pop("simulation")
    doc["replay"] = {
        "of": of, "source": f"/data/runs/{of}", "clock": "sim", "fingerprint": f"{session:012x}",
        "source_fingerprint": "5050505050aa", "playback": {"topics": ["/camera/image"]},
        "start_at_unix_s": 1772359200.0 + launch_s,
        "startup": {"session": session, "status": "started", "timings": {"launch_s": launch_s}},
        "verification": {"mode": "cached", "hashed_files": session % 7}}
    (run / "manifest.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))


def test_a_replay_is_compared_by_what_it_replayed_not_by_how_long_it_took():
    registry, a, _, _ = _three_runs()
    r1 = _write_run(registry, _spec(stamp="20260301T102000Z", label="r1", config="121212121212"))
    r2 = _write_run(registry, _spec(stamp="20260301T102100Z", label="r2", config="343434343434"))
    r3 = _write_run(registry, _spec(stamp="20260301T102200Z", label="r3", config="565656565656"))
    _as_replay(r1, of=a.name, launch_s=2.86, session=1001)
    _as_replay(r2, of=a.name, launch_s=3.41, session=1002)
    _as_replay(r3, of="20260201T000000Z_other", launch_s=2.90, session=1003)
    rc, doc = _diff(r1, r2)                                     # the same source, replayed twice
    assert rc == 0 and doc["differs"] == [] and "replay" in doc["same"]
    assert {"replay.startup.timings.launch_s", "replay.fingerprint",
            "replay.start_at_unix_s"} <= {c["key"] for c in doc["per_run"]}
    rc, doc = _diff(r1, r3)                                     # another source
    by_key = {(c["section"], c["key"]): (c["a"], c["b"]) for c in doc["differs"]}
    assert rc == 1 and by_key[("replay", "of")] == (a.name, "20260201T000000Z_other")
    assert {section for section, _ in by_key} == {"replay"}
    rc, doc = _diff(a, r1)                                      # a simulation session against a replay
    assert rc == 1                                              # one line each, not one per key
    assert [c for c in doc["differs"] if c["section"] in ("simulation", "replay")] == [
        {"section": "simulation", "key": "(recorded)", "a": "yes", "b": "no"},
        {"section": "replay", "key": "(recorded)", "a": "no", "b": "yes"}]


def test_two_vehicles_of_one_fleet_run_are_told_apart_in_the_header():
    """A fleet run gives every vehicle the same run id and label, so the header names the
    vehicle too."""
    a = _write_run(pathlib.Path(tempfile.mkdtemp()) / "data", _spec(vehicle="uav-07"))
    b = _write_run(pathlib.Path(tempfile.mkdtemp()) / "data", _spec(vehicle="spare-1"))
    rc, out, err = _rig("run", "diff", str(a), str(b))
    assert rc == 1, err
    head = [ln for ln in out.splitlines() if ln.startswith(("  A  ", "  B  "))]
    assert "uav-07" in head[0] and "spare-1" in head[1] and head[0][5:] != head[1][5:], head
    doc = json.loads(_rig("run", "diff", str(a), str(b), "--json")[1])
    assert (doc["a"]["vehicle"], doc["b"]["vehicle"]) == ("uav-07", "spare-1")


def test_run_diff_writes_nothing():
    registry, a, b, _ = _three_runs()

    def state() -> dict:
        return {str(p.relative_to(registry)): (p.stat().st_mtime_ns, p.read_bytes() if p.is_file() else None)
                for p in sorted(registry.rglob("*"))}

    before = state()
    _rig("run", "diff", str(a), str(b))
    _rig("run", "diff", str(a), str(b), "--json")
    assert state() == before


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
