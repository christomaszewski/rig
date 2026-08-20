"""pkg promote (overlay/profile/suite) + atomic suite install. Run: python3 tests/test_promote.py"""
import contextlib
import io
import pathlib
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import yaml  # noqa: E402

from test_install import _env, _git, _run, _world  # noqa: E402  (shared fixtures)

from rig_cli.init import init  # noqa: E402
from rig_cli.manifest import load_manifest  # noqa: E402
from rig_cli.registry_scaffold import registry_init  # noqa: E402
from rig_cli.resolve import materialize_manifest  # noqa: E402


def _internal(name="internal") -> pathlib.Path:
    reg = pathlib.Path(tempfile.mkdtemp()) / name
    with contextlib.redirect_stderr(io.StringIO()):
        registry_init(reg, namespace=name)
    _run("registry", "add", name, "--path", str(reg))
    return reg


def _install_acme(root):
    rc, _, err = _run("--root", str(root), "pkg", "install", "sensor:acme")
    assert rc == 0, err
    return root / "config" / "sensors" / "acme_cam.yaml"


def _rendered(root, instance="acme_cam") -> dict:
    manifest = materialize_manifest(load_manifest(root), root)
    sensor = next(s for s in manifest.sensors if s.name == instance)
    return yaml.safe_load(pathlib.Path(sensor.config).read_text())


def test_promote_overlay_roundtrip_identical_render():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        working = _install_acme(root)
        internal = _internal()
        working.write_text(working.read_text().replace("width: 1280", "width: 640"))
        before = _rendered(root)
        rc, _, err = _run("--root", str(root), "pkg", "promote", "acme_cam",
                          "--name", "zr-gideon", "--project", "gideon", "--to", "internal")
        assert rc == 0, err
        delta = yaml.safe_load((internal / "overlays" / "zr-gideon" / "config" / "delta.yaml").read_text())
        assert delta == {"usb": {"width": 640}}
        m = yaml.safe_load((internal / "overlays" / "zr-gideon" / "manifest.yaml").read_text())
        assert m["targets"] == [{"service": "testns/camish"}]     # fully qualified (not in `internal`)
        assert m["authored_against"] == {"service": "testns/camish@1.2.0",  # staleness tier 1
                                         "profile": "testns/camish:acme-cam@2.0.0"}
        assert _run("registry", "validate", str(internal))[0] == 0
        rc, _, err = _run("--root", str(root), "overlay", "apply", "acme_cam",
                          "internal/zr-gideon", "--clear-local")
        assert rc == 0, err
        assert _rendered(root) == before                          # THE round-trip law
        rc, out, _ = _run("--root", str(root), "config", "diff", "acme_cam")
        assert "clean" in out


def test_promote_requires_dirty_and_bump():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        working = _install_acme(root)
        _internal()
        rc, _, err = _run("--root", str(root), "pkg", "promote", "acme_cam", "--to", "internal")
        assert rc == 1 and "clean" in err
        working.write_text(working.read_text() + "extra: 1\n")
        assert _run("--root", str(root), "pkg", "promote", "acme_cam", "--name", "t",
                    "--to", "internal")[0] == 0
        working.write_text(working.read_text() + "extra2: 2\n")
        rc, _, err = _run("--root", str(root), "pkg", "promote", "acme_cam", "--name", "t",
                          "--to", "internal")
        assert rc == 1 and "--bump" in err
        rc, _, err = _run("--root", str(root), "pkg", "promote", "acme_cam", "--name", "t",
                          "--to", "internal", "--bump")
        assert rc == 0, err
        internal_root = [e for e in __import__("rig_cli.registries", fromlist=["load_entries"])
                         .load_entries() if e.name == "internal"][0].root
        m = yaml.safe_load((internal_root / "overlays" / "t" / "manifest.yaml").read_text())
        assert m["version"] == "1.0.1"


def test_promote_kind_profile():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        working = _install_acme(root)
        internal = _internal()
        working.write_text(working.read_text().replace("width: 1280", "width: 3840"))
        rc, _, err = _run("--root", str(root), "pkg", "promote", "acme_cam", "--kind", "profile",
                          "--name", "acme-lowlight", "--to", "internal",
                          "--match", "acme-ll", "--match", "usb:9999:ll*")
        assert rc == 0, err
        m = yaml.safe_load((internal / "profiles" / "camish" / "acme-lowlight" / "manifest.yaml").read_text())
        assert m["requires"]["service"] == "testns/camish@1.2.0"  # from the lock pin, cross-ns
        payload = yaml.safe_load(
            (internal / "profiles" / "camish" / "acme-lowlight" / "config" / "payload.yaml").read_text())
        assert payload["usb"]["width"] == 3840 and "name" not in payload
        assert _run("registry", "validate", str(internal))[0] == 0


