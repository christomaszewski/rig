"""runs — the session registry state machine (ROADMAP §3c). Run: python3 tests/test_runs.py

A fake `docker` on PATH controls the rotation/seal guard (`docker compose ls`); everything else is real
filesystem. The invariant under test throughout: at most one run is ever OPEN, and only explicit verbs
rotate or seal — `up`'s ensure never does either.
"""
import json
import os
import pathlib
import shutil
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from rig_cli import RigError, runs  # noqa: E402
from rig_cli.manifest import Manifest, RosSettings, Sensor, project_name  # noqa: E402

_SHIM = """\
#!/bin/sh
if [ "$1" = "compose" ] && [ "$2" = "ls" ]; then
  if [ "$SHIM_COMPOSE_LS" = "__fail__" ]; then echo "daemon wedged" >&2; exit 1; fi
  echo "${SHIM_COMPOSE_LS:-[]}"; exit 0
fi
exit 0
"""


def _shim_path() -> str:
    d = pathlib.Path(tempfile.mkdtemp())
    (d / "docker").write_text(_SHIM)
    (d / "docker").chmod(0o755)
    return str(d)


os.environ["PATH"] = _shim_path() + ":" + os.environ["PATH"]  # every test runs under the shim


def _manifest(data_dir) -> Manifest:
    return Manifest(
        vehicle="t", ros=RosSettings(domain_id=1, rmw="rmw_zenoh_cpp", distro=None),
        sensors=[Sensor(name="cam", service="c", config=pathlib.Path("/x"), enabled=True, order=10)],
        vehicle_id=1, data_dir=str(data_dir) if data_dir else None,
    )


def _mark_running(m: Manifest) -> None:
    os.environ["SHIM_COMPOSE_LS"] = json.dumps(
        [{"Name": project_name("cam", m.vehicle_id), "Status": "running(3)"}])


def _mark_idle() -> None:
    os.environ["SHIM_COMPOSE_LS"] = "[]"


def test_ensure_creates_auto_once_and_is_idempotent():
    _mark_idle()
    data = pathlib.Path(tempfile.mkdtemp())
    m = _manifest(data)
    a = runs.ensure(m, data)
    b = runs.ensure(m, data)
    assert a == b and a.endswith("_auto")
    assert len(list((data / "runs").iterdir())) == 1            # no second dir minted
    cur = data / "current"
    assert cur.is_symlink() and not os.readlink(cur).startswith("/")  # RELATIVE target (in-container)


def test_ensure_is_inert_without_data_dir():
    assert runs.ensure(_manifest(None), pathlib.Path("/tmp")) is None


def test_new_run_seals_old_and_opens_labeled():
    _mark_idle()
    data = pathlib.Path(tempfile.mkdtemp())
    m = _manifest(data)
    old = runs.ensure(m, data)
    new = runs.new_run(m, data, "dock-test")
    assert new != old and new.endswith("_dock-test")
    assert "ended:" in (data / "runs" / old / "manifest.yaml").read_text()   # rotation SEALS
    assert (data / "current").resolve().name == new
    rows = {r.run: r.state for r in runs.list_runs(m)}
    assert rows[old] == "sealed" and rows[new] == "OPEN"


def test_rotation_and_seal_refuse_while_running():
    data = pathlib.Path(tempfile.mkdtemp())
    m = _manifest(data)
    _mark_idle()
    runs.ensure(m, data)
    _mark_running(m)
    for fn in (lambda: runs.new_run(m, data, "x"), lambda: runs.end_run(m, data)):
        try:
            fn()
            raise AssertionError("expected RigError while stacks run")
        except RigError as exc:
            assert "running" in str(exc)
    assert runs.new_run(m, data, "x", force=True).endswith("_x")   # --force overrides
    _mark_idle()


