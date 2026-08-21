"""pkg install / rig add unification — service + profile installs, working-copy materialization,
lock anchoring, --locked reproduction. Run: python3 tests/test_install.py"""
import contextlib
import io
import os
import pathlib
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from rig_cli.cli import main  # noqa: E402
from rig_cli.init import init  # noqa: E402
from rig_cli.lock import load_lock, sha256_file  # noqa: E402
from rig_cli.manifest import load_manifest  # noqa: E402
from rig_cli.registry_scaffold import registry_init  # noqa: E402


@contextlib.contextmanager
def _env(**over):
    old = {k: os.environ.get(k) for k in over}
    os.environ.update({k: str(v) for k, v in over.items()})
    try:
        yield
    finally:
        for k, v in old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def _run(*argv) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = main(list(argv))
    return rc, out.getvalue(), err.getvalue()


def _git(*args, cwd):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          cwd=cwd, capture_output=True, text=True)


def _code_repo() -> tuple[pathlib.Path, str]:
    """One collection repo carrying TWO services (source.path case): infra `routerish` (named
    example) and sensor `camish` (instantiable)."""
    repo = pathlib.Path(tempfile.mkdtemp()) / "collection"
    shared = repo / "shared"
    shared.mkdir(parents=True)
    (shared / "build.sh").write_text("#!/bin/sh\necho built-from-source > /dev/null\n")
    (shared / "build.sh").chmod(0o755)
    for svc, tier, example in (
        ("routerish", "infra", "service: routerish\nname: routerish\nrate: 5\n"),
        ("camish", "sensor", "service: camish\ncamera: {type: usb}\n"),
        # hyphenated EMBEDDED name — the shipped rig-infra convention (e.g. `zenoh-router`);
        # accepted verbatim, while rig-DERIVED names stay ROS-safe
        ("dash-board", "infra", "service: dash-board\nname: dash-board\nport: 8080\n"),
    ):
        d = repo / svc
        (d / "config").mkdir(parents=True)
        build_line = ("build: { command: ../shared/build.sh, images: [routerish] }\n"
                      if svc == "routerish" else "")  # collection-sibling build ctx, NOT vendored
        (d / "rigging.yaml").write_text(
            f"service: {svc}\nlauncher: {svc}-up\ntier: {tier}\n{build_line}"
            f"examples: [config/{svc}.example.yaml]\nlaunch_surface: [{svc}-up]\n")
        (d / f"{svc}-up").write_text("#!/bin/sh\n")
        (d / f"{svc}-up").chmod(0o755)
        (d / "config" / f"{svc}.example.yaml").write_text(example)
    _git("init", "-q", cwd=repo)
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "code", cwd=repo)
    rev = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    return repo, rev


def _registry_with(repo: pathlib.Path, rev: str) -> pathlib.Path:
    reg = pathlib.Path(tempfile.mkdtemp()) / "reg"
    with contextlib.redirect_stderr(io.StringIO()):
        registry_init(reg, namespace="testns")
    for svc in ("routerish", "camish", "dash-board"):
        d = reg / "services" / svc
        d.mkdir(parents=True)
        (d / "manifest.yaml").write_text(yaml.safe_dump({
            "kind": "service", "name": svc, "version": "1.2.0",
            "source": {"repo": str(repo), "rev": rev, "path": svc}}))
    p = reg / "profiles" / "camish" / "acme-cam"
    (p / "config").mkdir(parents=True)
    (p / "manifest.yaml").write_text(yaml.safe_dump({
        "kind": "profile", "name": "acme-cam", "version": "2.0.0",
        "provides": {"sensor": [{"model": "ACME Cam", "match": ["acme", "usb:9999:*"]}]},
        "requires": {"service": "camish@^1.0"},
        "config": {"payload": "config/payload.yaml"}}))
    (p / "config" / "payload.yaml").write_text(
        "# tuned default\nservice: camish\ncamera: {type: usb}\nusb: {width: 1280}\n")
    for oname, payload in (("cam-tune", "usb: {width: 1600, fps: 25}\n"),
                           ("cam-tune-b", "usb: {width: 2222}\n")):
        o = reg / "overlays" / oname
        (o / "config").mkdir(parents=True)
        (o / "manifest.yaml").write_text(yaml.safe_dump({
            "kind": "overlay", "name": oname, "version": "1.0.0",
            "targets": [{"service": "camish"}], "project": "gideon",
            "config": {"payload": "config/delta.yaml"}}))
        (o / "config" / "delta.yaml").write_text(payload)
    _run("registry", "index", str(reg))
    return reg


