# Offline and local-source builds plan

Status, 2026-10-28, not yet released: phase 0 (section 8) implemented. Phase 1 (section 4)
implemented in Rig and rig-infra's `msgs/`; sbg_driver's adoption of `build.sources` is pending,
and section 9 B records what changed from this plan and what was verified. Phase 2 proposed.
Scope: Rig, rig-infra (`base/`, `msgs/`), sbg_driver and novatel. camera-service, ouster and
mavlink-router are deferred (section 10).

Goal: on a development box with a local registry, `rig build --offline` rebuilds every image
the deployment builds without reaching a remote git host, a public registry, or a package
mirror. That covers a code edit and a local message change. The vehicle then pulls from that
box's registry, or receives a `rig bake --bundle-images` artifact, exactly as today.

## 1 Decisions and boundaries

- **Where:** a development box running the deployment's registry (`images.registry`, for
  example `localhost:5000`). The matrix build path keeps pushing to it, and children keep
  building from the base by registry digest. Registry-free builds (`--load` only, a Jetson
  building for itself) are deferred.
- **When:** only with an explicit `rig build --offline`. Without it, network failures stay
  failures; rig may suggest `--offline` but never falls back on its own.
- **Two kinds of local source stay separate** (section 3). `--offline` builds the *pinned*
  revision from a local copy of its history. Building a *working tree* is a separate dev
  override that is labelled as such and cannot ship silently.
- **Git dependencies fetched inside builds are declared in the rigging.** Rig mirrors them and
  passes them in as named build contexts; Dockerfiles stop cloning.
- **First cut includes base images:** local-registry seeding, no `# syntax=` directives, no
  forced `--pull`, a shared `fleet-ros-sdk` builder, and a registry-backed build cache.
- **Offline is proven by disconnecting, not by a build flag.** This plan first had `--offline`
  run every `RUN` step with `--network none`. Measured 2026-10-28: BuildKit keys a `RUN` step's
  cache on its network mode, so that reran every step an online build had cached and failed at
  the first download. Acceptance needs a builder with no outside network instead, such as CI with
  egress blocked except to the local registry.

Deferred: automatic fallback, registry-free builds, camera-service, ouster and
mavlink-router adoption, an apt proxy, and moving mirror caches between machines.

## 2 Baseline before implementation

| Area | Current behavior | Required change |
| --- | --- | --- |
| Registries | Offline once `rig registry sync` has run; nothing else fetches ([registries.py](../rig_cli/registries.py)). | None. |
| Service source | `_fetch_source` clones into `~/.rig/cache/src/<slug>-<rev12>`, one full clone per revision, so a new pin always needs the network ([install.py](../rig_cli/install.py)). `rig build` falls back to it for vendored services ([build.py](../rig_cli/build.py) `_resolve_build_cwd`). | Clone from a per-repo bare mirror; offline, resolve from the mirror or fail by name. |
| msgs `source:` repos | Cloned inside the builder stage, with ssh agent forwarding for `git@` repos (rig-infra `msgs/build_msgs.py`). `FLEET_MSGS_LOCAL` substitutes working trees. | Export every source tree on the host; the builder never clones. |
| Fetches inside Dockerfiles | sbg_driver's CMake FetchContent clones sbgECom from GitHub during `colcon build`. | Declare it in the rigging; pass it as a named context. |
| Base images | `base/Dockerfile` starts from `ros:<distro>-ros-core`; novatel's build stage from `ros:<distro>-ros-base`. | Seed into the local registry; resolve from there. |
| Frontend | `# syntax=docker/dockerfile:1` in msgs, sbg, novatel, ouster and mavlink-router. Every build resolves it on Docker Hub, even when cached (observed on Docker 29). | Remove; use the built-in frontend. |
| `--pull` | `rig-build.sh` adds it with `--no-cache` unless `RIG_BUILD_PULL=0`. novatel always passes it. | Never under `--offline`; novatel drops it (section 7). |
| apt in build stages | Every build stage installs the ROS toolchain itself. | Shared `fleet-ros-sdk` builder plus a registry build cache. |
| Build cache | Lives only in the builder. Rig's managed builder is named by a hash of its config ([docker_registry.py](../rig_cli/docker_registry.py) `_managed_builder`), so a config change starts a new, empty cache. | Registry-backed cache in `images.registry`. |
| `mirror:` images | Copied from upstream on every build ([matrix_build.py](../rig_cli/matrix_build.py)). | Under `--offline`, use the copy already in the registry or fail by name. |
| Local-build guard | `rig image audit` compares the overlay's SHA only when it is known ([audit.py](../rig_cli/audit.py)), so a dirty local overlay (`rev: unknown`) passes. `rig bake` never checks. | Label local builds; audit warns, bake refuses (section 8). |

