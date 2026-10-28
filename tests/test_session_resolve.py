"""All public config resolution paths preserve the ephemeral session layer."""
import dataclasses
from pathlib import Path
import sys
import tempfile
import unittest

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rig_cli.manifest import Sensor
from rig_cli import resolve


class SessionResolution(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "planner.yaml"
        self.config.write_text(yaml.safe_dump({"name": "planner", "service": "planner",
                                              "gain": 1, "unused": True}))
        self.row = Sensor(name="planner", service="planner", config=self.config, enabled=True, order=0,
                          session_overrides={"gain": 2, "unused": None})

    def test_resolved_dict_agrees_with_root_and_without_root(self):
        expected = {"name": "planner", "service": "planner", "gain": 2}
        self.assertEqual(resolve.resolved_dict(self.row), expected)
        self.assertEqual(resolve.resolved_dict(self.row, self.root), expected)
        self.assertEqual(yaml.safe_load(self.config.read_text())["gain"], 1)

    def test_session_only_patch_materializes_a_frozen_config(self):
        rendered = resolve.materialize(self.row, self.root, out_subdir="session")
        self.assertNotEqual(rendered, self.config)
        self.assertEqual(yaml.safe_load(rendered.read_text()),
                         {"name": "planner", "service": "planner", "gain": 2})
        rendered_again = resolve.materialize(dataclasses.replace(self.row, config=rendered),
                                             self.root, out_subdir="session")
        self.assertEqual(rendered_again, rendered)
        unchanged = dataclasses.replace(self.row, session_overrides={})
        self.assertEqual(resolve.materialize(unchanged, self.root), self.config)

    def test_provider_layer_remains_above_session_layer(self):
        rendered = resolve.materialize(self.row, self.root, extra_overrides={"gain": 3})
        self.assertEqual(yaml.safe_load(rendered.read_text())["gain"], 3)


if __name__ == "__main__":
    unittest.main()