def _world():
    """RIG_HOME + registry + a fresh deployment; returns (deployment_root, registry_root)."""
    repo, rev = _code_repo()
    reg = _registry_with(repo, rev)
    _run("setup", "--no-default-registry")
    _run("registry", "add", "testns", "--path", str(reg))
    target = pathlib.Path(tempfile.mkdtemp()) / "veh"
    with contextlib.redirect_stderr(io.StringIO()):
        init(target, no_git=True)
    return target, reg


def test_fetch_initializes_submodules():
    # Driver source arriving via submodule: the pinned checkout must init it recursively (rig
    # build runs in this tree; the superproject commit pins submodule revs, so exact-pin holds
    # transitively). Launch surfaces stay submodule-free by design — this is about source.
    with _env(RIG_HOME=tempfile.mkdtemp(), GIT_CONFIG_COUNT="1",
              GIT_CONFIG_KEY_0="protocol.file.allow", GIT_CONFIG_VALUE_0="always"):
        sub = pathlib.Path(tempfile.mkdtemp()) / "driver-core"
        sub.mkdir(parents=True)
        (sub / "core.txt").write_text("upstream\n")
        _git("init", "-q", cwd=sub)
        _git("add", "-A", cwd=sub)
        _git("commit", "-q", "-m", "core", cwd=sub)
        repo = pathlib.Path(tempfile.mkdtemp()) / "subbed"
        (repo / "config").mkdir(parents=True)
        (repo / "rigging.yaml").write_text(
            "service: subbed\nlauncher: subbed-up\ntier: sensor\n"
            "examples: [config/subbed.example.yaml]\nlaunch_surface: [subbed-up]\n")
        (repo / "subbed-up").write_text("#!/bin/sh\n")
        (repo / "subbed-up").chmod(0o755)
        (repo / "config" / "subbed.example.yaml").write_text("service: subbed\nrate: 1\n")
        _git("init", "-q", cwd=repo)
        _git("submodule", "add", str(sub), "vendor/core", cwd=repo)
        _git("add", "-A", cwd=repo)
        _git("commit", "-q", "-m", "code", cwd=repo)
        rev = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
        reg = pathlib.Path(tempfile.mkdtemp()) / "subreg"
        with contextlib.redirect_stderr(io.StringIO()):
            registry_init(reg, namespace="subreg")
        d = reg / "services" / "subbed"
        d.mkdir(parents=True)
        (d / "manifest.yaml").write_text(yaml.safe_dump({
            "kind": "service", "name": "subbed", "version": "1.0.0",
            "source": {"repo": str(repo), "rev": rev}}))
        assert _run("registry", "index", str(reg))[0] == 0
        _run("setup", "--no-default-registry")
        assert _run("registry", "add", "subreg", "--path", str(reg))[0] == 0
        root = pathlib.Path(tempfile.mkdtemp()) / "veh"
        with contextlib.redirect_stderr(io.StringIO()):
            init(root, no_git=True)
        rc, _, err = _run("--root", str(root), "pkg", "add", "subreg/subbed")
        assert rc == 0, err
        cache = pathlib.Path(os.environ["RIG_HOME"]) / "cache" / "src" / f"subbed-{rev[:12]}"
        assert (cache / "vendor" / "core" / "core.txt").read_text() == "upstream\n"


