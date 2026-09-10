"""`rig run export` — the `export:` rigging block, `export_profiles:`, the omit globs, the export
tree (hardlinks, exporter dirs left to their verbs, the run's own exports/ never nested), the
exporter env contract (RIG_EXPORT_*) and the provenance file. Run: python3 tests/test_export.py

A REAL deployment tree with a stub service whose launcher answers `export` (writes a "slim" bag
under $RIG_EXPORT_DEST/bags/<name>/ and echoes the channel it saw) — no mocks on rig's side.
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

from rig_cli import RigError, dispatch, export  # noqa: E402
from rig_cli.cli import main  # noqa: E402
from rig_cli.descriptor import load_descriptor  # noqa: E402
from rig_cli.manifest import load_manifest  # noqa: E402

os.environ["RIG_VEHICLE_LOCAL"] = str(pathlib.Path(tempfile.mkdtemp()) / "absent.yaml")
for _stray in [k for k in os.environ
               if k in ("RIG_VEHICLE_ID", "RIG_VEHICLE_NAME") or k.startswith("RIG_VAR_")]:
    os.environ.pop(_stray)

_LAUNCHER = """\
#!/bin/sh
# $1 = config, $2 = verb. `export`: the contract — RIG_EXPORT_SOURCE (the run), RIG_EXPORT_DEST
# (write <data> under it), RIG_EXPORT_OPTIONS (the profile block for this instance), PROFILE, FORCE.
name=$(sed -n 's/^name: //p' "$1")
case "$2" in
  export)
    [ -d "$RIG_EXPORT_SOURCE" ] && [ -d "$RIG_EXPORT_DEST" ] && [ -f "$RIG_EXPORT_OPTIONS" ] || exit 3
    [ -f "$RIG_EXPORT_SOURCE/bags/$name/sess/sess_0.mcap" ] || exit 4
    out="$RIG_EXPORT_DEST/bags/$name/sess"
    mkdir -p "$out"
    cp "$RIG_EXPORT_OPTIONS" "$out/options.yaml"
    echo slim > "$out/sess_0.mcap"
    echo "$RIG_EXPORT_PROFILE force=${RIG_EXPORT_FORCE:-0} replay=${RIG_REPLAY_SOURCE:-none}" > "$out/env.txt"
    [ "$STUB_EXPORT_FAIL" = "$name" ] && exit 7
    exit 0 ;;
  *) exit 0 ;;
