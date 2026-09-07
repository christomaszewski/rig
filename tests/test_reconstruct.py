"""run capture + reconstruct/retrofit (rig-reconstruct-plan). Run: python3 tests/test_reconstruct.py

A mini deployment fixture (fake vendorable service repos, real vendor/bake staging code) drives
the LEAN capture for real; reconstruct/retrofit run against real tarballs and real content-
addressed snapshots. Docker is absent on CI — image-digest capture must degrade to nulls, never
fail (that IS one of the tests).
"""
import io
import contextlib
import pathlib
import sys
import tarfile
import tempfile
import textwrap

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import rig_cli.bake as bake  # noqa: E402
from rig_cli import RigError, reconstruct, runs  # noqa: E402
from rig_cli.common import load_yaml  # noqa: E402
from rig_cli.manifest import Manifest, RosSettings, Sensor  # noqa: E402


def _service_repo(base: pathlib.Path, name: str) -> pathlib.Path:
    repo = base / f"{name}-repo"
    repo.mkdir(parents=True)
    (repo / "rigging.yaml").write_text(textwrap.dedent(f"""\
        service: {name}
        launcher: {name}-up
        launch_surface: [{name}-up]
        """))
    (repo / f"{name}-up").write_text("#!/bin/sh\nexit 0\n")
    return repo


def _deployment(*, run_capture=True, player=("bag_player", "playr")):
    """(root, manifest) — two services (one row disabled), path-routed like a real dev tree.
    `player` = (name, service) of the disabled autonomy row (None = no autonomy row at all) —
    the default deliberately is NOT the real player service, i.e. a tree that flew without it."""
    base = pathlib.Path(tempfile.mkdtemp())
    root = base / "deploy"
    (root / "config" / "sensors").mkdir(parents=True)
    repo_a = _service_repo(base, "sensa")
    routes = f"  sensa: {{ path: {repo_a} }}\n"
    cfg_a = root / "config" / "sensors" / "cam.yaml"
    cfg_a.write_text("service: sensa\nname: cam\nvalue: original\n")
    rows = [Sensor(name="cam", service="sensa", config=cfg_a, enabled=True, order=10)]
    if player:
        pname, psvc = player
        repo_b = _service_repo(base, psvc)
        routes += f"  {psvc}: {{ path: {repo_b} }}\n"
        cfg_b = root / "config" / "sensors" / f"{pname}.yaml"
        cfg_b.write_text(f"service: {psvc}\nname: {pname}\n")
        rows.append(Sensor(name=pname, service=psvc, config=cfg_b, enabled=False, order=999,
                           tier="autonomy"))
    (root / "services.yaml").write_text("services:\n" + routes)
    manifest = Manifest(vehicle="veh", vehicle_id=1, sensors=rows, data_dir=str(base / "data"),
                        run_capture=run_capture,
                        ros=RosSettings(domain_id=1, rmw="rmw_zenoh_cpp", distro=None))
    return root, manifest


def _player_repo(base: pathlib.Path) -> pathlib.Path:
    """A stand-in ros2-bag-player checkout, laid out like rig-infra/ros2-bag-player."""
    repo = base / "rig-infra" / "ros2-bag-player"
    (repo / "config").mkdir(parents=True)
    (repo / "rigging.yaml").write_text(textwrap.dedent("""\
        service: ros2-bag-player
        launcher: ros2-bag-player-up
        tier: autonomy
        examples: [config/ros2-bag-player.example.yaml]
        launch_surface: [ros2-bag-player-up]
        """))
    (repo / "ros2-bag-player-up").write_text("#!/bin/sh\nexit 0\n")
    (repo / "config" / "ros2-bag-player.example.yaml").write_text(
        "service: ros2-bag-player\nname: bag_player\nplay: {rate: 1.0}\n")
    return repo


def _open(manifest, root, label="t"):
    data = pathlib.Path(manifest.data_dir)
    data.mkdir(parents=True, exist_ok=True)
    return runs._open_run(manifest, root, data, label), data


