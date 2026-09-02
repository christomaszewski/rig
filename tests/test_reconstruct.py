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
    assert any(n.endswith("config/sensors/bag_player.yaml") for n in names)
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
