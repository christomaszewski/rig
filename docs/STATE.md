# rig — project state & handoff (resume here)

> Snapshot for picking the project up cold in a new session. Read this first, then `CHEATSHEET.md` /
> `RUNBOOK.md` (deploy steps), then `DESIGN.md`/`ROADMAP.md` for rationale. As of: rig **v0.1.45**,
> branch **`main`**, 189 tests passing (`for t in tests/test_*.py; do python3 $t; done`).
> **The package-registry layer is IMPLEMENTED** (v0.1.35–v0.1.45; plan doc `rig-registry-plan.md`,
> untracked by request; design summary in DESIGN.md): registry model + `registry
> init|validate|index`, live seed registry
> **https://github.com/christomaszewski/rig-registry-public**, `~/.rig` client
> (`setup`/`add`/`sync`, ordered priority, degrade-not-fail), CLI noun taxonomy with permanent
> aliases, one extended `rig.lock`, `pkg install` (+ `rig add` porcelain: path | name | registry
> ref | `sensor:<id>`) with vendored-at-pin self-contained deployments, the working-copy pipeline
> (`config/.pins/` anchors, `config diff` attribution, `pkg upgrade` three-way), ordered overlay
> bindings (local beats overlays), `pkg promote` (overlay/profile/suite; write+validate, git
> publish stays manual) and atomic suite install with rollback. E2E-verified live: fresh
> `RIG_HOME` → setup → sync → `add public/zenoh-router` + `add sensor:zr30` → doctor 0 errors.
> Remaining: M7 distribution (deb/brew/release automation) — in progress; `Sensor`→`Instance`
> dataclass rename deferred (cosmetic, large mechanical diff). Tool at `/Users/ckt/ws/bringup`; run-from-source
> `./rig <verb>`.
> **Remote: https://github.com/christomaszewski/rig (public)** — Actions runs the test suite on push/PR.
> camera-service has a `rig certify` CI gate (launcher-contract) via PR #36 (+ the cam-up verbatim-pull-tag
> fix); the walkthrough's camera-service checkout sits on that PR branch until it merges. dashboard has NO
> GitHub remote (origin = local /Users/ckt/ws/dashboard) — no CI gate possible there yet.

## TL;DR — where things are

**The full 4-stack deployment is UP on the Orin** (2026-06-09): all 9 containers healthy, dashboard serving,
USB camera streaming + recording, zenoh mesh connected. The cross-repo work that was open here is **done and
landed** (camera-service #25–#33, dashboard image/tag/caddy fixes). What's left: physical-world verification
(RTSP source was powered off; webrtc video in a real browser) and the small follow-ups listed below.

**The live test:**
- **Dev box:** this Mac. Local registry at **`192.168.8.149:5000`** (compose-managed container
  `docker-registry-registry-1`; Docker Desktop trusts it via `insecure-registries`). Workspace
  `/Users/ckt/ws/rig-walkthrough/` (siblings: `rig/`, `camera-service/`, `dashboard/`, `test-vehicle/` = the
  deployment).
- **Vehicle (Orin):** ssh host `orin` (10.160.1.21, user `uxv`). `vehicle: orin-test-vehicle`,
  `vehicle_id: 1`, `rmw_zenoh_cpp`, `images.tag: jp7`, `images.registry: 192.168.8.149:5000`,
  `data_dir: /home/uxv/logs`. Artifact `test1` extracted + **running** at `~/ws/test1` (brought up via
  `./run.sh up`, compose-only form).
- **Stacks (4):** infra `zenoh-router` (order 0) + `dashboard` (order 5); sensors `cam_usb` + `cam_rtsp`
  (camera-service). Configs enable **both bridges** per camera (ros2-bridge + webrtc-bridge w/ NVENC H.264,
  signalling ports 8446/8445), recording on (`/data/recordings` → RIG_DATA_DIR), USB at 1080p MJPEG
  (stable `/dev/v4l/by-id/...NexiGo...` path), RTSP at 4K (ZR30 at `rtsp://10.160.1.80:8554/main.264`).
- **Verified up (2026-06-09):** all 4 stacks / 9 containers, compose projects `<name>-vehicle-1`; dashboard
  HTTP 200 on :8080, ws :10000, webrtc signalling :8445/:8446 listening; cam_usb 30fps no drops, recordings
  growing on the host; router + dashboard-zenoh sidecar connected. cam_rtsp healthy in its designed
  reconnect loop — the physical camera was **powered off** during the deploy; it self-recovers when on.

## What rig is (one paragraph)

A vehicle-level orchestrator — "a loop + a manifest" that delegates bring-up to each service's own
`<service>-up` launcher. One-way dependency (a service never imports rig; rig learns it via `rigging.yaml` +
the launcher CLI). rig owns the cross-cutting concerns: name-uniqueness, ordering, fleet env, status,
deployment artifacts. See `DESIGN.md`.

## The fleet env rig injects into every launcher (the contract)

`ROS_DOMAIN_ID`, `RMW_IMPLEMENTATION`, `VEHICLE_ID`, `RIG_IMAGE_REGISTRY`, `RIG_IMAGE_TAG` (e.g. `jp7`),
`RIG_DATA_DIR` (recordings/logs host dir), and per-call `COMPOSE_PROJECT_NAME=<name>-vehicle-<vehicle_id>`.
A launcher's compose opts into each (`${RIG_IMAGE_REGISTRY:+…}`, `:${RIG_IMAGE_TAG:-latest}`,
`${RIG_DATA_DIR}/…`), and a launcher honors `COMPOSE_PROJECT_NAME` by **not** passing `-p`.