def test_capture_rides_open_and_stamps_manifest():
    root, m = _deployment()
    rid, data = _open(m, root)
    run_dir = data / "runs" / rid
    doc = load_yaml(run_dir / "manifest.yaml")
    tar = run_dir / ".rig" / "artifact.tar.gz"
    assert tar.is_file() and doc["capture"]["sha256"] == bake._sha256(tar)
    with tarfile.open(tar) as tf:
        names = tf.getnames()
    # disabled row's surface AND config are inside; rig is bundled; metadata marks the kind
    assert any(n.endswith("services/playr/playr-up") for n in names)
    assert any(n.endswith("config/autonomy/bag_player.yaml") for n in names)   # TIER dir, not a flat one
    assert not any(n.endswith("config/sensors/bag_player.yaml") for n in names)
    assert any(n.endswith("config/sensors/cam.yaml") for n in names)            # the sensor row's own tier
    assert any("/rig_cli/" in n for n in names)
    with tarfile.open(tar) as tf:
        import yaml as _y
        member = next(n for n in tf.getnames() if n.endswith("capture/metadata.yaml"))
        kind = _y.safe_load(tf.extractfile(member).read())["kind"]
    assert kind == "run-capture"
    # docker absent here: identity capture degraded to nulls (or wrote nothing) without failing
    images = run_dir / ".rig" / "images.yaml"
    if images.exists():
        assert set((load_yaml(images).get("images") or {}).values()) <= {None}


def test_capture_opt_out_and_fail_soft():
    root, m = _deployment(run_capture=False)
    rid, data = _open(m, root)
    doc = load_yaml(data / "runs" / rid / "manifest.yaml")
    assert "capture" not in doc
    assert not (data / "runs" / rid / ".rig" / "artifact.tar.gz").exists()

    root2, m2 = _deployment()
    orig = bake.capture_run
    bake.capture_run = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rid2, data2 = _open(m2, root2)
        assert "capture failed" in err.getvalue()  # WARNed…
        assert (data2 / "runs" / rid2 / "manifest.yaml").exists()  # …and the run still opened
        assert "capture" not in load_yaml(data2 / "runs" / rid2 / "manifest.yaml")
    finally:
        bake.capture_run = orig


def test_reconstruct_native_capture_verifies_and_localizes():
    root, m = _deployment()
    rid, data = _open(m, root)
    run_dir = data / "runs" / rid
    dest = pathlib.Path(tempfile.mkdtemp()) / "tree"
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = reconstruct.cmd_reconstruct(None, run_ref=str(run_dir), into=str(dest), config=None)
    assert rc == 0
    assert (dest / "vehicle.yaml").exists() and (dest / "services" / "playr" / "playr-up").exists()
    local = load_yaml(dest / "vehicle.local.yaml")
    assert local["data_dir"] == str((dest / "var" / "data").resolve())  # localized off the vehicle path
    assert "rig replay" in out.getvalue()  # the next step is printed
    # tamper -> sha mismatch refused
    (run_dir / ".rig" / "artifact.tar.gz").write_bytes(b"garbage")
    try:
        reconstruct.cmd_reconstruct(None, run_ref=str(run_dir),
                                    into=str(dest.parent / "t2"), config=None)
        assert False, "sha mismatch must refuse"
    except RigError as exc:
        assert "sha256 mismatch" in str(exc)


def test_reconstruct_lays_configs_out_by_tier():
    """A reconstructed tree IS a deployment tree: `rig add` after it must not meet a second
    convention. Through v0.2.47 every row was flattened into config/sensors/."""
    from rig_cli.manifest import load_manifest
    root, m = _deployment(player=("nav", "playr"))
    rid, data = _open(m, root)
    dest = pathlib.Path(tempfile.mkdtemp()) / "tree"
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        assert reconstruct.cmd_reconstruct(None, run_ref=str(data / "runs" / rid),
                                           into=str(dest), config=None) == 0
    assert (dest / "config" / "sensors" / "cam.yaml").is_file()
    assert (dest / "config" / "autonomy" / "nav.yaml").is_file()
    assert not (dest / "config" / "sensors" / "nav.yaml").exists()
    rows = {s.name: str(s.config) for s in load_manifest(dest).sensors}   # rows point where files are
    assert rows["nav"].endswith("config/autonomy/nav.yaml") and pathlib.Path(rows["nav"]).is_file()


def test_reconstructed_tree_is_editable_by_the_line_verbs():
    """A staged tree IS a deployment tree: `rig swap` (and `pkg remove`) edit services.yaml by
    line, so the capture must write rig's generated form — safe_dump's block form parsed fine but
    made every reconstructed tree refuse the edit ('not in the generated single-line form')."""
    from rig_cli import install
    from rig_cli.manifest import load_manifest
    root, m = _deployment()
    rid, data = _open(m, root)
    dest = pathlib.Path(tempfile.mkdtemp()) / "tree"
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        assert reconstruct.cmd_reconstruct(None, run_ref=str(data / "runs" / rid),
                                           into=str(dest), config=None) == 0
    routes = (dest / "services.yaml").read_text()
    assert "  sensa: { path: services/sensa }" in routes          # the generated single-line form
    assert load_yaml(dest / "services.yaml")["services"]["sensa"] == {"path": "services/sensa"}
    fresh = _service_repo(pathlib.Path(tempfile.mkdtemp()), "sensa")   # newer code, same service
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        assert install.swap(dest, "sensa", str(fresh)) == 0
    assert str(fresh) in (dest / "services.yaml").read_text() or "../" in (dest / "services.yaml").read_text()
    assert load_manifest(dest)                                     # and the tree still loads
    assert "not in the generated" not in err.getvalue()


