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
                                         "profile": "testns/acme-cam@2.0.0"}
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
        m = yaml.safe_load((internal / "profiles" / "acme-lowlight" / "manifest.yaml").read_text())
        assert m["requires"]["service"] == "testns/camish@1.2.0"  # from the lock pin, cross-ns
        payload = yaml.safe_load(
            (internal / "profiles" / "acme-lowlight" / "config" / "payload.yaml").read_text())
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
        working = _install_acme(root)                       # pins testns/acme-cam@2.0.0
        mpath = reg / "profiles" / "acme-cam" / "manifest.yaml"
        m = yaml.safe_load(mpath.read_text())               # hand-add a schema, like a registry author
        m["config"]["overrides_schema"] = "config/schema.json"
        mpath.write_text(yaml.safe_dump(m))
        (reg / "profiles" / "acme-cam" / "config" / "schema.json").write_text('{"type": "object"}')
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
            (reg / "profiles" / "acme-cam" / "config" / "payload.yaml").read_text())
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
        m = yaml.safe_load((internal / "profiles" / "acme-cam" / "manifest.yaml").read_text())
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
        m = yaml.safe_load((internal / "profiles" / "handy" / "manifest.yaml").read_text())
        assert m["kind"] == "profile" and m["requires"]["service"] == "testns/camish@1.2.0"
        payload = yaml.safe_load(
            (internal / "profiles" / "handy" / "config" / "payload.yaml").read_text())
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
        assert s["members"]["profiles"] == ["testns/acme-cam@2.0.0"]   # foreign ref: alias == ns
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
        (internal / "profiles" / "keeper" / "NOTES.md").write_text("precious\n")
        rc, _, err = _run("--root", str(root), "pkg", "promote", "acme_cam", "--kind", "profile",
                          "--name", "keeper", "--to", "internal", "--bump",
                          "--requires", "internal/ghost@9.9.9")   # unresolvable -> validation fails
        assert rc == 1, "sabotaged promote should fail validation"
        m = yaml.safe_load((internal / "profiles" / "keeper" / "manifest.yaml").read_text())
        assert m["version"] == "1.0.0"                            # restored, not deleted
        assert m["provides"]["sensor"][0]["match"] == ["idA"]
        assert (internal / "profiles" / "keeper" / "NOTES.md").read_text() == "precious\n"


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