def test_pkg_add_is_canonical_and_install_is_alias():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        rc, _, err = _run("--root", str(root), "pkg", "add", "testns/routerish")
        assert rc == 0, err
        rc, _, err = _run("--root", str(root), "pkg", "install", "sensor:acme")  # permanent alias
        assert rc == 0, err
        names = {s.name for s in load_manifest(root).sensors}
        assert {"routerish", "acme_cam"} <= names


def test_service_install_vendors_routes_and_wires():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        rc, _, err = _run("--root", str(root), "add", "testns/routerish")
        assert rc == 0, err
        assert (root / "services" / "routerish" / "rigging.yaml").is_file()      # vendored
        assert (root / "services" / "routerish" / ".vendored.yaml").is_file()
        assert "path: services/routerish" in (root / "services.yaml").read_text()  # self-contained route
        manifest = load_manifest(root)
        row = next(s for s in manifest.sensors if s.service == "routerish")
        assert row.name == "routerish" and row.tier == "infra" and row.enabled   # named example honored
        lock = load_lock(root)
        assert "testns/routerish@1.2.0" in lock["packages"]
        assert lock["instances"]["routerish"]["base_sha256"] == \
            sha256_file(root / "config" / ".pins" / "routerish.yaml")  # the EXACT anchor


def test_service_vendors_pinned_rev_not_clone_head():
    # The clone lands at the repo's HEAD; the vendored surface must come from the PINNED rev —
    # a registry pin is a snapshot, not "whatever the repo carries today".
    with _env(RIG_HOME=tempfile.mkdtemp()):
        repo, rev = _code_repo()
        reg = _registry_with(repo, rev)
        _run("setup", "--no-default-registry")
        _run("registry", "add", "testns", "--path", str(reg))
        root = pathlib.Path(tempfile.mkdtemp()) / "veh"
        with contextlib.redirect_stderr(io.StringIO()):
            init(root, no_git=True)
        # a SECOND commit moves HEAD and rewrites the vendored files
        (repo / "camish" / "config" / "camish.example.yaml").write_text(
            "service: camish\ncamera: {type: usb}\nrate: 999   # HEAD-only\n")
        rigging = repo / "camish" / "rigging.yaml"
        rigging.write_text(rigging.read_text() + "# HEAD-only\n")
        _git("add", "-A", cwd=repo)
        _git("commit", "-q", "-m", "head moves past the pin", cwd=repo)
        rc, _, err = _run("--root", str(root), "pkg", "add", "testns/camish")
        assert rc == 0, err
        vendored = root / "services" / "camish"
        assert (vendored / "config" / "camish.example.yaml").read_text() == \
            "service: camish\ncamera: {type: usb}\n"             # the PINNED rev's bytes
        assert "HEAD-only" not in (vendored / "rigging.yaml").read_text()
        assert yaml.safe_load((vendored / ".vendored.yaml").read_text())["ref"] == rev
        assert "HEAD-only" not in \
            (root / "config" / "sensors" / "camish.yaml").read_text()  # working copy = pinned base


def test_package_name_must_equal_declared_service_name():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, reg = _world()
        # a package published under the REPO name while rigging.yaml declares another (sbg-driver/sbg)
        m = yaml.safe_load((reg / "services" / "camish" / "manifest.yaml").read_text())
        m["name"] = "camish-driver"
        d = reg / "services" / "camish-driver"
        d.mkdir(parents=True)
        (d / "manifest.yaml").write_text(yaml.safe_dump(m, sort_keys=False))
        _run("registry", "index", str(reg))
        rc, _, err = _run("--root", str(root), "add", "testns/camish-driver")
        assert rc == 1 and "declares service 'camish'" in err and "Rename the package" in err
        assert not (root / "services" / "camish-driver").exists()      # deployment untouched
        assert "camish-driver" not in (root / "vehicle.yaml").read_text()


def test_hyphenated_embedded_example_name_accepted_verbatim():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        rc, _, err = _run("--root", str(root), "add", "testns/dash-board")
        assert rc == 0, err
        assert any(s.name == "dash-board" for s in load_manifest(root).sensors)


