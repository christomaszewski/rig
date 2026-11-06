"""Unified selectors keep exact recorded identities across export, prepare and replay."""
import contextlib
import copy
import io
import json
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_replay_preparation as fixtures
from rig_cli import RigError, cli, replay_exports, replay_inputs, replay_recipe, service_exports
from rig_cli.replay_workflow import command, load_context


HELPER = '''#!/usr/bin/python3
import argparse,json,shutil
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--request');p.add_argument('--result');p.add_argument('--preflight',action='store_true')
a=p.parse_args();req=json.loads(Path(a.request).read_text());root=Path(req['source_root']);records=[]
for row in req['recordings']:
    selected=row.get('sessions') or [p.stem for p in (root/row['relative']).glob('*.json')]
    selected=[s['id'] if isinstance(s,dict) else s for s in selected]
    sessions=[]
    for sid in selected:
        files=sorted(str(p.relative_to(root)) for p in (root/row['relative']).glob(sid+'*') if p.is_file())
        if not a.preflight:
            for f in files:
                dst=Path(req['destination'])/f;dst.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(root/f,dst)
        sessions.append(dict(id=sid,files=files))
    records.append(dict(name=row['name'],relative=row['relative'],sessions=sessions))
if not a.preflight:
    with (Path(__file__).parent/'conversions').open('a') as f:f.write('x')
Path(a.result).write_text(json.dumps(dict(version=1,profile_sha256=req['profile']['sha256'],recordings=records,
    producer={'implementation_sha256':'fixture'},validation={'ok':True})))
'''


