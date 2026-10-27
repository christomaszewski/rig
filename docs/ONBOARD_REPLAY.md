# Replay recorded runs on the vehicle

An extracted Rig artifact is already a replay deployment. It ships Rig, the service launchers,
and all configured instances, including disabled ones. Replay can read a run directly from the
vehicle's `data_dir`; no reconstruction, source checkout, or copy of the recordings is needed.
The vehicle needs Python 3 + PyYAML as well as Docker Compose for replay. The bare-Docker
`up.sh` path alone does not provide the replay CLI.

## Prepare the deployment before shipping

`--include-replay` automatically adds and configures a missing `ros2-bag-player` in the artifact.
It creates a disabled autonomy instance at order 999, selects the latest bag session, and sets
`play.loop: false`. With exactly one enabled ROS 2 bag logger, it also sets `source.logger` to
that logger's instance name. Otherwise logger selection is deferred to the recorded run or a
replay recipe. Name/config collisions get an unused name such as `bag_player_2`.

The source deployment files stay unchanged, and existing player rows/configs are preserved.
The build machine locates the player from its existing `services.yaml` route, an exact package
pin in `rig.lock`, a nearby checkout (including a sibling of another routed infra service), or
its configured package registries, in that order. Registry sources are fetched at their pinned
commit and recorded in the artifact's lock. No checkout is needed onboard.

If discovery is ambiguous or the player lives elsewhere, select its source in the same command:

```sh
rig bake --tag field-v1 --include-replay --replay-player /path/to/rig-infra
# Or use a pinned package reference from a configured registry:
rig bake --tag field-v1 --include-replay --replay-player public/ros2-bag-player@1.18.0
```

Services need the current
`runtime-images`/image-pinning contract; the player also needs replay plan v1, recorded-call
inspection, and advertised image capability labels. Existing recipes and service replay
configuration continue to work. Give consumers that should start automatically
`replay: {start: true}` on their instance rows. This also selects disabled consumers for replay.
Infrastructure follows its replay startup policy; recording sources such as cameras use their
recordings when available. Bag-fed hardware drivers do not need to start for their topics to play.

With the release images available in the configured registry:

```sh
rig bake --tag field-v1 --include-replay
```

Registry access is the default: replay pulls missing native images when invoked. The new
`--include-replay` option checks the resulting player and queries the vendored launchers with
replay config overrides at bake time. It records the selected instances and image requirements
in `metadata.yaml.replay`, also visible through `rig artifact show field-v1`. It requires a known
deployment CPU (`arch` or `--arch`). Normal `up` keeps the player disabled.

The image plan covers enabled infrastructure allowed by policy, possible recording sources,
explicit `replay.start: true` instances, and the player. It uses the **vehicle's runtime and CPU**;
a service's portable `replay.platform` default does not switch an onboard deployment to another
runtime. Bake does not start containers or inspect a historical run. Per-run readiness is still
checked on the vehicle. Additional instances selected later by CLI can require additional images.

## Replay onboard

From the extracted artifact directory:

```sh
./rig down --end-run          # stop live recording and close its run
./rig runs                    # find the recorded run ID or label
./rig replay RUN --auto-end   # replay, then stop and close the new replay run
```

`./rig replay` is the direct entry point; `./run.sh replay` forwards to the same command.

Startup remains `legacy` by default, including when `--include-replay` added the player.
With coordinated-capable player/camera packages and rebuilt images, opt in per replay:

```sh
./rig replay check RUN --startup coordinated
./rig replay RUN --startup coordinated --auto-end 0
```

Only participating replay producers need the new coordination protocol: the bag player and
selected recorded cameras. Ordinary consumers use their recorded ROS endpoint evidence and
do not need a new service API. Existing services can keep running with legacy startup. Bake's
basic replay capability check does not promise coordinated support; the explicit check above
verifies the selected packages, images and run evidence. See [coordinated replay](replay-coordination.md).
Supply `RUN`, or use `--recipe NAME` with a saved source binding.

`RUN` can also be an absolute run directory. Replay uses source recordings read-only and, when
`data_dir` is configured, creates a new provenance-linked run for the outputs. It refuses to
start over a running deployment. `--auto-end` waits for the player to finish and then tears down
the replay; it does not restart live recording. Use `./run.sh up` when ready to resume live work.

To inspect selection first, use `./rig replay check RUN`. Check never pulls images, so a
cold image cache may report missing images that normal replay would fetch. To omit cameras,
add `--skip-service camera-service`. To limit the bag window, use `--from 10 --to 40`.
If a run needs metadata repair or custom bindings, use
`./rig replay prepare RUN --recipe field-test`, then
`./rig replay --recipe field-test --auto-end`; see [replay preparation](REPLAY.md).

## Optional offline replay

```sh
rig bake --tag field-offline --include-replay --bundle-images
```

This exports the union of normal startup and onboard replay images for the deployment CPU.
Replay-mode camera images and the disabled player are included even when normal startup does
not use them. Required replay reader capability labels are checked before publishing the archive.
The same option works with templated fleet artifacts; identity remains unresolved until the
vehicle runs it. Their normal image queries need `verbs.images`; replay queries need
`verbs.runtime-images` that can plan images without resolving vehicle-specific identity fields.

Normal startup loads the bundled images. On a fresh vehicle that will replay before its first
`up`, or after its image cache has been cleared, load them explicitly:

```sh
./run.sh load
./rig replay RUN --pull never --auto-end
```

Bundled replay verifies the recorded image identities and never fetches missing images from
the registry. A plain `--bundle-images` artifact still bundles only normal startup images;
it may lack replay requirements. Re-bake with both flags for offline replay. The bundle does
not include other runtimes/CPUs, arbitrary later config changes, or camera proxy conversion
helpers. Compatible proxies can be prepared separately and selected with `--use-proxies`.

Existing extracted artifacts retain their bundled Rig and launcher versions. Ship a newly
baked artifact to gain these checks and optional replay image payloads.