def test_route_set_rewrites_a_block_form_route_in_place():
    """Trees reconstructed BEFORE the capture wrote the generated form are already out there —
    the editors read the dumped block shape too, rewriting only the `path:` line."""
    import yaml

    from rig_cli import install
    tree = pathlib.Path(tempfile.mkdtemp())
    (tree / "services.yaml").write_text(yaml.safe_dump(
        {"services": {"sensa": {"path": "services/sensa"}, "playr": {"path": "services/playr"}}},
        sort_keys=False))
    with contextlib.redirect_stderr(io.StringIO()):
        assert install._route_set(tree, "sensa", "../checkout") is True
    doc = load_yaml(tree / "services.yaml")
    assert doc["services"]["sensa"] == {"path": "../checkout"}     # re-pointed
    assert doc["services"]["playr"] == {"path": "services/playr"}  # neighbour untouched
    with contextlib.redirect_stderr(io.StringIO()):
        install._drop_route(tree, "playr")                          # `pkg remove`'s half
    doc = load_yaml(tree / "services.yaml")
    assert "playr" not in doc["services"] and "sensa" in doc["services"]


def test_reconstruct_missing_capture_names_the_retrofit_path():
    run_dir = pathlib.Path(tempfile.mkdtemp()) / "r"
    run_dir.mkdir()
    (run_dir / "manifest.yaml").write_text("run: r\nartifact: v9\n")
    try:
        reconstruct.cmd_reconstruct(None, run_ref=str(run_dir), into=None, config=None)
        assert False
    except RigError as exc:
        assert "v9.tar.gz" in str(exc) and "retrofit" in str(exc)


def _snapshot(run_dir: pathlib.Path, files: dict[str, bytes]) -> str:
    digest = runs._config_digest(files)
    snap = run_dir / ".rig" / "config" / digest
    for rel, blob in files.items():
        (snap / rel).parent.mkdir(parents=True, exist_ok=True)
        (snap / rel).write_bytes(blob)
    return digest


def test_retrofit_then_reconstruct_overlays_last_snapshot_by_default():
    root, m = _deployment()
    # a "deploy artifact": capture the tree into a tag-shaped tarball under var/artifacts
    tmp_run = pathlib.Path(tempfile.mkdtemp()) / "x"
    tmp_run.mkdir()
    bake.capture_run(root, m, tmp_run)
    arts = root / "var" / "artifacts"
    arts.mkdir(parents=True)
    (tmp_run / ".rig" / "artifact.tar.gz").rename(arts / "v9.tar.gz")
    # an OLD run: manifest names the tag, no capture; snapshot carries a between-runs config edit
    run_dir = pathlib.Path(tempfile.mkdtemp()) / "20260829T000000Z_flight"
    run_dir.mkdir()
    snap_files = {
        "vehicle.yaml": (  # same rows the artifact has — the drift is in the rendered config
            b"vehicle: veh\nvehicle_id: 1\n"
            b"sensors:\n- {name: cam, service: sensa, config: config/sensors/cam.yaml, "
            b"enabled: true, order: 10}\n"
            b"autonomy:\n- {name: bag_player, service: playr, "
            b"config: config/sensors/bag_player.yaml, enabled: false, order: 999}\n"),
        "rendered/cam.yaml": b"service: sensa\nname: cam\nvalue: EDITED-BETWEEN-RUNS\n",
    }
    digest = _snapshot(run_dir, snap_files)
    import yaml as _y
    (run_dir / "manifest.yaml").write_text(_y.safe_dump(
        {"run": run_dir.name, "artifact": "v9", "ups": [{"at": "x", "config": digest}]}))
    rc = reconstruct.cmd_retrofit(root, run_refs=[str(run_dir)], artifact=None,
                                  from_dir=str(arts))
    assert rc == 0
    doc = load_yaml(run_dir / "manifest.yaml")
    assert doc["capture"]["retrofitted"] and doc["capture"]["sha256"]
    dest = pathlib.Path(tempfile.mkdtemp()) / "tree"
    with contextlib.redirect_stdout(io.StringIO()):
        rc = reconstruct.cmd_reconstruct(None, run_ref=str(run_dir), into=str(dest), config=None)
    assert rc == 0
    # the retrofitted default overlaid the LAST ups snapshot: the edit is in the tree
    assert b"EDITED-BETWEEN-RUNS" in (dest / "config" / "sensors" / "cam.yaml").read_bytes()
    assert (dest / "services" / "sensa" / "sensa-up").exists()  # surfaces from the artifact


