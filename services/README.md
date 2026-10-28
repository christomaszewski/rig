# services/

Vendored **launch surfaces** live here for deployment: `rig vendor <svc> --from <repo>` copies the few
small launch files (launcher + compose + render helper + rigging.yaml) with a provenance stamp
(`.vendored.yaml` records source + git SHA) — and `rig bake` vendors automatically. The dirs are derived
mirrors: edit the source repo and re-vendor, never the copy. Runtime images come from a registry
or a prepared offline image bundle/cache.

For local development, `services.yaml` instead points at sibling checkouts (`../novatel`, …) and this
directory stays empty.

See [host setup](../docs/HOST_SETUP.md), [runtime image requirements](../docs/REPLAY.md#runtime-images),
and [offline Isaac attachment](../docs/ISAAC_OFFLINE.md). Attaching a selected service requires
its launcher to inventory and pin its active images; a copied Compose file alone does not
establish that contract.
