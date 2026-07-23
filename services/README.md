# services/

Vendored **launch surfaces** live here for deployment: `rig vendor <svc> --from <repo>` copies the few
small launch files (launcher + compose + render helper + rigging.yaml) with a provenance stamp
(`.vendored.yaml` records source + git SHA) — and `rig bake` vendors automatically. The dirs are derived
mirrors: edit the source repo and re-vendor, never the copy. Runtime images are pulled from a registry.

For local development, `services.yaml` instead points at sibling checkouts (`../novatel`, …) and this
directory stays empty.

See `../docs/HOST_SETUP.md`.