def test_end_run_seals_removes_current_and_is_idempotent():
    _mark_idle()
    data = pathlib.Path(tempfile.mkdtemp())
    m = _manifest(data)
    rid = runs.ensure(m, data)
    assert runs.end_run(m, data, status_text="ok") == rid
    assert not (data / "current").exists()
    body = (data / "runs" / rid / "manifest.yaml").read_text()
    assert "ended:" in body and "status_at_end" in body
    assert runs.end_run(m, data) is None                            # nothing open -> no-op
    assert runs.ensure(m, data) != rid                              # next up opens a FRESH _auto


def test_up_run_joins_same_label_and_rotates_different():
    _mark_idle()
    data = pathlib.Path(tempfile.mkdtemp())
    m = _manifest(data)
    a = runs.up_run(m, data, "dock")
    assert runs.up_run(m, data, "dock") == a                        # idempotent join, never dock-2
    b = runs.up_run(m, data, "pm-survey")
    assert b != a
    assert {r.run: r.state for r in runs.list_runs(m)}[a] == "sealed"


def test_interrupted_state_is_surfaced():
    _mark_idle()
    data = pathlib.Path(tempfile.mkdtemp())
    m = _manifest(data)
    rid = runs.ensure(m, data)
    (data / "current").unlink()                                     # hand-meddled: open run orphaned
    assert {r.run: r.state for r in runs.list_runs(m)}[rid] == "interrupted"


def test_bad_label_rejected():
    data = pathlib.Path(tempfile.mkdtemp())
    try:
        runs.new_run(_manifest(data), data, "bad label!")
        raise AssertionError("expected RigError for label")
    except RigError as exc:
        assert "label" in str(exc)


def test_manifest_records_artifact_tag():
    _mark_idle()
    data = pathlib.Path(tempfile.mkdtemp())
    root = pathlib.Path(tempfile.mkdtemp())                         # an "extracted artifact" root
    (root / "metadata.yaml").write_text("tag: test9\nvehicle: t\n")
    m = _manifest(data)
    rid = runs.new_run(m, root, "field")
    assert "artifact: test9" in (data / "runs" / rid / "manifest.yaml").read_text()


def test_guard_fails_CLOSED_when_docker_cannot_answer():
    # The rotation/seal guard must refuse on unknown state — not silently rotate under a live recorder.
    data = pathlib.Path(tempfile.mkdtemp())
    m = _manifest(data)
    _mark_idle()
    runs.ensure(m, data)
    for bad in ("__fail__", "not json at all {"):
        os.environ["SHIM_COMPOSE_LS"] = bad
        try:
            runs.new_run(m, data, "x")
            raise AssertionError(f"expected fail-closed RigError for {bad!r}")
        except RigError as exc:
            assert "force" in str(exc)                          # tells the human the override exists
    assert runs.new_run(m, data, "x", force=True)               # --force skips the check entirely
    _mark_idle()


def test_guard_parses_ndjson_compose_output():
    # compose has drifted array->NDJSON before (ps in v2.21); the guard must not fail-closed on it.
    data = pathlib.Path(tempfile.mkdtemp())
    m = _manifest(data)
    _mark_idle()
    runs.ensure(m, data)
    row = {"Name": project_name("cam", m.vehicle_id), "Status": "running(3)"}
    os.environ["SHIM_COMPOSE_LS"] = json.dumps(row) + "\n" + json.dumps({"Name": "other", "Status": "exited"})
    try:
        runs.new_run(m, data, "x")
        raise AssertionError("expected guard refusal from NDJSON-reported running stack")
    except RigError as exc:
        assert "running" in str(exc)
    _mark_idle()


def test_guard_ignores_unrelated_running_project():
    # Precision: somebody ELSE's running compose stack on this host is not OUR evidence — only
    # this manifest's expected project names may block rotation.
    data = pathlib.Path(tempfile.mkdtemp())
    m = _manifest(data)
    _mark_idle()
    runs.ensure(m, data)
    os.environ["SHIM_COMPOSE_LS"] = json.dumps(
        [{"Name": "somebody-elses-stack", "Status": "running(2)"}])
    assert runs.new_run(m, data, "ours").endswith("_ours")      # unrelated stack -> no refusal
    _mark_idle()