## 3 Two kinds of local source

| | Dev override | Offline reproduction |
| --- | --- | --- |
| Builds | The working tree, including uncommitted and untracked files | The pinned revision |
| Comes from | A checkout the developer names | Rig's mirror of the upstream repo |
| Provenance | `ref` with `-dirty`, `rev: unknown` when dirty, `local: <path>` | Identical to an online build: declared `ref`, resolved `rev` |
| Safe to ship | No: labelled, audit warns, bake refuses without `--allow-local` | Yes |
| Selected by | `FLEET_MSGS_LOCAL` today; services already have `rig swap <svc> <path>` | `rig build --offline` (and every online build, which also goes through the mirror) |

`--offline` never substitutes a working tree, and a dev override never happens without being
named.

## 4 Source mirrors and `rig build --offline` (phase 1)

### Commands

```sh
rig src sync                  # online: fetch every repo this deployment builds from
rig build --offline --check   # report everything an offline build would miss; build nothing
rig build --offline           # build without remote git or public registries
```

### Mirror cache

- One bare mirror per repo at `~/.rig/cache/git/<slug>.git`, keyed by the existing
  `_repo_slug`, fetching `refs/heads/*` and `refs/tags/*`.
- The per-revision checkouts in `~/.rig/cache/src/` stay, but are cloned from the mirror
  locally, with `origin` reset to the upstream URL.
- Online builds fetch into the mirror first, so a box that has built a pin online can rebuild
  it offline without `rig src sync`.
- `rig src sync` covers: service pins in `rig.lock`, the union of `msgs.source` entries,
  `build.sources` entries (below), and submodules of each at the commit its superproject pins.
  Checkouts point each submodule's URL at its mirror for the clone, then back at the upstream.
  (Not `url.<mirror>.insteadOf`: git skips that rewrite for local-path submodule URLs.)
- A branch ref resolves from the mirror's last fetch. `--offline` prints the resolved SHA and
  when the mirror was fetched; provenance records the SHA.

### Declared build sources

A rigging declares each repo its build fetches:

```yaml
build:
  command: tools/build_image.sh
  sources:
    - name: sbgecom
      repo: https://github.com/SBG-Systems/sbgECom.git
      ref: 5.8.935-stable
```

Rig resolves each entry to a commit in the mirror, exports the tree (no `.git`, submodules
included) to `$RIG_BUILD_SOURCES/<name>/`, and writes `$RIG_BUILD_SOURCES/sources.tsv` (name,
repo, ref, SHA). The build script passes `--build-context <name>=$RIG_BUILD_SOURCES/<name>`.
Not implemented yet: recording these SHAs in `rig.lock` so `--locked` builds verify them.

The Dockerfile keeps working without rig through the pattern `msgs/Dockerfile` already uses
for `local-sources`. It declares an empty default stage (`FROM scratch AS sbgecom`) and binds it
into the build step. If the stage is non-empty, the build uses it; for sbg that means
`-DFETCHCONTENT_SOURCE_DIR_SBGECOM=<mount>`, a hook its CMake already supports. If it is empty,
the build keeps today's online fetch.

### msgs overlay: all git work on the host

- Rig exports each msgs `source:` repo at its pinned ref through the same `RIG_BUILD_SOURCES`
  contract. `build-msgs.sh` passes those trees in the `local-sources` context, with
  `sources.tsv` rows marked `pinned`. `FLEET_MSGS_LOCAL` rows replace a repo's pinned row and
  keep their `local` marking.
- Under rig, the builder never clones and needs no ssh agent. Standalone (no rig),
  `build-msgs.sh` still clones in the builder as before, so git, openssh and the ssh mount stay
  in the builder stage. Cloning on the host instead would need Python and PyYAML on every
  standalone build host; not worth it yet.
- Provenance for pinned rows is unchanged in content: the declared `ref`, and the SHA whose
  tree was exported. The rule "record the revision that was actually built" now applies at
  export time.

### Enforcement and preflight