def test_retrofit_refuses_mismatch_and_corrupt_snapshot_refused():
    root, m = _deployment()
    run_dir = pathlib.Path(tempfile.mkdtemp()) / "r"
    run_dir.mkdir()
    (run_dir / "manifest.yaml").write_text("run: r\nartifact: v9\n")
    bad = pathlib.Path(tempfile.mkdtemp()) / "other.tar.gz"
    bad.write_bytes(b"x")
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = reconstruct.cmd_retrofit(root, run_refs=[str(run_dir)], artifact=str(bad),
                                      from_dir=None)
    assert rc == 1 and "false provenance" in err.getvalue()

    # corrupt snapshot: dir name no longer matches its content digest -> refused at overlay
    root2, m2 = _deployment()
    rid, data = _open(m2, root2)
    run2 = data / "runs" / rid
    digest = _snapshot(run2, {"vehicle.yaml": b"vehicle: veh\n"})
    (run2 / ".rig" / "config" / digest / "vehicle.yaml").write_bytes(b"tampered: true\n")
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            reconstruct.cmd_reconstruct(None, run_ref=str(run2),
                                        into=str(run2.parent / "t3"), config=digest)
        assert False, "corrupt snapshot must refuse"
    except RigError as exc:
        assert "content-address" in str(exc)




def test_reconstruct_links_source_into_tree_registry_by_default():
    root, m = _deployment()
    rid, data = _open(m, root)
    run_dir = data / "runs" / rid
    dest = pathlib.Path(tempfile.mkdtemp()) / "tree"
    with contextlib.redirect_stdout(io.StringIO()):
        assert reconstruct.cmd_reconstruct(None, run_ref=str(run_dir), into=str(dest),
                                           config=None) == 0
    entry = dest / "var" / "data" / "runs" / rid
    assert entry.is_symlink() and entry.resolve() == run_dir.resolve()  # a reference, not a copy
    dest2 = dest.parent / "tree2"
    with contextlib.redirect_stdout(io.StringIO()):
        assert reconstruct.cmd_reconstruct(None, run_ref=str(run_dir), into=str(dest2),
                                           config=None, copy_run=True) == 0
    e2 = dest2 / "var" / "data" / "runs" / rid
    assert e2.is_dir() and not e2.is_symlink()  # --copy-run: a real copy
    dest3 = dest.parent / "tree3"
    with contextlib.redirect_stdout(io.StringIO()):
        assert reconstruct.cmd_reconstruct(None, run_ref=str(run_dir), into=str(dest3),
                                           config=None, no_import=True) == 0
    assert not (dest3 / "var" / "data" / "runs" / rid).exists()  # opted out


def test_reconstruct_enable_replay_path_wires_the_player_row():
    from rig_cli.manifest import load_manifest
    root, m = _deployment(player=("nav", "playr"))  # an autonomy row that is NOT the player
    rid, data = _open(m, root)
    run_dir = data / "runs" / rid
    player = _player_repo(pathlib.Path(tempfile.mkdtemp()))
    dest = pathlib.Path(tempfile.mkdtemp()) / "tree"
    err = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
        rc = reconstruct.cmd_reconstruct(None, run_ref=str(run_dir), into=str(dest), config=None,
                                         enable_replay=str(player))
    assert rc == 0, err.getvalue()
    assert "wired" in err.getvalue() and "harness only" in err.getvalue()
    assert f"ros2-bag-player: {{ path: {player.resolve()} }}" in (dest / "services.yaml").read_text()
    assert "play:" in (dest / "config" / "autonomy" / "bag_player.yaml").read_text()  # the example
    # the captured vehicle.yaml is safe_dump's INDENTLESS shape with an EXISTING autonomy section —
    # the row lands in it (at the section's own column) and the tree loads
    rows = [s for s in load_manifest(dest).sensors if s.tier == "autonomy"]
    assert [(s.name, s.service, s.enabled, s.order) for s in rows] == \
        [("nav", "playr", False, 999), ("bag_player", "ros2-bag-player", False, 999)]
    # a rig-infra CHECKOUT (the dir containing ros2-bag-player/) is accepted too
    dest2 = dest.parent / "tree2"
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        assert reconstruct.cmd_reconstruct(None, run_ref=str(run_dir), into=str(dest2), config=None,
                                           enable_replay=str(player.parent)) == 0
    assert any(s.service == "ros2-bag-player" for s in load_manifest(dest2).sensors)
    # a directory that is not the player refuses BEFORE extraction
    other = _service_repo(pathlib.Path(tempfile.mkdtemp()), "sensb")
    try:
        reconstruct.cmd_reconstruct(None, run_ref=str(run_dir), into=str(dest.parent / "t3"),
                                    config=None, enable_replay=str(other))
        assert False, "a non-player dir must refuse"
    except RigError as exc:
        assert "service: ros2-bag-player" in str(exc) and not (dest.parent / "t3").exists()


