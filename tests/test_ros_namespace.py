"""Descriptor ROS ownership namespaces are separate from Rig instance identity."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rig_cli import RigError
from rig_cli.descriptor import load_descriptor, resolve_ros_namespace


class RosNamespace(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)

    def descriptor(self, **fields):
        (self.repo / "rigging.yaml").write_text(json.dumps({"service": "controller", **fields}))
        return load_descriptor("controller", self.repo)

    def test_undeclared_preserves_legacy_names_and_missing_descriptors(self):
        desc = self.descriptor()
        self.assertIsNone(desc.ros_namespace)
        for descriptor in (desc, None, SimpleNamespace(service="controller")):
            with self.subTest(descriptor=descriptor):
                self.assertEqual(resolve_ros_namespace(descriptor, "old-driver"), "/old-driver")

    def test_literal_namespace_is_independent_of_instance_name(self):
        desc = self.descriptor(ros_namespace="/robot/controller")
        self.assertEqual(desc.ros_namespace, "/robot/controller")
        self.assertEqual(resolve_ros_namespace(desc, "different_instance"), "/robot/controller")
        self.assertEqual(resolve_ros_namespace(desc, "old-driver"), "/robot/controller")

    def test_name_template_resolves_nested_namespaces(self):
        for template, expected in (("/{name}", "/front"),
                                   ("/robot/{name}", "/robot/front"),
                                   ("/robot/{name}_driver", "/robot/front_driver"),
                                   ("/{name}/{name}", "/front/front")):
            with self.subTest(template=template):
                desc = self.descriptor(ros_namespace=template)
                self.assertEqual(desc.ros_namespace, template)
                self.assertEqual(resolve_ros_namespace(desc, "front"), expected)

    def test_invalid_declarations_fail_at_load(self):
        for value in (None, "", "/", "relative", " /robot", "/robot ", "/trailing/",
                      "/double//slash", "/bad-name", "/1number", "/robot/2number",
                      "/wild/*", "/~private", "/robot.$", "/{other}", "/{{name}}",
                      "/{name!r}", "/{name:>10}", "/{name", "/name}", "/éclair",
                      0, True, [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(RigError, "ros_namespace"):
                self.descriptor(ros_namespace=value)

    def test_declared_template_validates_expanded_namespace(self):
        desc = self.descriptor(ros_namespace="/robot/{name}")
        for name in ("bad-name", "1number", "", "bad name", "{name}", "bad//name"):
            with self.subTest(name=name), self.assertRaisesRegex(RigError, "ros_namespace.*instance"):
                resolve_ros_namespace(desc, name)
        self.assertEqual(resolve_ros_namespace(desc, "_camera2"), "/robot/_camera2")

    def test_programmatic_declarations_get_the_same_validation(self):
        for value in ("/{unknown}", "/bad-name", "", 3):
            with self.subTest(value=value), self.assertRaisesRegex(RigError, "ros_namespace"):
                resolve_ros_namespace(SimpleNamespace(service="controller", ros_namespace=value), "front")


if __name__ == "__main__":
    unittest.main()