def test_promote_all_suite_and_fresh_install_reproduces():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        working = _install_acme(root)
        _internal()
        working.write_text(working.read_text().replace("width: 1280", "width: 640"))
        rc, _, err = _run("--root", str(root), "pkg", "promote", "--all", "--project", "gideon",
                          "--suite", "gideon-boat", "--to", "internal")
        assert rc == 0, err
        before = _rendered(root)
        root2 = pathlib.Path(tempfile.mkdtemp()) / "veh2"
        with contextlib.redirect_stderr(io.StringIO()):
            init(root2, no_git=True)
        rc, _, err = _run("--root", str(root2), "pkg", "install", "internal/gideon-boat")
        assert rc == 0, err
        assert _rendered(root2) == before                          # fresh vehicle == origin (DoD)
        sensor = next(s for s in load_manifest(root2).sensors if s.name == "acme_cam")
        assert list(sensor.overlays) == ["internal/acme-cam-gideon@1.0.0"]


def test_suite_alone_when_nothing_dirty():
    # An all-clean deployment (everything adopted/pinned): --all --suite emits the PINS-ONLY
    # suite instead of exiting "nothing dirty" (v0.2.2); an empty deployment still refuses.
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        _install_acme(root)                              # pinned, untouched — nothing dirty
        internal = _internal()
        rc, _, err = _run("--root", str(root), "pkg", "promote", "--all",
                          "--suite", "boat", "--project", "g", "--to", "internal")
        assert rc == 0, err
        s = yaml.safe_load((internal / "suites" / "boat" / "manifest.yaml").read_text())
        assert s["members"]["profiles"] == ["testns/camish:acme-cam@2.0.0"]
        assert s["members"]["overlays"] == []
        assert _run("registry", "validate", str(internal))[0] == 0
        root2 = pathlib.Path(tempfile.mkdtemp()) / "veh2"  # fresh vehicle reproduces from pins
        with contextlib.redirect_stderr(io.StringIO()):
            init(root2, no_git=True)
        rc, _, err = _run("--root", str(root2), "pkg", "install", "internal/boat")
        assert rc == 0, err
        assert _rendered(root2) == _rendered(root)
        root3 = pathlib.Path(tempfile.mkdtemp()) / "veh3"  # nothing installed: suite would be empty
        with contextlib.redirect_stderr(io.StringIO()):
            init(root3, no_git=True)
        rc, _, err = _run("--root", str(root3), "pkg", "promote", "--all",
                          "--suite", "s", "--to", "internal")
        assert rc == 1 and "EMPTY" in err, err


def test_suite_install_rolls_back_midplan():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        working = _install_acme(root)
        internal = _internal()
        working.write_text(working.read_text().replace("width: 1280", "width: 640"))
        assert _run("--root", str(root), "pkg", "promote", "--all", "--project", "g",
                    "--suite", "s", "--to", "internal")[0] == 0
        # sabotage: the suite pins an overlay version the registry does not carry
        smanifest = internal / "suites" / "s" / "manifest.yaml"
        smanifest.write_text(smanifest.read_text().replace("@1.0.0", "@9.9.9"))
        root2 = pathlib.Path(tempfile.mkdtemp()) / "veh2"
        with contextlib.redirect_stderr(io.StringIO()):
            init(root2, no_git=True)
        veh_before = (root2 / "vehicle.yaml").read_text()
        files_before = sorted(str(p) for p in root2.rglob("*") if p.is_file())
        rc, _, err = _run("--root", str(root2), "pkg", "install", "internal/s")
        assert rc == 1 and "rolled back" in err
        assert (root2 / "vehicle.yaml").read_text() == veh_before
        assert sorted(str(p) for p in root2.rglob("*") if p.is_file()) == files_before
        assert not (root2 / "rig.lock").exists()