- `--offline` makes rig fetch nothing and exports `RIG_OFFLINE=1` and `RIG_BUILD_PULL=0` to
  every build. `RUN` steps keep whatever network the machine has (see section 1 for why not
  `--network none`): on a disconnected machine, a cached step is reused and an uncached one
  fails where it downloads.
- `--offline` refuses `--no-cache`, which would rerun every step.
- The preflight runs before any build and reports everything missing in one go: repos or refs
  absent from the mirror (submodules included), and `mirror:` images absent from
  `images.registry`. `--check` stops after the report. Declared base images and a builder that
  cannot start without pulling join the preflight in phase 2.
- `rig-build.sh` is unchanged in phase 1. It is copied into each service repo and nothing keeps
  the copies in sync; phase 2's registry cache will change it, so update the canonical
  `packaging/rig-build.sh` and the copies together then, and have `rig certify` report a copy
  that differs from the canonical one.

## 5 Offline base images (phase 2)

```sh
rig images seed               # online: copy declared external images into images.registry
```

- **Declare.** The base provider's rigging lists its external parents:
  `build.base_images: ["ros:${ROS_DISTRO}-ros-core"]`. After section 7, that is the only
  external parent in the first cut. Rig also seeds the BuildKit image its managed builder runs.
- **Seed.** `rig images seed` copies each image into `images.registry` under its upstream path
  (for example `localhost:5000/library/ros:lyrical-ros-core`). The copy preserves digests,
  reusing the `mirror:` code, and rig records the digests in `rig.lock`.
- **Resolve.** Rig's managed builder config gains `[registry."docker.io"] mirrors =
  ["<images.registry>"]`, next to the transport settings it already writes. Dockerfiles keep
  their public `FROM` names. `--offline` always builds with the managed builder so this applies.
- **Spike first.** Before committing to registry mirrors, verify on both rig's managed
  `docker-container` builder and Docker's own builder with the containerd image store:
  1. a mirrored `FROM` resolves with the upstream unreachable;
  2. the builder starts with no network when its BuildKit image is present locally;
  3. with `# syntax=` removed, the built-in frontend handles `RUN --mount` and msgs'
     `# check=` directive.

  If (1) fails, use a parent-image build argument that rig sets to the seeded digest. That
  means one Dockerfile change in the first cut.
- **Remove `# syntax=docker/dockerfile:1`** from msgs, sbg and novatel now, and from ouster
  and mavlink-router when they are adopted.
- **Registry build cache.** When rig sets `RIG_BUILD_CACHE_REF`, `rig-build.sh` adds
  `--cache-from` and `--cache-to type=registry,ref=<images.registry>/<image>:buildcache,mode=max`.
  This keeps service-specific apt steps available offline after `docker buildx prune` or a new
  managed builder, which any config change creates, including adding the mirror above.

## 6 Changing a dependency offline

A cached step whose inputs have not changed needs no network. Changing a service's apt
packages, adding a ROS dependency, or bumping a declared source to a ref the mirror has never
fetched needs the network once. `--offline` fails at that step or in the preflight and names
it. It never resolves a different version from whatever is cached.

## 7 The fleet-ros-sdk builder (phase 2)

**What it is.** `base/Dockerfile` already defines an `sdk` stage on top of fleet-ros's
`runtime-deps` that nothing builds. `base/build.sh` builds it as a second target and pushes
`fleet-ros-sdk:<tag>` next to `fleet-ros:<tag>`, for the same architectures. The base
provider's rigging names it, for example `sdk: fleet-ros-sdk` next to `provides: base`. Rig
records its digest after stage 0, as it does for the base, and exports `RIG_SDK_IMAGE` to
stages 1 and 2. No compose file references it, so bake never bundles it and vehicles never
pull it.

**Why.** Every service build stage currently starts by installing the ROS toolchain over the
network. With the SDK that happens once, in stage 0. A service build stage becomes: start from
the SDK, install its few extra packages (a cached step), copy the source, run `colcon build`.

It also prevents a version skew that exists online. novatel's build stage starts from public
`ros:<distro>-ros-base` while its runtime stage starts from fleet-ros, so `tools/build_image.sh`
always passes `--pull`. Its own comment explains why: a stale cached base can leave the two
stages on different ROS libraries, which has caused a runtime symbol-lookup failure. The SDK is
derived from the same stage as fleet-ros, so the two cannot drift and the forced `--pull` goes.

