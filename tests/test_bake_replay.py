"""Onboard replay packaging: real vendored queries, fake Docker, no vehicle services."""
import contextlib
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rig_cli import RigError, bake, cli, runtime_images
from rig_cli.catalog import load_catalog
from rig_cli.descriptor import load_descriptor
from rig_cli.dispatch import fleet_env
from rig_cli.manifest import load_manifest
from test_artifact_images import Docker


class BakeReplay(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, RIG_HOME=str(self.root / "user"),
                              RIG_VEHICLE_LOCAL=str(self.root / "absent"), BENCH_IMAGE="bad")
        self.env.start()
        self.addCleanup(self.env.stop)
        self.docker = Docker()
        self.docker.info["Config"] = {"Labels": {"io.rig.rosbag2.replay": "1", "camera.reader": "1"}}
        self.rows = {
            "infra": [dict(name="router", service="router", config="config/router.yaml")],
            "sensors": [dict(name="cam", service="camera", config="config/cam.yaml"),
                        dict(name="gnss", service="gnss", config="config/gnss.yaml")],
            "autonomy": [dict(name="planner", service="planner", config="config/planner.yaml",
                              enabled=False, replay={"start": True}),
                         dict(name="player", service="ros2-bag-player", config="config/player.yaml",
                              enabled=False, order=999)]}
        self.write("vehicle.yaml", dict(vehicle="car", vehicle_id=7, platform="jp7", arch="arm64",
                                        ros={"rmw": "rmw_cyclonedds_cpp"}, **self.rows))
        routes = {}
        for row in [r for rows in self.rows.values() for r in rows]:
            name, service = row["name"], row["service"]
            self.write(row["config"], dict(name=name, service=service))
            routes[service] = {"path": f"repos/{service}"}
            desc = dict(service=service, launcher="launch", launch_surface=["launch"],
                        verbs={"images": "images", "runtime-images": "runtime-images"})
            if service == "camera":
                desc["build"] = {"images": ["reader"], "platforms": ["jp7", "dev"],
                                 "architectures": ["arm64", "amd64"]}
                desc["replay"] = {"platform": "dev", "source": {
                    "data": "recordings/{name}", "binding": "direct",
                    "image_labels": {"reader": {"camera.reader": "1"}},
                    "overrides": {"camera": {"type": "replay"}, "input": "{{replay_input_path}}",
                                  "sessions": "{{replay_input_sessions}}"}}}
            if service == "ros2-bag-player":
                desc["verbs"]["replay-capabilities"] = "replay-capabilities"
            if service == "gnss":
                del desc["verbs"]["runtime-images"]  # bag-fed hardware is never queried for replay
            self.write(f"repos/{service}/rigging.yaml", desc)
            launcher = self.root / f"repos/{service}/launch"
            launcher.write_text(f'''#!{sys.executable}
import json, os, sys
sys.path.insert(0, {str(Path(yaml.__file__).parent.parent)!r})
import yaml
cfg = yaml.safe_load(open(sys.argv[1]))
verb = sys.argv[2]
if verb == "replay-capabilities":
    print(json.dumps(dict(plan=1, inspect_calls=1, image_labels={{"bag-player": {{"io.rig.rosbag2.replay": "1"}}}})))
    sys.exit(0)
assert 'BENCH_IMAGE' not in os.environ
assert os.environ['RIG_TARGET_PLATFORM'] == 'jp7'
assert os.environ['RIG_TARGET_ARCH'] == 'arm64'
name = cfg['name']
if cfg['service'] == 'ros2-bag-player':
    name = 'player'
if name == 'cam' and cfg.get('camera', {{}}).get('type') == 'replay':
    assert cfg['input'].endswith('/recordings/cam')
    assert cfg['sessions'] == []
    name = 'reader'
ref = 'reg/' + name + ':v1-jp7'
if verb == 'config':
    print(json.dumps(dict(services={{'app': {{'image': ref}}}})))
elif verb == 'images':
    print(json.dumps([ref]))
elif verb == 'runtime-images':
    role = 'bag-player' if name == 'player' else name
    print(json.dumps(dict(version=1, images=[ref], roles={{role: ref}})))
else:
    raise AssertionError(verb)
''')
            launcher.chmod(0o755)
        self.write("services.yaml", {"services": routes})

    def write(self, name, doc):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(doc, sort_keys=False))

    def bake(self, *, include=True, bundle=False, extra=()):
        with patch("subprocess.run", side_effect=self.docker.run), \
             patch("rig_cli.bake._resolve_digest", return_value=None), \
             contextlib.redirect_stderr(io.StringIO()) as err:
            args = ["--root", str(self.root), "bake", "--tag", "release"]
            if include:
                args.append("--include-replay")
            if bundle:
                args.append("--bundle-images")
            args.extend(extra)
            code = cli.main(args)
            self.assertEqual(code, 0, err.getvalue())
        tree = self.root / "var/bake/release"
        return tree, yaml.safe_load((tree / "metadata.yaml").read_text())

    def remove_player(self, *, route=True):
        doc = yaml.safe_load((self.root / "vehicle.yaml").read_text())
        doc["autonomy"] = [row for row in doc["autonomy"] if row["service"] != "ros2-bag-player"]
        self.write("vehicle.yaml", doc)
        if not route:
            doc = yaml.safe_load((self.root / "services.yaml").read_text())
            del doc["services"]["ros2-bag-player"]
            self.write("services.yaml", doc)

    def inputs_snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*")
                if p.is_file() and p.relative_to(self.root).parts[0] not in ("var", "user")}

    def test_adds_missing_player_to_artifact_from_existing_route(self):
        self.remove_player()
        # A nondefault logger name is selected without copying standalone sample settings.
        doc = yaml.safe_load((self.root / "vehicle.yaml").read_text())
        doc["infra"].append(dict(name="flight_bags", service="ros2-bag-logger", config="config/logger.yaml"))
        self.write("vehicle.yaml", doc)
        self.write("config/logger.yaml", {"service": "ros2-bag-logger", "name": "flight_bags"})
        self.write("repos/ros2-bag-logger/rigging.yaml", {
            "service": "ros2-bag-logger", "launcher": "launch", "launch_surface": ["launch"],
            "verbs": {"runtime-images": "runtime-images"}})
        import shutil
        shutil.copy2(self.root / "repos/router/launch", self.root / "repos/ros2-bag-logger/launch")
        routes = yaml.safe_load((self.root / "services.yaml").read_text())
        routes["services"]["ros2-bag-logger"] = {"path": "repos/ros2-bag-logger"}
        self.write("services.yaml", routes)
        before = self.inputs_snapshot()
        tree, meta = self.bake(bundle=True)
        self.assertEqual(before, self.inputs_snapshot())
        player = next(r for r in load_manifest(tree).sensors if r.service == "ros2-bag-player")
        self.assertEqual((player.name, player.enabled, player.order), ("bag_player", False, 999))
        cfg = yaml.safe_load(player.config.read_text())
        self.assertEqual(cfg["source"], {"logger": "flight_bags", "session": "latest"})
        self.assertEqual(cfg["play"], {"loop": False})
        self.assertIn("reg/player:v1-jp7", meta["bundle"]["refs"])
        self.assertNotIn("bag_player", meta["compose_only"])

    def test_fleet_automatically_discovers_sibling_player_without_source_edits(self):
        self.remove_player(route=False)
        doc = yaml.safe_load((self.root / "vehicle.yaml").read_text())
        doc["vehicle_id"] = "{{vehicle_id}}"
        self.write("vehicle.yaml", doc)
        before = self.inputs_snapshot()
        tree, meta = self.bake(bundle=True)
        self.assertTrue(meta["fleet"])
        self.assertEqual(before, self.inputs_snapshot())
        self.assertIn("bag_player", meta["replay"]["instances"])
        self.assertIn("{{vehicle_id}}", (tree / "vehicle.yaml").read_text())
        routes = load_catalog(tree)
        self.assertEqual(routes["ros2-bag-player"].path, (tree / "services/ros2-bag-player").resolve())

    def test_player_name_and_orphan_config_collisions_are_preserved(self):
        self.remove_player()
        doc = yaml.safe_load((self.root / "vehicle.yaml").read_text())
        doc["autonomy"][0]["name"] = "bag_player"
        self.write("vehicle.yaml", doc)
        self.write("config/planner.yaml", {"name": "bag_player", "service": "planner"})
        self.write("config/autonomy/bag_player_2.yaml", {"keep": "operator config"})
        before = self.inputs_snapshot()
        tree, meta = self.bake()
        self.assertIn("bag_player_3", meta["replay"]["instances"])
        self.assertEqual(before, self.inputs_snapshot())

    def test_registry_fallback_records_exact_package_source_in_artifact_lock(self):
        from rig_cli.registries import Entry
        from rig_cli.lock import load_lock
        self.remove_player(route=False)
        source = {"repo": "https://example.invalid/infra", "rev": "a" * 40, "path": "ros2-bag-player"}
        package = SimpleNamespace(name="ros2-bag-player", kind="service", version="1.20.0",
                                  manifest={"source": source})
        entry = Entry("public", "local-dir", str(self.root / "registry"))
        before = self.inputs_snapshot()
        with patch("rig_cli.bake_replay._local_players", return_value=[]), \
             patch("rig_cli.install.resolve_ref", return_value=(entry, None, package)) as lookup, \
             patch("rig_cli.install._fetch_source", return_value=self.root / "repos/ros2-bag-player") as fetch:
            tree, _ = self.bake()
        lookup.assert_called_once_with("ros2-bag-player", history=False)
        fetch.assert_called_once_with("ros2-bag-player", source)
        self.assertEqual(load_lock(tree)["packages"]["public/ros2-bag-player@1.20.0"]["source"], source)
        self.assertEqual(before, self.inputs_snapshot())

    def test_locked_player_source_wins_over_checkout_and_registry(self):
        from rig_cli.lock import load_lock
        self.remove_player(route=False)
        source = {"repo": "https://example.invalid/pinned", "rev": "b" * 40, "path": "ros2-bag-player"}
        self.write("rig.lock", {"schema": 1, "packages": {
            "public/ros2-bag-player@1.18.0": {"kind": "service", "source": source}}})
        with patch("rig_cli.bake_replay._local_players", side_effect=AssertionError("locked source wins")), \
             patch("rig_cli.install.resolve_ref", side_effect=AssertionError("no registry needed")), \
             patch("rig_cli.install._fetch_source", return_value=self.root / "repos/ros2-bag-player") as fetch:
            tree, _ = self.bake()
        fetch.assert_called_once_with("ros2-bag-player", source)
        self.assertEqual(load_lock(tree)["packages"], load_lock(self.root)["packages"])

    def test_plain_bake_does_not_add_missing_player(self):
        self.remove_player()
        with patch("rig_cli.bake_replay._player_source", side_effect=AssertionError("not requested")):
            tree, _ = self.bake(include=False)
        self.assertFalse(any(r.service == "ros2-bag-player" for r in load_manifest(tree).sensors))

    def test_explicit_player_path_is_one_step_and_existing_player_is_preserved(self):
        self.remove_player(route=False)
        with patch("rig_cli.bake_replay._local_players", side_effect=AssertionError("must use explicit source")):
            tree, _ = self.bake(extra=("--replay-player", str(self.root / "repos/ros2-bag-player")))
        self.assertTrue((tree / "config/autonomy/bag_player.yaml").is_file())
        # Re-baking a tree with its own player must not add a second instance or replace its config.
        self.write("config/player.yaml", {"name": "player", "service": "ros2-bag-player", "play": {"rate": 2}})
        self.write("vehicle.yaml", dict(vehicle="car", vehicle_id=7, platform="jp7", arch="arm64", **self.rows))
        routes = yaml.safe_load((self.root / "services.yaml").read_text())
        routes["services"]["ros2-bag-player"] = {"path": "repos/ros2-bag-player"}
        self.write("services.yaml", routes)
        with patch("rig_cli.bake_replay._player_source", side_effect=AssertionError("already configured")):
            tree, _ = self.bake(extra=("--overwrite",))
        players = [r for r in load_manifest(tree).sensors if r.service == "ros2-bag-player"]
        self.assertEqual(len(players), 1)
        self.assertEqual(yaml.safe_load(players[0].config.read_text())["play"], {"rate": 2})

    def test_unavailable_player_fails_without_wiring_source_or_publishing(self):
        self.remove_player(route=False)
        before = self.inputs_snapshot()
        with patch("rig_cli.bake_replay._local_players", return_value=[]), \
             patch("rig_cli.install.resolve_ref", side_effect=RigError("not configured")), \
             contextlib.redirect_stderr(io.StringIO()) as err:
            code = cli.main(["--root", str(self.root), "bake", "--tag", "missing", "--include-replay"])
        self.assertEqual(code, 1)
        self.assertIn("--replay-player", err.getvalue())
        self.assertFalse((self.root / "var/artifacts/missing.tar.gz").exists())
        self.assertEqual(before, self.inputs_snapshot())

    def test_registry_bake_records_replay_requirements_without_export_or_startup_changes(self):
        tree, meta = self.bake()
        self.assertNotIn("bundle", meta)
        self.assertEqual(set(meta["replay"]["instances"]), {"router", "cam", "planner", "player"})
        self.assertEqual(set(meta["replay"]["images"]), {
            "reg/router:v1-jp7", "reg/reader:v1-jp7", "reg/planner:v1-jp7", "reg/player:v1-jp7"})
        self.assertEqual(meta["compose_only"], ["router", "cam", "gnss"])
        self.assertNotIn("player", (tree / "up.sh").read_text())
        self.assertFalse((tree / "images.tar").exists())
        self.assertFalse(self.docker.calls)
        configs = {r.name: r for r in load_manifest(tree).sensors}
        self.assertFalse(configs["player"].enabled)
        self.assertEqual(yaml.safe_load(configs["cam"].config.read_text()), dict(name="cam", service="camera"))

    def test_bundle_includes_native_replay_images_and_runtime_accepts_loaded_aliases(self):
        tree, meta = self.bake(bundle=True)
        bundle = meta["bundle"]
        self.assertEqual(set(bundle["sources"]), {
            "reg/router:v1-jp7", "reg/cam:v1-jp7", "reg/gnss:v1-jp7",
            "reg/reader:v1-jp7", "reg/planner:v1-jp7", "reg/player:v1-jp7"})
        self.assertTrue((tree / "images.tar").is_file())
        self.assertNotIn("player", (tree / "up.sh").read_text().split("docker compose", 1)[1])
        for ref, item in meta["replay"]["images"].items():
            alias, identities = runtime_images._bundle_source(bundle, ref, "arm64", "jp7")
            self.assertIn(bundle["image_ids"][alias], identities)
        self.assertTrue(all(c[2:4] == ["--platform", "linux/arm64"]
                            for c in self.docker.calls if c[1] == "pull"))
        self.assertFalse(any(c[1] in ("run", "compose", "build") for c in self.docker.calls))

    def test_default_bundle_keeps_only_normal_startup_images(self):
        _, meta = self.bake(include=False, bundle=True)
        self.assertNotIn("replay", meta)
        self.assertEqual(set(meta["bundle"]["sources"]), {
            "reg/router:v1-jp7", "reg/cam:v1-jp7", "reg/gnss:v1-jp7"})

    def test_extracted_bundle_replay_check_uses_loaded_images_without_registry(self):
        from test_replay_preparation import Preparation
        _, meta = self.bake(bundle=True)
        tree = bake.unbake(self.root / "var/artifacts/release.tar.gz", self.root / "extracted")
        source = self.root / "recorded-run"
        Preparation.bag(self, source / "bags/logger/session")
        self.write("recorded-run/manifest.yaml", {"run": "recorded-run", "ended": "2026-01-01T00:00:00Z"})
        # Model docker load: only the archive's content aliases survive on the vehicle.
        self.docker.images = {ref: dict(self.docker.info, Id=image_id)
                              for ref, image_id in meta["bundle"]["image_ids"].items()}
        self.docker.calls.clear()
        with patch.dict(os.environ), patch("subprocess.run", side_effect=self.docker.run), \
             patch("rig_cli.image_catalog.docker_arch", return_value="arm64"), \
             patch("rig_cli.runs.running_projects", return_value=[]), \
             patch("rig_cli.doctor.collect", return_value=[]), \
             contextlib.redirect_stdout(io.StringIO()) as out, \
             contextlib.redirect_stderr(io.StringIO()) as err:
            os.environ.pop("BENCH_IMAGE")
            code = cli.main(["--root", str(tree), "replay", "check", str(source),
                             "--skip-service", "camera", "--json"])
        self.assertEqual(code, 0, err.getvalue() + out.getvalue())
        report = json.loads(out.getvalue())
        self.assertTrue(report["ready_to_run"])
        self.assertEqual(set(report["image_pins"]), {"router", "planner", "player"})
        self.assertTrue(self.docker.calls)
        self.assertTrue(all(c[1:3] == ["image", "inspect"] for c in self.docker.calls))

    def test_extracted_rig_entrypoint_replays_with_automatically_added_player(self):
        from test_replay_preparation import Preparation
        import shlex
        self.remove_player()
        _, meta = self.bake(bundle=True)
        tree = bake.unbake(self.root / "var/artifacts/release.tar.gz", self.root / "onboard")
        source = self.root / "onboard-run"
        Preparation.bag(self, source / "bags/logger/session")
        self.write("onboard-run/manifest.yaml", {"run": "onboard-run", "ended": "2026-01-01T00:00:00Z"})
        shim = self.root / "bin"
        shim.mkdir()
        docker = shim / "docker"
        images = {ref: dict(self.docker.info, Id=image_id)
                  for ref, image_id in meta["bundle"]["image_ids"].items()}
        docker.write_text(f'''#!{sys.executable}
import json, sys
args = sys.argv[1:]
if args[:1] == ['info']:
    print('linux/arm64')
elif args[:2] == ['image', 'inspect']:
    images = {images!r}
    assert args[-1] in images, args
    print(json.dumps(images[args[-1]]))
elif args[:2] == ['compose', 'ls']:
    print('[]')
else:
    raise AssertionError('unexpected Docker mutation: ' + str(args))
''')
        docker.chmod(0o755)
        python = shim / "python3"
        python.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
        python.chmod(0o755)
        env = dict(os.environ, PATH=str(shim) + os.pathsep + os.environ["PATH"],
                   PYTHONPATH=str(Path(yaml.__file__).parent.parent))
        env.pop("BENCH_IMAGE", None)
        checked = subprocess.run(["./rig", "replay", "check", str(source), "--skip-service", "camera", "--json"],
                                 cwd=tree, env=env, capture_output=True, text=True)
        self.assertEqual(checked.returncode, 0, checked.stderr + checked.stdout)
        self.assertEqual(json.loads(checked.stdout)["player"], "bag_player")
        preview = subprocess.run(["./rig", "replay", str(source), "--skip-service", "camera", "--dry-run"],
                                 cwd=tree, env=env, capture_output=True, text=True)
        self.assertEqual(preview.returncode, 0, preview.stderr + preview.stdout)
        self.assertIn("bag_player", preview.stderr + preview.stdout)

    def test_fleet_bake_preserves_identity_and_includes_disabled_player(self):
        self.write("vehicle.yaml", dict(vehicle="car", vehicle_id="{{vehicle_id}}", platform="jp7",
                                        arch="arm64", **self.rows))
        self.write("config/cam.yaml", dict(name="cam", service="camera", address="10.0.{{vehicle_id}}.2"))
        tree, meta = self.bake(bundle=True)
        self.assertTrue(meta["fleet"])
        self.assertIn("reg/player:v1-jp7", meta["bundle"]["refs"])
        self.assertIn("reg/reader:v1-jp7", meta["bundle"]["refs"])
        self.assertIn("{{vehicle_id}}", (tree / "config/cam.yaml").read_text())
        self.assertNotIn("vehicle.local.yaml", [p.name for p in tree.iterdir()])
        self.assertFalse((tree / "up.sh").exists())

    def test_policy_omits_sources_and_infra_but_keeps_explicit_disabled_consumer(self):
        doc = yaml.safe_load((self.root / "vehicle.yaml").read_text())
        doc["replay"] = {"services": {"camera": {"start": False}, "router": {"start": False}}}
        self.write("vehicle.yaml", doc)
        _, meta = self.bake()
        self.assertEqual(set(meta["replay"]["instances"]), {"planner", "player"})

    def test_missing_player_and_incompatible_launcher_fail_before_publishing(self):
        for mode in ("missing", "old"):
            with self.subTest(mode=mode):
                manifest = load_manifest(self.root, context="deployment")
                catalog = load_catalog(self.root)
                descriptors = {s: load_descriptor(s, c.path) for s, c in catalog.items()}
                if mode == "missing":
                    manifest = replace(manifest, sensors=[r for r in manifest.sensors if r.service != "ros2-bag-player"])
                else:
                    descriptors["ros2-bag-player"].verbs.pop("runtime-images")
                with patch("rig_cli.bake._resolve_digest", return_value=None), \
                     contextlib.redirect_stderr(io.StringIO()), self.assertRaises(RigError):
                    bake.bake(self.root, manifest, catalog, descriptors, fleet_env(manifest), mode,
                              include_replay=True)
                self.assertFalse((self.root / f"var/artifacts/{mode}.tar.gz").exists())

    def test_old_reader_image_fails_before_archive_is_published(self):
        self.docker.info["Config"]["Labels"].pop("camera.reader")
        with patch("subprocess.run", side_effect=self.docker.run), \
             patch("rig_cli.bake._resolve_digest", return_value=None), \
             contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(cli.main(["--root", str(self.root), "bake", "--tag", "bad",
                                      "--include-replay", "--bundle-images"]), 1)
        self.assertIn("requires image label camera.reader=1", err.getvalue())
        self.assertFalse((self.root / "var/artifacts/bad.tar.gz").exists())


if __name__ == "__main__":
    unittest.main()
