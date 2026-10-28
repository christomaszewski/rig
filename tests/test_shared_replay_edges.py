"""Shared recipe selection stays stable across source resolution and preparation."""
import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import shutil
import unittest
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_replay_preparation as fixtures
from rig_cli import replay_recipe, replay_workflow
from rig_cli import replay_prepare
from rig_cli.replay_sources import source_fingerprint


class SharedReplayEdges(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.Preparation()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def command(self, *args):
        with self.f.image_label_runtime({}), contextlib.redirect_stdout(io.StringIO()) as output, \
                contextlib.redirect_stderr(io.StringIO()):
            rc = replay_workflow.command(self.f.args(*args, "--json"), self.f.root)
        return rc, json.loads(output.getvalue())

    def generated_name(self):
        source = self.f.source.resolve()
        return source.name + "-" + replay_recipe.digest(str(source))[:8]

    def test_implicit_prepare_does_not_consume_a_same_named_shared_session(self):
        name = self.generated_name()
        shared = {"version": 2, "session": {"instances": ["idle"], "clock": "wall",
                    "overrides": {"idle": {"gain": 9}}}, "replay": {}}
        path = self.f.write(f"sessions/{name}.yaml", shared)
        before = path.read_bytes()
        rc, plan = self.command("prepare", str(self.f.source))
        self.assertEqual(rc, 0, plan["issues"])
        self.assertEqual(plan["selected"], ["router", "new", "player"])
        self.assertNotIn("session_recipe", plan)
        self.assertTrue(plan["sim_time"])
        self.assertEqual(path.read_bytes(), before)
        saved = yaml.safe_load((self.f.root / "replay" / (name + ".yaml")).read_text())
        self.assertEqual(saved["version"], 1)

    def test_existing_generated_v2_recipe_applies_its_selection_overrides_and_clock(self):
        # Explicitly supplied files may live anywhere, including replay/. Reusing
        # a previously chosen generated path must apply the whole document.
        name = self.generated_name()
        doc = {"version": 2, "session": {"instances": ["idle"], "clock": "wall",
                    "overrides": {"idle": {"gain": 9}}}, "replay": {}}
        path = self.f.write(f"replay/{name}.yaml", doc)
        original = path.read_bytes()
        rc, plan = self.command("prepare", str(self.f.source))
        self.assertEqual(rc, 0, plan["issues"])
        self.assertEqual(plan["selected"], ["idle", "player"])
        self.assertEqual(plan["configs"]["idle"]["gain"], 9)
        self.assertFalse(plan["sim_time"])
        self.assertEqual(plan["session_recipe"], doc)
        self.assertEqual(path.read_bytes(), original)

    def test_docker_routing_override_rejected_before_inventory_or_preparation(self):
        vehicle = yaml.safe_load((self.f.root / "vehicle.yaml").read_text())
        vehicle["env"] = {"DOCKER_HOST": "tcp://unrelated.invalid:2375"}
        self.f.write("vehicle.yaml", vehicle)
        self.f.write("sessions/shared.yaml", {"version": 2,
            "session": {"instances": ["idle"]}, "replay": {"source": str(self.f.source)}})
        with patch("rig_cli.replay_workflow._build_plan") as build:
            rc, result = self.command("prepare", "--recipe", "shared")
        self.assertEqual(rc, 1)
        self.assertIn("Docker routing", result["issues"][0]["message"])
        build.assert_not_called()
        self.assertFalse((self.f.root / "var/replay").exists())

    def test_proxy_generation_is_stable_until_shared_intent_changes(self):
        self.f.proxy_camera()
        self.f.recording()
        first = self.f.proxy_artifact()
        document = {"version": 2, "session": {"instances": ["cam"]},
                    "replay": {"source": str(self.f.source)}}
        path = self.f.write("sessions/proxy.yaml", document)
        original_source = source_fingerprint(self.f.source)
        rc, prepared = self.command("prepare", "--recipe", "proxy", "--use-proxies")
        self.assertEqual(rc, 0, prepared["issues"])
        self.assertEqual(prepared["recordings"]["cam"]["proxy"]["export_id"], first.name)
        self.f.proxy_artifact(variation="newer")
        rc, unchanged = self.command("check", "--recipe", "proxy", "--use-proxies")
        self.assertEqual(rc, 0, unchanged["issues"])
        self.assertEqual(unchanged["recordings"]["cam"]["proxy"]["export_id"], first.name)
        changed = copy.deepcopy(document)
        changed["session"]["overrides"] = {"cam": {"gain": 7}}
        self.f.write("sessions/proxy.yaml", changed)
        self.assertEqual(replay_prepare.proxy_pins(self.f.root, path, changed, self.f.source), {})
        rc, ambiguous = self.command("check", "--recipe", "proxy", "--use-proxies")
        self.assertEqual(rc, 1)
        self.assertTrue(any("multiple compatible" in issue["message"] for issue in ambiguous["issues"]))
        self.assertEqual(source_fingerprint(self.f.source), original_source)

    def test_prepared_source_binding_tracks_nested_replay_source_without_rewriting_yaml(self):
        second = self.f.source.parent / "second"
        third = self.f.source.parent / "third"
        shutil.copytree(self.f.source, second)
        shutil.copytree(self.f.source, third)
        document = {"version": 2, "session": {"instances": ["idle"]},
                    "replay": {"source": str(self.f.source)}}
        path = self.f.write("sessions/source.yaml", document)
        original = path.read_bytes()
        rc, overridden = self.command("prepare", str(second), "--recipe", "source")
        self.assertEqual(rc, 0, overridden["issues"])
        self.assertEqual(path.read_bytes(), original)
        rc, bound = self.command("check", "--recipe", "source")
        self.assertEqual(rc, 0, bound["issues"])
        self.assertEqual(bound["source"]["path"], str(second.resolve()))
        document["replay"]["source"] = str(third)
        self.f.write("sessions/source.yaml", document)
        rc, changed = self.command("check", "--recipe", "source")
        self.assertEqual(rc, 0, changed["issues"])
        self.assertEqual(changed["source"]["path"], str(third.resolve()))


if __name__ == "__main__":
    unittest.main()
