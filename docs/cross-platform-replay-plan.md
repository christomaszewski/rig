# Build deploy and replay portability plan

Historical implementation checkpoint. Later native/baked/offline acceptance is recorded in
the [v0.2.72 readiness report](releases/coordinated-replay-readiness-2026-10-27.md);
[BUILD_AND_REPLAY.md](BUILD_AND_REPLAY.md) is the current operator reference. The dated
findings and pending items below describe the original work, not today's release state.

Status: implemented in the local workspaces, 2026-10-16. Scope: Rig, camera-service, Doodle, Dashboard, and the shared images in rig-infra. See [operator and migration documentation](BUILD_AND_REPLAY.md). The findings below describe the pre-change baseline; image publication and hardware acceptance remain pending.

Build on an arm64 Mac or amd64 Linux machine, deploy an immutable artifact to an arm64 Jetson, and reconstruct its run on either development machine without manually rebuilding a second image set or editing away the recorded platform. Keep the deployed runtime and the replay runtime explicit in provenance.

The recommended sequence is to fix camera packaging first, add persistent replay service selection and bounded Doodle startup, then implement builds and reconstruction that distinguish runtime variants from CPU architectures. A normal build should produce all declared supported runtime/architecture combinations for the deployment's services. Explicit selectors narrow the build for faster iteration. Multiarch publishing supports this design; it cannot choose between JetPack and software camera runtimes by itself.

## Findings

| Area | Current behavior | Consequence |
| --- | --- | --- |
| Rig build | `rig_cli/build.py:build()` resolves one platform and composes `<version>-<platform>` for matrix services. | A Jetson build does not also produce the `dev` camera set. |
| CPU targeting | Camera uses an optional `PLATFORM_FLAG`; Doodle has a different `DOODLE_BUILD_PLATFORM` knob; fleet-ros and its messages overlay run plain `docker build`. Rig does not consume camera's `build.arch: arm64` descriptor field. | A declared Jetson variant does not by itself guarantee an arm64 image on an amd64 builder. Generic services and their parents also need architecture handling. |
| Cache policy | Rig forwards `--no-cache` as `RIG_BUILD_NO_CACHE=1`; each service must translate it into builder arguments. Dashboard's current script ignores it on all three image builds. | A successful Rig command does not currently guarantee that every service rebuilt without layer-cache reuse. |
| Camera dev | Ubuntu 26.04, pinned GStreamer 1.28.8, software media support, runc, unixfd transport, and bridge networking by default. The descriptor describes arm64 and x86 bench hosts. | Keep one `dev` runtime with architecture variants. amd64 build and playback compatibility still need actual validation. macOS runs the Linux container through Docker; it does not need a macOS image. |
| Camera packaging | `core-driver/Dockerfile.dev` copies code into `/app`; `docker-compose.dev.yml` then binds `./core-driver` onto `/app`. The launch surface carries example configs but no supervisor source. | A reconstructed/vendored tree hides the image's supervisor with a directory that lacks it. |
| Reconstruction | Captures serialize the effective platform into `vehicle.yaml`. Reconstruction can also copy the original local settings into its new `vehicle.local.yaml`; tree-local values override machine provisioning. | Changing host does not reliably change the runtime variant. A captured tree-local Jetson platform can outrank a bench machine's `dev` setting. |
| Image identity | Baked compose files pin resolved images; native run capture records digests as metadata. Replay invokes service launchers and composes image tags again. | Adding a companion build alone does not provide immutable replay image selection. Preserve and resolve both deployment and replay image identities. |
| Replay selection | `--skip-service` already removes instances from all tiers, preflight, startup, and relevant bag topics, and records the selection. | `--skip-service doodle` is available now in a sufficiently recent Rig. It also omits Doodle's recorded topics; it is broader than merely preventing a live radio connection. |
| Doodle startup | Detached startup polls convergence for `DOODLE_SETTLE_TIMEOUT`, default 120 seconds, then returns failure while leaving the container running. Status can itself perform radio requests. | An absent radio delays sequential Rig startup. Reducing a sleep alone does not bound total elapsed time. |
| Replay timing | The release timestamp is calculated before sequential startup, with a default 20-second delay. | A long infra startup can consume the release delay before producers and consumers are ready. Rig currently warns afterward. |