def test_reconstruct_player_hint_and_noop_when_present():
    root, m = _deployment()  # its 'bag_player' row is service playr — NOT the player
    rid, data = _open(m, root)
    dest = pathlib.Path(tempfile.mkdtemp()) / "tree"
    err = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
        assert reconstruct.cmd_reconstruct(None, run_ref=str(data / "runs" / rid),
                                           into=str(dest), config=None) == 0
    assert "--enable-replay" in err.getvalue()  # detect-and-tell — never a silent injection
    assert "ros2-bag-player" not in (dest / "services.yaml").read_text()
    # a run that carried the player (every capture since v0.2.36): --enable-replay is a no-op
    root2, m2 = _deployment(player=("bag_player", "ros2-bag-player"))
    rid2, data2 = _open(m2, root2)
    player = _player_repo(pathlib.Path(tempfile.mkdtemp()))
    dest2 = pathlib.Path(tempfile.mkdtemp()) / "tree"
    err = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
        assert reconstruct.cmd_reconstruct(None, run_ref=str(data2 / "runs" / rid2),
                                           into=str(dest2), config=None,
                                           enable_replay=str(player)) == 0
    assert "already carries" in err.getvalue() and "--enable-replay" not in err.getvalue()
    assert "path: services/ros2-bag-player" in (dest2 / "services.yaml").read_text()  # untouched


def test_reconstruct_enable_replay_registry_ref_installs_disabled_and_last():
    import os
    import subprocess

    import yaml

    from rig_cli.cli import main
    from rig_cli.lock import load_lock
    from rig_cli.manifest import load_manifest
    from rig_cli.registry_scaffold import registry_init
    base = pathlib.Path(tempfile.mkdtemp())
    player = _player_repo(base)  # base/rig-infra/ros2-bag-player — the collection repo is git-tracked
    repo = player.parent

    def git(*args):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                              cwd=repo, capture_output=True, text=True, check=True)
    git("init", "-q")
    git("add", ".")
    git("commit", "-q", "-m", "player")
    rev = git("rev-parse", "HEAD").stdout.strip()
    reg = base / "reg"
    with contextlib.redirect_stderr(io.StringIO()):
        registry_init(reg, namespace="testns")
    d = reg / "services" / "ros2-bag-player"
    d.mkdir(parents=True)
    (d / "manifest.yaml").write_text(yaml.safe_dump({
        "kind": "service", "name": "ros2-bag-player", "version": "1.10.0",
        "source": {"repo": str(repo), "rev": rev, "path": "ros2-bag-player"}}))
    old_home = os.environ.get("RIG_HOME")
    os.environ["RIG_HOME"] = str(base / "home")
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            assert main(["registry", "index", str(reg)]) == 0
            assert main(["setup", "--no-default-registry"]) == 0
            assert main(["registry", "add", "testns", "--path", str(reg)]) == 0
        root, m = _deployment(player=None)
        rid, data = _open(m, root)
        dest = base / "tree"
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            rc = reconstruct.cmd_reconstruct(None, run_ref=str(data / "runs" / rid),
                                             into=str(dest), config=None,
                                             enable_replay="testns/ros2-bag-player@1.10.0")
        assert rc == 0, err.getvalue()
        assert (dest / "services" / "ros2-bag-player" / ".vendored.yaml").is_file()  # vendored
        row = next(s for s in load_manifest(dest).sensors if s.service == "ros2-bag-player")
        # the installer's defaults (enabled, max+10) are overridden: declared-disabled, LAST forever
        assert (row.name, row.tier, row.enabled, row.order) == ("bag_player", "autonomy", False, 999)
        assert "testns/ros2-bag-player@1.10.0" in load_lock(dest)["packages"]  # pinned
        try:  # a ref that is not the player refuses BEFORE extraction
            reconstruct.cmd_reconstruct(None, run_ref=str(data / "runs" / rid),
                                        into=str(base / "t2"), config=None,
                                        enable_replay="testns/zenoh-router")
            assert False, "a non-player ref must refuse"
        except RigError as exc:
            assert "registry ref" in str(exc) and not (base / "t2").exists()
    finally:
        if old_home is None:
            os.environ.pop("RIG_HOME", None)
        else:
            os.environ["RIG_HOME"] = old_home


