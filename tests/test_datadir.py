"""The registry's home (rig ≥ v0.2.56): the USER tier (~/.rig/config.yaml data_dir — `rig setup
--data-dir`, the first-run question), `rig setup --show`, `--migrate` (move a registry, symlink
left behind, catalog root rewritten), and `rig run archive` (bytes to a drive, a linked entry
kept). Run: python3 tests/test_datadir.py
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

from rig_cli import RigError, datadir, registries, runcatalog, runs, userconfig  # noqa: E402
from rig_cli.cli import main  # noqa: E402
from rig_cli.completions import candidates  # noqa: E402
from rig_cli.manifest import load_manifest  # noqa: E402

for _stray in [k for k in os.environ
               if k in ("RIG_VEHICLE_ID", "RIG_VEHICLE_NAME") or k.startswith("RIG_VAR_")]:
    os.environ.pop(_stray)


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


def _fresh():
    """(RIG_HOME, an absent machine file) — a laptop nobody provisioned."""
    return (str(pathlib.Path(tempfile.mkdtemp()) / "home"),
            str(pathlib.Path(tempfile.mkdtemp()) / "absent.yaml"))


def _machine(data_dir=None) -> str:
    path = pathlib.Path(tempfile.mkdtemp()) / "vehicle.local.yaml"
    path.write_text("vehicle: box\nvehicle_id: 1\n" + (f"data_dir: {data_dir}\n" if data_dir else ""))
    return str(path)


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


def _run_dir(data: pathlib.Path, name: str, *, sealed=True, kb=64) -> pathlib.Path:
    d = data / "runs" / name
    (d / "bags").mkdir(parents=True)
    (d / "bags" / "big.mcap").write_bytes(b"x" * kb * 1024)
    (d / "manifest.yaml").write_text(f"run: {name}\nvehicle: veh\nvehicle_id: 4\n"
                                     f"started: 2026-09-01T10:00:00Z\n"
                                     + ("ended: 2026-09-01T11:00:00Z\n" if sealed else ""))
    return d


def _cli(*argv, cwd=None) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    old = os.getcwd()
    os.chdir(cwd or tempfile.mkdtemp())
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = main(list(argv))
            except SystemExit as exc:
                rc = int(exc.code or 0)
    finally:
        os.chdir(old)
    return rc, out.getvalue(), err.getvalue()


# --- the user tier ------------------------------------------------------------------------------------

def test_user_data_dir_precedence_tree_user_machine_vehicle_yaml():
    home, absent = _fresh()
    machine_dd = pathlib.Path(tempfile.mkdtemp()) / "machine-data"
    user_dd = pathlib.Path(tempfile.mkdtemp()) / "user-data"
    with _env(RIG_HOME=home, RIG_VEHICLE_LOCAL=_machine(machine_dd)):
        assert load_manifest(_tree(yaml_data_dir="/tmp/v")).data_dir == str(machine_dd)
        userconfig.save_user_config(data_dir=str(user_dd))
        m = load_manifest(_tree(yaml_data_dir="/tmp/v"))
        assert m.data_dir == str(user_dd) and m.host_data_dir is None   # the user's IS the shared one
        m = load_manifest(_tree(local_data_dir="/tmp/ws"))
        assert m.data_dir == "/tmp/ws" and m.host_data_dir == str(user_dd)  # tree beats user; reads through
        # completion mirrors it
        root = _tree(local_data_dir="/tmp/ws")
        from rig_cli.completions import _registry_dirs
        assert _registry_dirs(str(root)) == [pathlib.Path("/tmp/ws"), user_dd]
        assert _registry_dirs(str(_tree())) == [user_dd]
        # the catalog: the user's registry implicit (as "user"), the machine's kept as a root too
        assert runcatalog.roots() == [(user_dd, "user"), (machine_dd, "machine")]
        runcatalog.remember(user_dd)
        assert not runcatalog.roots_file().exists()                      # never written down
        # a typo'd key refuses loudly (a misspelled setting must not silently do nothing)
        userconfig.user_config_file().write_text("data_dirs: /x\n")
        try:
            load_manifest(_tree())
            assert False
        except RigError as exc:
            assert "unknown key" in str(exc) and "data_dirs" in str(exc)


def test_setup_data_dir_flag_and_first_run_question():
    home, absent = _fresh()
    with _env(RIG_HOME=home, RIG_VEHICLE_LOCAL=absent):
        # non-interactive first run: no question, a pointer instead
        rc, _, err = _cli("setup")
        assert rc == 0 and "none set" in err and "--data-dir" in err
        assert userconfig.user_data_dir() is None
        # the flag: absolute only (~ expands)
        rc, _, err = _cli("setup", "--data-dir", "relative/dir")
        assert rc == 1 and "ABSOLUTE" in err
        dd = pathlib.Path(tempfile.mkdtemp()) / "runs-home"
        rc, _, err = _cli("setup", "--data-dir", str(dd))
        assert rc == 0 and f"data_dir {dd}" in err and "minted" in err
        assert userconfig.user_data_dir() == str(dd)
        assert yaml.safe_load(userconfig.user_config_file().read_text()) == {"data_dir": str(dd)}
        rc, _, err = _cli("setup")
        assert rc == 0 and "left untouched" in err                        # idempotent
        # the run verbs now have a registry without sudo
        root = _tree()
        m = load_manifest(root)
        assert m.data_dir == str(dd)
        # the first-run QUESTION (interactive): Enter keeps the suggestion, `none` skips
        err = io.StringIO()
        with _env(RIG_HOME=_fresh()[0]), contextlib.redirect_stderr(err):
            rc = registries.setup(shell=False, no_default_registry=True, purge=False, yes=False,
                                  ask=lambda prompt: "")
            assert rc == 0 and userconfig.user_data_dir() == str(pathlib.Path("~/rig-data").expanduser())
            assert "[~/rig-data]" in err.getvalue() or "rig-data" in err.getvalue()
        with _env(RIG_HOME=_fresh()[0]), contextlib.redirect_stderr(io.StringIO()):
            rc = registries.setup(shell=False, no_default_registry=True, purge=False, yes=False,
                                  ask=lambda prompt: "none")
            assert rc == 0 and userconfig.user_data_dir() is None
        with _env(RIG_HOME=_fresh()[0]), contextlib.redirect_stderr(io.StringIO()):
            rc = registries.setup(shell=False, no_default_registry=True, purge=False, yes=False,
                                  ask=lambda prompt: "/Volumes/ssd/runs")
            assert userconfig.user_data_dir() == "/Volumes/ssd/runs"
        with _env(RIG_HOME=_fresh()[0]), contextlib.redirect_stderr(io.StringIO()):
            rc = registries.setup(shell=False, no_default_registry=True, purge=False, yes=False,
                                  skip_data_dir=True, ask=lambda prompt: (_ for _ in ()).throw(AssertionError("asked")))
            assert rc == 0 and userconfig.user_data_dir() is None
    # with a machine registry, no question and no user override unless asked for
    with _env(RIG_HOME=_fresh()[0], RIG_VEHICLE_LOCAL=_machine("/data/rig")), \
            contextlib.redirect_stderr(io.StringIO()) as err:
        rc = registries.setup(shell=False, no_default_registry=True, purge=False, yes=False,
                              ask=lambda prompt: (_ for _ in ()).throw(AssertionError("asked")))
        assert rc == 0 and "the machine's (/data/rig)" in err.getvalue()


def test_setup_show_is_one_screen():
    home, absent = _fresh()
    user_dd = pathlib.Path(tempfile.mkdtemp()) / "user-data"
    with _env(RIG_HOME=home, RIG_VEHICLE_LOCAL=absent):
        nowhere = tempfile.mkdtemp()   # --root to a dir without vehicle.yaml: outside any deployment
        rc, out, _ = _cli("--root", nowhere, "setup", "--show")
        assert rc == 0
        assert out.startswith("rig ") and "user state:" in out
        assert "absent (`rig setup --data-dir <dir>` writes it)" in out
        assert "NOT PROVISIONED" in out and "deployment: none here" in out
        assert "catalog roots: 0" in out
        _cli("setup", "--data-dir", str(user_dd))
        _run_dir(user_dd, "20260901T100000Z_a")
        ws = pathlib.Path(tempfile.mkdtemp()) / "ws-data"
        _run_dir(ws, "20260902T100000Z_b")
        root = _tree(local_data_dir=ws)
        rc, out, _ = _cli("--root", str(root), "setup", "--show")
        root = root.resolve()  # the CLI resolves --root (macOS: /var -> /private/var)
        assert f"config.yaml: data_dir {user_dd}  — 1 run(s)" in out
        assert f"deployment: {root} — vehicle 'veh' id 4" in out
        assert f"data_dir: {ws}  (from {root / 'vehicle.local.yaml'}) — 1 run(s)" in out
        assert f"host registry: {user_dd}  (read-through) — 1 run(s)" in out
        assert "catalog roots: 1" in out and f"user      {user_dd}" in out
    with _env(RIG_HOME=_fresh()[0], RIG_VEHICLE_LOCAL=_machine("/data/rig")):
        rc, out, _ = _cli("--root", str(_tree()), "setup", "--show")
        assert "machine identity:" in out and "vehicle 'box' id 1" in out
        assert "data_dir: /data/rig  (from " in out and "vehicle.local.yaml) — not created yet" in out


# --- migrate --------------------------------------------------------------------------------------------

def test_migrate_moves_registry_keeps_hardlinks_leaves_symlink_and_rewrites_catalog():
    home, absent = _fresh()
    old = pathlib.Path(tempfile.mkdtemp()) / "old-data"
    new = pathlib.Path(tempfile.mkdtemp()) / "ssd" / "rig-data"
    with _env(RIG_HOME=home, RIG_VEHICLE_LOCAL=absent):
        _cli("setup", "--data-dir", str(old))
        run = _run_dir(old, "20260901T100000Z_a")
        exp = run / "exports" / "review"
        exp.mkdir(parents=True)
        os.link(run / "manifest.yaml", exp / "manifest.yaml")             # an export's hardlink
        runcatalog.remember(old / "somewhere-else")                         # an unrelated root
        (old / "current").symlink_to(pathlib.Path("runs") / "20260901T100000Z_a")
        rc, _, err = _cli("setup", "--data-dir", str(new), "--migrate")
        assert rc == 1 and "OPEN run" in err and userconfig.user_data_dir() == str(old)
        (old / "current").unlink()
        rc, _, err = _cli("setup", "--migrate")
        assert rc == 1 and "needs --data-dir" in err
        rc, _, err = _cli("setup", "--data-dir", str(new), "--migrate")
        assert rc == 0, err
        assert "moved" in err and "symlink" in err
        assert userconfig.user_data_dir() == str(new)
        assert (new / "runs" / "20260901T100000Z_a" / "bags" / "big.mcap").stat().st_size == 64 * 1024
        moved = new / "runs" / "20260901T100000Z_a"
        assert (moved / "manifest.yaml").stat().st_ino == (moved / "exports" / "review" / "manifest.yaml").stat().st_ino
        assert old.is_symlink() and old.resolve() == new.resolve()         # the old path still works
        assert (old / "runs" / "20260901T100000Z_a" / "manifest.yaml").is_file()
        m = load_manifest(_tree())
        assert m.data_dir == str(new)
        assert [r.run for r in runs.list_runs(m)] == ["20260901T100000Z_a"]
        # a second migrate onto a non-empty dir refuses; --keep-old copies without the symlink
        third = pathlib.Path(tempfile.mkdtemp()) / "third"
        (third / "junk").mkdir(parents=True)
        rc, _, err = _cli("setup", "--data-dir", str(third), "--migrate")
        assert rc == 1 and "not empty" in err
        fourth = pathlib.Path(tempfile.mkdtemp()) / "fourth"
        rc, _, err = _cli("setup", "--data-dir", str(fourth), "--migrate", "--keep-old")
        assert rc == 0 and "kept as-is" in err
        assert (new / "runs").is_dir() and (fourth / "runs" / "20260901T100000Z_a").is_dir()
        assert not new.is_symlink()


def test_provision_migrate_moves_the_machine_registry():
    home, _ = _fresh()
    old = pathlib.Path(tempfile.mkdtemp()) / "machine-data"
    new = pathlib.Path(tempfile.mkdtemp()) / "moved"
    mfile = _machine(old)
    _run_dir(old, "20260901T100000Z_m")
    with _env(RIG_HOME=home, RIG_VEHICLE_LOCAL=mfile):
        rc, _, err = _cli("provision", "--migrate")
        assert rc == 1 and "needs --data-dir" in err
        rc, _, err = _cli("provision", "--data-dir", str(new), "--migrate")
        assert rc == 0, err
        assert yaml.safe_load(pathlib.Path(mfile).read_text())["data_dir"] == str(new)
        assert (new / "runs" / "20260901T100000Z_m" / "manifest.yaml").is_file() and old.is_symlink()


# --- archive ---------------------------------------------------------------------------------------------

def test_archive_moves_bytes_keeps_a_linked_entry_and_joins_the_catalog():
    home, absent = _fresh()
    dd = pathlib.Path(tempfile.mkdtemp()) / "data"
    nas = pathlib.Path(tempfile.mkdtemp()) / "nas"
    nas.mkdir()
    with _env(RIG_HOME=home, RIG_VEHICLE_LOCAL=absent):
        _cli("setup", "--data-dir", str(dd))
        _run_dir(dd, "20260901T100000Z_a")
        _run_dir(dd, "20260902T100000Z_b", sealed=False)
        opened = _run_dir(dd, "20260903T100000Z_open", sealed=False)
        (dd / "current").symlink_to(pathlib.Path("runs") / opened.name)
        root = _tree()
        rc, _, err = _cli("run", "archive", "20260901T100000Z_a", "20260902T100000Z_b",
                          "20260903T100000Z_open", "nope", "--to", str(nas), cwd=root)
        assert rc == 1
        assert "not sealed" in err and "OPEN run" in err and "no run 'nope'" in err
        archived = nas / "veh" / "20260901T100000Z_a"
        assert (archived / "bags" / "big.mcap").stat().st_size == 64 * 1024
        entry = dd / "runs" / "20260901T100000Z_a"
        assert entry.is_symlink() and entry.resolve() == archived.resolve()   # linked entry
        assert (dd / "runs" / "20260902T100000Z_b" / "bags").is_dir()         # untouched
        m = load_manifest(root)
        rows = {r.run: r for r in runs.list_runs(m)}
        assert rows["20260901T100000Z_a"].linked and rows["20260901T100000Z_a"].state == "sealed"
        assert runs.resolve_ref(m, "a", verb="t")[1] == entry                 # still resolves
        assert runcatalog.roots()[-1] == (nas, "archive")
        entries, _ = runcatalog.scan()
        assert sum(1 for e in entries if e.run == "20260901T100000Z_a") == 1  # seen once
        # already archived: a no-op with the target named; --force moves an unsealed run
        rc, _, err = _cli("run", "archive", "20260901T100000Z_a", "--to", str(nas), cwd=root)
        assert rc == 0 and "already a link" in err
        rc, _, err = _cli("run", "archive", "20260902T100000Z_b", "--to", str(nas), "--force",
                          "--no-link", cwd=root)
        assert rc == 0 and not (dd / "runs" / "20260902T100000Z_b").exists()
        assert (nas / "veh" / "20260902T100000Z_b" / "manifest.yaml").is_file()
        # `run rm` on the linked entry unlinks only; an unmounted archive lists as dangling
        rc, _, err = _cli("run", "rm", "20260901T100000Z_a", cwd=root)
        assert rc == 0 and "unlinked" in err and (archived / "manifest.yaml").is_file()
        import shutil
        entry.symlink_to(archived.resolve())
        shutil.rmtree(nas)
        rows = {r.run: r for r in runs.list_runs(load_manifest(root))}
        assert rows["20260901T100000Z_a"].state == "dangling"
        # a host-registry run is not this registry's to archive
        ws = pathlib.Path(tempfile.mkdtemp()) / "ws"
        (ws / "runs").mkdir(parents=True)
        nas.mkdir()
        rc, _, err = _cli("run", "archive", "20260902T100000Z_b", "--to", str(nas),
                          cwd=_tree(local_data_dir=ws))
        assert rc == 1 and "no run" in err


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
