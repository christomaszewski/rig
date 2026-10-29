# Portable builds and replay

Command help includes target rules and examples: `rig image build -h`, `rig artifact bake -h`,
`rig reconstruct -h`, and `rig provision -h`. `--platform` means a runtime variant; `--arch`
means a Linux CPU architecture. Replay input preparation is documented by `rig replay prepare -h`.

`rig build` builds every declared runtime variant and supported CPU architecture for the deployment's
services, regardless of the build machine's provisioned platform. `platform: jp6m` and optional
`arch: arm64` choose the deployment target. They do not narrow a release build. Services with only the legacy build contract
must adopt the portable contract before joining a portable release; Rig reports which service needs
updating before running image builds.

```sh
rig build --dry-run
rig build --no-cache -j 2
rig build --scope deployment --tag vehicle-iteration
rig build --scope host --tag laptop-iteration
rig build --scope deployment,replay --tag release-1
rig build --platform jp6m --arch arm64 --tag bench-1
rig build --platform dev --arch amd64 --tag bench-2
rig build sbg novatel --tag release-1    # only these services
rig src sync && rig build --offline       # build without remote git or upstream registries
```

Use the same version in `images.tag` when running an iteration built with `--tag`. A targeted build
cannot replace an existing tag that contains other CPU architectures; use a new iteration tag. The
refusal names the architectures the registry tag serves and the ones the build would write. A
single-platform tag (a plain `docker push`) counts as its one architecture, so rebuilding that
architecture over it is allowed (v0.2.75).
Omitting `images.tag` retains platform tags such as `dev` and generic `latest` tags.

Builds run in stages: shared base images first, then the messages overlay, then independent
service/runtime builds. Rig runs up to **two builds concurrently** by default within each stage.
Use `rig build -j 4` for more concurrency or `rig build -j 1` for serial builds. Each build can itself
compile for multiple CPU architectures, so choose a job count that fits the builder's memory.

Normal output shows each build or mirror starting and finishing, elapsed time, and completed and
remaining counts. Active steps report their status every 15 seconds. A portable build is complete
only after Rig verifies its published images. Full output is saved under
`var/build/<session>/`, with one log per step; failures show the last 40 lines and the full log path.
Use `rig build --verbose` (or `-v`) to stream that output live, prefixed by step number so concurrent
builds remain distinguishable. `--dry-run` prints the detailed plan without creating build logs.

Runtime and CPU are separate choices:

| Camera runtime | Image set | Architectures |
| --- | --- | --- |
| `jp6` | core, ROS 2 bridge, ROS 1 bridge, WebRTC bridge | arm64 |
| `jp6m`, `jp7` | core, ROS 2 bridge, WebRTC bridge | arm64 |
| `dev` | software core, ROS 2 bridge, WebRTC bridge | arm64, amd64 |

Docker selects arm64 or amd64 from an index. It cannot distinguish JetPack versions that share
arm64. Runtime variants therefore keep separate tags. `dev` uses software codecs and requires no
NVIDIA runtime; its core and WebRTC bridge share the pinned modern GStreamer userspace. It is not
a macOS container: Docker Desktop runs the arm64 Linux member.

## Building some services, and building offline

`rig build SERVICE...` builds only the named services (service names, or instance names standing
for their service); every other service keeps the images already in the registry. A named consumer
builds FROM the base already published. Naming a base provider rebuilds the base and its messages
overlay — still from the whole fleet's `msgs:` union — but not the consumers. The run is recorded
as targeted in `rig.lock` `last_build`. When builds fail, Rig prints the command that retries only
the services that failed or never ran, repeating the options that change what is built.

Every git source a build needs — service source pins in `rig.lock`, the messages overlay's
`source:` repos and a rigging's `build.sources` — resolves through one bare mirror per repo in
`$RIG_HOME/cache/git` (default `~/.rig`). Online builds fetch into the mirror first; `rig src sync`
fills every mirror, submodules included, without building. `rig build --offline` fetches nothing,
turns `--pull` off (`RIG_OFFLINE=1`, `RIG_BUILD_PULL=0`) and requires `mirror:` images to already be
in the build registry. It reports everything missing before any build starts;
`rig build --offline --check` stops after that report. Image layers come from the build cache: a
step not cached by an earlier online build (a new apt package, say) needs the network once.
`--offline` cannot be combined with `--no-cache`. See the
[offline builds plan](offline-builds-plan.md) for what is verified and what remains.

## Targets and scopes