## rig capabilities (all built/tested — bullets carry their own version tags)

- Lifecycle `up/down(--purge)/status/logs/config/doctor`; tiered ordering (infra → sensors → autonomy;
  down reversed, so autonomy stops FIRST); tier-aware output ("2 sensors + 2 infra + 1 autonomy").
- `vehicle.yaml`: `vehicle_id` (→ ROS domain + `VEHICLE_ID`), `ros{rmw,distro}`, `images{registry,tag}`,
  `data_dir`, `infra:`, `sensors:`, `autonomy:`. Config overrides + nameless profiles (deep-merge).
- `doctor`: one-distro check, launcher-present, host-port clash (enabled-aware `plugins[name=x,enabled=true].params.port`
  selector), **non-ROS-safe name warning** (hyphens → invalid ROS namespace; sensor + autonomy tiers),
  zenoh-router guardrail, autonomy-with-no-enabled-sensors warning ("a brain with no eyes").
- `rig build [-j N] [--registry] [--tag]`: per-unique-service **build** (`rigging.yaml build:`) + **mirror**
  (`mirror:`, via `docker pull/tag/push` so a plain-HTTP registry works). Concurrent with `-j`.
- `rig vendor` (copy launch surface, files **and dirs**), `rig bake [--registry] --tag` / `rig unbake`:
  tagged artifact = resolved configs + complete vehicle.yaml + vendored surfaces + rig + a **compose-only**
  form (build-stripped, registry-pinned, runs on just Docker). Built images digest-pinned; **mirrored
  images kept as registry tags** (multi-arch digests are fragile). `run.sh` prefers the compose-only form.
- `rig init` + cwd deployment detection (tool and deployment can be separate dirs).
- Shared infra services: moved to **rig-infra** (https://github.com/christomaszewski/rig-infra) —
  zenoh-router / ros2-bag-logger / ros1-bag-logger + the `fleet-ros` base image; rig's `templates/` is a
  deprecation stub for one version (v0.1.28).
- `rig pull` + baked `pull.sh` (v0.1.19): pre-pull every stack's images with NO container changes — prime
  the vehicle's cache while the registry is reachable, then run offline; safe against a live deployment.
- v0.1.34: **`--tier infra|sensor|autonomy` on `add` and `rigify`** — `add --tier` overrides the
  service's DECLARED tier for one deployment (section placement + the enabled-vs-menu behavior follow;
  a note nudges toward declaring it in the repo's rigging.yaml when it's yours — the vehicle.yaml
  SECTION is the runtime authority, the descriptor tier only routes the automation); `rigify --tier`
  emits an uncommented `tier:` declaration in the generated rigging.yaml instead of the commented hint.