esac
"""


def tree(*, profiles: str = "", exporter: bool = True) -> tuple[pathlib.Path, pathlib.Path]:
    """(deployment root, data dir): one exporting service (bagsvc) with two instances, one plain."""
    root = pathlib.Path(tempfile.mkdtemp()) / "veh"
    svc = pathlib.Path(tempfile.mkdtemp()) / "bagsvc"
    svc.mkdir(parents=True)
    (svc / "rigging.yaml").write_text("service: bagsvc\nlauncher: bag-up\ntier: infra\n"
                                      "launch_surface: [bag-up]\n"
                                      + ("export: { data: 'bags/{name}' }\n" if exporter else ""))
    (svc / "bag-up").write_text(_LAUNCHER)
    (svc / "bag-up").chmod(0o755)
    plain = pathlib.Path(tempfile.mkdtemp()) / "camsvc"
    plain.mkdir(parents=True)
    (plain / "rigging.yaml").write_text("service: camsvc\nlauncher: cam-up\ntier: sensor\n"
                                        "launch_surface: [cam-up]\n")
    (plain / "cam-up").write_text("#!/bin/sh\nexit 0\n")
    (plain / "cam-up").chmod(0o755)
    data = pathlib.Path(tempfile.mkdtemp()) / "data"
    (root / "config" / "infra").mkdir(parents=True)
    (root / "config" / "sensors").mkdir(parents=True)
    (root / "vehicle.yaml").write_text(textwrap.dedent(f"""\
        vehicle: veh
        vehicle_id: 4
        data_dir: {data}
        infra:
          - {{name: bag_logger, service: bagsvc, config: config/infra/bag_logger.yaml}}
          - {{name: bag_aux, service: bagsvc, config: config/infra/bag_aux.yaml}}
        sensors:
          - {{name: cam_front, service: camsvc, config: config/sensors/cam_front.yaml}}
        """) + profiles)
    for n in ("bag_logger", "bag_aux"):
        (root / "config" / "infra" / f"{n}.yaml").write_text(f"service: bagsvc\nname: {n}\n")
    (root / "config" / "sensors" / "cam_front.yaml").write_text("service: camsvc\nname: cam_front\n")
    (root / "services.yaml").write_text(f"services:\n  bagsvc: {{ path: {svc} }}\n"
                                        f"  camsvc: {{ path: {plain} }}\n")
    return root, data


PROFILES = textwrap.dedent("""\
    export_profiles:
      review:
        omit: ["recordings/**/*.mkv", "scratch/*"]
        bagsvc: { preset: zstd_small, exclude: ['.*/points$'] }
        bag_aux: { preset: zstd_fast }
      novideo:
        omit: ["**/*.mkv"]
    """)


def run_dir(data: pathlib.Path, name="20260901T100000Z_field", *, sealed=True) -> pathlib.Path:
    run = data / "runs" / name
    for rel, body in {
        "bags/bag_logger/sess/sess_0.mcap": "x" * 4096, "bags/bag_logger/sess/metadata.yaml": "m",
        "bags/bag_aux/sess/sess_0.mcap": "y" * 2048,
        "recordings/cam_front/s1.mkv": "v" * 8192, "recordings/cam_front/s1.csv": "t,fid\n",
        "recordings/cam_front/s1.json": "{}", "scratch/tmp.bin": "z" * 100,
        "graph/bag_logger/epoch_20260901T100000Z.yaml": "schema: 1\n",
        ".rig/config/abc/rendered/bag_logger.yaml": "record: {}\n", ".rig/logs/bag_logger.log": "l",
        "exports/stale/manifest.yaml": "run: nope\n",  # an OLD export: never nested into a new one
    }.items():
        (run / rel).parent.mkdir(parents=True, exist_ok=True)
        (run / rel).write_text(body)
    (run / "manifest.yaml").write_text(f"run: {name}\nstarted: 2026-09-01T10:00:00Z\n"
                                       + ("ended: 2026-09-01T11:00:00Z\n" if sealed else ""))
    return run


def _cli(root: pathlib.Path, *argv) -> tuple[int, str]:
    err = io.StringIO()
    cwd = os.getcwd()
    os.chdir(root)
    try:
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            try:
                rc = main(list(argv))
            except SystemExit as exc:
                rc = int(exc.code or 0)
    finally:
        os.chdir(cwd)
    return rc, err.getvalue()


def _expect(fn, needle):
    try:
        fn()
        assert False, f"expected RigError mentioning {needle!r}"
    except RigError as exc:
        assert needle in str(exc), str(exc)


# --- the declarations ------------------------------------------------------------------------------

def test_descriptor_export_block_and_strictness():
    repo = pathlib.Path(tempfile.mkdtemp())

    def _load(block):
        (repo / "rigging.yaml").write_text("service: svc\nlauncher: svc-up\n" + block)
        return load_descriptor("svc", repo)

    assert _load("").export_source is None
    es = _load("export: { data: 'bags/{name}' }\n").export_source
    assert es is not None and es.data_path("bag_logger") == "bags/bag_logger"
    assert _load("export:\n  data: /video/{name}/\n").export_source.data == "video/{name}"
    for bad, needle in (("export: bags\n", "mapping"),
                        ("export: { data: 'bags/{name}', preset: zstd_small }\n", "unknown key"),
                        ("export: { data: '' }\n", "run-relative"),
                        ("export: { data: ../elsewhere }\n", "inside the run")):
        _expect(lambda: _load(bad), needle)


def test_manifest_export_profiles_parse_and_refusals():
    root, _ = tree(profiles=PROFILES)
    m = load_manifest(root)
    assert set(m.export_profiles) == {"review", "novideo"}
    assert m.export_profiles["review"]["omit"] == ["recordings/**/*.mkv", "scratch/*"]
    assert m.export_profiles["review"]["bagsvc"] == {"preset": "zstd_small", "exclude": [".*/points$"]}
    assert m.export_profiles["novideo"] == {"omit": ["**/*.mkv"]}
    assert load_manifest(tree()[0]).export_profiles == {}

    def _bad(text, needle):
        r, _ = tree(profiles=text)
        _expect(lambda: load_manifest(r), needle)

    _bad("export_profiles: [review]\n", "mapping")
    _bad("export_profiles:\n  'bad name': {}\n", "profile name")
    _bad("export_profiles:\n  review: { omit: 'recordings/**' }\n", "list")
    _bad("export_profiles:\n  review: { bagsvc: zstd_small }\n", "must be a mapping")
    r, _ = tree(profiles="export_profiles:\n  empty:\n")  # an empty profile = a plain slim copy
    assert load_manifest(r).export_profiles == {"empty": {"omit": []}}


def test_glob_to_regex():
    def hit(pattern, rel):
        return export.glob_to_regex(pattern).match(rel) is not None

    assert hit("recordings/**/*.mkv", "recordings/cam_front/s1.mkv")
    assert hit("recordings/**/*.mkv", "recordings/s1.mkv")            # ** matches zero segments
    assert hit("recordings/**/*.mkv", "recordings/a/b/c/s1.mkv")
    assert not hit("recordings/**/*.mkv", "recordings/cam_front/s1.csv")
    assert not hit("recordings/**/*.mkv", "other/recordings/s1.mkv")  # anchored at the run root
    assert hit("**/*.mkv", "s1.mkv") and hit("**/*.mkv", "a/b/s1.mkv")
    assert not hit("*.mkv", "a/s1.mkv")                               # * never crosses a slash
    assert hit("scratch/*", "scratch/tmp.bin") and not hit("scratch/*", "scratch/deep/x")
    assert hit("scratch/**", "scratch/deep/x")
    assert hit("bags/bag_?", "bags/bag_a") and not hit("bags/bag_?", "bags/bag_ab")
    assert not hit("a.b", "axb")                                      # dots are literal


def test_plan_skips_exports_and_exporter_dirs_and_omits():
    _, data = tree()
    run = run_dir(data)
    kept, left, left_bytes = export.plan(run, ["recordings/**/*.mkv", "scratch/*"],
                                         ["bags/bag_logger", "bags/bag_aux"])
    assert "manifest.yaml" in kept and ".rig/config/abc/rendered/bag_logger.yaml" in kept
    assert "recordings/cam_front/s1.csv" in kept and "recordings/cam_front/s1.json" in kept
    assert "graph/bag_logger/epoch_20260901T100000Z.yaml" in kept
    assert not any(p.startswith("bags/") for p in kept)      # the exporters' verbs fill those
    assert not any(p.startswith("exports/") for p in kept + left)  # never nest the exports
    assert sorted(left) == ["recordings/cam_front/s1.mkv", "scratch/tmp.bin"]
    assert left_bytes == 8192 + 100


# --- the verb --------------------------------------------------------------------------------------

def test_export_end_to_end_hardlinks_exporter_env_and_provenance():
    root, data = tree(profiles=PROFILES)
    run = run_dir(data)
    rc, err = _cli(root, "run", "export", "field", "--profile", "review")  # by LABEL
    assert rc == 0, err
    dest = run / "exports" / "review"
    assert "-> 20260901T100000Z_field (newest run with that label)" in err
    # kept files are hardlinks of the run's own (no disk cost), omitted ones absent, sidecars kept
    assert (dest / "manifest.yaml").stat().st_ino == (run / "manifest.yaml").stat().st_ino
    assert (dest / "recordings" / "cam_front" / "s1.csv").is_file()
    assert (dest / "recordings" / "cam_front" / "s1.json").is_file()
    assert not (dest / "recordings" / "cam_front" / "s1.mkv").exists()
    assert not (dest / "scratch").exists()
    assert not (dest / "exports").exists()                        # the old export not nested
    assert (dest / ".rig" / "config" / "abc" / "rendered" / "bag_logger.yaml").is_file()
    # each exporter ran with ITS options block (instance key beats service key), the channel set
    for name, want in (("bag_logger", {"preset": "zstd_small", "exclude": [".*/points$"]}),
                       ("bag_aux", {"preset": "zstd_fast"})):
        out = dest / "bags" / name / "sess"
        assert (out / "sess_0.mcap").read_text().strip() == "slim"
        assert yaml.safe_load((out / "options.yaml").read_text()) == want
        assert yaml.safe_load((dest / ".rig" / "export" / f"{name}.yaml").read_text()) == want
        assert (out / "env.txt").read_text().strip() == "review force=0 replay=none"
    assert not (dest / "bags" / "bag_logger" / "sess" / "metadata.yaml").exists()  # theirs to write
    prov = yaml.safe_load((dest / ".rig" / "export.yaml").read_text())["export"]
    assert prov["profile"] == "review" and prov["of"] == run.name and prov["ok"] is True
    assert prov["omit"] == ["recordings/**/*.mkv", "scratch/*"]
    assert prov["omitted"] == {"files": 2, "bytes": 8192 + 100}
    assert prov["kept"]["files"] == len(export.plan(run, prov["omit"],
                                                    ["bags/bag_logger", "bags/bag_aux"])[0])
    assert prov["kept"]["linked"] == prov["kept"]["files"] and prov["kept"]["copied"] == 0
    assert prov["services"]["bag_logger"] == {"service": "bagsvc", "data": "bags/bag_logger",
                                              "rc": 0, "options": {"preset": "zstd_small",
                                                                   "exclude": [".*/points$"]}}
    assert prov["bytes"]["export"] < prov["bytes"]["source"]
    assert "exports:" not in prov["source"]
    # the source run is untouched by its export (the exports/ dir aside)
    assert (run / "recordings" / "cam_front" / "s1.mkv").is_file()
    assert (run / "bags" / "bag_logger" / "sess" / "sess_0.mcap").read_text() == "x" * 4096
    # a second export refuses, --force redoes it and tells the exporters
    rc, err = _cli(root, "run", "export", run.name, "--profile", "review")
    assert rc == 1 and "--force" in err
    (dest / "stray.txt").write_text("gone on redo")
    rc, err = _cli(root, "run", "export", run.name, "--profile", "review", "--force")
    assert rc == 0, err
    assert not (dest / "stray.txt").exists()
    assert (dest / "bags" / "bag_aux" / "sess" / "env.txt").read_text().startswith("review force=1")


def test_export_single_profile_needs_no_name_and_empty_profile_is_a_slim_copy():
    root, data = tree(profiles="export_profiles:\n  copy:\n")
    run = run_dir(data)
    rc, err = _cli(root, "run", "export", str(run))          # by PATH; the one profile implied
    assert rc == 0, err
    dest = run / "exports" / "copy"
    assert (dest / "recordings" / "cam_front" / "s1.mkv").is_file()  # nothing omitted…
    assert (dest / "bags" / "bag_logger" / "sess" / "sess_0.mcap").read_text().strip() == "slim"
    assert yaml.safe_load((dest / ".rig" / "export" / "bag_logger.yaml").read_text()) == {}
    root2, data2 = tree(profiles=PROFILES)
    rc, err = _cli(root2, "run", "export", str(run_dir(data2)))
    assert rc == 1 and "--profile" in err and "review" in err     # two declared: name one


def test_export_refusals_and_exporter_failure_recorded():
    root, data = tree(profiles=PROFILES)
    run = run_dir(data)
    rc, err = _cli(root, "run", "export", run.name, "--profile", "nope")
    assert rc == 1 and "no profile 'nope'" in err and "novideo, review" in err
    rc, err = _cli(root, "run", "export", "missing", "--profile", "review")
    assert rc == 1 and "no run 'missing'" in err
    rc, err = _cli(tree()[0], "run", "export", str(run), "--profile", "review")
    assert rc == 1 and "no `export_profiles:`" in err
    # the OPEN run is refused; an unsealed one warns
    opened = run_dir(data, "20260902T100000Z_open", sealed=False)
    (data / "current").symlink_to(pathlib.Path("runs") / opened.name)
    rc, err = _cli(root, "run", "export", opened.name, "--profile", "review")
    assert rc == 1 and "OPEN" in err
    (data / "current").unlink()
    rc, err = _cli(root, "run", "export", opened.name, "--profile", "review")
    assert rc == 0 and "not sealed" in err
    # a failing exporter mars the export (rc 1, recorded), the rest still lands
    os.environ["STUB_EXPORT_FAIL"] = "bag_aux"
    try:
        rc, err = _cli(root, "run", "export", run.name, "--profile", "review")
    finally:
        os.environ.pop("STUB_EXPORT_FAIL")
    assert rc == 1 and "bag_aux [bagsvc] export failed (exit 7)" in err
    prov = yaml.safe_load((run / "exports" / "review" / ".rig" / "export.yaml").read_text())["export"]
    assert prov["ok"] is False and prov["services"]["bag_aux"]["rc"] == 7
    assert prov["services"]["bag_logger"]["rc"] == 0
    assert (run / "exports" / "review" / "manifest.yaml").is_file()


def test_export_dry_run_touches_nothing_and_names_unknown_profile_keys():
    root, data = tree(profiles=PROFILES + "  odd:\n    ghost: { preset: x }\n")
    run = run_dir(data)
    rc, err = _cli(root, "run", "export", run.name, "--profile", "odd", "--dry-run")
    assert rc == 0, err
    assert "'ghost', which is neither a service nor an instance" in err
    assert "exporters: bag_logger, bag_aux" in err and "export bags/bag_logger" in err
    assert not (run / "exports" / "odd").exists()


def test_exporter_without_data_in_the_run_is_noted_not_run():
    root, data = tree(profiles=PROFILES)
    run = run_dir(data)
    import shutil
    shutil.rmtree(run / "bags" / "bag_aux")
    rc, err = _cli(root, "run", "export", run.name, "--profile", "novideo")
    assert rc == 0, err
    assert "bag_aux: nothing under bags/bag_aux in the run" in err
    dest = run / "exports" / "novideo"
    assert (dest / "bags" / "bag_logger" / "sess" / "sess_0.mcap").is_file()
    assert not (dest / "bags" / "bag_aux").exists()
    prov = yaml.safe_load((dest / ".rig" / "export.yaml").read_text())["export"]
    assert list(prov["services"]) == ["bag_logger"]


def test_fleet_env_strips_the_export_channel():
    root, _ = tree()
    m = load_manifest(root)
    os.environ.update({"RIG_EXPORT_SOURCE": "/x", "RIG_EXPORT_DEST": "/y",
                       "RIG_EXPORT_OPTIONS": "/z", "RIG_EXPORT_PROFILE": "p", "RIG_EXPORT_FORCE": "1"})
    try:
        env = dispatch.fleet_env(m)
    finally:
        for k in ("RIG_EXPORT_SOURCE", "RIG_EXPORT_DEST", "RIG_EXPORT_OPTIONS",
                  "RIG_EXPORT_PROFILE", "RIG_EXPORT_FORCE"):
            os.environ.pop(k)
    assert not any(k.startswith("RIG_EXPORT_") for k in env)


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
