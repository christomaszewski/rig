# Windows replay without WSL: tentative plan

Status: proposed future work, 2026-11-05. Not implemented or qualified. This plan
records the intended Windows replay path; it does not add Windows support to the
current release or commit to a delivery date.

## 1 Scope and constraints

- Run recorded sessions locally on Windows machines without WSL or WSL2.
- Docker Desktop using its Hyper-V backend is allowed. Require a supported Windows
  installation, enabled virtualization, and a Linux Docker engine; check the
  current [Docker Desktop Windows prerequisites](https://docs.docker.com/desktop/setup/install/windows-install/)
  before defining the supported host matrix.
- Initially qualify x64 hosts with CPU/software replay using compatible `dev`
  runtime images for `linux/amd64`. Keep live sensor access, Jetson runtimes, Windows
  containers, and a full native Windows port outside this first scope.
- Do not promise GPU replay through this backend. Docker documents Linux-container
  [GPU support on Windows through WSL2](https://docs.docker.com/desktop/features/gpu/),
  which is excluded by the deployment constraint.

## 2 Proposed execution model

```text
Windows launcher
  -> Linux controller container on Docker Desktop / Hyper-V
       -> artifact's bundled rig and vendored service launchers
            -> sibling Linux replay containers on the same Docker engine
```

The Windows launcher would handle preflight, artifact selection, import/export,
and forwarding commands and interruption. The controller would supply Python,
PyYAML, Bash, Docker CLI/Compose, and the supporting Linux tools. It would control
the existing Docker engine, rather than start a separate nested Docker daemon.

Execute the baked artifact's own `rig` and `rig_cli` package. Its recorded Rig
version and service launch surfaces remain the authority for replay behavior;
the controller is a compatible execution environment. Define and validate the
controller/artifact compatibility contract before supporting arbitrary artifacts.
Keep the controller used by an active session available until teardown finishes.

## 3 Shared storage and session ownership

This is the main integration work, not just a container wrapper. Docker resolves
[bind-mount sources on the daemon host](https://docs.docker.com/engine/storage/bind-mounts/),
not inside the controller. A recording mounted at `/runs` in the controller is
not automatically available at `/runs` to a sibling service container.

Use persistent Linux-backed storage for the extracted artifact, working configs,
run registry, and generated replay state. Choose and test an explicit mapping
between controller paths and daemon-visible storage, or a shared-volume mount
adapter. Do not depend on undocumented Docker Desktop VM paths. The mapping must
cover vendored launchers' rendered params, recording sources, output directories,
helper/request files, and temporary files passed to Docker.

Coordinated replay currently writes control and immutable config files beneath
the deployment's `var/replay/sessions/`; helper/export operations also create
temporary files. Put any temporary root that can become a container mount in
shared storage. Preserve file locks, symlinks, permissions, and atomic replacement
on Linux storage, rather than initially placing the live run registry on a
Windows filesystem share.

Import recordings from a Windows directory and export selected outputs back to
Windows. Keep original recordings intact. Persist deployment/session state and
stable paths across controller invocations: saved sessions record configuration
hashes, launcher paths, Docker engine identity, and owned containers. A restarted
launcher must support inspection and teardown of the existing session, not lose
ownership or silently create another session. Upgrades and removal must preserve
recordings and workspace data by default.

## 4 Baked artifacts and offline operation

Reuse the existing bake/image verification contracts. For a deployment whose
selected services support the target, the existing bake options can produce the
required replay image set:

```sh
rig bake --tag windows-replay \
  --platform dev --arch amd64 \
  --bundle-images --include-replay
```

`--include-replay` includes the disabled bag player and replay-mode image
requirements for the selected runtime and CPU. It does not bundle all companion
architectures. An ARM64/Jetson artifact does not become an offline amd64/dev
artifact merely because its release catalog lists those companions; the needed
image bytes must be built/published and explicitly bundled. See the existing
[bundle behavior](BUILD_AND_REPLAY.md#builder-and-publication) and
[onboard replay documentation](ONBOARD_REPLAY.md).

A proposed offline distribution would contain the Windows launcher, controller
image archive, baked artifact, and a manifest of image identities/checksums and
compatibility requirements. Recordings remain separate inputs, optionally shipped
alongside that distribution.

The native Windows bootstrap would load and verify the controller image first
using Docker, so bootstrapping does not itself require a registry connection. Once
the controller can access the artifact and shared workspace, run the artifact's
existing `ensure-images.sh`/`load.sh` verification before invoking its bundled Rig
for replay. Preserve content aliases, architecture checks, exact image identity
validation, and the prohibition on pulling missing images in bundled replay.
Docker Desktop remains an installed prerequisite, not part of this offline payload.

## 5 Release packaging

Start with a Windows x64 ZIP preview. Pair the launcher with an immutable controller
image digest; offer a controller archive for offline use. Publish the controller
as a Linux OCI image and keep replay/service bundles independently versioned and
digest-pinned. The release manifest must distinguish the controller version from
the artifact's bundled Rig version. An installer or WinGet package can follow
after installation, upgrades, and recovery are qualified.

Extend the existing [release workflow](../.github/workflows/release.yaml) only when
this implementation and acceptance gate are ready. It currently emits wheel,
sdist, and Debian assets, with optional Homebrew updates. Container publication
would also require the appropriate registry permissions.

Before publishing Windows preview/RC tags, add explicit prerelease handling. The
current `v*` trigger accepts prerelease-shaped tags, while release creation does
not mark them as prereleases and the Homebrew step does not exclude them. Preview
builds must not update stable/latest distribution channels or the Homebrew tap.

## 6 Acceptance before claiming support

Qualify on an actual Windows x64 machine with WSL/WSL2 disabled and Docker Desktop
using Hyper-V. Add a dedicated Windows/Hyper-V acceptance runner; ordinary Windows
Python tests or Linux container tests alone do not establish this support.

Required checks:

1. Fresh installation and cold image loading, then warm-cache startup, without
   requiring host Python, Bash, or WSL.
2. Import a known bag and camera recording; verify replay timing, synchronized
   outputs, completion, and the browser/viewer networking used by the workflow.
3. Exercise controller-to-daemon mount mapping, Windows paths with spaces, generated
   configs, helper files, run symlinks, and writes from service container users.
4. Interrupt startup and playback; verify owned-process cleanup, saved-session
   inspection, restart/reconnect, teardown, and preservation of unrelated containers.
5. Upgrade and rollback while preserving workspace data and the ability to finish
   an already recorded session.
6. On a clean machine/cache with network access blocked, load the controller and
   bundled replay images and replay successfully. Missing/corrupt images or an
   incompatible architecture must fail clearly without attempting a hidden pull.

Keep supported services, Docker/Windows versions, performance limits, and test
evidence explicit in the eventual release notes. No Windows acceptance has been
performed for this tentative plan.
