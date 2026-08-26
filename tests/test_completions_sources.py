"""Shell completion engine — M2: dynamic value sources (raw reads, fail-soft).
Run: python3 tests/test_completions_sources.py"""
import contextlib
import json
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from rig_cli.completions import candidates  # noqa: E402


def _c(*words):
    """The LAST word passed is the one under the cursor (possibly "")."""
    words = list(words)
    return candidates(words, len(words) - 1)


@contextlib.contextmanager
def _env(**kv):
    old = {k: os.environ.get(k) for k in kv}
    for k, v in kv.items():
        os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


# --- fixtures: a deployment tree + a ~/.rig home + a fleet roster, all raw files -------------

TMP = pathlib.Path(tempfile.mkdtemp())

ROOT = TMP / "veh"
(ROOT / "var" / "artifacts").mkdir(parents=True)
(ROOT / "vehicle.yaml").write_text(
    "vehicle: t\n"
    "infra:\n"
    "  - { name: zenoh, service: zenoh-router, config: c.yaml }\n"
    "sensors:\n"
    "  - { name: cam0, service: cam, config: cam0.yaml,\n"
    "      overlays: [public/cam-tune@1.0.0, team/cam-night@0.2.0] }\n"
    "  - { name: gnss, service: gnss, config: g.yaml, enabled: false }\n"
    "autonomy:\n"
    "  - { name: planner, service: nav, config: p.yaml }\n")
(ROOT / "services.yaml").write_text(
    "services:\n  cam: { path: /nowhere/cam }\n  gnss: { path: /nowhere/gnss }\n")
(ROOT / "rig.lock").write_text(
    "packages:\n  public/siyi-zr30@1.0.0: { kind: service }\n")
(ROOT / "var" / "artifacts" / "demo-v1.tar.gz").write_bytes(b"")
(ROOT / "var" / "artifacts" / "demo-v2.tar.gz").write_bytes(b"")
R = str(ROOT)

HOME = TMP / "home"
TEAMREG = TMP / "teamreg"
(HOME / "cache" / "registries" / "public").mkdir(parents=True)
TEAMREG.mkdir()
(HOME / "registries.yaml").write_text(
    "registries:\n"
    "  - { name: public, type: git, url: https://example.invalid/reg.git }\n"
    f"  - {{ name: team, type: local-dir, path: {TEAMREG} }}\n")
(HOME / "cache" / "registries" / "public" / "index.json").write_text(json.dumps({
    "packages": {"zenoh-router": {"kind": "service", "version": "1.2.0"},
                 "cam-service:indoor": {"kind": "profile", "version": "1.0.0"},
                 "cam-tune": {"kind": "overlay", "version": "1.0.0"}},
    "sensors": {"2b:0033": [["cam-service:indoor", "sensor"]]}}))
(TEAMREG / "index.json").write_text(json.dumps({
    "packages": {"cam-night": {"kind": "overlay", "version": "0.2.0"},
                 "nav-suite": {"kind": "suite", "version": "2.0.0"}}}))
H = str(HOME)

FLEET_DIR = TMP / "gcs"
FLEET_DIR.mkdir()
(FLEET_DIR / "fleet.yaml").write_text(
    "fleet: demo\nvehicles:\n"
    "  - { id: 1, name: alpha, host: a, path: /d }\n"
    "  - { id: 2, name: bravo, host: b, path: /d }\n")

EMPTY = TMP / "empty"
EMPTY.mkdir()

BROKEN = TMP / "broken"
BROKEN.mkdir()
(BROKEN / "vehicle.yaml").write_text("::: [ not yaml\n")


# --- instance names ---------------------------------------------------------

def test_instance_names_all_tiers():
    assert _c("--root", R, "down", "") == ["cam0", "gnss", "planner", "zenoh"]
    assert _c("--root", R, "logs", "-f", "ca") == ["cam0"]


def test_instances_through_grouped_spelling():
    assert _c("--root", R, "image", "audit", "") == ["cam0", "gnss", "planner", "zenoh"]
    assert "cam0" in _c("--root", R, "config", "")  # bare config: verbs + legacy names


def test_cleanup_covers_disabled_instances():
    assert "gnss" in _c("--root", R, "cleanup", "")