def test_guard_missing_docker_reads_as_nothing_running():
    # A missing docker binary alone is NOT "cannot tell": nothing of ours can run without it, so
    # rotation proceeds (the guard's one deliberate fail-soft carve-out).
    data = pathlib.Path(tempfile.mkdtemp())
    m = _manifest(data)
    _mark_idle()
    runs.ensure(m, data)
    old_path = os.environ["PATH"]
    os.environ["PATH"] = tempfile.mkdtemp()                     # empty dir: no docker anywhere
    try:
        rid = runs.new_run(m, data, "nodocker")
    finally:
        os.environ["PATH"] = old_path
    assert rid.endswith("_nodocker")                            # rotation still works


def test_stale_current_tmp_does_not_brick():
    _mark_idle()
    data = pathlib.Path(tempfile.mkdtemp())
    (data / ".current.tmp").symlink_to("runs/ghost")            # crash leftover between symlink+replace
    assert runs.ensure(_manifest(data), data).endswith("_auto")  # self-heals, no FileExistsError


def test_current_pointing_outside_registry_is_a_loud_error():
    _mark_idle()
    data = pathlib.Path(tempfile.mkdtemp())
    m = _manifest(data)
    elsewhere = pathlib.Path(tempfile.mkdtemp()) / "foo"
    elsewhere.mkdir()
    (data / "current").symlink_to(elsewhere)                    # absolute, outside runs/
    for fn in (lambda: runs.end_run(m, data), lambda: runs.new_run(m, data, "x"),
               lambda: runs.ensure(m, data)):
        try:
            fn()
            raise AssertionError("expected RigError for non-contained current")
        except RigError as exc:                                  # RigError, never a raw traceback
            assert "outside" in str(exc)
    assert "registry problem" in runs.status_line(m)             # status survives it


def test_current_as_real_directory_is_a_loud_error():
    _mark_idle()
    data = pathlib.Path(tempfile.mkdtemp())
    (data / "current").mkdir(parents=True)
    try:
        runs.ensure(_manifest(data), data)
        raise AssertionError("expected RigError for non-symlink current")
    except RigError as exc:
        assert "not a symlink" in str(exc)


def test_corrupt_open_manifest_never_wedges_up():
    _mark_idle()
    data = pathlib.Path(tempfile.mkdtemp())
    m = _manifest(data)
    rid = runs.ensure(m, data)
    (data / "runs" / rid / "manifest.yaml").write_text("{{{ not yaml")
    assert runs.ensure(m, data) == rid                          # `up`'s ensure still works
    assert runs.status_line(m) is not None                      # status still works
    assert {r.run: r.state for r in runs.list_runs(m)}[rid] == "OPEN"
    assert runs.end_run(m, data) == rid                         # sealing rewrites a minimal manifest
    body = (data / "runs" / rid / "manifest.yaml").read_text()
    assert "ended:" in body and "corrupt: true" in body


def test_dangling_current_self_heals_on_next_ensure():
    _mark_idle()
    data = pathlib.Path(tempfile.mkdtemp())
    m = _manifest(data)
    rid = runs.ensure(m, data)
    shutil.rmtree(data / "runs" / rid)                          # the run dir behind the link is gone
    assert (data / "current").is_symlink()                      # …so `current` now dangles
    healed = runs.ensure(m, data)                               # reads as None -> heals, no error
    assert healed is not None and (data / "current").resolve().name == healed
    assert (data / "runs" / healed / "manifest.yaml").is_file()


def test_sealed_run_with_corrupt_manifest_lists_as_corrupt():
    _mark_idle()
    data = pathlib.Path(tempfile.mkdtemp())
    m = _manifest(data)
    rid = runs.ensure(m, data)
    runs.end_run(m, data)                                       # sealed; `current` removed
    (data / "runs" / rid / "manifest.yaml").write_text("{{{ not yaml")
    assert {r.run: r.state for r in runs.list_runs(m)}[rid] == "corrupt"  # reported, not hidden