The camera compose defect was reproduced by staging its declared launch surface and rendering `cam-up ... config --format json`: the selected image was `cam-dev`, the command was `python3 supervisor.py`, and the `/app` bind source had no `supervisor.py`. Git blame dates that bind to June 6, rather than the October dependency update. This verifies the packaging failure path; it does not establish which service revision is inside the user's particular artifact.

Docker documents that a bind mount obscures existing image files at its destination, and that multiarch images select manifests by OS and CPU architecture. JetPack compatibility still requires our separate runtime variant. See [bind mounts](https://docs.docker.com/engine/storage/bind-mounts/) and [multi-platform builds](https://docs.docker.com/build/building/multi-platform/).

## 1 Camera packaging fix

Make `dev` an image-backed deployable runtime. Remove the `/app` source bind from its normal overlay. Keep source editing available through a separate explicit development overlay/flag, with a clear error if a full checkout is absent. Preserve the external config, recording input, output, and socket mounts.

Publish the corrected launch surface and refresh the camera package in affected reconstructed workspaces. Updating only the image cannot fix a launcher that still mounts over `/app`. If an existing image already contains the matching supervisor and code, this correction does not itself require rebuilding that image. Keep the source run's capture untouched and record the workspace's service revision change as replay drift.

Acceptance: render from a vendored tree containing no driver source; assert no normal dev mount hides `/app`; start the published image with builds disabled; replay a small known recording. Repeat on arm64 and amd64. Check that the opt-in source-editing path still works.

## 2 Replay service policy and Doodle startup

Add a replay startup policy independent of normal deployment `enabled`. A service descriptor can declare that it should not start live by default during replay; Doodle should use that default. A deployment can override by service or instance, and an explicit CLI inclusion supports hardware-in-the-loop sessions.

Suggested contract names, subject to implementation review: descriptor `replay.start: false`, deployment overrides under `replay.services` and `replay.instances`, and `--include-service` / `--skip-instance` CLI controls. Preserve existing `--skip-service` semantics. Explicit contradictory CLI selections are errors; otherwise use CLI, instance, service, descriptor, then existing tier defaults in that order.

Distinguish two operations:

- **Do not start live:** no radio container or hardware contact, but recorded radio diagnostics remain eligible for bag playback.
- **Skip entirely:** current `--skip-service doodle`, omitting live startup and the service's recorded topics.

Use the same resolved selection for preflight, topic selection, launch order, snapshots, log capture, and teardown. Keep routers/loggers under explicit policy rather than blanket-disabling infra. Show the selection and reason in `--dry-run`, and record it in replay provenance. Add focused infra-tier coverage; existing skip tests concentrate on cameras.

For Doodle, introduce a short configurable initial connection budget, provisionally 10 seconds, separate from the existing 120-second convergence budget once a radio is reachable. A present radio can legitimately require tens of seconds for RF changes; preserve that allowance. Distinguish unreachable transport, authentication/configuration errors, and pending convergence with structured status rather than parsing human error strings.

Enforce deadlines through the entire status path: Docker exec, control socket, radio requests, retries, and polling. Prefer a cached supervisor status for launcher readiness checks so polling does not initiate extra radio observations. Return a clear nonzero result on timeout. Keep the existing background reconnect behavior explicit in the message: timing out the launcher does not stop the container. Authentication/configuration failures can fail immediately. Preserve bounded shutdown power handling.

Acceptance: absent radio returns within the configured connection budget plus a small documented process allowance; reachable slow RF startup retains its longer budget; invalid credentials are distinguished; a stalled status request cannot defeat the deadline; background recovery and shutdown remain correct.

## 3 Build every supported runtime and architecture by default

Model two independent dimensions:

| Runtime variant | Supported image architectures | Purpose |
| --- | --- | --- |
| `jp6`, `jp6m`, `jp7` | `linux/arm64` | Matching Jetson userspace and runtime integration |
| `dev` | `linux/arm64`, `linux/amd64` after validation | Software camera capture/replay on bench hosts |
| Generic service | Declared supported architectures | Same service runtime across hosts |

Use explicit service metadata rather than a guessed `hardware_accelerated` boolean. Retain `build.platforms` and extend the descriptor with supported architectures per variant, a declared replay variant (camera: `dev`, used for runtime selection), and whether the build command accepts the new architecture contract. Declare per-variant image outputs too: `cam-dev` replaces `cam-core`, and the current flat `build.images` list is a superset, not the outputs of every build. A declared supported combination is a build obligation, so experimental or unvalidated combinations must be identified separately before adopting the new default.

Proposed user flow:

```sh
# Proposed default behavior and selectors; --arch is new:
rig build --tag vNEXT                       # all supported variants and architectures
rig build --tag vNEXT --platform jp6m        # JP6m set, arm64
rig build --tag vNEXT --platform dev         # dev set, arm64 + amd64
rig build --tag vNEXT --platform dev --arch amd64  # one development target
```

Default to all variants declared by each in-use service, and all supported architectures within each variant. Services without a runtime matrix build their generic image for their declared architectures. Scope remains the deployment's services, not every package in a registry. The machine's provisioned platform and `vehicle.yaml platform` select deployment/runtime behavior; they no longer silently restrict the default build matrix. Preserve explicit `--platform` as the fast runtime selector and add `--arch` for architecture selection. A requested unsupported pair such as `jp6m` plus `amd64` is an error. There is no separate `--with-replay` or `--multiarch` opt-in in the proposed default.

Resolve the complete build plan before running anything. Deduplicate by source revision, image/runtime variant, architecture, and build inputs. Recompute base and messages-overlay dependencies for each required architecture. A full build produces generic arm64 and amd64 images once each, shared across compatible runtime variants, regardless of which laptop starts the build. A targeted JP6m build needs only the relevant arm64 dependency set. Do not run all build scripts repeatedly and let a later push replace an earlier architecture under the same tag.

Adopt one Rig-owned build environment field for requested OCI platforms and explicit descriptor opt-in. Unsupported legacy commands should produce an actionable preflight error for a requested cross-architecture build. Update camera, fleet-ros, the messages overlay, and other selected generic service builders to use Buildx with deliberate push/load behavior. Audit all enabled deployment services and replay dependencies before claiming the full workflow portable. Check builder capabilities before expensive work and show the complete matrix in `--dry-run`; do not silently skip an unavailable architecture or call a partial release complete.

Keep version/variant tags such as `cam-core:vNEXT-jp6m` and `cam-dev:vNEXT-dev`. Publish an OCI index when a variant needs multiple architectures: `cam-dev:vNEXT-dev`, its dev bridge images, and portable images such as `fleet-ros:vNEXT` contain arm64 and amd64 manifests; JetPack variants remain arm64. Record actual outputs, source revisions, index digests and child manifests in the build result/lock metadata. Keep complete-release and targeted-build status explicit. A targeted build must not replace an existing multiarch release index with one architecture; use an iteration tag or a deliberate publication operation that preserves and verifies the other members. Registry-backed builds are the initial delivery path; an offline bundle must explicitly carry every promised variant/architecture rather than relying on a host-native `docker save`. Bake still selects the deployment runtime and architecture, so publishing the full matrix does not force every image into the Jetson artifact.

The cost is more build time and registry storage, especially the initial GStreamer/ROS compilation under emulation. Preserve BuildKit caches and concurrency controls, and support native arm64 and amd64 builder nodes for expensive compilation. Treat emulation as a supported builder capability where validated, not as a guarantee that every hardware image can build or run on the laptop. This cost is the reason for explicit fast selectors, rather than leaving a normal release missing images needed by another supported host.

Acceptance: the same default matrix from both laptop architectures even when their provisioned platforms differ; targeted runtime and architecture selectors; unsupported-pair errors; all supported Jetson variants; deduplication; base ordering; missing builder support; and per-variant output sets. Inspect published indexes, ensure targeted builds cannot discard existing architecture members, and run native dev smoke tests on both CPU architectures. A successful cross-build is not proof of Jetson hardware operation; retain a real Jetson deploy/run check.

### Make no cache behavior consistent

Rig's sequential, parallel, base, and messages-overlay build paths pass `RIG_BUILD_NO_CACHE=1` when requested. That is currently a cooperative service contract: Rig cannot force an arbitrary script to pass the corresponding Docker argument. Certification does not currently check this behavior.

A command-capture audit of the current local build scripts, using a fake Docker executable with the flag both unset and set, found:

| Build command | With `RIG_BUILD_NO_CACHE=1` |
| --- | --- |
| Camera, all four runtime variants | Every emitted image build receives `--no-cache --pull` |
| Doodle | Receives `--no-cache --pull` |
| Dashboard | All three image builds omit both flags; confirmed missing support |
| fleet-ros base | Receives `--no-cache --pull` |
| fleet-ros-msgs | Receives `--no-cache`; deliberately omits `--pull` to use the deployment base built in stage zero |

Fix Dashboard and audit the exact pinned source revisions used by deployments, not just adjacent working checkouts. When a vendored service lacks its build context, Rig builds from the source pinned in `rig.lock`; an uncached rebuild does not advance that revision. It also does not change a baked deployment's image digest or recreate running containers.

Make cache support a required, tested part of the revised build contract. Record builder capabilities and refuse an unsupported `--no-cache` request before starting expensive work, with a message naming the service and source revision. Exercise each adopted build command at its Docker/Buildx boundary with cache enabled and disabled, including every nested image build. Add Rig forwarding coverage for sequential, parallel, base and messages-overlay paths, and all selected runtime/architecture combinations. Report source revision, build context, cache policy and parent-image policy in the build plan/results.

Put the behavioral check in `rig certify`, with lightweight reporting in `rig doctor`. This follows the existing split: certify tests service conformance, doctor checks the deployment, and `doctor --deep` already invokes certification.

- **`certify`, check `build-no-cache`:** exercise the real build entrypoint with the variable unset and with `RIG_BUILD_NO_CACHE=1`, capturing Docker/Buildx arguments. Check every expected image build in each declared variant/architecture, plus separately declared base and messages-overlay commands. With the variable set, every applicable build must disable its instruction cache; with it unset, the script must not unconditionally disable caching. A script emitting no build commands is not proof of compliance. Report failures by service, variant, image and source revision. Dashboard's current three omissions must produce an ERROR.
- **`doctor`:** inspect declared build-contract capabilities and report missing/unknown no-cache support as a WARN with the certification command to run. A declaration is not a behavioral pass. Normal runtime preflight must not execute build scripts, fetch source checkouts, or block `up` solely because build certification is unavailable. If certification evidence is persisted later, only reuse it for the matching source/build-context fingerprint, covered matrix and certifier version; dirty helper changes invalidate it.
- **`doctor --deep`:** run the same `build-no-cache` check when the full local build context and supported probe are available. A vendored launch-only surface, missing context, or unsupported probe is explicitly NOT CHECKED with an actionable warning, never a green pass. Do not silently fetch or execute an unrelated sibling checkout in place of the deployment's pinned source.

The build probe must remain free of actual builds, pulls and pushes. Execute in temporary staging with an execution environment that cannot reach the real Docker daemon or network; command interception alone is not containment for arbitrary scripts using absolute paths, sudo, or other tools. Initially support the known shell Docker/Buildx build entrypoints, with explicit unsupported results for other build systems. Apply a timeout and keep source checkouts unchanged. CI should require completed build-contract checks for services it publishes; lacking a local build context on a deployed vehicle is a different condition from a builder failing the contract.

Define `--no-cache` as disabling reuse of image-build instruction results. Parent-image refresh is a distinct policy: current camera, Doodle and fleet-ros scripts also request `--pull`, while the messages overlay intentionally consumes the just-built base. Preserve that dependency relationship when moving to Buildx by passing the exact newly published parent digest; do not blindly pull a mutable parent tag over the current build's result. External parent refresh must be explicit and consistent. Source/dependency version pins remain authoritative. Never implement this option by globally pruning the user's Docker caches. Docker documents the separate [`--no-cache` and `--pull` options](https://docs.docker.com/reference/cli/docker/buildx/build/).

Acceptance: Dashboard fails certification before its fix and passes for all three images afterward; the check also catches one missing flag among multiple builds, an ignored variable, unconditional no-cache, and a no-op build script. Cover sequential/parallel forwarding, base and messages builds, vendored-only context, unsupported probes and timeouts. Normal doctor remains read-only and never invokes the build entrypoint. Every participating service passes the command-capture checks; unsupported builders fail visibly when no-cache is requested; internal parents resolve to this build's output; and rebuilding a locked source revision is clearly distinguished from updating source pins or redeploying an artifact. This audit executed no real builds, pushes or cache deletion.

## 4 Reconstruct onto the replay host

Keep the recorded platform and original image digests immutable in source-run provenance. Add an explicit replay target selection, with reconstruction options such as `--platform dev` and `--platform recorded`. For the laptop workflow, prefer the destination host's provisioned runtime; when none is declared, use the services' declared software replay mapping and validate it. An explicit request for the recorded runtime preserves Jetson replay and hardware-in-the-loop use.

Write the resolved runtime into the reconstructed workspace's local settings so `doctor`, `pull`, `replay`, `status`, and `down` agree. Do not blindly restore the source's local platform over the new host's selection. Preserve vehicle identity and config provenance separately from execution hardware. Validate each selected service's compatibility; never silently map an unsupported variant to another one.

Use the companion image catalog from the original build to select compatible digests, including bridge images, fleet-ros and messages overlays. Keep deployment compose pins separate; do not rewrite Jetson digest suffixes or reuse a frozen Jetson compose file on a laptop. If a companion is missing, report its exact variant/architecture and the build command needed before starting infra. Old runs without companion metadata can use an explicit rebuild/migration path with recorded provenance; they cannot promise byte-identical companion images retroactively.

Resolve registry relocation as well as image identity: preserve repository/digest mapping when a mirror contains the same bytes, and diagnose missing content. Record source and effective runtime, Docker architecture, image digests, and service selection in every replay run.

Acceptance: captured tree-local Jetson platform versus provisioned dev host; explicit recorded-platform replay; native capture versus retrofitted baked artifact; missing companions; mirrored registry; subsequent status/down consistency; and unchanged source-run checksums.

## Delivery and verification

Land camera packaging independently first, then replay startup policy plus Doodle's bounded connection handling. Fix Dashboard's missing cache-flag support independently, and make cache policy part of the build-contract adoption. Introduce the build contract and adopt it across required builders before switching normal builds to all supported runtime/architecture combinations. Document the changed default and the explicit `--platform` / `--arch` iteration path. Land reconstruction selection with the build's image catalog, and document migration for old artifacts containing bundled older Rig/launcher versions.

The end-to-end acceptance matrix is arm64 Mac and amd64 Linux builders, arm64 JP6m and JP7 deployment, then native replay on both laptop architectures from the same recording. Verify camera output/timestamps, software decode of Jetson-produced recordings, ROS transport, bags, restart behavior, and no live Doodle contact under the default replay policy. Include networking explicitly: dev currently defaults to bridge; Docker Desktop WebRTC media and ROS connectivity may need host networking/configuration independently of CPU support. Do not promise Jetson throughput from software replay.

Track a related follow-up: release replay producers only after selected services are ready, using a shared arm/release contract with the camera sources and bag player. Until then, skipping hardware infra removes the reported delay, while `--start-delay` remains the existing workaround for other slow startup. Merely timing out Doodle faster does not prove synchronized replay startup.

Implementation verification: the missing host dependencies were installed into a temporary test environment. Rig's module suites, camera's seven platform/build/compose tests, and Doodle's 72 host tests (one ROS-only skip) were exercised. Strict certification passes all six participating service targets with zero warnings or errors, including isolated cached/uncached builds across their architecture matrices. The probe denies network/daemon access and uses fake Docker commands. Shell checks pass for the changed shared helpers and Doodle/Dashboard/infra scripts. No images were built or published. Native image execution and Jetson-to-laptop end-to-end replay remain hardware acceptance work. Portable catalogs intentionally require registry delivery; offline multiarch image bundles are not implemented.