class ReplayInputs(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.Preparation()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def camera(self):
        self.f.proxy_camera()
        self.f.rows["sensors"][0]["replay"] = {"start": True}
        self.f.vehicle()

    def export_args(self, *extra):
        return cli.build_parser().parse_args(cli.translate_argv([
            "run", "export", "--recipe", "inputs", *extra]))

    def invoke(self, *args):
        with patch("rig_cli.image_catalog.docker_arch", side_effect=RigError("offline")), \
                patch("rig_cli.replay._guard_clean_host"), \
                contextlib.redirect_stdout(io.StringIO()) as output, \
                contextlib.redirect_stderr(io.StringIO()):
            result = command(self.f.args(*args, "--json"), self.f.root)
        return result, json.loads(output.getvalue())

    def test_schema_and_repeatable_cli_overrides_for_all_commands(self):
        doc = {"version": 1, "bags": [{"logger": "front"}, {"logger": "rear", "session": "latest"}],
               "inputs": {"bags": {"profile": "compact"}, "instance:cam": {"use": "original"}},
               "playback": {"image_sources": {"cam": "bag"}}}
        self.assertEqual(replay_recipe.validate(copy.deepcopy(doc)), doc)
        for args in (["--input", "bags=original"], ["check", "--input", "bags=original"],
                     ["prepare", "--input", "bags=original"]):
            parsed = self.f.args(*args)
            updated = replay_inputs.with_overrides(doc, parsed.inputs)
            self.assertEqual(updated["inputs"]["bags"], {"use": "original"})
            self.assertEqual(doc["inputs"]["bags"], {"profile": "compact"})
        self.assertEqual(self.export_args("--input", "bags=original").inputs, ["bags=original"])
        for invalid in ({"bag": {}, "bags": "all"}, {"bags": []},
                        {"inputs": {"bags": {"profile": "compact", "proxy": "abc"}}}):
            with self.assertRaises(RigError):
                replay_recipe.validate({"version": 1, **invalid})
        with self.assertRaisesRegex(RigError, "conflicting"):
            replay_inputs.parse_overrides(["bags=original", "bags=profile:compact"])

    def test_unknown_and_future_subinputs_fail_actionably(self):
        manifest, _, _ = load_context(self.f.root)
        for target in ("instance:cam/image", "instance:cam:raw", "future:stream"):
            doc = replay_recipe.validate({"version": 1, "inputs": {target: {"use": "original"}}})
            with self.assertRaisesRegex(RigError, "sub-input support"):
                replay_inputs.validate_targets(doc, manifest)
        with self.assertRaisesRegex(RigError, "unknown destination"):
            replay_inputs.validate_targets({"inputs": {"instance:absent": {"use": "original"}}}, manifest)

    def test_explicit_profile_does_not_need_flag_and_default_stays_original(self):
        self.camera()
        self.f.recording()
        artifact = self.f.proxy_artifact()
        original = self.f.plan()
        self.assertNotIn("proxy", original["recordings"]["cam"])
        doc = {"version": 1, "inputs": {"instance:cam": {"profile": "h264"}}}
        plan = self.f.plan(doc)
        self.assertTrue(plan["ready_to_prepare"], plan["issues"])
        self.assertTrue(plan["use_proxies"])
        self.assertEqual(plan["recordings"]["cam"]["proxy"]["export_id"], artifact.name)

    def test_changed_profile_re_resolves_implicit_pin_but_explicit_id_stays_exact(self):
        self.camera()
        self.f.recording()
        first = self.f.proxy_artifact()
        recipe = {"version": 1, "inputs": {"instance:cam": {"profile": "h264"}}}
        previous = self.f.plan(recipe)["recordings"]["cam"]
        path = self.f.root / "services/camera/export-profiles/h264.yaml"
        definition = yaml.safe_load(path.read_text())
        definition["revision"] += 1
        path.write_text(yaml.safe_dump(definition))
        replacement = self.f.proxy_artifact(variation="new-profile")
        plan = self.f.plan(recipe, opts={"_prepared_proxies": {"cam": previous}})
        self.assertTrue(plan["ready_to_prepare"], plan["issues"])
        self.assertEqual(plan["recordings"]["cam"]["proxy"]["export_id"], replacement.name)
        pinned = self.f.plan({"version": 1, "inputs": {"instance:cam": {"proxy": first.name}}})
        self.assertEqual(pinned["recordings"]["cam"]["proxy"]["export_id"], first.name)

    def test_explicit_original_wins_legacy_flag_but_conflicting_declarations_fail(self):
        self.camera()
        self.f.recording()
        doc = {"version": 1, "inputs": {"instance:cam": {"use": "original"}}}
        plan = self.f.plan(doc, opts={"use_proxies": True})
        self.assertTrue(plan["ready_to_prepare"], plan["issues"])
        self.assertNotIn("proxy", plan["recordings"]["cam"])
        doc["proxies"] = {"services": {"camera": {"profile": "h264"}}}
        failed = self.f.plan(doc)
        self.assertFalse(failed["ready_to_prepare"])
        self.assertTrue(any("conflicts with legacy" in e["message"] for e in failed["issues"]))

    def test_prepare_persists_cli_selector_and_keeps_exact_pin(self):
        self.camera()
        self.f.recording()
        first = self.f.proxy_artifact()
        path = self.f.write("replay/inputs.yaml", {"version": 1, "source": str(self.f.source)})
        _, prepared = self.invoke("prepare", "--recipe", "inputs", "--input", "instance:cam=profile:h264")
        self.assertEqual(yaml.safe_load(path.read_text())["inputs"], {"instance:cam": {"profile": "h264"}})
        self.assertEqual(prepared["recordings"]["cam"]["proxy"]["export_id"], first.name)
        self.f.proxy_artifact(variation="later")
        _, checked = self.invoke("check", "--recipe", "inputs")
        self.assertTrue(checked["ready_to_prepare"], checked["issues"])
        self.assertEqual(checked["recordings"]["cam"]["proxy"]["export_id"], first.name)
        shutil.rmtree(first)
        _, broken = self.invoke("check", "--recipe", "inputs")
        self.assertFalse(broken["ready_to_prepare"])
        self.assertTrue(any(first.name in e["message"] for e in broken["issues"]))

    def test_failed_preparation_restores_recipe_cli_choices(self):
        self.camera()
        self.f.recording()
        self.f.proxy_artifact()
        path = self.f.write("replay/inputs.yaml", {"version": 1, "source": str(self.f.source)})
        before = path.read_bytes()
        with patch("rig_cli.replay_prepare.create", side_effect=RigError("fixture failure")):
            with self.assertRaisesRegex(RigError, "fixture failure"):
                # command catches RigError in JSON mode; use the human command path here.
                with patch("rig_cli.image_catalog.docker_arch", side_effect=RigError("offline")), \
                        patch("rig_cli.replay._guard_clean_host"), contextlib.redirect_stderr(io.StringIO()):
                    command(self.f.args("prepare", "--recipe", "inputs", "--input",
                                        "instance:cam=profile:h264"), self.f.root)
        self.assertEqual(path.read_bytes(), before)

    def test_recipe_export_uses_alias_and_exact_sessions_then_keeps_pin(self):
        self.camera()
        self.f.recording("old_cam", "early", 101)
        self.f.recording("old_cam", "late", 109)
        helper = self.f.root / "services/camera/tools/export-recordings"
        helper.write_text(HELPER)
        doc = {"version": 1, "source": str(self.f.source),
               "instances": {"cam": {"recorded_as": "old_cam"}},
               "recordings": {"cam": {"from": "recordings/old_cam", "sessions": ["late"]}},
               "inputs": {"instance:cam": {"profile": "h264"}}}
        path = self.f.write("replay/inputs.yaml", doc)
        before = {p: p.read_bytes() for p in (self.f.source / "recordings").rglob("*") if p.is_file()}
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(replay_exports.command(self.export_args(), self.f.root), 0)
            self.assertEqual(replay_exports.command(self.export_args(), self.f.root), 0)
        self.assertEqual((helper.parent / "conversions").read_text(), "x")
        artifact, contents = service_exports.committed_artifacts(self.f.source)[0]
        self.assertEqual(contents["recordings"][0]["name"], "old_cam")
        self.assertEqual([s["id"] for s in contents["recordings"][0]["sessions"]], ["late"])
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        pins = replay_exports.load_pins(self.f.root, path, doc, self.f.source)
        self.assertEqual(pins["recordings"]["cam"]["proxy"]["export_id"], artifact.name)
        _, checked = self.invoke("check", "--recipe", "inputs")
        self.assertTrue(checked["ready_to_prepare"], checked["issues"])
        self.assertEqual(checked["recordings"]["cam"]["proxy"]["export_id"], artifact.name)

    def test_exact_pin_finds_nondefault_profile(self):
        self.camera()
        self.f.recording()
        helper = self.f.root / "services/camera/tools/export-recordings"
        helper.write_text(HELPER)
        descriptor_path = self.f.root / "services/camera/rigging.yaml"
        definition = yaml.safe_load(descriptor_path.read_text())
        definition["export"]["profiles"]["review"] = "export-profiles/review.yaml"
        descriptor_path.write_text(yaml.safe_dump(definition))
        self.f.write("services/camera/export-profiles/review.yaml", {
            "version": 1, "id": "review", "revision": 1, "format": "camera-recording-proxy/v1", "lossy": True})
        manifest, catalog, descriptors = load_context(self.f.root)
        plan = self.f.plan()
        with contextlib.redirect_stderr(io.StringIO()):
            created = service_exports.build_binding(manifest, catalog, descriptors,
                plan["source"], plan["recordings"]["cam"], service="camera", profile="review")
        doc = {"version": 1, "inputs": {"instance:cam": {"proxy": created["proxy"]["export_id"]}}}
        selected = self.f.plan(doc)
        self.assertTrue(selected["ready_to_prepare"], selected["issues"])
        self.assertEqual(selected["recordings"]["cam"]["proxy"]["profile"], "review")

    def second_camera(self):
        self.f.rows["sensors"].append(dict(name="cam_b", service="camera", config="cam_b.yaml",
                                            replay={"start": True}))
        self.f.write("cam_b.yaml", {"name": "cam_b", "service": "camera"})
        self.f.vehicle()
        self.f.recording("cam_b")

    def test_profiles_are_per_instance_and_late_export_failure_keeps_artifacts(self):
        self.camera()
        self.f.recording()
        self.second_camera()
        helper = self.f.root / "services/camera/tools/export-recordings"
        helper.write_text(HELPER)
        descriptor_path = self.f.root / "services/camera/rigging.yaml"
        descriptor = yaml.safe_load(descriptor_path.read_text())
        descriptor["export"]["profiles"]["review"] = "export-profiles/review.yaml"
        descriptor_path.write_text(yaml.safe_dump(descriptor))
        self.f.write("services/camera/export-profiles/review.yaml", {
            "version": 1, "id": "review", "revision": 1, "format": "camera-recording-proxy/v1", "lossy": True})
        doc = {"version": 1, "source": str(self.f.source), "inputs": {
            "instance:cam": {"profile": "h264"}, "instance:cam_b": {"profile": "review"}}}
        path = self.f.write("replay/inputs.yaml", doc)
        helper.write_text(HELPER.replace("for row in req['recordings']:",
            "for row in req['recordings']:\n    if row['name']=='cam_b': raise SystemExit(8)"))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaisesRegex(RigError, "helper failed"):
            replay_exports.command(self.export_args(), self.f.root)
        committed = service_exports.committed_artifacts(self.f.source)
        self.assertEqual(len(committed), 1)
        self.assertEqual(committed[0][1]["recordings"][0]["name"], "cam")
        self.assertFalse((replay_recipe.workspace(self.f.root, path) / "export-pins.yaml").exists())
        helper.write_text(HELPER)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(replay_exports.command(self.export_args(), self.f.root), 0)
        pins = replay_exports.load_pins(self.f.root, path, doc, self.f.source)
        self.assertEqual({n: b["proxy"]["profile"] for n, b in pins["recordings"].items()},
                         {"cam": "h264", "cam_b": "review"})

    def test_subinput_export_never_falls_back_to_whole_input(self):
        self.f.write("replay/inputs.yaml", {"version": 1, "source": str(self.f.source),
            "inputs": {"instance:cam/image": {"profile": "h264"}}})
        with self.assertRaisesRegex(RigError, "sub-input support"):
            replay_exports.command(self.export_args(), self.f.root)

    def test_bag_and_camera_export_pins_flow_through_prepare_and_cli_override(self):
        from test_bag_proxies import BagProxies
        bags = BagProxies()
        bags.setUp()
        self.addCleanup(bags.doCleanups)
        self.f = bags.f
        self.camera()
        self.f.recording()
        helper = self.f.root / "services/camera/tools/export-recordings"
        helper.write_text(HELPER)
        doc = {"version": 1, "source": str(self.f.source), "inputs": {
            "bags": bags.choice, "instance:cam": {"profile": "h264"}}}
        path = self.f.write("replay/inputs.yaml", doc)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(replay_exports.command(self.export_args(), self.f.root), 0)
        pins = replay_exports.load_pins(self.f.root, path, doc, self.f.source)
        first_bag = pins["bag"]["export_id"]
        first_camera = pins["recordings"]["cam"]["proxy"]["export_id"]
        _, prepared = self.invoke("prepare", "--recipe", "inputs")
        self.assertTrue(prepared["ready_to_prepare"], prepared["issues"])
        self.assertEqual(prepared["source"]["bag_proxy"]["export_id"], first_bag)
        (bags.repo / "variant").write_text("newer export")
        second = bags.build(force=True)
        self.f.proxy_artifact(variation="second")
        _, checked = self.invoke("check", "--recipe", "inputs")
        self.assertTrue(checked["ready_to_prepare"], checked["issues"])
        self.assertEqual(checked["source"]["bag_proxy"]["export_id"], first_bag)
        self.assertEqual(checked["recordings"]["cam"]["proxy"]["export_id"], first_camera)
        _, overridden = self.invoke("check", "--recipe", "inputs", "--input",
            "bags=proxy:" + second["export_id"], "--input", "instance:cam=proxy:" + first_camera)
        self.assertTrue(overridden["ready_to_prepare"], overridden["issues"])
        self.assertEqual(overridden["source"]["bag_proxy"]["export_id"], second["export_id"])
        self.assertEqual(yaml.safe_load(path.read_text()), doc)  # check never persists overrides

    def test_bag_proxy_changed_during_run_capture_never_launches(self):
        from test_bag_proxies import BagProxies
        from rig_cli.replay_workflow import execute
        bags = BagProxies()
        bags.setUp()
        self.addCleanup(bags.doCleanups)
        binding = bags.build()
        doc = {"version": 1, "inputs": {"bags": bags.choice}}
        plan = bags.f.plan(doc, opts={"_prepared_bag_proxy": binding})
        bags.manifest.data_dir = str(bags.f.root / "data")
        def capture(*args, **kwargs):
            (Path(binding["path"]) / "bag/data.mcap").write_bytes(b"changed during capture")
            return "fixture-run"
        with patch("rig_cli.replay._guard_clean_host"), \
                patch("rig_cli.runs.new_run", side_effect=capture), \
                patch("rig_cli.dispatch.run_verb") as launch, \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(RigError, "changed file"):
                execute(bags.f.args(str(bags.f.source)), bags.f.root, bags.manifest,
                        bags.descriptors, plan, bags.f.source)
        launch.assert_not_called()

    def test_image_authority_survives_final_launch_render_and_provenance(self):
        from rig_cli.replay_workflow import execute
        self.camera()
        self.f.recording()
        self.f.bag(self.f.source / "bags/logger/session-1", topic="/cam/image_raw")
        metadata_path = self.f.source / "bags/logger/session-1/metadata.yaml"
        metadata = yaml.safe_load(metadata_path.read_text())
        metadata["rosbag2_bagfile_information"]["topics_with_message_count"][0]["topic_metadata"]["type"] = "sensor_msgs/msg/Image"
        metadata_path.write_text(yaml.safe_dump(metadata))
        self.f.write("cam.yaml", {"name": "cam", "service": "camera", "plugins": [
            {"name": "ros2-bridge", "enabled": True}, {"name": "webrtc", "enabled": True}]})
        doc = {"version": 1, "playback": {"image_sources": {"cam": "bag"}}}
        plan = self.f.plan(doc)
        self.assertTrue(plan["ready_to_prepare"], plan["issues"])
        self.assertIn("/cam/image_raw", plan["playback"]["topics"])
        manifest, _, descriptors = load_context(self.f.root)
        manifest.data_dir = str(self.f.root / "data")
        def launch(pairs, env, verb, **kwargs):
            camera = next(row for row, _ in pairs if row.name == "cam")
            config = yaml.safe_load(camera.config.read_text())
            self.assertEqual({p["name"]: p["enabled"] for p in config["plugins"]},
                             {"ros2-bridge": False, "webrtc": True})
            return []
        with patch("rig_cli.replay._guard_clean_host"), \
                patch("rig_cli.runs.new_run", return_value="image-run") as captured, \
                patch("rig_cli.replay_workflow._empty_projects", return_value=set(plan["selected"])), \
                patch("rig_cli.runs.snapshot"), patch("rig_cli.dispatch.run_verb", side_effect=launch), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(execute(self.f.args(str(self.f.source)), self.f.root, manifest,
                                     descriptors, plan, self.f.source), 0)
        self.assertEqual(captured.call_args.kwargs["replay"]["image_sources"]["cam"]["source"], "bag")
        self.assertEqual(len(captured.call_args.kwargs["replay"]["bags"]), 1)


if __name__ == "__main__":
    unittest.main()
