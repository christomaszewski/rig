# services/

Service repos live here as **git submodules** for deployment (so each launcher + compose is pinned on the
robot; runtime images are pulled from a registry). For local development, `services.yaml` instead points
at sibling checkouts (`../novatel`, …) and this directory stays empty.

```bash
git submodule add <url> services/novatel
git submodule add <url> services/sbg_driver
git submodule add <url> services/gige-vision-service
# then set services.yaml paths to services/<name>
```

See `../docs/HOST_SETUP.md`.