def test_reconstruct_registry_localizes_images_registry():
    from rig_cli.manifest import load_manifest
    root, m = _deployment()
    rid, data = _open(m, root)
    run_dir = data / "runs" / rid
    dest = pathlib.Path(tempfile.mkdtemp()) / "tree"
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        assert reconstruct.cmd_reconstruct(None, run_ref=str(run_dir), into=str(dest), config=None,
                                           registry="localhost:5000/") == 0
    local = load_yaml(dest / "vehicle.local.yaml")
    assert local["images"] == {"registry": "localhost:5000"} and local["data_dir"]  # slash dropped
    assert load_manifest(dest).image_registry == "localhost:5000"  # tree-local outranks vehicle.yaml
    try:
        reconstruct.cmd_reconstruct(None, run_ref=str(run_dir), into=str(dest.parent / "t2"),
                                    config=None, registry="http://localhost:5000")
        assert False, "a URL must refuse"
    except RigError as exc:
        assert "HOST" in str(exc) and not (dest.parent / "t2").exists()  # refused BEFORE extraction

# --- v0.2.51: reconstruction never writes outside its own tree (findings 3, 14) ---------------

def test_overlay_redirects_rows_that_point_outside_the_tree():
    """A source deployment's rows may point ANYWHERE — an absolute path, a ../shared config —
    and the snapshot carries those rows verbatim. `tree / <absolute>` IS the absolute path, so
    the overlay wrote the run's historical config over the operator's CURRENT one in the
    original deployment. Those land in the tree's own tier layout now, and the row follows."""
    from rig_cli.manifest import load_manifest
    base = pathlib.Path(tempfile.mkdtemp())
    shared = base / "shared"
    shared.mkdir()
    for n in ("cam", "nav"):
        (shared / f"{n}.yaml").write_text(f"service: x\nname: {n}\nvalue: CURRENT\n")
    snap = base / "snap"
    (snap / "rendered").mkdir(parents=True)
    (snap / "vehicle.yaml").write_text(
        "vehicle: veh\nvehicle_id: 1\n"
        f"sensors:\n- {{name: cam, service: x, config: {shared / 'cam.yaml'}, enabled: true, order: 10}}\n"
        "autonomy:\n- {name: nav, service: x, config: ../shared/nav.yaml, enabled: false, order: 999}\n")
    for n in ("cam", "nav"):
        (snap / "rendered" / f"{n}.yaml").write_text(f"service: x\nname: {n}\nvalue: HISTORICAL\n")
    tree = base / "tree"
    tree.mkdir()
    written = reconstruct._overlay(tree, snap)
    assert set(written) == {"vehicle.yaml", "config/sensors/cam.yaml", "config/autonomy/nav.yaml"}
    for n in ("cam", "nav"):
        assert "CURRENT" in (shared / f"{n}.yaml").read_text()       # the source is untouched
    assert "HISTORICAL" in (tree / "config" / "sensors" / "cam.yaml").read_text()
    assert "HISTORICAL" in (tree / "config" / "autonomy" / "nav.yaml").read_text()
    rows = {r["name"]: r["config"] for tier in ("sensors", "autonomy")
            for r in load_yaml(tree / "vehicle.yaml")[tier]}
    assert rows == {"cam": "config/sensors/cam.yaml", "nav": "config/autonomy/nav.yaml"}


def test_overlay_keeps_a_contained_row_path_verbatim():
    """The redirect is for escapes only: a row that already points inside the tree keeps its own
    path — reconstruct reads the row, never a convention (pre-v0.2.48 artifacts flattened every
    tier into config/sensors/ and must keep working)."""
    base = pathlib.Path(tempfile.mkdtemp())
    snap = base / "snap"
    (snap / "rendered").mkdir(parents=True)
    (snap / "vehicle.yaml").write_text(
        "vehicle: veh\nautonomy:\n- {name: nav, service: x, config: config/sensors/nav.yaml, "
        "enabled: false, order: 999}\n")
    (snap / "rendered" / "nav.yaml").write_text("service: x\nname: nav\n")
    tree = base / "tree"
    tree.mkdir()
    assert reconstruct._overlay(tree, snap) == ["vehicle.yaml", "config/sensors/nav.yaml"]
    assert (tree / "config" / "sensors" / "nav.yaml").is_file()
    assert "config: config/sensors/nav.yaml" in (tree / "vehicle.yaml").read_text()   # row untouched


def test_reconstruct_into_an_existing_empty_dir_makes_it_the_root():
    """--into accepted an empty directory and then shutil.move'd the tree INTO it:
    dest/<tag>/vehicle.yaml beside a dest/vehicle.local.yaml, and instructions naming dest."""
    root, m = _deployment()
    rid, data = _open(m, root)
    dest = pathlib.Path(tempfile.mkdtemp()) / "tree"
    dest.mkdir()                                                   # exists, empty
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        assert reconstruct.cmd_reconstruct(None, run_ref=str(data / "runs" / rid),
                                           into=str(dest), config=None, no_import=True) == 0
    assert (dest / "vehicle.yaml").is_file() and (dest / "rig").is_file()
    assert not [p for p in dest.iterdir() if p.is_dir() and (p / "vehicle.yaml").exists()]  # no nesting