**Which services use it.** A build stage uses the SDK when the service (1) builds ROS 2
packages with colcon and (2) has a runtime stage that starts from fleet-ros. The second
condition is what matters: the SDK's ROS libraries match the runtime's only when both share the
parent.

| Image | Uses the SDK | Notes |
| --- | --- | --- |
| fleet-ros-msgs | Yes | Its builder already installs the SDK's package set. Builder and runtime still install the manifest's `apt:` entries in the same build. |
| sbg | Yes | Both stages already start from fleet-ros. Keeps installing nav-msgs, tf2-ros, std-srvs, rtcm-msgs and nmea-msgs itself. |
| novatel | Yes | Build stage moves off public ros-base; the forced `--pull` is removed. |
| ouster | Yes, toolchain only | Already on fleet-ros. Its rosdep steps remain its own. Not built today. |
| camera-service | No | Own ubuntu, l4t, GStreamer and Rust bases per platform. Deferred. |
| mavlink-router | No | Alpine C++, no ROS. Deferred. |
| zenoh-router, bag logger, bag player | Not applicable | No build stage; they run fleet-ros directly. |

**Contents: fixed, not assembled.** The existing `sdk` stage's toolchain (build-essential,
cmake, git, colcon, rosidl generators), plus ninja and the packages sbg and novatel both
install today: rclcpp, rclcpp-lifecycle, rclcpp-components, lifecycle-msgs, sensor-msgs,
geometry-msgs, std-msgs, diagnostic-msgs, diagnostic-updater, generate-parameter-library and
rosidl-default-runtime. Rule for later additions: a package belongs in the SDK when it is
generic toolchain or at least two consumers need it; otherwise it stays in the service's
Dockerfile. Assembling the SDK from per-service declarations, the way the msgs overlay is
built, would rebuild it whenever any service adds a dependency and discard every other
service's build cache.

**Adopting it** takes two Dockerfile lines and one build-script argument:

```dockerfile
ARG SDK_IMAGE=ros:${ROS_DISTRO}-ros-base   # standalone fallback, like BASE_IMAGE
FROM ${SDK_IMAGE} AS build
```

```sh
--build-arg "SDK_IMAGE=${RIG_SDK_IMAGE:-ros:${ROS_DISTRO:-lyrical}-ros-base}"
```

**Cost.** One extra image per architecture in the local registry, rebuilt only when fleet-ros
is.

## 8 Phase 0: finish `FLEET_MSGS_LOCAL`

The rig-infra working tree adds `FLEET_MSGS_LOCAL`: host snapshots of named checkouts replace
the pinned clones of matching msgs `source:` repos, passed as the `local-sources` named context.
Its 18 unit tests pass. Real BuildKit confirms that the empty default stage binds, and that
`SSH_AUTH_SOCK` is set without a forwarded agent, so `build_msgs.py` is right to test that the
socket exists. Changes made before committing:

1. **Snapshot without writing to the checkout.** `read-tree HEAD` plus `add -A` through a
   temporary index re-hashes every tracked file and writes every untracked, non-ignored file
   into the checkout's `.git/objects`. A stray bag file ends up there and in the build input,
   and a read-only checkout fails. Use `git ls-files -z --cached --others --exclude-standard`,
   skipping deleted paths, piped into `tar --null -T -`, and `git status --porcelain` to
   decide "dirty".
2. **Repos with no commits.** A directory with `.git` but no commits currently takes the
   "plain directory" path, which copies `.git` and the `build/`, `install/` and `log/` trees.
   The `ls-files` approach handles it: record `unknown`/`unknown`.
3. **Document submodules.** Their contents are not snapshotted. The builder's clone does not
   fetch them either, but rig's own source cache does.
4. **Guard against shipping it.** `build-msgs.sh` adds a `io.rig.build.local` label naming the
   overridden keys. `rig image audit` warns on that label and on provenance entries carrying
   `local:`. `rig bake` refuses labelled images unless `--allow-local`. Pushing to the local
   registry stays allowed; that is the dev loop.

Phase 1 then moves the remaining builder clone to the host (section 4). The snapshot code
becomes the dev-override half of that export.

## 9 Delivery sequence and acceptance

### A Phase 0: `FLEET_MSGS_LOCAL` fixes and the guard (rig-infra, Rig)

Acceptance: the existing tests pass. New tests show that the checkout's `.git/objects` and index
are unchanged after a snapshot, that a read-only checkout and a repo with no commits work, and
that audit warns about, and bake refuses, an image carrying the local label.

