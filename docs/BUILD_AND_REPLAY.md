# Portable builds and replay

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
```

Use the same version in `images.tag` when running an iteration built with `--tag`. A targeted build
cannot replace an existing tag that contains other CPU architectures; use a new iteration tag.
Omitting `images.tag` retains platform tags such as `dev` and generic `latest` tags.

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
prints selected targets and build outputs. Full-matrix builds remain the release default.

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

Replay companions come from each service's `replay.platform` (falling back to the deployment runtime)
and its supported architectures. `replay.start` and deployment `replay.services`/`replay.instances`
select the default replay services. Use `all` to include every optional live/replay service in a release.

## Builder and publication

Use a Buildx builder that can **push** multiarch indexes to the deployment registry. Check
`docker buildx inspect --bootstrap`; it must advertise every requested `linux/arm64` and/or
`linux/amd64` platform. Native builder nodes are preferable for expensive GStreamer/ROS compilation;
emulation must also support the instructions executed during builds. Rig checks builder support
before starting. Configure the builder's registry authentication and HTTP/TLS trust separately from
the host Docker daemon as needed.

Rig publishes the shared base first, then its messages overlay, then service images. Consumers receive
the freshly published base **digest**. Shared build contexts are deduplicated. Every published output
must report its digest and expose every requested architecture in its registry index. Only verified
results are saved in `rig.lock.build_catalog`; `last_build` records whether the request covered the
whole matrix or was targeted. A failure can leave partial registry uploads but never records them as
a successful release. Use unique release tags to avoid mutating an existing release during failure.

Bake and native run capture carry the complete catalog, including companion images that the Jetson
does not run. `rig bake --bundle-images` exports only images used by enabled deployment stacks, for
the selected runtime and CPU. Catalog-backed images are fetched by their recorded architecture child
digest. Other images are inspected or pulled for that CPU. Architecture-filtered export requires
Docker API 1.48 or newer; it also works when the vehicle CPU differs from the builder's CPU.

Bundled Compose files use content-specific aliases because Docker save/load does not reliably restore
registry digest references. Startup checks the target and exact image IDs, reloads missing or different
images, verifies every ID, and disables pulls. A missing archive, wrong CPU or unresolved image selection
fails before containers start. Companion images remain catalog metadata; replay on another runtime/CPU
needs those bytes from the release registry or a mirror.

Bake verifies the exported Docker and OCI manifests against the selected aliases and CPU, and records
both config digests and exported OCI digests. Startup accepts only those verified identities, so the
same artifact loads on classic and containerd Docker image stores even though they report different IDs.

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

Defaults come from each descriptor's `replay.start` (true when absent), then service policy, then
instance policy. Local files merge over deployment entries. Explicit instance names, `--live NAME`,
and `--include-service SERVICE` override startup defaults. The player cannot be disabled this way.
Startup omission preserves the service's recorded bag topics. Use `--skip-service SERVICE` or
`--skip-instance NAME` to omit startup **and** recorded data. Conflicting include/skip requests fail.

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
readiness deadlines. Native image execution on both architectures, registry publication, Jetson
capture, and software replay of actual Jetson recordings remain release acceptance checks. Camera
CI includes native modern-runtime jobs on both CPUs. Shared producer readiness/release synchronization
remains a separate follow-up; `--start-delay` is still needed for unusually slow selected services.