No target files are required. Declare stable deployment defaults in `vehicle.yaml`:

```yaml
platform: jp6m
arch: arm64  # optional when the selected services support exactly one common architecture
```

`rig bake --platform dev --arch amd64` overrides those defaults for one artifact. Rig infers a
missing runtime or CPU only when the selected service matrices have one common choice. An ambiguous
bundled target fails before export; the builder's CPU never chooses the vehicle CPU. The resolved
Linux target and its selection source are recorded in `metadata.yaml`. Older non-bundled deployments
without architecture declarations can still bake without a CPU constraint.

| Build scope | Runtime and CPU selection |
| --- | --- |
| `all` (default) | Every declared runtime and supported CPU, including disabled deployment rows |
| `deployment` | Enabled services for the deployment target, plus shared build providers |
| `host` | Enabled services for the development machine's runtime and Docker engine CPU, plus providers |
| `replay` | Services selected by replay startup policy, the disabled bag player, and shared providers; each service's replay runtime and all its supported CPUs |

Combine scopes with commas or repeated `--scope`; Rig unions their image requirements and deduplicates
shared builds. `--platform` and `--arch` still narrow the default full matrix. With explicit narrower
scopes, they override only the `deployment` scope; host and replay resolve independently. `--dry-run`
prints selected targets and build outputs. Using either override with narrower scopes requires
including `deployment`; `all` cannot be combined with narrower scopes. The `replay` scope is a
conservative build superset and does not inspect a particular run or recipe. Full-matrix builds
remain the release default.

Provision a developer machine once with `sudo rig provision --platform dev`. An optional
`--arch amd64` asserts the expected Docker engine architecture; it does not enable cross compilation.
Without a provisioned runtime, `host` uses an agreed service replay default, then an unambiguous
service matrix. It never reads the deployment directory's `vehicle.local.yaml` to identify the host.

Normal build target selection and bake load deployment declarations. Bake excludes both local
override files, shell configuration variables, generated fleet state, and reconstructed replay
execution state. Launcher capture receives explicit deployment `env:` plus tool/transport settings
such as PATH and Docker connection settings. A workspace `vehicle.local.yaml` may provide bench
identity, paths and execution overrides for `rig up`; those values cannot choose a baked target or
enter baked configs. Native run capture intentionally records the effective configuration that ran.

`rig build` selects its registry in this order: `--registry`, the deployment directory's
`vehicle.local.yaml`, the build machine's `/etc/rig/vehicle.local.yaml`, then `vehicle.yaml`.
Set `images.registry` to a literal address such as `localhost:5000`; absent, null, or blank values
fall through to the next source. Rig prints the selected registry and its source. Local image tags,
base images, variables, and environment settings do not override deployment build inputs. Runtime
and CPU selection still follow the requested scopes, including provisioned host settings for `host`.

If the registry is reachable as `localhost:5000` on the build machine and by a LAN address on vehicles,
keep the LAN address in `vehicle.yaml` and put this in the deployment's `vehicle.local.yaml`:

```yaml
images:
  registry: localhost:5000
```

Alternatively, set the machine-wide default with `sudo rig provision --registry localhost:5000`.
Then `rig build` uses that local endpoint, while `rig bake` uses `vehicle.yaml` (or an explicit
`rig bake --registry`). Bake excludes local files and rewrites the artifact's image catalog and
build-output references to the deployment registry, retaining the verified digests and replay
companions. Templated artifacts carry logical image names until the registry resolves on the vehicle.
The source workspace's build catalog keeps the actual build destination. The registry
must serve the same repositories and digests through both addresses; the override does not copy
images between separate registries. These local registry settings also apply to execution commands
such as `rig up` and `rig pull`.

Replay companions come from each service's `replay.platform` (falling back to the deployment runtime)
and its supported architectures. `replay.start` and deployment `replay.services`/`replay.instances`
select the default replay services. Use `all` to include every optional live/replay service in a release.