def test_reconstruct_refuses_a_file_as_into():
    root, m = _deployment()
    rid, data = _open(m, root)
    dest = pathlib.Path(tempfile.mkdtemp()) / "not-a-dir"
    dest.write_text("x")
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            reconstruct.cmd_reconstruct(None, run_ref=str(data / "runs" / rid), into=str(dest),
                                        config=None, no_import=True)
    except RigError as exc:
        assert "empty directory" in str(exc)
    else:
        raise AssertionError("expected RigError")


def test_repoint_rows_round_trips_a_hand_shaped_manifest():
    """The redirect line-edits the generated single-line row form; a hand-shaped (block-form)
    vehicle.yaml has no such line, so it round-trips through YAML instead — comments go, the
    row lands on the contained path, and nothing else about the document changes."""
    veh = pathlib.Path(tempfile.mkdtemp()) / "vehicle.yaml"
    veh.write_text(
        "vehicle: veh\nvehicle_id: 3\n"
        "sensors:\n  - name: cam\n    service: x\n    config: /abs/cam.yaml\n    enabled: true\n"
        "    order: 10\n  - {name: gnss, service: y, config: config/sensors/gnss.yaml, order: 20}\n")
    reconstruct._repoint_rows(veh, {"cam": "config/sensors/cam.yaml"})
    doc = load_yaml(veh)
    assert doc["vehicle_id"] == 3
    rows = {r["name"]: r for r in doc["sensors"]}
    assert rows["cam"]["config"] == "config/sensors/cam.yaml" and rows["cam"]["order"] == 10
    assert rows["gnss"]["config"] == "config/sensors/gnss.yaml"           # untouched neighbour



def test_linked_run_with_instance_recordings_warns_copy_run():
    # A per-sensor replay source reads its recordings INSIDE a container that binds only the
    # tree's data root: a run linked from an archive is invisible there. Say so at reconstruct
    # time; a copied run needs no warning.
    import contextlib
    import io
    root, m = _deployment()
    rid, data = _open(m, root)
    run_dir = data / "runs" / rid
    rec = run_dir / "recordings" / "cam"
    rec.mkdir(parents=True)
    (rec / "cam-1.json").write_text("{}")
    for copy_run in (False, True):
        dest = pathlib.Path(tempfile.mkdtemp()) / "tree"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = reconstruct.cmd_reconstruct(None, run_ref=str(run_dir), into=str(dest), config=None,
                                             copy_run=copy_run)
        assert rc == 0
        warned = "--copy-run" in err.getvalue() and "recordings/cam" in err.getvalue()
        assert warned is (not copy_run), err.getvalue()
    assert reconstruct._instance_recordings(run_dir) == ["cam"]
    (run_dir / "recordings" / "empty").mkdir()
    assert reconstruct._instance_recordings(run_dir) == ["cam"]          # an empty dir is nothing



def _flat_session(d: pathlib.Path, prefix: str, header: dict, parts: int = 1) -> None:
    import json as _json
    (d / f"{prefix}.json").write_text(_json.dumps({"pixel_format": "GRAY8", "width": 8, "height": 8, **header}))
    (d / f"{prefix}.csv").write_text("frame_id,pts_ns,timestamp_ns,source,chunk_ns,camera_ns,system_ns\n")
    for i in range(parts):
        (d / f"{prefix}-{i:05d}.mkv").write_bytes(b"x" * 10)


