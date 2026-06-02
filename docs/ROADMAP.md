# rig — roadmap

## 1. Config overrides & reusable profiles (next)

### Motivation
Two needs, **one mechanism**:
1. **Multiple instances of one sensor type** that differ only in a physical id (camera serial, serial
   `by_id`, receiver IP) — without copying a whole config per instance.
2. **Flipping a sensor's data source per run** (e.g. replay GNSS while everything else runs live on real
   hardware — see §2) — without editing its deploy config.

Both are "a shared base + a small per-instance/per-run patch." This is a **rig-only** feature: the launchers
and the launcher contract do not change.

### Manifest schema
A sensor entry gains an optional `overrides:` mapping. `config:` may point at a full **named instance
config** (today's behavior) OR a **nameless profile** (a config with `service:` but no `name:`):

```yaml
# config/sensors/camera.profile.yaml   — reusable profile, NO `name`
service: gige-vision
camera: { fake: false, pixel_format: Mono8, frame_rate: 20.0, ptp_enable: true }
recording: { enabled: true }
```
```yaml
# vehicle.yaml
sensors:
  - { name: cam_front, service: gige-vision, config: config/sensors/camera.profile.yaml,
      overrides: { camera: { camera_id: "Lucid-2448-AAA" } } }
  - { name: cam_rear,  service: gige-vision, config: config/sensors/camera.profile.yaml,
      overrides: { camera: { camera_id: "Lucid-2448-BBB" } } }
```

### Resolution pipeline (rig-side, per sensor)
1. Load the base config at `config:`.
2. **Name**: if the base has `name`, it must equal the manifest `name` (current cross-check); otherwise rig
   injects the manifest `name`. `service` must be present in the base and match the manifest `service`.
3. **Deep-merge** `overrides` onto the base (semantics below).
4. **Render only when needed**: if the base already has the matching `name` AND there are no `overrides`,
   pass the original file path unchanged (no render — keeps the common case file-for-file). Otherwise write
   the merged document to `var/rendered/<name>.yaml` and pass *that* path to the launcher.
5. The launcher receives a complete, named config exactly as today and never knows templating happened.

rig reads only `service` + `name`; the merge is a mechanical key overlay, so rig stays **schema-opaque** —
it never interprets what `camera_id` (or anything else) means.

### Merge semantics
- **Mappings**: recursive deep-merge; override keys win.
- **Scalars**: override replaces.
- **Lists**: **replace the whole list (v1)**. Predictable, and it covers the real cases (ids / IPs /
  `connection` are scalars-in-maps, not lists). *Keyed* list-merge — match items by their `name` field so you
  can tweak one `plugins:` entry without restating the list — is a v2 enhancement.
- **Deletion**: an override value of `null` deletes that key (so a profile default can be removed).

### Where rendered configs land
`var/rendered/<name>.yaml` (gitignored, mirroring the launchers' own `var/run/`). Overwritten each run.
`rig --dry-run` / `rig config` print the rendered path so a run is inspectable.

### Cross-checks & doctor
- `service` required and matches the manifest; instance `name` unique across the vehicle (unchanged).
- A nameless profile is valid only when referenced by a manifest row that supplies the `name`.
- `rig doctor` surfaces the resolved per-instance id/source so a run is self-documenting, and warns on
  dangerous combinations (e.g. a replay/sim source under a vehicle footprint — see §2).

### Phasing
- **v1**: per-sensor `overrides` (dict deep-merge, list-replace, `null`-delete) + nameless profiles +
  render-to-staging. Rig-only; no launcher changes.
- **v2**: keyed list-merge; run-level override layers (apply one patch across many sensors, for §2).

### Open decisions
- Confirm **list = replace** for v1 (vs keyed-merge now).
- **Layering**: single per-entry override (v1) vs a profile + shared + per-entry override stack (later).
- Keep **both styles** (named instance files AND profiles+overrides) indefinitely? (Recommended: yes —
  named files stay valid and are simplest for one-offs.)

### Non-goals (v1)
No launcher changes; no semantic interpretation of config bodies by rig; no simulator integration.

---

## 2. SIL / HIL via per-sensor source × per-run footprint (related)

Not modeled as enforced vehicle-wide modes. Two independent axes:
- **Data source** — per *sensor*: live | replay | sim (a config / override concern, §1).
- **Footprint** — per *run*: vehicle | bench | laptop (images / runtime / net; gige's existing `--dev`).

"HIL test" = real-hardware footprint with a chosen per-sensor source mix (e.g. **replay GNSS while the
algorithm stack runs live on the real Jetson** to characterize compute/memory). Replay runs *through the
real driver* (file transport), so the load is faithful.

rig owns: the per-run footprint token (passed to launchers), **surfacing/validating the source mix** (refuse
or warn a replay/sim source under a vehicle footprint), and **source-aware doctor** (check "receiver
pingable" only for live stacks; "capture exists" only for replay stacks). Launchers own the mechanics
(mounting the replay capture; the footprint image/net swap). Named presets (`deploy`/`hil`/`sil`) are
overridable shorthands, never straitjackets.

**Caveat to design for**: under mixed live/replay, **time-base coherence** breaks for any node that *fuses*
sources (replayed GNSS timestamps vs live camera PTP time). Sound for resource/perf characterization; for
fusion fidelity, needs a coherent clock (`use_sim_time` + paced replay, or a simulator generating all
sources on one clock). Replay (single recorded source, own time) vs sim (generated, can be multi-source
coherent) is the real fidelity fork.

## 3. Other tracked items
- **Boot-time bring-up**: a systemd unit running `rig up` (Compose handles per-stack restart thereafter).
- **ROS `/diagnostics`** as the second health layer in `rig status`.
- **Host-facing port-clash** extraction for list-structured configs (gige WebRTC port), via the
  `host_ports` path syntax or a launcher `ports` query.
- **Submodule pinning** of the service repos under `services/` for deployment.
- **gige `deploy.yaml`**: add `external_volumes: ["gige_{name}_sock"]` so `rig down --purge` GCs it.
