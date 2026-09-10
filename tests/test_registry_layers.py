"""The layered run registry (rig ≥ v0.2.55): a deployment whose data_dir is not the machine's
(a tree-local vehicle.local.yaml, a reconstruct workspace) sees the HOST registry read-through —
run refs, `rig runs`, TAB — while writes (new runs, import, rm) stay in its own; a deployment
on the machine registry sees no other deployment's runs. Plus `rig run tag`/`untag`: tags in
the run's own manifest. Run: python3 tests/test_registry_layers.py
"""
import contextlib
import io
import os
import pathlib
import sys
import tempfile
import textwrap

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from rig_cli import RigError, runs  # noqa: E402
from rig_cli.cli import main  # noqa: E402
from rig_cli.completions import candidates  # noqa: E402
from rig_cli.manifest import load_manifest  # noqa: E402

for _stray in [k for k in os.environ
               if k in ("RIG_VEHICLE_ID", "RIG_VEHICLE_NAME") or k.startswith("RIG_VAR_")]:
    os.environ.pop(_stray)
os.environ["RIG_HOME"] = str(pathlib.Path(tempfile.mkdtemp()) / "home")  # the catalog's roots file


def _run_dir(data: pathlib.Path, name: str, *, sealed=True, tags=None, bags=False) -> pathlib.Path:
    d = data / "runs" / name
    d.mkdir(parents=True)
    doc = {"run": name, "vehicle": "veh", "vehicle_id": 4, "started": "2026-09-01T10:00:00Z"}
    if "_" in name:
        doc["label"] = name.split("_", 1)[1]
    if sealed:
        doc["ended"] = "2026-09-01T11:00:00Z"
    if tags:
        doc["tags"] = tags
    (d / "manifest.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    if bags:
        (d / "bags" / "bag_logger").mkdir(parents=True)
    return d


def _machine(data_dir) -> pathlib.Path:
    """A simulated machine identity file naming the HOST registry."""
    path = pathlib.Path(tempfile.mkdtemp()) / "vehicle.local.yaml"
    path.write_text(f"vehicle: box\nvehicle_id: 1\ndata_dir: {data_dir}\n")
    return path


def _tree(*, local_data_dir=None, yaml_data_dir=None) -> pathlib.Path:
    root = pathlib.Path(tempfile.mkdtemp()) / "veh"
    root.mkdir()
    (root / "vehicle.yaml").write_text(textwrap.dedent(f"""\
        vehicle: veh
        vehicle_id: 4
        {'data_dir: ' + str(yaml_data_dir) if yaml_data_dir else ''}
        infra: []
        """))
    if local_data_dir:
        (root / "vehicle.local.yaml").write_text(f"data_dir: {local_data_dir}\n")
    (root / "services.yaml").write_text("services: {}\n")
    return root


@contextlib.contextmanager
def _env(**over):
    old = {k: os.environ.get(k) for k in over}
    for k, v in over.items():
        os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, str(v))
    try:
        yield
    finally:
        for k, v in old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def _cli(root, *argv) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    cwd = os.getcwd()
    os.chdir(root)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = main(list(argv))
            except SystemExit as exc:
                rc = int(exc.code or 0)
    finally:
        os.chdir(cwd)
    return rc, out.getvalue(), err.getvalue()


def _layered():
    """A workspace with its own registry + a provisioned host registry: (root, local, host)."""
    local = pathlib.Path(tempfile.mkdtemp()) / "local"
    host = pathlib.Path(tempfile.mkdtemp()) / "host"
    _run_dir(local, "20260901T100000Z_bench", bags=True)
    _run_dir(host, "20260801T100000Z_survey", bags=True)
    _run_dir(host, "20260802T100000Z_survey", bags=True, tags=["site:mojave"])
    _run_dir(host, "20260803T100000Z_bench", bags=True)   # same label as a local run, NEWER
    root = _tree(local_data_dir=local)
    return root, local, host


# --- the manifest ---------------------------------------------------------------------------------

def test_host_data_dir_only_when_the_registries_differ():
    host = pathlib.Path(tempfile.mkdtemp()) / "host"
    with _env(RIG_VEHICLE_LOCAL=str(_machine(host))):
        m = load_manifest(_tree(local_data_dir="/tmp/ws-data"))
        assert m.data_dir == "/tmp/ws-data" and m.host_data_dir == str(host)
        m = load_manifest(_tree(yaml_data_dir="/tmp/veh-data"))    # the MACHINE beats vehicle.yaml
        assert m.data_dir == str(host) and m.host_data_dir is None  # (data_dir is a machine fact)
        m = load_manifest(_tree())                                  # the machine's = ONE registry
        assert m.data_dir == str(host) and m.host_data_dir is None
        m = load_manifest(_tree(local_data_dir=host))               # same dir spelled locally
        assert m.host_data_dir is None
    with _env(RIG_VEHICLE_LOCAL=str(pathlib.Path(tempfile.mkdtemp()) / "absent.yaml")):
        assert load_manifest(_tree(local_data_dir="/tmp/x")).host_data_dir is None


