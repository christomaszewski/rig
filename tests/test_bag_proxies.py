"""Bag artifacts: source-set identity, immutable publication, recipe workflow and pins."""
import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_replay_preparation as fixtures
from rig_cli import RigError, bag_proxies as bp, cli, replay_exports, replay_prepare
from rig_cli.replay_sources import inspect_source
from rig_cli.replay_workflow import load_context


CONVERTER = '''#!/usr/bin/python3
import json,sys
from pathlib import Path
req=json.loads(Path(sys.argv[1]).read_text());stage=Path(req['destination']);out=stage/'bag';out.mkdir()
variant=Path(__file__).with_name('variant')
(out/'data.mcap').write_text(variant.read_text() if variant.exists() else 'merged')
# Fixture provider emits a valid structural bag inventory; ROS byte correctness is
# separately covered by the converter's native integration tests.
info=dict(storage_identifier='mcap',relative_file_paths=['data.mcap'],
 starting_time={'nanoseconds_since_epoch':100000000000},duration={'nanoseconds':10000000000},
 topics_with_message_count=[{'topic_metadata':{'name':'/old/input','type':'std_msgs/msg/String'},'message_count':20}])
(out/'metadata.yaml').write_text(json.dumps({'rosbag2_bagfile_information':info}))
(stage/'messages.jsonl').write_text('{}\\n')
result=dict(version=1,policy=req['policy'],producer={'implementation_sha256':'fixture'},
 validation={'ok':True},statistics={'input_messages':20,'output_messages':20,'ambiguous_preserved':2})
Path(req['result']).write_text(json.dumps(result))
'''


