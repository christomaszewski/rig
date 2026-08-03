"""runs — the session registry state machine (ROADMAP §3c). Run: python3 tests/test_runs.py

A fake `docker` on PATH controls the rotation/seal guard (`docker compose ls`); everything else is real
filesystem. The invariant under test throughout: at most one run is ever OPEN, and only explicit verbs
rotate or seal — `up`'s ensure never does either.
"""
import json
import os
import pathlib
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
    for fn in (lambda: runs.new_run(m, data, "x"), lambda: runs.end_run(m)):
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
    assert runs.end_run(m, status_text="ok") == rid
    assert not (data / "current").exists()
    body = (data / "runs" / rid / "manifest.yaml").read_text()
    assert "ended:" in body and "status_at_end" in body
    assert runs.end_run(m) is None                                  # nothing open -> no-op
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
    for fn in (lambda: runs.end_run(m), lambda: runs.new_run(m, data, "x"),
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
    assert runs.end_run(m) == rid                               # sealing rewrites a minimal manifest
    body = (data / "runs" / rid / "manifest.yaml").read_text()
    assert "ended:" in body and "corrupt: true" in body


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
    mpath.write_text(mpath.read_text().replace("label: '123'", "label: 123"))  # sh-style unquoted int
    assert runs.up_run(m, data, "123") == rid                   # joins; no silent rotation on type drift


def test_status_line_states():
    _mark_idle()
    data = pathlib.Path(tempfile.mkdtemp())
    m = _manifest(data)
    assert "none active" in runs.status_line(m)
    runs.new_run(m, data, "dock")
    assert "dock" in runs.status_line(m)
    assert runs.status_line(_manifest(None)) is None


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