# --- resolution ------------------------------------------------------------------------------------

def test_refs_resolve_in_the_deployment_registry_then_the_host():
    root, local, host = _layered()
    with _env(RIG_VEHICLE_LOCAL=str(_machine(host))):
        m = load_manifest(root)
        assert runs.registries(m) == [local, host]
        assert runs.resolve_ref(m, "20260901T100000Z_bench", verb="t")[1] == local / "runs" / "20260901T100000Z_bench"
        assert runs.resolve_ref(m, "20260801T100000Z_survey", verb="t")[1] == host / "runs" / "20260801T100000Z_survey"
        # a LABEL: the deployment's registry first even when the host has a newer one
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rid, rdir = runs.resolve_ref(m, "bench", verb="t")
        assert rid == "20260901T100000Z_bench" and "host" not in err.getvalue()
        with contextlib.redirect_stderr(err):
            rid, rdir = runs.resolve_ref(m, "survey", verb="t")
        assert rid == "20260802T100000Z_survey" and "in the host registry" in err.getvalue()
        assert runs.by_label(m, "survey") == "20260802T100000Z_survey"
        # a PATH anywhere; a miss names BOTH registries
        rid, rdir = runs.resolve_ref(m, str(host / "runs" / "20260801T100000Z_survey"), verb="t")
        assert rid == "20260801T100000Z_survey"
        try:
            runs.resolve_ref(m, "nope", verb="t")
            assert False
        except RigError as exc:
            assert str(local / "runs") in str(exc) and str(host / "runs") in str(exc)
            assert "rig runs" in str(exc)
        # the OPEN run of EITHER registry is open from here
        (host / "current").symlink_to(pathlib.Path("runs") / "20260803T100000Z_bench")
        assert runs.is_open(m, host / "runs" / "20260803T100000Z_bench")
        assert not runs.is_open(m, local / "runs" / "20260901T100000Z_bench")


def test_the_machine_registry_deployment_sees_no_workspace_runs():
    """The asymmetry the doctrine is built on: a deployment ON the host registry sees only it."""
    root, local, host = _layered()
    plain = _tree()  # no data_dir of its own: the machine's registry IS its registry
    with _env(RIG_VEHICLE_LOCAL=str(_machine(host))):
        m = load_manifest(plain)
        assert runs.registries(m) == [host]
        assert [r.run for r in runs.list_runs(m)] == ["20260801T100000Z_survey",
                                                      "20260802T100000Z_survey",
                                                      "20260803T100000Z_bench"]
        assert runs.list_runs(m, host=True) == []
        try:
            runs.resolve_ref(m, "20260901T100000Z_bench", verb="t")   # the workspace's run
            assert False
        except RigError:
            pass


def test_run_verbs_find_host_runs_through_the_one_grammar():
    from rig_cli import export, graph, replay
    root, local, host = _layered()
    with _env(RIG_VEHICLE_LOCAL=str(_machine(host))):
        m = load_manifest(root)
        with contextlib.redirect_stderr(io.StringIO()):
            assert replay.resolve_source(m, "20260801T100000Z_survey")[1].parent == host / "runs"
            assert graph.resolve_run(m, "survey")[0] == "20260802T100000Z_survey"
            assert export.resolve_run(m, "20260801T100000Z_survey")[0] == "20260801T100000Z_survey"
            assert graph.resolve_run(m, None)[0] == "20260901T100000Z_bench"  # default: OWN newest


# --- listing + writes -------------------------------------------------------------------------------

def test_rig_runs_shows_both_registries_and_tags_column():
    root, local, host = _layered()
    with _env(RIG_VEHICLE_LOCAL=str(_machine(host))):
        rc, out, _ = _cli(root, "runs")
        assert rc == 0
        head, _, tail = out.partition("host registry")
        assert "20260901T100000Z_bench" in head and "20260801T100000Z_survey" not in head
        assert str(host / "runs") in tail and "(read-through)" in tail
        assert "20260802T100000Z_survey" in tail and "site:mojave" in tail   # TAGS column (host)
        assert "TAGS" in tail and "TAGS" not in head                         # only where tagged
        # a deployment on the machine registry: one table, no host section
        rc, out, _ = _cli(_tree(), "runs")
        assert rc == 0 and "host registry" not in out and "20260802T100000Z_survey" in out