class BagProxies(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.Preparation()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.bag(self.f.source / "bags/logger2/session-2", topic="/old/input")
        self.repo = self.f.root / "services/ros2-bag-player"
        desc = yaml.safe_load((self.repo / "rigging.yaml").read_text())
        desc["verbs"]["replay-export-bags"] = "replay-export-bags"
        (self.repo / "rigging.yaml").write_text(yaml.safe_dump(desc))
        script = (self.repo / "up").read_text().replace('"plan":1', '"plan":2,"multi_input":1')
        script = script.replace('else exit 42; fi',
            'elif [ "$2" = replay-export-bags ]; then exec /usr/bin/python3 "$(dirname "$0")/convert.py" "$3"; else exit 42; fi')
        (self.repo / "up").write_text(script)
        (self.repo / "convert.py").write_text(CONVERTER)
        self.definition = dict(version=1, id="arrival-conservative", revision=1, format=bp.FORMAT,
            lossy=True, policy=dict(time_source="received", deduplication="conservative",
            sensor_timestamp_field="header.stamp", timestamp_fallback="error", topic_timestamp_fields={},
            max_clock_skew_s=60.0, dedup_window_ms=100.0))
        (self.repo / "export-profiles").mkdir()
        self.profile_path = self.repo / "export-profiles/arrival-conservative.yaml"
        self.profile_path.write_text(json.dumps(self.definition))
        self.manifest, self.catalog, self.descriptors = load_context(self.f.root)
        self.row = next(r for r in self.manifest.sensors if r.name == "player")
        self.descriptor = self.descriptors[self.row.service]
        self.source = inspect_source(self.manifest, str(self.f.source))
        self.choice = {"profile": "arrival-conservative"}

    def build(self, choice=None, **kwargs):
        with contextlib.redirect_stderr(io.StringIO()):
            return bp.build(self.source, choice or self.choice, self.row, self.descriptor,
                            self.manifest, self.descriptors, root=self.f.root, **kwargs)

    def test_publish_verify_and_reuse_exact_selected_sessions(self):
        binding = self.build()
        self.assertEqual(len(binding["manifest"]["inputs"]), 2)
        self.assertTrue((Path(binding["path"]) / "messages.jsonl").is_file())
        self.assertFalse(list(Path(binding["path"]).glob(".*request*")))
        self.assertEqual(bp.verify_binding(binding, self.f.source)["export_id"], binding["export_id"])
        self.assertEqual(self.build()["export_id"], binding["export_id"])
        applied = bp.apply_selection(self.source, self.choice, self.row, self.descriptor,
                                    {"_deployment_root": str(self.f.root)})
        self.assertEqual(len(applied["bags"]), 1)
        self.assertEqual(len(applied["original_bags"]), 2)
        self.assertEqual(applied["bag_proxy"]["export_id"], binding["export_id"])
        self.assertEqual(len(self.source["bags"]), 2)

    def test_subset_cannot_use_combined_proxy(self):
        binding = self.build()
        subset = inspect_source(self.manifest, str(self.f.source), {"logger": "logger"})
        with self.assertRaisesRegex(RigError, "exact selected inputs"):
            bp.resolve(subset, {"proxy": binding["export_id"]}, self.row, self.descriptor)

    def test_ambiguity_requires_pin_and_never_chooses_latest(self):
        first = self.build()
        (self.repo / "variant").write_text("another conversion")
        second = self.build(force=True)
        self.assertNotEqual(first["export_id"], second["export_id"])
        with self.assertRaisesRegex(RigError, "Multiple compatible"):
            self.build()
        self.assertEqual(self.build({"proxy": first["export_id"]})["export_id"], first["export_id"])

    def test_corrupt_output_and_changed_original_are_rejected(self):
        binding = self.build()
        output = Path(binding["path"]) / "bag/data.mcap"
        payload = output.read_bytes()
        output.write_bytes(b"corruption")
        with self.assertRaisesRegex(RigError, "changed file"):
            bp.verify_binding(binding, self.f.source)
        output.write_bytes(payload)
        (self.f.source / "bags/logger/session-1/data.mcap").write_bytes(b"changed original")
        with self.assertRaisesRegex(RigError, "changed file"):
            bp.verify_binding(binding, self.f.source)

    def test_config_changes_profile_identity_and_explicit_pin_conflicts(self):
        binding = self.build()
        self.f.write("player.yaml", {"name": "player", "service": "ros2-bag-player",
                                    "play": {"dedup_window_ms": 20}})
        with self.assertRaises(bp.MissingProxy):
            bp.resolve(self.source, self.choice, self.row, self.descriptor)
        with self.assertRaisesRegex(RigError, "conflicts"):
            bp.resolve(self.source, {"proxy": binding["export_id"]}, self.row, self.descriptor)
        replacement = self.build()
        selected = bp.resolve(self.source, self.choice, self.row, self.descriptor, pin=binding)
        self.assertEqual(selected["export_id"], replacement["export_id"])

    def test_failed_build_does_not_publish_or_mutate_originals(self):
        original = (self.f.source / "bags/logger/session-1/data.mcap").read_bytes()
        (self.repo / "convert.py").write_text("raise SystemExit(9)\n")
        with self.assertRaisesRegex(RigError, "converter failed"):
            self.build()
        self.assertEqual(list(bp.committed_artifacts(self.f.source)), [])
        self.assertEqual((self.f.source / "bags/logger/session-1/data.mcap").read_bytes(), original)
        self.assertFalse(list((self.f.source / "exports/inputs/bags/arrival-conservative").glob(".stage-*")))

    def test_tf_lead_refines_policy_without_rewriting_legacy_artifact_identity(self):
        binding = self.build()
        original = copy.deepcopy(binding["manifest"])
        self.assertNotIn("tf_lead_time_ms", original["profile"]["definition"]["policy"])
        self.assertEqual(original["profile"]["sha256"], bp.artifacts.digest(self.definition))
        self.assertEqual(bp.load_artifact(binding["path"]), original)
        self.f.write("player.yaml", {"name": "player", "service": "ros2-bag-player",
                                    "play": {"tf_lead_time_ms": 25}})
        # The old artifact does not attest any TF lead; an explicit pin must not claim it does.
        with self.assertRaisesRegex(RigError, "tf_lead_time_ms conflicts"):
            bp.resolve(self.source, {"proxy": binding["export_id"]}, self.row, self.descriptor)
        replacement = self.build()
        self.assertEqual(replacement["profile"]["definition"]["policy"]["tf_lead_time_ms"], 25)
        self.assertNotEqual(replacement["export_id"], binding["export_id"])
        self.assertEqual(bp.load_artifact(binding["path"]), original)

    def test_tf_lead_policy_rejects_invalid_values_and_preserves_missing_setting(self):
        legacy = copy.deepcopy(self.definition["policy"])
        self.assertEqual(bp._policy(legacy), self.definition["policy"])
        self.assertNotIn("tf_lead_time_ms", legacy)
        for lead in (0, 100, 12.5):
            self.assertEqual(bp._policy({**legacy, "tf_lead_time_ms": lead})["tf_lead_time_ms"], lead)
        for lead in (-1, True, "100", None, float("nan"), float("inf")):
            with self.subTest(lead=lead), self.assertRaisesRegex(RigError, "tf_lead_time_ms"):
                bp._policy({**legacy, "tf_lead_time_ms": lead})

    def test_changed_converter_requires_new_profile_resolution_but_exact_pin_remains_valid(self):
        binding = self.build()
        (self.repo / "tools").mkdir()
        (self.repo / "tools/bag_proxy.py").write_text("# new converter implementation\n")
        with self.assertRaises(bp.MissingProxy):
            bp.resolve(self.source, self.choice, self.row, self.descriptor)
        self.assertEqual(bp.resolve(self.source, {"proxy": binding["export_id"]}, self.row,
                                    self.descriptor)["export_id"], binding["export_id"])

    def test_extra_artifact_file_is_rejected(self):
        binding = self.build()
        (Path(binding["path"]) / "unexpected").write_text("not attested")
        with self.assertRaisesRegex(RigError, "unexpected"):
            bp.verify_binding(binding, self.f.source)

    def test_recipe_export_builds_and_pins_for_prepare(self):
        recipe = {"version": 1, "source": str(self.f.source), "bags": "all",
                  "inputs": {"bags": self.choice}}
        path = self.f.write("replay/bags.yaml", recipe)
        args = cli.build_parser().parse_args(cli.translate_argv(["run", "export", "--recipe", "bags"]))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(replay_exports.command(args, self.f.root), 0)
        pins = replay_exports.load_pins(self.f.root, path, recipe, self.f.source)
        self.assertTrue(pins["bag"]["export_id"])
        # A second generation must not alter the exported recipe's choice.
        (self.repo / "variant").write_text("another conversion")
        self.build(force=True)
        plan = self.f.plan(recipe, opts={"_prepared_bag_proxy": pins["bag"]})
        self.assertTrue(plan["ready_to_prepare"], plan["issues"])
        self.assertEqual(plan["source"]["bag_proxy"]["export_id"], pins["bag"]["export_id"])
        runtime = replay_prepare.create(self.f.root, path, recipe, plan, self.manifest, self.descriptors)
        self.assertEqual(runtime, self.f.source.resolve())
        document = replay_prepare.player_document(plan, runtime)
        self.assertEqual(document["version"], 2)
        self.assertEqual(document["proxy"]["export_id"], pins["bag"]["export_id"])

    def test_original_time_source_must_be_received(self):
        self.f.write("player.yaml", {"name": "player", "service": "ros2-bag-player",
                                    "play": {"time_source": "sensor"}})
        with self.assertRaisesRegex(RigError, "Original bags"):
            bp.apply_selection(self.source, {"use": "original"}, self.row, self.descriptor, {})


if __name__ == "__main__":
    unittest.main()