def test_relative_data_dir_rejected():
    import dataclasses

    m = dataclasses.replace(_manifest(None), data_dir="relative/logs")
    try:
        runs.ensure(m, pathlib.Path("/tmp"))
        raise AssertionError("expected RigError for relative data_dir")
    except RigError as exc:
        assert "ABSOLUTE" in str(exc)


def test_up_run_label_comparison_survives_yaml_typing():
    _mark_idle()
    data = pathlib.Path(tempfile.mkdtemp())
    m = _manifest(data)
    rid = runs.new_run(m, data, "123")
    mpath = data / "runs" / rid / "manifest.yaml"
    text = mpath.read_text()
    rewritten = text.replace("label: '123'", "label: 123")      # sh-style unquoted int
    assert rewritten != text, "manifest lost the quoted label form this test rewrites"
    mpath.write_text(rewritten)
    assert yaml.safe_load(mpath.read_text())["label"] == 123    # the int really is on disk
    assert runs.up_run(m, data, "123") == rid                   # joins; no silent rotation on type drift


def test_status_line_states():
    _mark_idle()
    data = pathlib.Path(tempfile.mkdtemp())
    m = _manifest(data)
    assert "none active" in runs.status_line(m)
    runs.new_run(m, data, "dock")
    assert "dock" in runs.status_line(m)
    assert runs.status_line(_manifest(None)) is None


# --- config snapshots (capture at `up`, dirty-check at seal) ------------------------------------

import dataclasses  # noqa: E402

import yaml  # noqa: E402


def _world(port: int = 1) -> tuple[Manifest, pathlib.Path, pathlib.Path]:
    """A real deployment tree (vehicle.yaml + one rendered config) + its own data dir."""
    data = pathlib.Path(tempfile.mkdtemp())
    root = pathlib.Path(tempfile.mkdtemp())
    (root / "vehicle.yaml").write_text(f"vehicle: t\nvehicle_id: 1\nport: {port}\n")
    cfg = root / "config" / "cam.yaml"
    cfg.parent.mkdir()
    cfg.write_text(f"name: cam\nport: {port}\n")
    m = Manifest(
        vehicle="t", ros=RosSettings(domain_id=1, rmw="rmw_zenoh_cpp", distro=None),
        sensors=[Sensor(name="cam", service="c", config=cfg, enabled=True, order=10)],
        vehicle_id=1, data_dir=str(data),
    )
    return m, root, data


def _run_doc(data: pathlib.Path, rid: str) -> dict:
    return yaml.safe_load((data / "runs" / rid / "manifest.yaml").read_text())


def test_up_snapshot_writes_files_and_manifest():
    _mark_idle()
    m, root, data = _world()
    rid = runs.ensure(m, root)
    digest = runs.snapshot(m, root, stacks=["cam"])
    assert digest is not None
    snap = data / "runs" / rid / ".rig" / "config" / digest
    assert (snap / "vehicle.yaml").read_text() == (root / "vehicle.yaml").read_text()
    assert (snap / "rendered" / "cam.yaml").read_text() == "name: cam\nport: 1\n"
    assert "vars:" in (snap / "vars.yaml").read_text()          # resolved context is captured
    assert not (snap / "rig.lock").exists()                     # optional files simply absent
    doc = _run_doc(data, rid)
    assert doc["config"] == digest
    assert doc["ups"][0]["stacks"] == ["cam"] and doc["ups"][0]["config"] == digest
    dep = (root / "var" / "deployment-id").read_text().strip()
    assert doc["deployment"] == dep and doc["ups"][0]["deployment"] == dep


def test_snapshot_dedups_unchanged_config():
    _mark_idle()
    m, root, data = _world()
    rid = runs.ensure(m, root)
    a = runs.snapshot(m, root, stacks=["cam"])
    b = runs.snapshot(m, root, stacks=["cam"])
    assert a == b
    assert len(list((data / "runs" / rid / ".rig" / "config").iterdir())) == 1  # one dir…
    assert len(_run_doc(data, rid)["ups"]) == 2                                 # …two events