def test_pkg_instance_verbs():
    assert _c("--root", R, "pkg", "upgrade", "pl") == ["planner"]
    assert _c("--root", R, "pkg", "save", "cam") == ["cam", "cam0"]  # service + instance


# --- pkg refs from the registry caches --------------------------------------

def test_pkg_add_refs_qualified_and_bare():
    with _env(RIG_HOME=H):
        menu = _c("--root", R, "pkg", "add", "")
        for ref in ("public/zenoh-router", "zenoh-router", "team/cam-night",
                    "public/cam-service:indoor", "sensor:2b:0033", "cam", "gnss"):
            assert ref in menu, f"'{ref}' missing from pkg add menu"
        assert _c("--root", R, "pkg", "add", "pub") == [
            "public/cam-service:indoor", "public/cam-tune", "public/zenoh-router"]


def test_top_level_add_same_grammar():
    with _env(RIG_HOME=H):
        assert "sensor:2b:0033" in _c("--root", R, "add", "sensor")


def test_overlay_apply_filters_kind():
    with _env(RIG_HOME=H):
        menu = _c("--root", R, "overlay", "apply", "cam0", "")
        assert set(menu) == {"public/cam-tune", "cam-tune", "team/cam-night", "cam-night"}


def test_rebase_completes_profiles_only():
    with _env(RIG_HOME=H):
        menu = _c("pkg", "rebase", "")
        assert "cam-service:indoor" in menu and "zenoh-router" not in menu


def test_overlay_remove_completes_bound_refs_only():
    with _env(RIG_HOME=H):
        expected = ["public/cam-tune@1.0.0", "team/cam-night@0.2.0"]
        assert _c("--root", R, "overlay", "remove", "cam0", "") == expected
        assert _c("--root", R, "overlay", "reorder", "cam0", "team") == ["team/cam-night@0.2.0"]
        assert _c("--root", R, "overlay", "remove", "planner", "") == []  # nothing bound


def test_pkg_remove_instances_plus_lock_names():
    menu = _c("--root", R, "pkg", "remove", "")
    assert "cam0" in menu and "siyi-zr30" in menu


# --- registry names ---------------------------------------------------------

def test_registry_name_flags_and_positionals():
    with _env(RIG_HOME=H):
        assert _c("pkg", "promote", "--to", "") == ["public", "team"]
        assert _c("pkg", "yank", "x", "--from", "te") == ["team"]
        assert _c("pkg", "search", "--registry", "") == ["public", "team"]
        assert _c("registry", "sync", "") == ["public", "team"]
        assert _c("registry", "remove", "pu") == ["public"]


def test_build_registry_flag_is_not_a_rig_registry():
    with _env(RIG_HOME=H):
        assert _c("--root", R, "build", "--registry", "") == []  # docker registry host


# --- artifacts + fleet ------------------------------------------------------

def test_unbake_artifacts():
    menu = _c("--root", R, "unbake", "")
    assert len(menu) == 2 and all(m.endswith(".tar.gz") for m in menu)
    assert any("demo-v1" in m for m in menu)


def test_fleet_vehicles_via_env_and_flag():
    with _env(RIG_FLEET=str(FLEET_DIR / "fleet.yaml")):
        assert _c("fleet", "up", "") == ["alpha", "bravo"]
        assert _c("fleet", "status", "br") == ["bravo"]
    with _env(RIG_FLEET=None):
        assert _c("fleet", "up", "--fleet", str(FLEET_DIR / "fleet.yaml"), "al") == ["alpha"]


# --- fail-soft --------------------------------------------------------------

def test_broken_vehicle_yaml_completes_nothing():
    assert _c("--root", str(BROKEN), "down", "") == []


def test_outside_a_deployment_completes_nothing():
    assert _c("--root", str(EMPTY), "down", "") == []
    assert _c("--root", str(EMPTY), "unbake", "") == []


def test_absent_rig_home_degrades_to_local_sources():
    with _env(RIG_HOME=str(EMPTY)):
        menu = _c("--root", R, "pkg", "add", "")
        assert "cam" in menu and not any("/" in m for m in menu)
        assert _c("pkg", "promote", "--to", "") == []


def test_no_fleet_anywhere():
    with _env(RIG_FLEET=None):
        assert _c("--root", str(EMPTY), "fleet", "up", "") == []


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