def test_writes_stay_in_the_deployment_registry():
    root, local, host = _layered()
    with _env(RIG_VEHICLE_LOCAL=str(_machine(host))):
        m = load_manifest(root)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = runs.remove_runs(m, ["20260801T100000Z_survey"])
        assert rc == 1 and "HOST registry" in err.getvalue()
        assert (host / "runs" / "20260801T100000Z_survey").is_dir()      # untouched
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = runs.import_runs(m, [str(host / "runs" / "20260801T100000Z_survey")])
        assert rc == 0 and "already in the HOST registry" in err.getvalue()
        assert not (local / "runs" / "20260801T100000Z_survey").exists()  # no pointless copy
        # a run from elsewhere with a host twin's NAME but another identity still imports
        other = pathlib.Path(tempfile.mkdtemp()) / "elsewhere"
        _run_dir(other, "20260801T100000Z_survey")
        with contextlib.redirect_stderr(io.StringIO()):
            rc = runs.import_runs(m, [str(other / "runs" / "20260801T100000Z_survey")])
        assert rc == 0 and (local / "runs" / "20260801T100000Z_survey").is_dir()


# --- tags -------------------------------------------------------------------------------------------

def test_tag_untag_persist_in_the_run_manifest_and_survive_the_seal():
    root, local, host = _layered()
    with _env(RIG_VEHICLE_LOCAL=str(_machine(host))):
        rc, _, err = _cli(root, "run", "tag", "bench", "site:mojave", "event:demo-day")
        assert rc == 0 and "tags now [event:demo-day, site:mojave]" in err
        mpath = local / "runs" / "20260901T100000Z_bench" / "manifest.yaml"
        assert yaml.safe_load(mpath.read_text())["tags"] == ["event:demo-day", "site:mojave"]
        rc, _, err = _cli(root, "run", "tag", "bench", "site:mojave")     # idempotent
        assert rc == 0 and "unchanged" in err
        rc, _, err = _cli(root, "run", "untag", "bench", "site:mojave", "ghost")
        assert rc == 0 and "not tagged ghost" in err and "tags now [event:demo-day]" in err
        rc, _, err = _cli(root, "run", "untag", "bench", "event:demo-day")
        assert rc == 0 and "tags" not in yaml.safe_load(mpath.read_text())
        rc, _, err = _cli(root, "run", "tag", "bench", "bad tag")
        assert rc == 1 and "site:mojave" in err                            # the grammar named
        # a HOST run is taggable from here (the run's metadata, not the registry's)…
        rc, _, err = _cli(root, "run", "tag", "20260801T100000Z_survey", "weather:rain")
        assert rc == 0
        hdoc = yaml.safe_load((host / "runs" / "20260801T100000Z_survey" / "manifest.yaml").read_text())
        assert hdoc["tags"] == ["weather:rain"]
        # …and the seal keeps tags (load-modify-dump)
        runs._seal(host / "runs" / "20260801T100000Z_survey", None)
        hdoc = yaml.safe_load((host / "runs" / "20260801T100000Z_survey" / "manifest.yaml").read_text())
        assert hdoc["tags"] == ["weather:rain"] and hdoc["ended"]


# --- TAB ----------------------------------------------------------------------------------------------

def test_completion_offers_both_registries_but_rm_only_the_deployments():
    root, local, host = _layered()
    with _env(RIG_VEHICLE_LOCAL=str(_machine(host))):
        ids = candidates(["--root", str(root), "replay", ""], 3)        # (the engine sorts)
        assert "20260901T100000Z_bench" in ids                           # own registry…
        assert "20260803T100000Z_bench" in ids and "20260802T100000Z_survey" in ids  # …and host
        assert "survey" in ids and "bench" in ids                        # labels from both
        rm = candidates(["--root", str(root), "run-rm", ""], 3)
        assert rm == ["20260901T100000Z_bench", "bench"]                 # the host's never offered
        tags = candidates(["--root", str(root), "run-untag", "20260802T100000Z_survey", ""], 4)
        assert tags[:1] == ["site:mojave"]
        # a deployment on the machine registry: the host's ids, nothing from the workspace
        ids = candidates(["--root", str(_tree()), "replay", ""], 3)
        assert "20260901T100000Z_bench" not in ids and "20260803T100000Z_bench" in ids


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except Exception as exc:  # noqa: BLE001
                failures += 1
                import traceback
                print("FAIL", name, "->", exc)
                traceback.print_exc()
    sys.exit(1 if failures else 0)