def test_retrofit_recordings_adopts_the_sessions_inside_the_window():
    # Recordings made BEFORE camera-service knew the run registry sit outside every run (a flat
    # /data/recordings). `--recordings NAME=DIR` moves the sessions that began inside the run's
    # window under <run>/recordings/NAME/, stamps provenance, and lists the instance in stacks.
    import yaml as _y
    run_dir = pathlib.Path(tempfile.mkdtemp()) / "20260829T100000Z_flight"
    run_dir.mkdir()
    (run_dir / "manifest.yaml").write_text(_y.safe_dump(
        {"run": run_dir.name, "started": "2026-08-29T10:00:00+00:00", "ended": "2026-08-29T11:00:00+00:00",
         "stacks": ["gnss"]}))
    flat = pathlib.Path(tempfile.mkdtemp()) / "recordings"
    flat.mkdir()
    t = lambda iso: int(__import__("datetime").datetime.fromisoformat(iso).timestamp() * 1e9)  # noqa: E731
    _flat_session(flat, "cam-20260829-101500", {"first_timestamp_ns": t("2026-08-29T10:15:00+00:00")}, parts=2)
    _flat_session(flat, "cam-20260829-104000", {"base_timestamp_ns": t("2026-08-29T10:40:00+00:00")})   # older header
    _flat_session(flat, "cam-20260828-090000", {"created_unix_s": t("2026-08-28T09:00:00+00:00") / 1e9})  # the day before
    (flat / "notes.json").write_text("{}")                                                                 # not a sidecar
    rc = reconstruct.cmd_retrofit(pathlib.Path("."), run_refs=[str(run_dir)], artifact=None, from_dir=None,
                                  recordings=[f"cam={flat}"])
    assert rc == 0
    dest = run_dir / "recordings" / "cam"
    assert sorted(p.name for p in dest.iterdir()) == [
        "cam-20260829-101500-00000.mkv", "cam-20260829-101500-00001.mkv", "cam-20260829-101500.csv",
        "cam-20260829-101500.json", "cam-20260829-104000-00000.mkv", "cam-20260829-104000.csv",
        "cam-20260829-104000.json"]
    assert sorted(p.name for p in flat.iterdir()) == [                                                     # moved, not copied
        "cam-20260828-090000-00000.mkv", "cam-20260828-090000.csv", "cam-20260828-090000.json", "notes.json"]
    doc = load_yaml(run_dir / "manifest.yaml")
    block = doc["retrofit"]["recordings"]["cam"]
    assert block["sessions"] == ["cam-20260829-101500", "cam-20260829-104000"] and block["moved"] is True
    assert block["skipped_outside_window"] == ["cam-20260828-090000"] and block["from"] == str(flat.resolve())
    assert doc["stacks"] == ["gnss", "cam"]
    # the day-before session comes in with --all-sessions, copied this time (the originals stay)
    rc = reconstruct.cmd_retrofit(pathlib.Path("."), run_refs=[str(run_dir)], artifact=None, from_dir=None,
                                  recordings=[f"cam={flat}"], all_sessions=True, copy=True)
    assert rc == 0 and (dest / "cam-20260828-090000.json").exists() and (flat / "cam-20260828-090000.json").exists()
    doc = load_yaml(run_dir / "manifest.yaml")
    assert doc["retrofit"]["recordings"]["cam"]["sessions"] == ["cam-20260828-090000"]   # the latest adoption
    # a session is adopted once: the same copy again is refused, nothing overwritten
    import contextlib
    import io
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = reconstruct.cmd_retrofit(pathlib.Path("."), run_refs=[str(run_dir)], artifact=None, from_dir=None,
                                      recordings=[f"cam={flat}"], all_sessions=True, copy=True)
    assert rc == 1 and "adopted once" in err.getvalue()
    assert reconstruct._instance_recordings(run_dir) == ["cam"]


def test_retrofit_recordings_refusals_are_legible():
    import contextlib
    import io
    import yaml as _y
    run_dir = pathlib.Path(tempfile.mkdtemp()) / "20260829T100000Z_x"
    run_dir.mkdir()
    (run_dir / "manifest.yaml").write_text(_y.safe_dump({"run": run_dir.name}))          # no window
    flat = pathlib.Path(tempfile.mkdtemp())
    _flat_session(flat, "cam-1", {"created_unix_s": 1.0})
    for specs, extra, needle in (([f"cam={flat}"], {}, "no started/ended window"),
                                 (["cam"], {"all_sessions": True}, "NAME=DIR"),
                                 (["../x=" + str(flat)], {"all_sessions": True}, "instance name"),
                                 ([f"cam={flat}/missing"], {"all_sessions": True}, "no directory"),
                                 ([f"cam={pathlib.Path(tempfile.mkdtemp())}"], {"all_sessions": True}, "no camera-service sessions")):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = reconstruct.cmd_retrofit(pathlib.Path("."), run_refs=[str(run_dir)], artifact=None,
                                          from_dir=None, recordings=specs, **extra)
        assert rc == 1 and needle in err.getvalue(), (specs, err.getvalue())
    (run_dir / "manifest.yaml").write_text(_y.safe_dump(
        {"run": run_dir.name, "started": "2026-08-29T10:00:00+00:00", "ended": "2026-08-29T11:00:00+00:00"}))
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = reconstruct.cmd_retrofit(pathlib.Path("."), run_refs=[str(run_dir)], artifact=None,
                                      from_dir=None, recordings=[f"cam={flat}"])
    assert rc == 1 and "none of the 1 session(s)" in err.getvalue() and "--all-sessions" in err.getvalue()


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