def test_profile_install_transitive_and_working_copy():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, reg = _world()
        rc, _, err = _run("--root", str(root), "pkg", "install", "sensor:acme")
        assert rc == 0, err
        manifest = load_manifest(root)
        row = next(s for s in manifest.sensors if s.service == "camish")
        assert row.name == "acme_cam" and row.profile == "testns/camish:acme-cam@2.0.0"
        working = root / "config" / "sensors" / "acme_cam.yaml"
        assert "# tuned default" in working.read_text()          # VERBATIM copy — comments survive
        lock = load_lock(root)
        assert lock["packages"]["testns/camish:acme-cam@2.0.0"]["requires"] == "testns/camish@1.2.0"
        inst = lock["instances"]["acme_cam"]
        assert inst["profile"] == "testns/camish:acme-cam@2.0.0"
        assert inst["base_sha256"] == sha256_file(root / "config" / ".pins" / "acme_cam.yaml")
        assert (root / "services" / "camish" / "rigging.yaml").is_file()  # transitive service install


def test_sensor_glob_match_and_as_name():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        rc, _, err = _run("--root", str(root), "add", "sensor:usb:9999:0042", "--as", "cam_front")
        assert rc == 0, err
        assert any(s.name == "cam_front" for s in load_manifest(root).sensors)


def _extra_profile(reg, short: str, match: list[str]) -> None:
    p = reg / "profiles" / "camish" / short
    (p / "config").mkdir(parents=True)
    (p / "manifest.yaml").write_text(yaml.safe_dump({
        "kind": "profile", "name": short, "version": "1.0.0",
        "provides": {"sensor": [{"model": short, "match": match}]},
        "requires": {"service": "camish@^1.0"},
        "config": {"payload": "config/payload.yaml"}}))
    (p / "config" / "payload.yaml").write_text("service: camish\ncamera: {type: usb}\n")


def test_sensor_ambiguity_and_exact_beats_glob():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, reg = _world()
        _extra_profile(reg, "dupe-a", ["dupecam"])       # TWO exact matches for one identifier
        _extra_profile(reg, "dupe-b", ["dupecam"])
        _extra_profile(reg, "glob-cam", ["acme*"])       # a glob that also covers 'acme'
        assert _run("registry", "index", str(reg))[0] == 0
        rc, _, err = _run("--root", str(root), "add", "sensor:dupecam")
        assert rc == 1 and "ambiguous" in err and "install one by name" in err
        assert "camish:dupe-a" in err and "camish:dupe-b" in err   # the choices, spelled out
        assert not any(s.service == "camish" for s in load_manifest(root).sensors)  # nothing installed
        # precedence: the EXACT match wins over the glob that also covers the identifier
        rc, _, err = _run("--root", str(root), "pkg", "install", "sensor:acme")
        assert rc == 0, err
        row = next(s for s in load_manifest(root).sensors if s.name == "acme_cam")
        assert row.profile == "testns/camish:acme-cam@2.0.0"


def test_hand_authored_vehicle_yaml_stays_untouched_with_paste_ready_row():
    # A flow-style tier section isn't the generated block form: install must stage config + pin +
    # lock anchor, print the paste-ready row, and leave vehicle.yaml byte-identical.
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        veh = root / "vehicle.yaml"
        veh.write_text(veh.read_text().replace("sensors:\n", "sensors: []\n", 1))
        before = veh.read_bytes()
        rc, _, err = _run("--root", str(root), "pkg", "install", "sensor:acme")
        assert rc == 0, err
        assert "INCOMPLETE" in err and "name: acme_cam" in err     # the paste-ready warning
        assert veh.read_bytes() == before                          # file untouched, byte for byte
        assert (root / "config" / "sensors" / "acme_cam.yaml").is_file()   # staged halves present
        assert (root / "config" / ".pins" / "acme_cam.yaml").is_file()
        assert "acme_cam" in (load_lock(root).get("instances") or {})


def test_as_name_must_be_ros_safe():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        rc, _, err = _run("--root", str(root), "pkg", "install", "sensor:acme", "--as", "Bad-Name")
        assert rc == 1 and "ROS-safe" in err
        assert not (root / "config" / "sensors" / "Bad-Name.yaml").exists()
        assert not (root / "services" / "camish").exists()         # rolled back, no leaked vendor