- v0.1.33: **`rig fetch`** — unblocks the HAND-AUTHORED workflow (init → write vehicle.yaml +
  services.yaml yourself → fetch). Reads vehicle.yaml RAW (the deployment is unloadable until configs
  exist — that's the point): every row whose `config:` is missing gets the routed service's first
  example copied TO THAT PATH as a nameless profile (row stamps the name; a surviving example name that
  differs WARNs with both names); routed-but-unreferenced services get material into
  config/{infra,sensors,autonomy}/ with a suggested row ECHOED, never written. Never edits manifests,
  never overwrites, per-route failures reported not fatal, ends by saying whether the manifest now
  loads. (`pull` fetches images; `fetch` fetches configs.) Verb taxonomy complete: rigify makes a repo
  compatible → certify grades it → init/add/fetch wire deployments → doctor grades the vehicle.
- v0.1.32: **`rig rigify <dir> [--service NAME]`** — retrofit rig-compatibility onto EXISTING software
  (`rig_cli/rigify.py`; deployment-independent like `certify --repo`). Generates only-if-absent, never
  overwrites: rigging.yaml (tier/ros_distro/build/mirror/host_ports/external_volumes as COMMENTED
  hints), a contract-correct `<svc>-up` launcher (COMPOSE_PROJECT_NAME honored, name-from-config,
  stderr discipline), `config/<svc>.example.yaml`, and a compose skeleton only when none exists. A
  bounded read-only analysis seeds real values: found composes are `-f`-pre-wired into the launcher +
  launch_surface, their host ports / external volumes / literal images / build sections become the
  hints, ROS launch files seed the command suggestion, entry scripts are called out. Acceptance
  (tested, incl. against real `docker compose config`): a rigified bare dir passes `rig certify` with
  ZERO hand edits. The onboarding arc is now rigify → certify --repo → add.
- v0.1.31: **`rig add <name|path>`** — wire one more service into an EXISTING deployment (init's
  accelerators are init-time only). Same resolution as `--infra` (path, or bare-name one-level
  workspace scan), same asymmetry (infra = ENABLED row, zenoh-router pinned order 0; sensor/autonomy =
  commented menu row + nameless-profile config copy). The ONE place rig edits operator-owned files —
  guarded: parse-first duplicate refusal, line-append only into generated block shapes, re-parse +
  manifest reload after writing with restore-on-failure, and a paste-ready snippet fallback for
  hand-authored flow-style files (never a mangled manifest).
- v0.1.30: `--infra` + `--discover` over one workspace no longer prints "duplicate service" for the dirs
  --infra just wired (same-path rediscovery = the designed overlap, quiet skip); the warning is reserved
  for two DIFFERENT dirs claiming one service key, and now names both paths.
- v0.1.29: **`ros.distro` → `ROS_DISTRO` at build time** — `rig build` exports vehicle.yaml's
  `ros.distro` into every `build:` command's env (shown in the build/dry-run lines), so rig-infra's
  fleet-ros bakes the fleet's distro without the operator remembering an env var. The
  vehicle-vs-services distro disagreement in doctor is upgraded WARN → **ERROR** (ros.distro is now
  load-bearing: a mismatch means the next build bakes images the services don't target), and
  `rig build` prints an inline WARNING per mismatched service at the moment it bakes.
- v0.1.28: **infra spin-out** (ROADMAP §3e) — the bundled templates moved to the sibling **rig-infra**
  repo with an added `base/` **fleet-ros image** (`ros:<distro>-ros-base` + rmw-zenoh-cpp +
  rosbag2+mcap; `base/build.sh` follows the rig build contract). Opinionated defaults: router =
  `fleet-ros:${RIG_IMAGE_TAG}` running `rmw_zenohd` (rmw-family version-match by construction), ros2
  bag logger = `fleet-ros` (decoupled from ros2-bridge; ~1 GB on camera-less vehicles); both declare
  `build: {command: ../base/build.sh, images: [fleet-ros]}` so certify enforces build/pull agreement.
  rig side: `init --infra` takes a path or a bare name resolved by a one-level workspace scan
  (ambiguity errors; bundled fallback warns DEPRECATED); `--discover` descends one level into
  collection repos (skips `var/`/hidden); `templates/` is a stub for THIS version only (README pointer;
  after deletion, old services.yaml paths fail with a rig-infra pointer via load_descriptor); rig CI
  dropped the live-template certify steps (rig-infra CI certifies 3/3 + the router_config path).
- v0.1.27: **`autonomy:` tier** (ROADMAP §3d) — third manifest tier for graph CONSUMERS (planners, SLAM,
  perception). Hard ordering partition infra=0 → sensors=1 → autonomy=2 regardless of per-entry `order`;
  `down` reverses, so the decider dies before its eyes. `rigging.yaml` accepts `tier: autonomy`;
  `init` scaffolds `config/autonomy/` and `--discover` routes autonomy repos to an `autonomy:` menu
  section (MENU only, never auto-enabled — no `--autonomy` wiring flag by design: autonomy arrives from
  real repos). Baked vehicle.yaml preserves all three tiers; compose-only up.sh/down.sh hold the
  partition. Ordering stays a courtesy — consumers must retry; tier gating (`up --wait-healthy`) is the
  future health-verb payoff this structure attaches to.
- v0.1.26: baked `pull.sh`/`up.sh` **alias digest-pinned images back to their tags** (`docker tag <@sha>
  <:tag>`, best-effort) — a digest pull doesn't create the tag name, so the launcher path (tag refs:
  `up --run`, field `./rig up`) used to re-pull online and FAIL offline on a digest-primed cache.
  Registry mode only (bundle mode keeps tags throughout).
- v0.1.25: **run directories** (ROADMAP §3c) — one session, one folder under `data_dir/runs/`, `current`
  symlink (RELATIVE target, resolvable in-container), provenance manifest (`ended:` ⇔ sealed ⇔ safe to
  sync). Verbs: `new-run [label]` / `end-run [--force]` / `runs` / `up --run L` / `down --end-run`; run
  header in `status`. `up` ensures (`_auto` safety net), never rotates; rotation/seal refuse while this
  manifest's stacks run (writers pin their run at process start). bake emits sh parity
  (new-run/end-run/runs.sh, up.sh ensure-guard, status header); bag-logger templates adopted
  (`current/bags/<name>`, flat fallback). camera-service adoption = pending cross-repo PR (recordings →
  `current/recordings/<name>`). Validated live end-to-end incl. the running-stack guard refusal.
- v0.1.24: **`rig init` accelerators** — the target dirname seeds `vehicle:`; `--vehicle-id N`;
  `--infra <template>` (repeatable) fully wires a bundled template (config + catalog + ENABLED entry,
  router pinned order 0); `--discover [DIR]` scans a workspace for repos with a `rigging.yaml` and
  populates services.yaml (routing name from the DESCRIPTOR — catches `sbg_driver`→`sbg`) + copies
  examples as **nameless profiles** (top-level `name:` commented; the manifest entry stamps it) + writes
  a commented-out vehicle.yaml MENU (never auto-enabled: repo presence ≠ hardware presence). New optional
  rigging.yaml fields: `tier: infra|sensor`, `examples: [...]` (also the default `--config` for
  `certify --repo`). Acceptance (tested): `init --infra ...` → `rig doctor` green with zero edits;
  uncomment a menu line → still green.
- v0.1.22: **bag-logger templates** (`templates/ros2-bag-logger/`, `templates/ros1-bag-logger/`) — shared
  infra services that record the ROS telemetry graph to `${RIG_DATA_DIR}/bags/<name>`. Config schema:
  `record.mode: all|allow|exclude` (+ `exclude_images`, default true — the cameras' raw `image_raw` is huge
  over ROS and already recorded compressed at source), `output` storage/compression/split. A testable
  `tools/bag_cmd.py` maps config → `ros2 bag record`/`rosbag record` argv, rendered to `var/run/<name>/
  record.sh` (bake-captured, restart-safe via runtime stamp). Default image reuses a driver image (has
  rmw_zenoh + rosbag2). Both certified in CI. ROS1 is for ROS-1 fleets (needs a roscore; rig's fleet env is
  ROS2-shaped) — **structurally complete but unrun against a live ROS1 master.** `services:` (service-call
  recording) is a documented FUTURE knob, ignored for now. Place in `infra:` at order ~1 (just after the
  router) so it records from startup. NOT YET run live on the Orin (would add a 5th stack; gated on the
  registry re-point).
- v0.1.21: `rig bake --bundle-images` — docker-saves the image set INTO the artifact (tag refs + artifact
  sha256 as integrity; digests recorded as audit metadata; `up.sh` self-loads when refs are missing,
  `run.sh load` forces it). Plus **parent provenance**: a re-bake inside an extracted artifact stamps
  `parent:` (tag/created/rig_version/sources) into metadata — field-day chains (`test2` → `day3-final`).
  Validated live: a bundled bake of the running deployment succeeded with the registry UNREACHABLE.
- v0.1.20: `rig init` scaffolds `config/infra/` alongside sensors; the zenoh-router template takes an
  **inline `router_config:` mapping** (instance YAML → rendered `var/run/<name>/zenohd.json5` → `-c` +
  `ZENOH_ROUTER_CONFIG_URI`; bake captures it like any rendered file — inline only, paths don't bake);
  rig's CI certifies the template (reference launcher) on both config paths. Also validated: **re-bake
  inside an extracted artifact works** (field-state capture) — provenance stamping landed in v0.1.21.
- `rig certify [name…|--repo R --config C] [--emit F|--diff A B]` + `rig doctor --deep` (v0.1.18): the
  launcher contract as executable checks (poison env; project-name/registry/tag/ros-env/determinism/
  identity/discipline/status). `--emit` on two hosts + `--diff` proves `config` output host-independence.
  On its first live run it caught cam-up + dash-up overriding `COMPOSE_PROJECT_NAME` (masked until then by
  the baked scripts' explicit `-p`) — both fixed + re-certified, 0 errors.

## Deploy recipe (current)

```
# Dev box (Mac): trust the registry in Docker Desktop (insecure-registries: ["192.168.8.149:5000"])
rig build -j 3                       # build + push + mirror images
curl -s http://192.168.8.149:5000/v2/_catalog
rig bake --tag testN                 # compose-only, pinned, complete vehicle.yaml
scp var/artifacts/testN.tar.gz orin:/tmp/
# Orin: MERGE insecure-registries (KEEP nvidia runtime) into /etc/docker/daemon.json, with the :5000 PORT;
#       restart docker; tar xzf; ./run.sh up   (uses the compose-only scripts; pulls by digest/tag)
```

## OPEN ITEMS

The big cross-repo batch from the previous handoff is **all landed** (verified live 2026-06-09). The
recurring principle held: **a launcher's `config` output must be host-independent** so a dev-box bake is
correct for the target. For the record — camera-service: `RIG_IMAGE_TAG` as platform (#26), v4l2 device
mapping (#25) + host-independent config (#29), `RIG_DATA_DIR` recordings (#27), numeric-coerce + fail-fast
(#28), webrtc H.264 level/profile + NVENC rank (#30–#32), 1080p example (#33). dashboard: built
`dashboard-web` image (Caddy + bundle baked in), `RIG_IMAGE_TAG`-tagged pulls, `vehicleHost` signalling fix,
reworked `rigging.yaml` (infra, no "BUILD phase" framing). `COMPOSE_PROJECT_NAME`: genuinely honored as of
2026-06-09 — `rig certify` caught cam-up (`tools/sensor_env.py`) and dash-up overriding it (the baked
scripts' explicit `-p` had masked this; an un-baked `rig up` would have made orphan projects); one-line
fallback fixes in both repos, re-certified clean.

Still open:
1. **Live-deploy verification (physical world):** power the RTSP camera (ZR30 at `10.160.1.80`) and watch
   cam_rtsp self-recover; open `http://10.160.1.21:8080` in Chrome and confirm both webrtc streams render
   (NVENC H.264). One startup-time `listConsumers` parse warning in the webrtc signalling log is a known
   benign dialect probe.
2. **boilerplate `<device>-up` (novatel/sbg launchers):** honor `COMPOSE_PROJECT_NAME` (drop `-p`, standalone
   fallback) — same one-liner the other launchers got. Find + prove it with
   `rig certify --repo ../novatel --config <example.yaml>` (the project-name check fails until fixed).
3. **§3e infra spin-out — repo + rig side DONE (v0.1.28); migration steps 3–4 remain:** update the
   live deployments' services.yaml (test-vehicle + walkthrough clones: `../rig-infra/<svc>`), rebuild
   the registry with `fleet-ros` (`rig build`) before the next bake, **batch the standing chore: put
   dashboard on GitHub** while doing repo work, and NEXT version delete the `templates/` stub (its
   pointer error in `load_descriptor` is already in place; move `tests/test_bag_logger.py`'s
   `templates/` import to a rig-infra checkout or drop it then). (§3d `autonomy:` tier shipped in
   v0.1.27.)
4. **rig follow-ups (`ROADMAP.md`):** health verb + reconciler/systemd (top open items), OCI artifact
   format, fleet mode (one artifact, N vehicles). (`bake --bundle-images` shipped in v0.1.21.)

## Gotchas learned the hard way (deployment debugging)

- **Registry trust is needed on BOTH machines, with the port.** Mac (Docker Desktop `insecure-registries`)
  for push; Orin (`/etc/docker/daemon.json`, **MERGE** — keep the `nvidia` runtime!) for pull. A bare IP
  (`192.168.8.149`) does NOT match a registry on `:5000` — use `192.168.8.149:5000`.
- **`buildx imagetools` ignores `insecure-registries`;** rig mirrors via `docker pull/tag/push` instead.
- **The baked artifact runs the compose-only form, not rig+vendored-launchers** (those have `build:` sections
  and would try to build from absent source). `run.sh` prefers `up.sh`/`down.sh`/`status.sh`.
- **Mirrored multi-arch image digests are fragile** (index vs per-arch manifest, re-push churn) → rig keeps
  them as registry **tags**; only built single-arch images are digest-pinned.
- **`rig build` and `rig bake` must see one consistent registry state**, and the registry must **persist**
  (`-v registry-data:/var/lib/registry`) so digests survive until the vehicle pulls.
- **ROS 2 names allow no hyphens** — `cam_usb`, not `cam-usb` (the camera namespaces a node by the name).
- **`images.tag: jp7` is the deployment tag** — platform-specific services (camera) use it; platform-agnostic
  ones (dashboard) must still pull the *same* tag or they won't find their image.

## Resume checklist

1. The vehicle is **already up** — check it first: `ssh orin 'cd ~/ws/test1 && ./run.sh status'` (or
   `docker ps`). Dashboard: `http://10.160.1.21:8080`.
2. Finish the physical verification (open item 1): RTSP camera power, webrtc streams in a browser.
3. To iterate: edit configs/repos in `/Users/ckt/ws/rig-walkthrough/` → `rig build -j 3` (if images changed)
   → `rig bake --tag testN` → `scp` to the Orin → `tar xzf` → `./run.sh up`. Verify
   `curl -s http://192.168.8.149:5000/v2/_catalog` between build and bake.
4. Teardown when done testing: `ssh orin 'cd ~/ws/test1 && ./run.sh down'`; stop the dev-box registry.