def test_promote_to_git_registry_branches_and_keeps_cache_clean():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        working = _install_acme(root)
        remote = pathlib.Path(tempfile.mkdtemp()) / "internal-remote"
        with contextlib.redirect_stderr(io.StringIO()):
            registry_init(remote, namespace="internal")
        _git("init", "-q", cwd=remote)
        _git("add", "-A", cwd=remote)
        _git("commit", "-q", "-m", "seed", cwd=remote)
        _run("registry", "add", "internal", str(remote))
        assert _run("registry", "sync", "internal")[0] == 0
        working.write_text(working.read_text().replace("width: 1280", "width: 640"))
        rc, _, err = _run("--root", str(root), "pkg", "promote", "acme_cam", "--name", "zg",
                          "--to", "internal")
        assert rc == 0 and "promote/zg" in err and "push origin" in err
        from rig_cli.registries import cache_dir
        cache = cache_dir("internal")
        branches = subprocess.run(["git", "-C", str(cache), "branch"],
                                  capture_output=True, text=True).stdout
        assert "promote/zg" in branches
        head = subprocess.run(["git", "-C", str(cache), "rev-parse", "--abbrev-ref", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        assert head in ("main", "master")                          # cache view back on the default
        assert not subprocess.run(["git", "-C", str(cache), "status", "--porcelain"],
                                  capture_output=True, text=True).stdout.strip()
        show = subprocess.run(["git", "-C", str(cache), "show", "promote/zg:overlays/zg/config/delta.yaml"],
                              capture_output=True, text=True).stdout
        assert "width: 640" in show


# --- update flow: name-from-provenance, auto-bump, carry-forward, inference (v0.1.61) -----------


def test_repromote_carries_forward_and_autobumps_on_provenance():
    # Updating the profile the instance is PINNED to: name defaults from provenance, --bump is
    # implied, and the existing manifest's provides/overrides_schema survive the rewrite.
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, reg = _world()
        working = _install_acme(root)                       # pins testns/camish:acme-cam@2.0.0
        mpath = reg / "profiles" / "camish" / "acme-cam" / "manifest.yaml"
        m = yaml.safe_load(mpath.read_text())               # hand-add a schema, like a registry author
        m["config"]["overrides_schema"] = "config/schema.json"
        mpath.write_text(yaml.safe_dump(m))
        (reg / "profiles" / "camish" / "acme-cam" / "config" / "schema.json").write_text('{"type": "object"}')
        working.write_text(working.read_text().replace("width: 1280", "width: 3840"))
        rc, _, err = _run("--root", str(root), "pkg", "promote", "acme_cam",
                          "--kind", "profile", "--to", "testns")   # no --name, no --bump
        assert rc == 0, err
        assert "auto-bump" in err
        m = yaml.safe_load(mpath.read_text())
        assert m["version"] == "2.0.1"                             # bumped the EXISTING package
        assert m["provides"]["sensor"][0]["model"] == "ACME Cam"   # carried, not regenerated
        assert m["provides"]["sensor"][0]["match"] == ["acme", "usb:9999:*"]
        assert m["config"]["overrides_schema"] == "config/schema.json"
        payload = yaml.safe_load(
            (reg / "profiles" / "camish" / "acme-cam" / "config" / "payload.yaml").read_text())
        assert payload["usb"]["width"] == 3840
        # --match REPLACES the carried set (still auto-bumped)
        rc, _, err = _run("--root", str(root), "pkg", "promote", "acme_cam",
                          "--kind", "profile", "--to", "testns", "--match", "neo")
        assert rc == 0, err
        m = yaml.safe_load(mpath.read_text())
        assert m["version"] == "2.0.2" and m["provides"]["sensor"][0]["match"] == ["neo"]


def test_profile_bump_still_required_without_provenance_match():
    # A bare NAME collision in a registry the provenance does not point at keeps the guard.
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        working = _install_acme(root)
        internal = _internal()
        working.write_text(working.read_text() + "extra: 1\n")
        assert _run("--root", str(root), "pkg", "promote", "acme_cam", "--kind", "profile",
                    "--name", "acme-cam", "--to", "internal")[0] == 0        # fresh 1.0.0
        rc, _, err = _run("--root", str(root), "pkg", "promote", "acme_cam", "--kind", "profile",
                          "--name", "acme-cam", "--to", "internal")
        assert rc == 1 and "--bump" in err          # provenance says testns/, target is internal/
        m = yaml.safe_load((internal / "profiles" / "camish" / "acme-cam" / "manifest.yaml").read_text())
        assert m["version"] == "1.0.0"              # refusal wrote nothing


def test_kind_inferred_profile_for_hand_authored_instance():
    # No pinned base ⇒ overlay is impossible ⇒ bare promote infers profile, loudly.
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        _install_acme(root)                          # routes camish + locks its service pin
        internal = _internal()
        (root / "config" / "sensors" / "handy.yaml").write_text(
            "camera: {type: usb}\nusb: {width: 111}\n")
        veh = root / "vehicle.yaml"
        body = veh.read_text()
        row = next(line for line in body.splitlines() if "name: acme_cam" in line)
        indent = row[:len(row) - len(row.lstrip())]
        veh.write_text(body.replace(row, row + "\n" + indent + "- { name: handy, service: camish, "
                                    "config: config/sensors/handy.yaml, enabled: true, order: 20 }"))
        rc, _, err = _run("--root", str(root), "pkg", "promote", "handy", "--to", "internal")
        assert rc == 0, err
        assert "PROFILE" in err                      # the loud inference note
        m = yaml.safe_load((internal / "profiles" / "camish" / "handy" / "manifest.yaml").read_text())
        assert m["kind"] == "profile" and m["requires"]["service"] == "testns/camish@1.2.0"
        payload = yaml.safe_load(
            (internal / "profiles" / "camish" / "handy" / "config" / "payload.yaml").read_text())
        assert payload["usb"]["width"] == 111 and "name" not in payload
        assert _run("registry", "validate", str(internal))[0] == 0


def test_alias_neq_namespace_emits_registry_namespace():
    # Manifests are registry-side documents: refs carry the registry's OWN namespace, never the
    # consumer's alias (else suite members don't resolve and staleness checks never fire).
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        working = _install_acme(root)
        reg = pathlib.Path(tempfile.mkdtemp()) / "corp-reg"
        with contextlib.redirect_stderr(io.StringIO()):
            registry_init(reg, namespace="corp")     # namespace 'corp' …
        _run("registry", "add", "internal", "--path", str(reg))  # … added under alias 'internal'
        working.write_text(working.read_text().replace("width: 1280", "width: 640"))
        rc, _, err = _run("--root", str(root), "pkg", "promote", "--all", "--project", "g",
                          "--suite", "s", "--to", "internal")
        assert rc == 0, err
        s = yaml.safe_load((reg / "suites" / "s" / "manifest.yaml").read_text())
        assert s["members"]["overlays"] == ["corp/acme-cam-g@1.0.0"]   # namespace, NOT the alias
        assert s["members"]["profiles"] == ["testns/camish:acme-cam@2.0.0"]   # foreign ref: alias == ns
        assert _run("registry", "validate", str(reg))[0] == 0


def test_failed_repromote_restores_preexisting_package():
    # Rollback must RESTORE a package that predated this promote — never delete it.
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        working = _install_acme(root)
        internal = _internal()
        working.write_text(working.read_text() + "extra: 1\n")
        assert _run("--root", str(root), "pkg", "promote", "acme_cam", "--kind", "profile",
                    "--name", "keeper", "--to", "internal", "--match", "idA")[0] == 0
        (internal / "profiles" / "camish" / "keeper" / "NOTES.md").write_text("precious\n")
        rc, _, err = _run("--root", str(root), "pkg", "promote", "acme_cam", "--kind", "profile",
                          "--name", "keeper", "--to", "internal", "--bump",
                          "--requires", "internal/ghost@9.9.9")   # unresolvable -> validation fails
        assert rc == 1, "sabotaged promote should fail validation"
        m = yaml.safe_load((internal / "profiles" / "camish" / "keeper" / "manifest.yaml").read_text())
        assert m["version"] == "1.0.0"                            # restored, not deleted
        assert m["provides"]["sensor"][0]["match"] == ["idA"]
        assert (internal / "profiles" / "camish" / "keeper" / "NOTES.md").read_text() == "precious\n"


def _dev_service(name="devsvc", subdir=None):
    """A dev service checkout with a bare `origin` it has pushed to — the publishable shape.
    Returns (route_path, head_sha, origin_path)."""
    repo = pathlib.Path(tempfile.mkdtemp()) / name
    d = repo / subdir if subdir else repo
    (d / "config").mkdir(parents=True)
    (d / "rigging.yaml").write_text(
        f"service: {name}\nlauncher: {name}-up\ntier: sensor\n"
        f"examples: [config/{name}.example.yaml]\nlaunch_surface: [{name}-up]\n")
    (d / f"{name}-up").write_text("#!/bin/sh\n")
    (d / f"{name}-up").chmod(0o755)
    (d / "config" / f"{name}.example.yaml").write_text(f"service: {name}\nrate: 1\n")
    _git("init", "-q", cwd=repo)
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "dev", cwd=repo)
    origin = pathlib.Path(tempfile.mkdtemp()) / f"{name}.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], capture_output=True)
    _git("remote", "add", "origin", str(origin), cwd=repo)
    _git("push", "-q", "-u", "origin", "HEAD", cwd=repo)
    return d, _git("rev-parse", "HEAD", cwd=repo).stdout.strip(), origin


def test_promote_kind_service_publishes_code_pointer():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        internal = _internal()
        route, head, origin = _dev_service()
        assert _run("--root", str(root), "add", str(route))[0] == 0   # wires the services.yaml route
        rc, _, err = _run("--root", str(root), "pkg", "promote", "devsvc", "--kind", "service",
                          "--to", "internal", "--version", "0.1.0")
        assert rc == 0, err
        m = yaml.safe_load((internal / "services" / "devsvc" / "manifest.yaml").read_text())
        assert m == {"kind": "service", "name": "devsvc", "version": "0.1.0",
                     "source": {"repo": str(origin), "rev": head}}
        assert _run("registry", "validate", str(internal))[0] == 0
        # re-publish same version refuses; --bump carries hand-added fields forward
        rc, _, err = _run("--root", str(root), "pkg", "promote", "devsvc", "--kind", "service",
                          "--to", "internal", "--version", "0.1.0")
        assert rc == 1 and "--bump" in err
        m["platforms"] = ["linux/arm64"]  # registry-author hand edit
        (internal / "services" / "devsvc" / "manifest.yaml").write_text(yaml.safe_dump(m))
        rc, _, err = _run("--root", str(root), "pkg", "promote", "devsvc", "--kind", "service",
                          "--to", "internal", "--bump")
        assert rc == 0, err
        m2 = yaml.safe_load((internal / "services" / "devsvc" / "manifest.yaml").read_text())
        assert m2["version"] == "0.1.1" and m2["platforms"] == ["linux/arm64"]


def test_promote_profile_preserves_comments_verbatim():
    """Comments in the working file survive into the payload (verbatim-when-faithful), through
    --adopt, and back out through a consumer install — the full circle."""
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        internal = _internal()
        cfg = _install_acme(root)
        cfg.write_text(cfg.read_text()
                       + "# toggle: uncomment for bench runs\n"
                       + "# bench: {mode: sim}\n"
                       + "rate: 9\n")
        rc, _, err = _run("--root", str(root), "pkg", "promote", "acme_cam", "--kind", "profile",
                          "--name", "commented", "--to", "internal", "--adopt")
        assert rc == 0, err
        payload = (internal / "profiles" / "camish" / "commented" / "config" / "payload.yaml")
        text = payload.read_text()
        assert "# toggle: uncomment for bench runs" in text            # comments in the payload
        assert "# tuned default" in text                               # the base's comments too
        assert yaml.safe_load(text)["rate"] == 9                       # and it parses to the data
        assert "# toggle" in cfg.read_text()                           # adopt kept them local
        assert (root / "config" / ".pins" / "acme_cam.yaml").read_text() == cfg.read_text()
        # the circle closes: a consumer install materializes the comments verbatim
        root2 = pathlib.Path(tempfile.mkdtemp()) / "veh2"
        with contextlib.redirect_stderr(io.StringIO()):
            init(root2, no_git=True)
        rc, _, err = _run("--root", str(root2), "pkg", "add", "internal/camish:commented",
                          "--as", "consumer")
        assert rc == 0, err
        assert "# toggle: uncomment for bench runs" in \
            (root2 / "config" / "sensors" / "consumer.yaml").read_text()


def test_promote_hand_authored_comments_and_name_commented():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        internal = _internal()
        _install_acme(root)                                            # locks the camish service
        (root / "config" / "sensors" / "hand.yaml").write_text(
            "service: camish\nname: hand\n# tuning notes live here\nmode: x\n")
        veh = root / "vehicle.yaml"
        veh.write_text(veh.read_text().replace(
            "sensors:", "sensors:\n  - { name: hand, service: camish, "
                        "config: config/sensors/hand.yaml, enabled: true, order: 90 }", 1))
        rc, _, err = _run("--root", str(root), "pkg", "promote", "hand", "--kind", "profile",
                          "--name", "handy", "--to", "internal")
        assert rc == 0, err
        text = (internal / "profiles" / "camish" / "handy" / "config" / "payload.yaml").read_text()
        assert "# tuning notes live here" in text                      # comments preserved
        assert "# name: hand" in text                                  # name commented, not kept
        assert yaml.safe_load(text).get("name") is None                # payload is nameless


def test_promote_kind_service_adopt_lock_tracks():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        _internal()
        route, head, origin = _dev_service()
        assert _run("--root", str(root), "add", str(route))[0] == 0
        rc, out, _ = _run("--root", str(root), "pkg", "list")
        assert "devsvc" in out and "unpublished" in out                # local before adopt
        rc, _, err = _run("--root", str(root), "pkg", "promote", "devsvc", "--kind", "service",
                          "--to", "internal", "--version", "0.0.1", "--adopt")
        assert rc == 0, err
        assert "ADOPTED" in err and "dev route stays" in err
        lock = yaml.safe_load((root / "rig.lock").read_text())
        row = lock["packages"]["internal/devsvc@0.0.1"]
        assert row["kind"] == "service" and row["source"]["rev"] == head
        rc, out, _ = _run("--root", str(root), "pkg", "list")
        assert "internal/devsvc@0.0.1" in out                          # registry-tracked now
        assert "unpublished" not in out
        # once adopted, a republish (pkg save) carries the lock row to the new version
        (route / "more.txt").write_text("x\n")
        _git("add", "-A", cwd=route)
        _git("commit", "-q", "-m", "more", cwd=route)
        _git("push", "-q", "origin", "HEAD", cwd=route)
        rc, _, err = _run("--root", str(root), "pkg", "save", "devsvc")
        assert rc == 0, err
        lock = yaml.safe_load((root / "rig.lock").read_text())
        assert "internal/devsvc@0.0.2" in lock["packages"]
        assert "internal/devsvc@0.0.1" not in lock["packages"]


def test_promote_kind_service_guards():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        _internal()
        route, _, _ = _dev_service()
        assert _run("--root", str(root), "add", str(route))[0] == 0
        # dirty checkout refuses
        (route / "extra.txt").write_text("wip\n")
        rc, _, err = _run("--root", str(root), "pkg", "promote", "devsvc", "--kind", "service",
                          "--to", "internal")
        assert rc == 1 and "uncommitted" in err
        # committed but UNPUSHED refuses
        _git("add", "-A", cwd=route)
        _git("commit", "-q", "-m", "wip", cwd=route)
        rc, _, err = _run("--root", str(root), "pkg", "promote", "devsvc", "--kind", "service",
                          "--to", "internal")
        assert rc == 1 and "push it" in err
        _git("push", "-q", "origin", "HEAD", cwd=route)
        assert _run("--root", str(root), "pkg", "promote", "devsvc", "--kind", "service",
                    "--to", "internal", "--version", "0.1.0")[0] == 0
        # config-flags don't apply to a code pointer; vendored routes are not publishable
        rc, _, err = _run("--root", str(root), "pkg", "promote", "devsvc", "--kind", "service",
                          "--to", "internal", "--match", "x")
        assert rc == 1 and "CODE POINTER" in err
        _install_acme(root)   # camish routes to the VENDORED surface
        rc, _, err = _run("--root", str(root), "pkg", "promote", "camish", "--kind", "service",
                          "--to", "internal")
        assert rc == 1 and "VENDORED" in err
        # --version outside --kind service refuses
        rc, _, err = _run("--root", str(root), "pkg", "promote", "acme_cam", "--kind", "profile",
                          "--to", "internal", "--version", "2.0.0")
        assert rc == 1 and "--kind service only" in err


def test_promote_kind_service_collection_repo_stamps_path():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        internal = _internal()
        route, head, origin = _dev_service(name="colsvc", subdir="drivers/colsvc")
        assert _run("--root", str(root), "add", str(route))[0] == 0
        rc, _, err = _run("--root", str(root), "pkg", "promote", "colsvc", "--kind", "service",
                          "--to", "internal", "--version", "0.1.0")
        assert rc == 0, err
        m = yaml.safe_load((internal / "services" / "colsvc" / "manifest.yaml").read_text())
        assert m["source"] == {"repo": str(origin), "rev": head, "path": "drivers/colsvc"}


# --- fork lineage: based_on, pkg rebase, --adopt (v0.1.67) --------------------------------------


def _fork_org_cam(root):
    """Install acme (pinned testns/camish:acme-cam@2.0.0), dirty it, fork it into internal/org-cam."""
    working = _install_acme(root)
    internal = _internal()
    working.write_text(working.read_text().replace("width: 1280", "width: 2048"))
    rc, _, err = _run("--root", str(root), "pkg", "promote", "acme_cam", "--kind", "profile",
                      "--name", "org-cam", "--to", "internal")
    assert rc == 0, err
    return working, internal


def _bump_parent(reg, version, payload):
    mpath = reg / "profiles" / "camish" / "acme-cam" / "manifest.yaml"
    m = yaml.safe_load(mpath.read_text())
    m["version"] = version
    mpath.write_text(yaml.safe_dump(m, sort_keys=False))
    (reg / "profiles" / "camish" / "acme-cam" / "config" / "payload.yaml").write_text(payload)
    _run("registry", "index", str(reg))


def _gitify(reg):
    """A local-dir registry that IS a git checkout — history serves old parent versions
    (the capability-detected dev workflow rebase relies on)."""
    _git("init", "-q", cwd=reg)
    _git("add", "-A", cwd=reg)
    _git("commit", "-q", "-m", "seed", cwd=reg)


def _commit(reg, msg):
    _git("add", "-A", cwd=reg)
    _git("commit", "-q", "-m", msg, cwd=reg)


def test_fork_promote_stamps_based_on_and_self_repromote_keeps_it():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        working, internal = _fork_org_cam(root)
        m = yaml.safe_load((internal / "profiles" / "camish" / "org-cam" / "manifest.yaml").read_text())
        assert m["based_on"] == "testns/camish:acme-cam@2.0.0"           # the FORK records its parent
        working.write_text(working.read_text() + "extra: 1\n")    # self re-promote of the fork
        rc, _, err = _run("--root", str(root), "pkg", "promote", "acme_cam", "--kind", "profile",
                          "--name", "org-cam", "--to", "internal", "--bump")
        assert rc == 0, err
        m = yaml.safe_load((internal / "profiles" / "camish" / "org-cam" / "manifest.yaml").read_text())
        assert m["version"] == "1.0.1"
        assert m["based_on"] == "testns/camish:acme-cam@2.0.0"           # baseline moves only on rebase


def test_rebase_three_ways_onto_new_parent():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, reg = _world()
        _gitify(reg)                                              # parent history must survive bumps
        _fork_org_cam(root)                                       # fork changed usb.width -> 2048
        _bump_parent(reg, "2.1.0",                                # parent adds fps, keeps width
                     "# v2.1\nservice: camish\ncamera: {type: usb}\nusb: {width: 1280, fps: 30}\n")
        _commit(reg, "bump 2.1.0")
        rc, _, err = _run("pkg", "rebase", "camish:org-cam", "--to", "internal")
        assert rc == 0, err
        internal_root = [e for e in __import__("rig_cli.registries", fromlist=["load_entries"])
                         .load_entries() if e.name == "internal"][0].root
        m = yaml.safe_load((internal_root / "profiles" / "camish" / "org-cam" / "manifest.yaml").read_text())
        assert m["version"] == "1.0.1"
        assert m["based_on"] == "testns/camish:acme-cam@2.1.0"           # baseline advanced
        payload = yaml.safe_load(
            (internal_root / "profiles" / "camish" / "org-cam" / "config" / "payload.yaml").read_text())
        assert payload["usb"] == {"width": 2048, "fps": 30}       # parent's new key + MY width
        assert _run("registry", "validate", str(internal_root))[0] == 0


def test_rebase_conflict_keeps_yours_loudly():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, reg = _world()
        _gitify(reg)
        _fork_org_cam(root)                                       # fork: width 2048
        _bump_parent(reg, "3.0.0",                                # parent ALSO moves width
                     "service: camish\ncamera: {type: usb}\nusb: {width: 4096}\n")
        _commit(reg, "bump 3.0.0")
        rc, _, err = _run("pkg", "rebase", "camish:org-cam", "--to", "internal")
        assert rc == 0, err
        assert "CONFLICT usb.width" in err and "keeping yours: 2048" in err
        internal_root = [e for e in __import__("rig_cli.registries", fromlist=["load_entries"])
                         .load_entries() if e.name == "internal"][0].root
        payload = yaml.safe_load(
            (internal_root / "profiles" / "camish" / "org-cam" / "config" / "payload.yaml").read_text())
        assert payload["usb"]["width"] == 2048                    # ours, kept


def test_rebase_guards():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, reg = _world()
        rc, _, err = _run("pkg", "rebase", "camish:acme-cam", "--to", "testns")
        assert rc == 1 and "no based_on lineage" in err           # the fixture profile: no fork
        # old parent version vanished from a NON-git registry -> the clear history error
        _fork_org_cam(root)
        _bump_parent(reg, "2.2.0", "service: camish\ncamera: {type: usb}\nusb: {width: 9}\n")
        rc, _, err = _run("pkg", "rebase", "camish:org-cam", "--to", "internal")
        assert rc == 1 and "git history cannot serve it" in err


def test_adopt_closes_the_profile_round_trip():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        working = _install_acme(root)
        internal = _internal()
        _run("--root", str(root), "overlay", "apply", "acme_cam", "testns/cam-tune")
        working.write_text(working.read_text().replace("width: 1280", "width: 640"))
        before = _rendered(root)
        rc, _, err = _run("--root", str(root), "pkg", "promote", "acme_cam", "--kind", "profile",
                          "--name", "org-cam", "--to", "internal", "--adopt")
        assert rc == 0, err
        assert "ADOPTED internal/camish:org-cam@1.0.0" in err
        assert _rendered(root) == before                          # THE round-trip law
        from rig_cli.manifest import load_manifest
        sensor = next(s for s in load_manifest(root).sensors if s.name == "acme_cam")
        assert sensor.profile == "internal/camish:org-cam@1.0.0"         # provenance = the fork now
        assert not sensor.overlays and not sensor.overrides       # baked in + dropped
        rc, out, _ = _run("--root", str(root), "config", "diff", "acme_cam")
        assert "clean" in out
        lock = yaml.safe_load((root / "rig.lock").read_text())
        assert "internal/camish:org-cam@1.0.0" in lock["packages"]
        assert "testns/cam-tune@1.0.0" not in lock["packages"]    # unbound + GC'd
        rc, out, _ = _run("--root", str(root), "pkg", "list")
        row = next(line for line in out.splitlines() if "internal/camish:org-cam" in line)
        assert "active" in row and "acme_cam" in row              # visible as THE active package
        svc_row = next(line for line in out.splitlines() if "testns/camish@" in line)
        assert "dependency of" in svc_row                         # its service = a dependency


def test_adopt_guards_and_hand_authored_gains_provenance():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        working = _install_acme(root)
        internal = _internal()
        working.write_text(working.read_text() + "extra: 1\n")
        # bare --adopt implies --kind profile (v0.2.1): a dirty PINNED instance fork-adopts with
        # the short name defaulted from provenance, instead of erroring "would emit an overlay"
        rc, _, err = _run("--root", str(root), "pkg", "promote", "acme_cam",
                          "--to", "internal", "--adopt")
        assert rc == 0, err
        assert "ADOPTED internal/camish:acme-cam@1.0.0" in err
        m = yaml.safe_load((internal / "profiles" / "camish" / "acme-cam" / "manifest.yaml").read_text())
        assert m["based_on"] == "testns/camish:acme-cam@2.0.0"
        # the EXPLICIT overlay+adopt combination still refuses
        rc, _, err = _run("--root", str(root), "pkg", "promote", "acme_cam", "--kind", "overlay",
                          "--to", "internal", "--adopt")
        assert rc == 1 and "--adopt" in err
        # hand-authored instance: bare promote infers profile; --adopt writes provenance
        (root / "config" / "sensors" / "handy.yaml").write_text(
            "camera: {type: usb}\nusb: {width: 111}\n")
        veh = root / "vehicle.yaml"
        row = next(line for line in veh.read_text().splitlines() if "name: acme_cam" in line)
        indent = row[:len(row) - len(row.lstrip())]
        veh.write_text(veh.read_text().replace(
            row, row + "\n" + indent + "- { name: handy, service: camish, "
            "config: config/sensors/handy.yaml, enabled: true, order: 30 }"))
        rc, _, err = _run("--root", str(root), "pkg", "promote", "handy",
                          "--to", "internal", "--adopt")
        assert rc == 0, err
        from rig_cli.manifest import load_manifest
        sensor = next(s for s in load_manifest(root).sensors if s.name == "handy")
        assert sensor.profile == "internal/camish:handy@1.0.0"           # provenance from NOTHING
        assert (root / "config" / ".pins" / "handy.yaml").is_file()
        rc, _, err = _run("--root", str(root), "pkg", "lock")
        assert rc == 0, err                                       # anchors coherent


def test_adopt_implies_profile_for_example_anchored_instance():
    # `rig add <service>` pins the vendored example as the base, so bare promote infers OVERLAY —
    # but --adopt is profile-only, so it implies the kind instead of erroring (v0.2.1).
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, _ = _world()
        internal = _internal()
        assert _run("--root", str(root), "pkg", "add", "testns/camish")[0] == 0
        working = root / "config" / "sensors" / "camish.yaml"
        working.write_text(working.read_text() + "extra: 1\n")
        rc, _, err = _run("--root", str(root), "pkg", "promote", "camish",
                          "--to", "internal", "--adopt", "--name", "generic")
        assert rc == 0, err
        assert "ADOPTED internal/camish:generic@1.0.0" in err
        m = yaml.safe_load((internal / "profiles" / "camish" / "generic" / "manifest.yaml").read_text())
        assert m["kind"] == "profile" and "based_on" not in m      # example base: a ROOT, no lineage
        payload = yaml.safe_load(
            (internal / "profiles" / "camish" / "generic" / "config" / "payload.yaml").read_text())
        assert payload["extra"] == 1 and payload["camera"] == {"type": "usb"}  # base ⊕ edits


def test_rebase_then_consumer_upgrade_round_trip():
    # The full three-tier loop: fork adopted -> parent moves -> rebase -> the deployment
    # follows with the NORMAL upgrade flow (nothing new to learn on the consumer side).
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, reg = _world()
        _gitify(reg)
        working = _install_acme(root)
        internal = _internal()
        working.write_text(working.read_text().replace("width: 1280", "width: 2048"))
        assert _run("--root", str(root), "pkg", "promote", "acme_cam", "--kind", "profile",
                    "--name", "org-cam", "--to", "internal", "--adopt")[0] == 0
        _bump_parent(reg, "2.1.0",
                     "service: camish\ncamera: {type: usb}\nusb: {width: 1280, fps: 30}\n")
        _commit(reg, "bump 2.1.0")
        assert _run("pkg", "rebase", "camish:org-cam", "--to", "internal")[0] == 0
        rc, _, err = _run("--root", str(root), "pkg", "upgrade", "acme_cam")
        assert rc == 0, err
        assert "internal/camish:org-cam@1.0.0 -> internal/camish:org-cam@1.0.1" in err
        data = yaml.safe_load(working.read_text())
        assert data["usb"] == {"width": 2048, "fps": 30}          # org width + parent's new key
        rc, out, _ = _run("--root", str(root), "config", "diff", "acme_cam")
        assert "clean" in out                                     # delta-free after the follow


def test_pkg_info_shows_lineage_and_freshness():
    with _env(RIG_HOME=tempfile.mkdtemp()):
        root, reg = _world()
        _fork_org_cam(root)
        rc, out, _ = _run("pkg", "info", "internal/camish:org-cam")
        assert rc == 0 and "based_on: testns/camish:acme-cam@2.0.0" in out
        assert "available" not in out                             # parent unchanged: no hint
        _bump_parent(reg, "2.5.0", "service: camish\ncamera: {type: usb}\nusb: {width: 1}\n")
        rc, out, _ = _run("pkg", "info", "internal/camish:org-cam")
        assert "2.5.0 available" in out and "pkg rebase camish:org-cam" in out


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