def test_snapshot_new_digest_on_config_change():
    _mark_idle()
    m, root, data = _world()
    rid = runs.ensure(m, root)
    a = runs.snapshot(m, root, stacks=["cam"])
    (root / "config" / "cam.yaml").write_text("name: cam\nport: 99\n")
    b = runs.snapshot(m, root, stacks=["cam"])
    assert a != b
    assert len(list((data / "runs" / rid / ".rig" / "config").iterdir())) == 2
    doc = _run_doc(data, rid)
    assert doc["config"] == b and [u["config"] for u in doc["ups"]] == [a, b]


def test_snapshot_failure_never_wedges_up():
    _mark_idle()
    m, root, data = _world()
    runs.ensure(m, root)
    (root / "vehicle.yaml").unlink()                            # not a deployment tree anymore
    assert runs.snapshot(m, root, stacks=["cam"]) is None       # warns, returns None, no raise


def test_snapshot_into_corrupt_manifest_rewrites_minimal():
    _mark_idle()
    m, root, data = _world()
    rid = runs.ensure(m, root)
    (data / "runs" / rid / "manifest.yaml").write_text("{{{ not yaml")
    digest = runs.snapshot(m, root, stacks=["cam"])
    doc = _run_doc(data, rid)
    assert digest and doc["corrupt"] is True and doc["config"] == digest
    assert {r.run: r.state for r in runs.list_runs(m)}[rid] == "OPEN"


def test_seal_flags_dirty_when_config_changed_after_last_up():
    _mark_idle()
    m, root, data = _world()
    rid = runs.ensure(m, root)
    runs.snapshot(m, root, stacks=["cam"])
    (root / "vehicle.yaml").write_text("vehicle: t\nvehicle_id: 1\nport: 7\n")  # edited, never upped
    assert runs.end_run(m, root) == rid
    assert _run_doc(data, rid)["config_dirty_at_seal"] is True


def test_seal_clean_when_config_unchanged():
    _mark_idle()
    m, root, data = _world()
    rid = runs.ensure(m, root)
    runs.snapshot(m, root, stacks=["cam"])
    runs.end_run(m, root)
    assert "config_dirty_at_seal" not in _run_doc(data, rid)


def test_seal_dirty_check_skipped_without_snapshot():
    _mark_idle()
    m, root, data = _world()
    rid = runs.ensure(m, root)                                  # sh-opened style: no ups/config
    runs.end_run(m, root)
    assert "config_dirty_at_seal" not in _run_doc(data, rid)


def test_seal_from_other_deployment_never_flags_dirty():
    # THE forgotten-seal scenario: run from tree A left open, new artifact untarred as tree B
    # (same data_dir), `up --run <label>` rotates — sealing A's run from B must NOT read as dirty.
    _mark_idle()
    m_a, root_a, data = _world(port=1)
    rid_a = runs.ensure(m_a, root_a)
    runs.snapshot(m_a, root_a, stacks=["cam"])
    m_b, root_b, _ = _world(port=2)                             # different tree, different config
    m_b = dataclasses.replace(m_b, data_dir=str(data))          # …but the SAME data disk
    rid_b = runs.up_run(m_b, root_b, "field")                   # rotates: seals A's run from B
    assert rid_b != rid_a
    doc_a = _run_doc(data, rid_a)
    assert "ended" in doc_a and "config_dirty_at_seal" not in doc_a
    id_a = (root_a / "var" / "deployment-id").read_text().strip()
    id_b = (root_b / "var" / "deployment-id").read_text().strip()
    assert id_a != id_b and _run_doc(data, rid_b)["deployment"] == id_b


def test_deployment_id_minted_once_and_stable():
    _, root, _ = _world()
    a = runs.deployment_id(root)
    assert a and runs.deployment_id(root) == a                  # stable across calls
    _, other, _ = _world()
    assert runs.deployment_id(other) != a                       # a fresh tree is a fresh instance


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