Replay acquires the **active runtime images**, independently of the build-output inventory.
`rig replay RUN` defaults to `--pull missing`, including for a replay-only disabled player;
`--pull always` refreshes selected references and `--pull never` requires a local cache.
Check, prepare, and dry-run only report proposed pulls. Catalog-backed replay pulls recorded
native child digests from the selected registry, then pins launch to inspected image IDs.
A custom player image must be covered by the catalog when one exists. Bundled replay remains
offline and requires its recorded image identities. See [runtime images](REPLAY.md#runtime-images)
for service launcher compatibility and offline preparation details.

## Builder and publication

Use a Buildx builder that can **push** multiarch indexes to the deployment registry. Check
`docker buildx inspect --bootstrap`; it must advertise every requested `linux/arm64` and/or
`linux/amd64` platform. Native builder nodes are preferable for expensive GStreamer/ROS compilation;
emulation must also support the instructions executed during builds. Rig checks builder support
before starting. Registry authentication uses your Docker CLI configuration (`docker login`).
Rig reports the builder it checked. If Buildx returns an incomplete inspection after startup,
Rig retries that same builder once; persistent inspection errors are reported separately from
missing architectures. A missing-architecture error includes the actual advertised platforms.

Rig inherits insecure-registry settings from the **active Docker daemon's** `docker info`, including
explicit host:port entries and CIDR ranges. Keep `images.registry` as `host:port[/namespace]`, without
a URL scheme. No deployment HTTP setting or hand-written BuildKit configuration is required. Rig
prefers HTTPS for approved insecure registries that support it (including self-signed TLS), and
selects HTTP for approved registries that only speak HTTP. Registries outside that daemon policy
retain normal TLS verification; authentication failures do not trigger an HTTP fallback.

Buildx's registry client does not inherit dockerd's policy itself. Rig supplies it through a reusable
`rig-registry-<hash>` builder configuration for manifest inspection and mirroring. The configuration
is scoped to the Docker daemon and registry policy; Rig never changes the globally selected builder.
With the built-in `docker` driver and Docker's **containerd image store**, image builds continue
using the existing builder and cache; the registry helper does not start a BuildKit container.
Docker's classic image store cannot publish multiarch images with attestations. Rig detects that
store and automatically uses a managed `docker-container` builder, even for secure registries.
This retains provenance without requiring a daemon configuration change. For a single
`docker-container` node on the current Docker context, Rig also uses the managed builder when it
needs inherited registry policy. Managed builders retain their own cache between runs; the first
build cannot reuse the built-in Docker driver's cache. Their host network lets them reach
registries published on the Docker host's loopback interface.

**DNS in managed builders (v0.2.75).** BuildKit cannot hand a loopback DNS stub (systemd-resolved's
`127.0.0.53` in `/etc/resolv.conf`) to RUN steps and falls back to `8.8.8.8`. On networks that block
public DNS, every fetch then stalls and fails, while `docker build` works. When the Docker daemon is
this host's and its resolv.conf names only a loopback stub, Rig gives the managed builder the
upstream servers and search domains from `/run/systemd/resolve/resolv.conf`. Set
`RIG_BUILDKITD_CONFIG` to a `buildkitd.toml` snippet to add your own settings; a `[dns]` table in it
replaces the automatic one. These settings do not change the builder's name, so its cache survives
a network change: when they differ from what the builder was created with, Rig re-creates it under
the same name with `docker buildx rm --keep-state`. A build step that fails on name resolution
ends with a hint saying so.

Explicit `BUILDX_BUILDER` / `BUILDKIT_HOST` selections and remote/cloud/multinode builders keep their
build settings and topology; those build servers must already have the required registry access.
An explicitly selected `docker` builder also needs the containerd image store for attestations;
unset `BUILDX_BUILDER` to let Rig select a compatible builder automatically.
Rig still inherits the Docker daemon's policy for its own registry inspections. Bake and replay
manifest lookups also retry HTTP/TLS failures using that policy. Existing Docker credentials and
context are preserved. Old managed builders can be listed with `docker buildx ls` and removed by
name with `docker buildx rm` when their caches are no longer needed.

Rig publishes the shared base first, then its messages overlay, then service images. Consumers receive
the freshly published base **digest**. Shared build contexts are deduplicated. Every published output
must report its digest and expose every requested architecture in its registry index. Only verified
results are saved in `rig.lock.build_catalog`; `last_build` records whether the request covered the
whole matrix or was targeted. A failure can leave partial registry uploads but never records them as
a successful release. Use unique release tags to avoid mutating an existing release during failure.

**Adopting an image rig did not build (v0.2.76).** Replay refuses an image without a catalog row,
for example a tag built and pushed by hand. `rig image adopt <registry>/<image>:<tag>` records it
from the registry's own manifest: its digest, architectures and per-architecture children. The tag
must be in the deployment's build registry. The row replaces any rows for the same image and tag
and is marked `adopted` with no source revision; it never copies another row's provenance.
`--like <catalog key>` takes only the service and runtime variant from that row; without it, rig
uses the image's existing rows when they agree. `--dry-run` shows the row without writing
`rig.lock`. `rig image audit` warns about adopted rows, and `rig bake` refuses a catalog that has
one unless `--allow-local`. A later `rig build` of that tag replaces the row with a built one.

Bake and native run capture carry the complete catalog, including companion images that the Jetson
does not run. `rig bake --bundle-images` normally exports only images used by enabled deployment stacks.
Add `--include-replay` to also query and bundle onboard replay requirements, including the disabled
player and replay-mode recording sources, for the selected deployment runtime and CPU.
See [onboard replay](ONBOARD_REPLAY.md) for setup and vehicle commands.
Catalog-backed images are fetched by their recorded architecture child
digest. Other images are inspected or pulled for that CPU. Architecture-filtered export requires
Docker API 1.48 or newer (1.49 for images stored as manifest indexes); it also works when the
vehicle CPU differs from the builder's CPU. Index members are inspected for the selected CPU,
so the saved identity remains valid when export omits other architectures or attestations.

Bundled Compose files use content-specific aliases because Docker save/load does not reliably restore
registry digest references. Startup checks the target and exact image IDs, reloads missing or different
images, verifies every ID, and disables pulls. A missing archive, wrong CPU or unresolved image selection
fails before containers start. Other companion images remain catalog metadata; replay on another runtime/CPU
needs those bytes from the release registry or a mirror.

Bake verifies the exported Docker and OCI manifests against the selected aliases and CPU, and records
both config digests and exported OCI digests. Startup accepts only those verified identities, so the
same artifact loads on classic and containerd Docker image stores even though they report different IDs.

Bake refuses an image labelled `io.rig.build.local` — such as a messages overlay built from
rig-infra's `FLEET_MSGS_LOCAL` working-tree snapshots — unless given `--allow-local`;
`rig image audit` warns about such images and names the checkouts.

Templated fleet artifacts preserve identity variables until execution. To bundle one, every enabled
service must declare `verbs: {images: images}` in `rigging.yaml`. Rig invokes the launcher as
`<launcher> <layered-config> images` with the selected runtime, CPU and image inputs. It must emit only
a YAML list of concrete image references to stdout, perform no deployment, and work with unresolved
identity fields. If an unresolved field affects image choice, it must fail rather than guess. The
planner lists runtime images, not all build outputs. On the vehicle, Rig renders real Compose with
the provisioned identity and refuses any image outside that planned bundle. Services without this
contract can still use non-bundled fleet artifacts. Identity-resolved artifacts use Compose capture
directly and need no additional verb.

## Service build contract

```yaml
build:
  command: tools/build-images.sh
  images: [core, software-core, bridge]
  platforms: [jetson, dev]
  architectures: [arm64, amd64]
  capabilities: {no_cache: true, platforms: true}
  sources: [{name: sbgecom, repo: https://github.com/SBG-Systems/sbgECom.git, ref: 5.8.935-stable}]
  variants:
    jetson: {architectures: [arm64], images: [core, bridge]}
    dev: {images: [software-core, bridge]}
images:
  override_env: {core: CORE_IMAGE, software-core: DEV_IMAGE, bridge: BRIDGE_IMAGE}
replay:
  platform: dev
```

Generic services omit `platforms` and `variants`: the same image runs across runtime choices.
NovAtel, SBG, Ouster, and MAVLink Router declare `arm64` and `amd64` with no runtime-specific tags.
Camera keeps its explicit JetPack/dev variants; the shared infrastructure is also runtime-independent.
An architecture selection from Rig takes precedence over a service's standalone CPU override.

The command receives `<registry> <tag>` positionally,
`RIG_TARGET_PLATFORM` for a runtime variant, `RIG_BUILD_PLATFORMS` as a comma-separated Docker platform
list, and `RIG_BUILD_RESULT` as a JSONL output path. Each line must contain `ref` and the published
`digest`. Copy [packaging/rig-build.sh](../packaging/rig-build.sh) into the build context, source it,
and call `rig_build IMAGE [docker build arguments...]` for each declared output. The helper uses
Buildx with provenance and registry publication under Rig, while retaining standalone Docker builds.
With standalone `PUSH=0`, a Buildx invocation may load one architecture locally; multiple architectures
require publication. A portable `rig build` always publishes.

`build.sources` lists git repos the build would otherwise fetch itself (a CMake FetchContent, a
`git clone` in a Dockerfile). Rig exports each one from its local mirror and passes
`RIG_BUILD_SOURCES`: one directory per source plus `sources.tsv` (name, repo, ref, commit). Pass
`--build-context <name>=$RIG_BUILD_SOURCES/<name>` and bind that stage in the Dockerfile, keeping an
empty default stage (`FROM scratch AS <name>`) so standalone builds still fetch. Names match
`[a-z0-9][a-z0-9_-]*`. The messages overlay receives its `source:` repos the same way.

`images.override_env` declares the service-owned image variables honored by its compose/launcher.
Rig uses these to inject catalog digest references for build/pull agreement. Infra that runs
`RIG_BASE_IMAGE` or `RIG_MSGS_IMAGE` directly already uses the shared digest channel.
Certification renders every runtime with declared image overrides and checks that those exact
digest references reach Compose.

`--no-cache` disables Docker instruction-result reuse, not source pins, package-download caches,
or existing containers. These builders also request parent refresh with `--pull`; the messages
overlay deliberately omits it and consumes the just-built base digest. Nothing prunes Docker caches.

`rig certify --repo SOURCE --require-build-check` executes the build entrypoint in temporary staging,
with Docker command capture and network/daemon access denied. It checks every expected image,
runtime, architecture, and combined architecture set, with caching both enabled and disabled.
Portable builds must use Buildx publication and report exactly one ref/digest record per output,
matching the builder metadata. No-op builds, partial flag forwarding, ignored platform lists,
missing or incorrect result records, local-only exports, and unconditional no-cache fail.
Linux requires usable Bubblewrap and libseccomp2 (socket syscalls are denied); macOS uses sandbox-exec. CI must use the strict flag. Missing
build context or unavailable containment reports **NOT CHECKED**, which fails strict certification.
Normal `doctor` only reports declared capability; `doctor --deep` runs the behavioral check when
the full local source context exists. Neither silently substitutes a different source checkout.

Ubuntu 24.04 can also require an AppArmor profile granting `/usr/bin/bwrap` the `userns`
permission. A `bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted` error means
the host denied sandbox setup before the build probe ran. Rig's Ubuntu CI uses
[setup-build-probe](../.github/actions/setup-build-probe/action.yml) to install the dependencies
and a profile for that executable, keeping the host-wide namespace restriction enabled.
Service CI can use the same local action after checking out Rig. See Ubuntu's
[namespace restriction guidance](https://documentation.ubuntu.com/release-notes/24.04/)
for administrator-managed workstations; certification never changes host security policy.

## Reconstruction and selection

```sh
rig reconstruct /path/to/run --platform dev --registry registry.lan:5000
# Or omit --platform: destination machine provision, then service replay mapping, then recorded runtime.
rig reconstruct /path/to/run --platform recorded
```

The reconstructed workspace records its execution platform and CPU in local overrides and the lockfile.
Legacy captures keep CPU selection in the lockfile so their embedded Rig does not receive an unsupported
local configuration key. The original
source runtime and image identities remain provenance in the capture and lockfile. Replay checks
companion digests against the Docker engine architecture before starting infrastructure, then records
the effective images and selection in the new run. A registry override must contain the same bytes.
Conflicting per-image environment overrides are refused for catalog-backed replay.
When reconstructing a retrofitted deployment bundle, frozen startup scripts, image payload and target
metadata move to `var/source-deployment/` for provenance. The replay workspace uses its selected host
target and companion catalog; the archived source artifact is unchanged.

Replay startup is independent of bag-data exclusion. In a deployment or `vehicle.local.yaml`:

```yaml
replay:
  services:
    doodle: {start: false}
  instances:
    bench_radio: {start: true}
```

Descriptor `replay.start` controls automatic eligibility. Deployment service or instance
`start: true` positively selects instances, including disabled consumers. Rows may carry
`replay: {start: true|false}` as an alias of top-level instance policy. Local files merge over
deployment entries. Explicit instance names, `--live NAME`,
and `--include-service SERVICE` override startup defaults. The player cannot be disabled this way.
Startup omission preserves the service's recorded bag topics. Use `--skip-service SERVICE` or
`--skip-instance NAME` to omit startup, source recordings, and bag topics whose recorded publishers
belong exclusively to the skipped instances. Conflicting include/skip requests fail. Broad replay
never infers topic ownership from a topic namespace alone; shared or unknown publishers remain
eligible, and exact `playback.exclude` entries remove known outputs when graph evidence is absent.
Automatic suppression and skips preserve eligibility for `/tf`, `/tf_static`, and `/diagnostics`;
explicit topic lists, recorded subscriber selection, or `playback.exclude` can still omit them.

Recorded ROS requests now default to automatic selection, independently of broad/recorded topic
mode. Rig selects unambiguous historical servers mapped to the entire effective started set and
excludes targets with a known selected live client. Omitted, unmapped, or ambiguous servers are
excluded. In a recipe, `playback.services: []` disables recorded calls; an explicit nonempty list
manually selects targets, with ownership/duplicate-call warnings when appropriate. A call script
replaces recorded calls before request-event validation. Existing recipes that omit `services`
adopt the automatic policy; preparation preserves a user-written empty list.

An event topic alone does not prove that request contents were recorded. Prepare and execute
inspect the selected window through a bounded read-only helper in the verified native player
image. Check and dry-run only reuse valid cached evidence, otherwise reporting `not_checked`.
The resolved player `play.services_source` chooses server-origin requests by default (`service`)
or client-origin requests (`client`), and the same value is frozen for inspection and playback.
Metadata-only, response-only, wrong-origin, unsupported, missing, or out-of-window events do not
qualify. Repeated request events in the chosen origin remain repeated calls, as in native rosbag2;
the report exposes duplicates rather than silently deduplicating. See the JSON selection and
inspection evidence before relying on automatic calls.

Hand-built deployments can use `rig replay check RUN` and
`rig replay prepare RUN --recipe NAME`. See [Replay preparation](REPLAY.md) for local recipes,
broad playback defaults, exact-session timing, and source-preserving legacy adapters.
Recipe packaging with suites remains deferred.

Camera-service defines a default `h264` profile for `rig run export RUN --service camera-service`.
Its dedicated offline helper uses the service's native CPU image; install the updated launch
surface and explicitly build/pull that image first. Outputs live under
`RUN/exports/services/camera-service/h264/<export-id>/`, with only transformed recordings and
their manifest. Existing originals and bags are preserved; no vehicle export profile is needed.
Replay/check/prepare use these generations only with `--use-proxies` on that invocation. Exact
camera paths and session lists bind directly, including renamed instances, while the bag retains
its original or independently repaired path. A compatible current reader can reuse another
machine's generation without the producer's deployment. See [Offline camera proxies](REPLAY.md#offline-camera-proxies)
for lossiness, regeneration, pinning, and transfer semantics. H.264 still decodes to normal camera
frames; this does not add hardware decoding or encoded WebRTC passthrough.

Doodle's descriptor sets `replay.start: false`. When explicitly started, its launcher waits up to
`DOODLE_CONNECT_TIMEOUT` (10 seconds by default) for reachability, then up to `DOODLE_SETTLE_TIMEOUT`
(120 seconds) for power reconciliation. Each status command is independently bounded. Failure returns
nonzero and leaves the service retrying in the background. Authentication/configuration errors are
reported immediately when observed. These are host launcher environment settings, not config keys.

## Existing deployments and validation

Update the service descriptors/launch surfaces **and** rebuild their images. Vendored services build
from the source revision pinned in `rig.lock`, so updating a neighboring checkout or passing
`--no-cache` does not update that pin. Use `rig swap SERVICE PATH|REF` as appropriate, rebuild, and
bake a new artifact. Old artifacts bundle their old Rig and launchers; a newer laptop executable
does not rewrite them automatically.

Old captures have no companion catalog. Reconstruction/replay warn and retain tag-based operation;
rebuild the matching source into a new workspace to establish companion provenance. Do not present
newly built companions as the bytes used by a historical run. For old Doodle surfaces, add the local
startup policy above. For old camera surfaces, update the launcher: `dev` now uses `/app` from the
image. Source mounting is explicit (`cam-up --dev --source CONFIG ...`) and requires a full checkout.

Local tests cover orchestration, command boundaries, compose packaging, catalog selection and
readiness deadlines. The [v0.2.72 readiness report](releases/coordinated-replay-readiness-2026-10-27.md)
records native AMD64/ARM64, baked/offline, real-recording and companion-publication evidence.
[Coordinated startup](replay-coordination.md) supplies opt-in producer holding, consumer readiness
and common release; legacy startup remains the default, and its `--start-delay` is only a timer.
New deployments still need acceptance with their own recordings and service versions. Isaac
attachment has a separate [offline acceptance checklist](ISAAC_OFFLINE.md); replay acceptance
does not certify a live simulator integration.