def test_collision_demands_as_name():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        assert _run("--root", str(root), "pkg", "install", "sensor:acme")[0] == 0
        rc, _, err = _run("--root", str(root), "pkg", "install", "sensor:acme")
        assert rc == 1 and "--as" in err
        rc, _, err = _run("--root", str(root), "pkg", "install", "sensor:acme", "--as", "cam_b")
        assert rc == 0, err
        names = [s.name for s in load_manifest(root).sensors if s.service == "camish"]
        assert sorted(names) == ["acme_cam", "cam_b"]            # two instances of one service coexist


def test_bare_name_registry_fallback_via_add():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        rc, _, err = _run("--root", str(root), "add", "routerish")   # not in the workspace -> registry
        assert rc == 0, err
        assert (root / "services" / "routerish").is_dir()
        rc, _, err = _run("--root", str(root), "add", "nowhere-at-all")
        assert rc == 1 and "registry fallback" in err


def test_version_spec_and_wrong_kind_errors():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, reg = _world()
        rc, _, err = _run("--root", str(root), "pkg", "install", "testns/routerish@9.9.9")
        assert rc == 1 and "is at 1.2.0" in err
        rc, _, err = _run("--root", str(root), "pkg", "install", "testns/ghost")
        assert rc == 1 and "not found" in err
        # WRONG-KIND refs: an overlay is BOUND, never installed standalone
        rc, _, err = _run("--root", str(root), "pkg", "install", "testns/cam-tune")
        assert rc == 1 and "BOUND" in err and "overlay apply" in err
        # ...and a vehicle plan installs only through its suite
        v = reg / "vehicles" / "planish"
        (v / "config").mkdir(parents=True)
        (v / "manifest.yaml").write_text(yaml.safe_dump({
            "kind": "vehicle", "name": "planish", "version": "1.0.0",
            "config": {"payload": "config/vehicle.yaml"}}))
        (v / "config" / "vehicle.yaml").write_text(
            'vehicle: "{{vehicle}}"\nvehicle_id: "{{vehicle_id}}"\n')
        assert _run("registry", "index", str(reg))[0] == 0
        rc, _, err = _run("--root", str(root), "pkg", "install", "testns/planish")
        assert rc == 1 and "vehicle plan" in err and "suite" in err
        assert not (root / "rig.lock").exists()                  # neither refusal touched the tree


def test_build_falls_back_to_pinned_source_for_vendored_services():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        assert _run("--root", str(root), "pkg", "add", "testns/routerish")[0] == 0
        assert not (root / "services" / "routerish" / ".." / "shared" / "build.sh").resolve().exists()
        rc, _, err = _run("--root", str(root), "image", "build", "--dry-run")
        assert rc == 0, err
        assert "using the pinned source checkout" in err               # fallback engaged
        assert "cache/src/routerish-" in err                           # cwd = the pinned clone
        # a service with a repo-relative build command, NO context, NO lock pin: pointed error, rc 1
        fake = pathlib.Path(tempfile.mkdtemp()) / "handsvc"
        (fake / "config").mkdir(parents=True)
        (fake / "rigging.yaml").write_text("service: handsvc\nlauncher: handsvc-up\ntier: infra\n"
                                           "build: tools/nope.sh\nlaunch_surface: [handsvc-up]\n"
                                           "examples: [config/handsvc.example.yaml]\n")
        (fake / "handsvc-up").write_text("#!/bin/sh\n")
        (fake / "handsvc-up").chmod(0o755)
        (fake / "config" / "handsvc.example.yaml").write_text("service: handsvc\nname: handsvc\n")
        assert _run("--root", str(root), "add", str(fake))[0] == 0     # workspace add: no lock pin
        rc, _, err = _run("--root", str(root), "image", "build", "--dry-run")
        assert rc == 1 and "no source pin for 'handsvc'" in err


