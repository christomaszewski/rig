#!/bin/sh
# Build the arch=all Debian package (plan M7). Pure-Python + python3-yaml (both in Ubuntu's repos),
# so no dh/pybuild machinery — a staged tree + dpkg-deb is the whole build. The package installs
# the PROGRAM only; user state (~/.rig, default registry, shell wiring) is `rig setup`'s job and
# purging it is `rig setup --purge` — the deb never touches $HOME (Debian policy, and the reason
# one design serves source checkouts, pipx, deb, and brew without rework).
#
# Output: build/deb/rig_<version>_all.deb   (offline vehicles: `sudo dpkg -i` from a release asset)
set -e
cd "$(dirname "$0")/.."
command -v dpkg-deb >/dev/null 2>&1 || { echo "build-deb: dpkg-deb not found (run on Debian/Ubuntu or in CI)" >&2; exit 1; }

VERSION=$(python3 -c "import rig_cli; print(rig_cli.__version__)")
STAGE="build/deb/rig_${VERSION}_all"
rm -rf "$STAGE"
mkdir -p "$STAGE/DEBIAN" "$STAGE/usr/bin" "$STAGE/usr/lib/python3/dist-packages"

cp -R rig_cli "$STAGE/usr/lib/python3/dist-packages/rig_cli"
find "$STAGE" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

cat > "$STAGE/usr/bin/rig" <<'LAUNCHER'
#!/usr/bin/python3
"""rig — installed entry point. First run: `rig setup` (creates ~/.rig + the default registry)."""
from rig_cli.cli import main

raise SystemExit(main())
LAUNCHER
chmod 755 "$STAGE/usr/bin/rig"

cat > "$STAGE/DEBIAN/control" <<CONTROL
Package: rig
Version: ${VERSION}
Section: utils
Priority: optional
Architecture: all
Depends: python3 (>= 3.9), python3-yaml
Maintainer: Chris Tomaszewski <christomaszewski@gmail.com>
Homepage: https://github.com/christomaszewski/rig
Description: vehicle-level sensor-stack orchestrator
 A loop + a manifest: rig reads vehicle.yaml and delegates bring-up/teardown
 of each sensor/infra/autonomy stack to that service's own launcher. Includes
 the package-registry layer (profiles, overlays, suites) and deployment
 artifacts (bake). Run 'rig setup' once per user to initialize ~/.rig.
CONTROL

dpkg-deb --build --root-owner-group "$STAGE" "build/deb/rig_${VERSION}_all.deb" >/dev/null
echo "built build/deb/rig_${VERSION}_all.deb"
