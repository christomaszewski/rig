"""The checked-in example recipes under examples/ stay valid for what their READMEs tell an
operator to run: every one is accepted by the recipe validators for each provider section it
carries, and one on a Zenoh RMW selects a router, without which `rig replay` refuses the
session (replay_plan: "Replay needs a selected zenoh router"). Both examples once omitted it,
so the documented `rig replay check --recipe …` step failed.
Run: python3 tests/test_examples.py"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from rig_cli.session_recipe import provider_recipe, validate  # noqa: E402

EXAMPLES = pathlib.Path(__file__).resolve().parent.parent / "examples"


def _recipes():
    found = sorted(p for p in EXAMPLES.rglob("*.yaml")
                   if (yaml.safe_load(p.read_text()) or {}).get("version") == 2)
    assert found, f"no version-2 recipes under {EXAMPLES}"
    return found


def test_every_example_recipe_validates_for_each_provider_it_carries():
    for path in _recipes():
        doc = yaml.safe_load(path.read_text())
        validate(doc)
        for provider in ("simulation", "replay"):
            if provider in doc:
                provider_recipe(doc, provider)


def test_a_zenoh_example_selects_a_router_for_replay():
    for path in _recipes():
        doc = yaml.safe_load(path.read_text())
        rmw = ((doc.get("simulation") or {}).get("ros") or {}).get("rmw", "")
        if "zenoh" not in rmw or "replay" not in doc:
            continue
        instances = doc["session"]["instances"]
        assert any("zenoh" in name for name in instances), (
            f"{path.relative_to(EXAMPLES.parent)}: {rmw} session {instances} selects no router — "
            f"`rig replay` refuses it")


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