def test_pkg_list_shows_installed_users_and_upgrades():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, reg = _world()
        rc, out, _ = _run("--root", str(root), "pkg", "list")
        assert rc == 0 and "no packages in this deployment" in out
        assert _run("--root", str(root), "add", "testns/routerish")[0] == 0
        assert _run("--root", str(root), "pkg", "install", "sensor:acme")[0] == 0
        assert _run("--root", str(root), "overlay", "apply", "acme_cam", "testns/cam-tune")[0] == 0
        rc, out, _ = _run("--root", str(root), "pkg", "list")
        assert rc == 0, out
        lines = {l.split()[0]: l for l in out.splitlines() if "/" in l}
        assert "acme_cam" in lines["testns/camish:acme-cam@2.0.0"]           # profile -> its instance
        assert "acme_cam" in lines["testns/camish@1.2.0"]             # service -> instances using it
        assert "routerish" in lines["testns/routerish@1.2.0"]
        assert "acme_cam" in lines["testns/cam-tune@1.0.0"]           # overlay -> bound instance
        assert "available" not in out                                  # everything current
        # registry moves -> the UPGRADE column lights up
        mpath = reg / "services" / "routerish" / "manifest.yaml"
        m = yaml.safe_load(mpath.read_text())
        m["version"] = "2.0.0"
        mpath.write_text(yaml.safe_dump(m, sort_keys=False))
        _run("registry", "index", str(reg))
        rc, out, _ = _run("--root", str(root), "pkg", "list")
        assert "2.0.0 available" in out and "pkg upgrade" in out


def test_locked_reproduces_and_detects_drift():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, reg = _world()
        assert _run("--root", str(root), "pkg", "install", "sensor:acme")[0] == 0
        # second deployment reproduces from the lock
        root2 = pathlib.Path(tempfile.mkdtemp()) / "veh2"
        with contextlib.redirect_stderr(io.StringIO()):
            init(root2, no_git=True)
        (root2 / "rig.lock").write_text((root / "rig.lock").read_text())
        rc, _, err = _run("--root", str(root2), "pkg", "install", "testns/camish:acme-cam", "--locked")
        assert rc == 0, err
        assert (root / "config" / "sensors" / "acme_cam.yaml").read_text() == \
               (root2 / "config" / "sensors" / "acme_cam.yaml").read_text()  # byte-identical base
        # registry payload drifts under the pin -> --locked refuses
        payload = reg / "profiles" / "camish" / "acme-cam" / "config" / "payload.yaml"
        payload.write_text(payload.read_text() + "usb2: {}\n")
        _run("registry", "index", str(reg))
        root3 = pathlib.Path(tempfile.mkdtemp()) / "veh3"
        with contextlib.redirect_stderr(io.StringIO()):
            init(root3, no_git=True)
        (root3 / "rig.lock").write_text((root / "rig.lock").read_text())
        rc, _, err = _run("--root", str(root3), "pkg", "install", "testns/camish:acme-cam", "--locked")
        assert rc == 1 and "hash" in err
        # registry VERSION moves under the lock (2.0.0 -> 2.1.0 re-publish) -> --locked refuses too
        mpath = reg / "profiles" / "camish" / "acme-cam" / "manifest.yaml"
        m = yaml.safe_load(mpath.read_text())
        m["version"] = "2.1.0"
        mpath.write_text(yaml.safe_dump(m, sort_keys=False))
        _run("registry", "index", str(reg))
        root4 = pathlib.Path(tempfile.mkdtemp()) / "veh4"
        with contextlib.redirect_stderr(io.StringIO()):
            init(root4, no_git=True)
        (root4 / "rig.lock").write_text((root / "rig.lock").read_text())
        rc, _, err = _run("--root", str(root4), "pkg", "install", "testns/camish:acme-cam", "--locked")
        assert rc == 1 and "not the locked pin" in err
        assert "testns/camish:acme-cam@2.0.0" in err              # the lock's pin, named in the hint
        assert not (root4 / "config" / "sensors" / "acme_cam.yaml").exists()  # nothing materialized


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
