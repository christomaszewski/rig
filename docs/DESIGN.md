# rig — design & decision log

## Problem

A vehicle computer (Jetson) runs several heterogeneous sensor/autonomy stacks: a rich GigE Vision camera
service (`gige-vision-service`) and thin in-house ROS 2 nav drivers (`novatel`, `sbg`, and more to come,
all scaffolded from a shared Copier template). Each is its own repo with its own image and a per-sensor
launcher. We need one machine-level tool to bring the whole vehicle up/down/observe — without coupling to
any one sensor type, and without re-implementing per-stack logic.

## Core decision: a loop + a manifest that delegates

`rig` is deliberately thin. The hard, stack-specific work (capture/timestamp/record/shm for the camera;
lifecycle + params + transport for the nav drivers) already lives in each service's launcher. `rig`:

1. reads `vehicle.yaml` (which sensors, order, fleet ROS env) + `services.yaml` (where each repo lives),
2. for each sensor, reads the repo's `deploy.yaml` and invokes `<launcher> <config> <verb>`,
3. owns only what is genuinely vehicle-wide (below).

This keeps the one-way dependency clean: **a service never imports or knows about rig.** New services
(a lidar, an autonomy stack, a ported third-party driver) join by adding a launcher + a `deploy.yaml`,
not by changing rig.

## The launcher contract (rig-compatible)

A launcher must: expose `up/down/status/logs/config` on **one** config; accept a config at an **arbitrary
host path**; derive **all identity from the config's `name`**; **honor** `ROS_DOMAIN_ID`/`RMW_IMPLEMENTATION`
from the environment; observe **stdout/stderr discipline** (machine output — `status`→`ps --format json`,
`config` — on stdout; human lines on stderr, so rig parses clean JSON); and ship a `deploy.yaml`.

`gige-up` is the exemplar; the Copier template (`boilerplate`) emits a `<device>-up` that satisfies the
same contract for every thin driver. rig does **not** reshape services toward one template — it adapts to
each via `deploy.yaml`'s `verbs` map (e.g. gige-up takes compose subcommands, so `status → ps`).

## What rig owns

- **Instance-`name` uniqueness — the top correctness check.** Identity (compose project, external volumes,
  ROS namespace, ports) all derive from `name`; rig rejects a manifest with duplicates before doing
  anything. It also cross-checks each manifest entry against the config's own `service`/`name`.
- **Bring-up order: producers → consumers.** Ascending `order` for `up`, reversed for `down`, so shm/topics
  exist before consumers attach. Consumers are best-effort/retry regardless (`restart: unless-stopped`).
- **Fleet ROS env.** rig exports one `ROS_DOMAIN_ID` + `RMW_IMPLEMENTATION` before each launcher call; the
  launchers pass them into their containers, so every stack shares one DDS graph. Topics are namespaced
  `/<name>/…`.
- **Status/health.** rig calls each launcher's `status` (`ps --format json`) and rolls a project up to one
  row: healthy iff every *healthchecked* container is healthy and all are running (a plugin without a probe
  doesn't drag the sensor to "unknown"). ROS `/diagnostics` aggregation is a planned second layer.
- **Lifecycle/cleanup.** Restart/boot/teardown are the substrate's job (Docker Compose now;
  systemd/Quadlet/k3s later). External volumes survive `down` by design (a consumer may still be attached);
  `rig down --purge` removes the `deploy.yaml`-declared `external_volumes` on **final** teardown only —
  `docker volume rm` refuses an in-use volume, which is the safety we want.
- **Resource budgets (advisory).** `rig doctor` warns about `/dev/shm` aggregate and NVENC session budgets;
  it never blocks (rig treats driver configs as opaque).

## Decisions carried from gige-vision-service (do not re-litigate)

- **Docker Compose per sensor** (one project each); delegate supervision to the substrate. Rejected: a
  Python supervisor driving the Docker socket.
- **Static compose, selected + parameterized** by each launcher; never generate compose.
- **shm is host-level**: an external named volume (`gige_<name>_sock`, the socket/*address*) + `--ipc=host`
  (the `/dev/shm` frame *data*). A consumer needs both. Rejected: podman/k8s pods (pod-scoped IPC walls shm
  off from other stacks).
- **Host networking** for sensor discovery; per-instance ports/topics namespaced by `name`.
- **One ROS distro fleet-wide** (Lyrical) + one RMW, so all stacks interoperate on one graph.

## Status & roadmap

Implemented: manifest/catalog/descriptor loaders with validation; dispatch with fleet env + dry-run +
ordering; `status` roll-up; `doctor`; `up/down/--purge/logs/config`. Validated against the three real
launchers (gige-up, novatel-up, sbg-up): correct ordering, env propagation, params render, and status.

Open items (see the project plan): host-facing **port-clash** extraction for list-structured configs (the
`host_ports` path syntax exists; the gige WebRTC port could also be reported by a launcher `ports` query);
a **dev-vs-prod** affordance (gige's `--dev` vs config-driven replay for thin drivers); ROS `/diagnostics`
as the second health layer; boot-time bring-up via a systemd unit; and submodule pinning for deployment.

See **`docs/ROADMAP.md`** for the detailed spec of the next item — **config overrides & reusable profiles**
(one mechanism for multi-instance sharing *and* per-run data-source overrides) — and the **SIL/HIL** model
(per-sensor source × per-run footprint).
