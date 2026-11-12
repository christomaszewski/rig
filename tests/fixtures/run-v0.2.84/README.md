# A run written by rig v0.2.84

`data/runs/20261112T170944Z_auto/` is an open run exactly as the v0.2.84 release wrote it:
`runs.ensure` then `runs.snapshot`, called from the installed 0.2.84 package against `tree/`
with `RIG_VEHICLE_LOCAL` pointing at `machine.yaml`. The snapshot holds what that version
captured: `vehicle.yaml`, the generated `vars.yaml` (values only) and the rendered config. It
has no machine file and no value sources.

Two things differ from what was on disk:

- the `root:` line of the `ups:` entry was rewritten to `/home/uxv/ws/v1` (it held the path of
  the machine that generated the fixture; nothing reads it back);
- `data/current` and `tree/var/` are not stored. A test that needs an open run recreates the
  `current` symlink and writes `tree/var/deployment-id` from the manifest's `deployment:` value.

Tests copy this directory before using it. Do not regenerate it with a newer rig: its purpose
is to hold the older capture set still.