### B Phase 1: source mirrors and `--offline` (Rig, rig-infra msgs, sbg_driver)

Mirror cache, `rig src sync`, `build.sources`, host-side msgs export, and the preflight.

Acceptance:

- Rig host tests: a new pin resolves offline from the mirror; missing repos and refs are
  reported together before any build; a branch ref prints its SHA and fetch time; submodules
  resolve through their mirrors.
- The msgs overlay (sbg_msgs pinned, from the mirror) and sbg (sbgECom from its declared
  source) build on a disconnected builder with a warm cache.
- The overlay's `provenance.yaml` matches an online build's, with no `local:` entries.
- Standalone `build-msgs.sh` still works without rig.

Implemented 2026-10-28, except sbg_driver's `build.sources` adoption: its Dockerfile, build
script and rigging carried another branch's uncommitted work, so they were left alone. Changes
from this plan: no `RIG_BUILD_NETWORK` (section 1), standalone msgs builds still clone (section
4), and no `rig.lock` record of build-source SHAs yet. Verified:

- Rig host tests cover every bullet above but sbg: mirrors, offline resolution, fetch by commit
  id, submodules (including relative URLs) offline, `--offline` preflight and `--check`,
  exports into both build paths, and channels popped on online builds.
- rig-infra msgs tests cover pinned rows, a local checkout winning over them, and mismatch
  refusals, on macOS and Ubuntu 24.04.
- On the real deployment, `rig src sync` mirrored sbg_driver v0.8.2 from GitHub and
  `rig build --offline --check` passed. A real Docker build of the overlay from rig's mirror
  export compiled sbg_msgs without a clone. Its provenance records `v0.8.2` at the commit the
  GitHub tag names, with no `local:` and no local-build label.
- Not verified: a build on a builder that is actually disconnected.

### C Phase 2: base images and the SDK (Rig, rig-infra base and msgs, sbg_driver, novatel)

Spike results first, then seeding and mirror config, removal of `# syntax=`, the registry build
cache, `fleet-ros-sdk`, and SDK adoption by msgs, sbg and novatel.

Acceptance, on a development box that has run `rig registry sync`, `rig src sync`,
`rig images seed` and one online `rig build`, with outbound traffic blocked except to the local
registry (in CI, drop egress with iptables after the preparation step):

- `rig build --offline` builds fleet-ros, fleet-ros-sdk, fleet-ros-msgs, sbg and novatel for
  amd64 and arm64:
  - after an edit to sbg source;
  - after a message change built through `FLEET_MSGS_LOCAL`;
  - after the managed builder is recreated with an empty local cache.
- `rig image audit` passes to the SHA tier for pinned builds and warns for the local build.
- The sbg, novatel and msgs build stages start from fleet-ros-sdk, and novatel no longer passes
  `--pull`.
- A vehicle pulls from the development box's registry and `rig up` runs.

Update `BUILD_AND_REPLAY.md`, the cheat sheet, CLI help and release notes. Release rig-infra,
sbg_driver and novatel images with the SDK before a rig release relies on `RIG_SDK_IMAGE`; an
older service ignores it and keeps building from public ros-base.

## 10 Deferred

- **camera-service:** its own ubuntu, l4t and GStreamer bases per platform, rustup and cargo,
  an unchecked Aravis download, and NVIDIA apt. It can adopt seeding and the registry cache
  later; it is not an SDK consumer.
- **ouster:** `rosdep init` and `rosdep update` download from GitHub, and `vcs import` clones
  ouster-ros. It should adopt `build.sources`, with the rigging replacing `ouster.repos` as the
  pin, and possibly a rosdep database baked into the SDK, when it joins `vehicle.yaml`.
- **mavlink-router:** floating `alpine:3` and an upstream clone. It should adopt
  `build.sources` and seeding when built.
- **Registry-free builds:** single-architecture `--load` on the vehicle. The matrix path forces
  `PUSH=1`, verifies the pushed digest and chains children by registry digest.
- Automatic fallback when a remote is unreachable.
- An apt proxy such as apt-cacher-ng, as an alternative to caching apt steps.
- Exporting and importing mirror caches between machines.

## 11 Open questions

- One `rig offline prepare` command (registry sync, source sync, image seed, one online build)
  in addition to, or instead of, the separate commands.
- Whether a permanently disconnected box records `--offline` as a machine setting
  (`vehicle.local.yaml` or `/etc/rig`) rather than passing it every time.
