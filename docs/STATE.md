# rig — project state & handoff (resume here)

> Snapshot for picking the project up cold in a new session. Read this first, then `CHEATSHEET.md` /
> `RUNBOOK.md` (deploy steps), then `DESIGN.md`/`ROADMAP.md` for rationale. As of: rig **v0.2.32**,
> branch **`main`**, 597 tests passing (`for t in tests/test_*.py; do python3 $t; done`).
> **v0.2.32 (2026-08-27) — graph topology as a run artifact** (ROADMAP §15; plan
> `rig-graph-plan.md`, contract `~/ws/infra/rig-graph-capture-handoff.md`): rig-infra v1.7.0's
> graph-snapshotter sidecar (in ros2-bag-logger, profile-gated by the logger config's `graph:`
> block) records append-only change-deduped EPOCH files — per-node pubs/subs/service
> servers/clients with validity windows — into `<run>/graph/<name>/`; rig v0.2.32 ships the READER
> (`rig_cli/graph.py`, pure YAML — rig stays ROS-free): `rig graph [run] [--check]
> [--contract INSTANCE] [-o FILE]` derives union/instance-grouping at read time (no union at
> seal — one path for sealed/unsealed/crashed runs), plus the rigging `interface:` block
> (publishes/subscribes/provides/requires; relative = instance-ns, absolute = shared-bus) with
> WARN-only declared-vs-observed checks both directions; `--contract` prints the scaffold from
> observation, never auto-edits a rigging. Plumbing (rosout, parameter/type-description services)
> recorded raw, hidden from derived views. NEXT: the replay arc (`rig-replay-plan.md`, renumbered
> v0.2.33/34 + rig-infra v1.8.0) consumes these epochs as its topic selector.
> **v0.2.31 (2026-08-27) — docker log capture at seal** (ROADMAP §3c): `down --end-run` now saves
> `docker logs --timestamps` (stderr merged — one file reads like the terminal) from every container
> of the deployment's compose projects into the sealing run, `runs/<id>/.rig/logs/<sensor>/
> <container>.log` (under `.rig/` so a data kind named `logs` under `current/<kind>/` can't collide).
> Capture happens in cmd_down BEFORE the down verb dispatches — `compose down` REMOVES the
> containers, their stdout/stderr goes with them, so seal time is too late (standalone `end-run`,
> guarded to run only after teardown, therefore cannot capture). `docker ps -a` per compose project,
> so crashed containers' logs are kept too; a partial down retried later composes (per-file
> overwrite, each capture writes only what still exists). Manifest gains `docker_logs:`
> (`at`/`containers`) when anything was captured. Fail-SOFT like config snapshots — capture never
> wedges `down`; rig-only like the flagged forms (`run.sh down --end-run` routes through the bundled
> rig, so baked artifacts and `fleet down --end-run` get it with no extra plumbing).
> **v0.2.30 (2026-08-27) — the provenance pin-skew tiers**: `rig image audit` consumes the
> rig-infra ≥ v1.6.0 provenance convention (`/opt/fleet-msgs/provenance.yaml` schema v1:
> repo/ref/rev per built interface repo; frozen in `~/ws/infra/rig-msgs-provenance-handoff.md`
> §ADDENDUM) — each `msgs.source` pin is checked against what the declaring service's OWN
> image(s) recorded building. Tiers per contract §A4: file absent → WARN unadopted (names
> `provenance-record.sh`); declared repo missing from a present file → ERROR; ref mismatch →
> ERROR naming both sides; refs equal but service and overlay `rev` SHAs differ → ERROR ("same
> ref, different tree — rebuild the older side": a moved tag or a branch built twice, the drift
> only SHAs can see); `rev: unknown` (vendored) or malformed file (bad YAML/wrong
> version/duplicate normalized repos) → WARN, never ERROR. Repo join per §A3 normalization
> (scheme dropped, scp form rewritten, one `.git` dropped, host lowercased) so https/ssh
> spellings of one repo match. Overlay absent provenance = pre-provenance build WARN (§A2:
> v1.6.0+ overlays always write it, `source: []` when apt-only). This completes the msgs arc:
> prevention (v0.2.28 refusals) → stale detection (v0.2.29) → pin-skew detection (v0.2.30).
> **v0.2.29 (2026-08-27) — the stale-overlay audit** (`rig-msgs-plan.md` §Fast-follow): the overlay
> in the registry is whatever the LAST `rig build` baked — a `msgs:` declaration added or a pin
> bumped since then leaves `up` pulling the OLD fleet-ros-msgs under the SAME tag, and the new
> types silently vanish from bags. `rig image audit` now probes the resolved RIG_MSGS_IMAGE (even
> when no rendered compose pulls it — a BAG_LOGGER_IMAGE override still leaves it the deployment's
> overlay): baked `/opt/fleet-msgs/manifest.yaml` vs the CURRENT union — drift = ERROR naming what
> changed (comparison normalizes repo spellings per the provenance contract §A3 and sorts lists,
> so hand-authored manifests compare by content); each declared `apt` package installed via the
> shared `ros-<distro>-<'_'→'-'>` mapping — missing = ERROR. Absent/unparseable baked manifest or
> unpullable overlay = WARN, never ERROR. The file probe reads manifest + provenance in one docker
> run — the provenance half feeds v0.2.30's pin-skew tiers (contract frozen in
> `~/ws/infra/rig-msgs-provenance-handoff.md` ADDENDUM; rig-infra v1.6.0/`b1b5bea` ships the
> provider side: overlay always writes provenance, `provenance-record.sh` for services).
> **v0.2.28 (2026-08-27) — the msgs overlay** (`rig-msgs-plan.md`; ROADMAP §14; contract:
> `~/ws/infra/rig-msgs-image-handoff.md`, rig-infra `ed94cbc`): rosbag2 can't record a topic whose
> message package isn't in the recorder's image (logs "unknown type", keeps going — bags silently
> missing the fleet's custom types). Riggings now declare `msgs: {apt, source[repo/ref/packages]}`
> (top-level, strict, independent of build:/mirror:) and base providers declare
> `build.msgs_overlay: {command, image}` (trigger option (b) from the handoff). `rig build` unions
> the declarations (same repo at two refs = refusal naming the services, BEFORE anything builds),
> renders the union to a temp file → `RIG_MSGS_MANIFEST`, and runs the overlay right after stage 0
> (FROM `RIG_BASE_IMAGE`; an external images.base gets an overlay too; tag platform-composes
> through the provider's matrix). `fleet_env` exports `RIG_MSGS_IMAGE` exactly like
> `RIG_BASE_IMAGE` (rig-owned/set-or-popped/certify-unset) — rig-infra's logger compose already
> prefers it, so the logger upgraded the moment this shipped. Doctor: OK ref line, conflict
> ERRORs, and WARN on `msgs:` declarations with no overlay mechanism. Empty union = no overlay =
> bare base, by design. Rig-infra follow-ups landed the same day (**rig-infra v1.5.0**, `a64c7c5`:
> `msgs_overlay` declared on the router+logger riggings, the "rig does not export this var yet"
> caveats dropped, registry release auto-pinned zenoh-router/ros2-bag-logger/ros1-bag-logger at
> 1.5.0). Queued as the **v0.2.29 fast-follow** (specced in `rig-msgs-plan.md`): audit's
> stale-overlay check (baked manifest vs current union + declared apt vs dpkg); the
> source-pin-vs-service-image check first needs a provider-side provenance convention.
> **v0.2.27 (2026-08-26) — zsh eval route self-initializes compsys**: macOS ships no default
> ~/.zshrc, so a stock zsh has never run compinit — compdef doesn't exist, the eval'd completion
> script errored at startup and bound nothing. The emitted zsh script now runs
> `autoload -Uz compinit && compinit -i` iff compdef is absent; brew/deb's fpath file route still
> needs the user's own fpath+compinit (HOST_SETUP documents the caveat). Found live on the dev
> Mac minutes after v0.2.26.
> **v0.2.26 (2026-08-26) — TAB completion** (`rig-completion-plan.md`): a hidden `rig _complete`
> engine (intercepted before argparse; introspects the parser ⊕ `_GROUP_VERBS`, so grouped and
> flat spellings complete equally) + `rig completion bash|zsh` emitters. Menu teaches the
> CANONICAL grammar only (legacy flat spellings/aliases parse forever, never suggested); dynamic
> values raw-read vehicle.yaml/services.yaml/fleet.yaml/`~/.rig` index.jsons — instance names,
> pkg refs (kind-filtered for `overlay apply`; `overlay remove` completes the row's BOUND refs),
> registry names, `sensor:<id>`, artifacts, fleet rosters. Fail-soft absolutely (broken state
> completes to nothing, never stderr). bash protocol = COMP_LINE/COMP_POINT parsed in Python
> (colon/equals wordbreak trimming tested, not shell-scripted); zsh gets verb descriptions via
> `--describe`. deb ships both scripts; `setup --shell` writes an `eval "$(rig completion …)"`
> line even when rig is already on PATH. **Release tail: the brew formula needs
> `generate_completions_from_executable(bin/"rig", "completion", shells: [:bash, :zsh])` in the
> tap when bumping.** Deferred by plan: fish, `--platform` values, `@version` completion.
> **v0.2.25 (2026-08-24) — the phantom base race**: registry installs fetched a per-SERVICE clone of
> the collection repo, so zenoh-router + ros2-bag-logger — both declaring rig-infra's
> `../base/build.sh` — resolved it to two paths and every fresh install refused `rig build` as a
> "race" between identical builds. Three fixes: the src cache is keyed by (repo, rev) — services
> from one repo SHARE a checkout (legacy per-service dirs renamed in place, so offline machines
> never reclone); provider dedupe is by CONTENT — script name + git tree hash of the script's dir
> in clean checkouts (the contract: a base script's directory IS its whole build context;
> unprovable = path identity, toward refusing), so even rev-divergent pins dedupe while `base/` is
> untouched; and the refusal names the resolved scripts + revs ("align their source pins") instead
> of printing one command string twice and calling it "different".
> **v0.2.24 (2026-08-24) — rebasing alone doesn't stop skew** (camera-service consumer finding):
> a consumer that plain-`apt-get install`s a package the base already carries silently upgrades it
> (base built earlier, ROS apt repo moved) — docs now mandate `--no-upgrade` for consumer extras;
> audit's skew ERROR is base-aware (diagnoses the reinstall + names the fix when a skewed ref IS
> the resolved base) and the cross-image check widened past ros-*: non-ROS divergence across ROS
> images is ONE summarized WARN (the libtiff6 case — real ABI hazards were being certified as
> "versions agree"), never an error, exit code untouched.
> **v0.2.23 (2026-08-22) — the zenoh guardrail goes both ways**: doctor warns on an ENABLED zenoh
> router under a non-zenoh `ros.rmw`. Load-bearing since v0.2.22 — the router runs `rmw_zenohd` out
> of a base built for the DECLARED rmw, so on a DDS fleet that image carries no zenoh at all: it
> builds clean and dies on `up`. Now said at preflight.
> **v0.2.22 (2026-08-22) — base-provider agreement + RIG_ROS_RMW** (ROADMAP §13): rig-infra's
> adoption of the v0.2.21 contract surfaced two order-dependence holes — providers of one base
> disagreeing on `build.platforms` (composed tag followed descriptor order) or on the build script
> (two `[base]` builds racing for one tag) — both now refused rather than guessed. `ros.rmw` reaches
> build commands as rig-owned `RIG_ROS_RMW`, giving audit's rmw check a prevention counterpart.
> **v0.2.21 (2026-08-21) — one base image per deployment** (ROADMAP §13): `RIG_BASE_IMAGE` from
> vehicle.yaml `images.base` or a `provides: base` rigging staged FIRST (conflicting providers =
> ERROR), `rig image audit` (one ROS distro, the declared rmw installed, shared ros-* versions
> agree), `rig build --no-cache`. Plus a full test-suite audit (6 tranches): 384 → 469 tests, each
> new test mutation-checked, the suite hermetic under a poisoned host environment, and two real
> bugs fixed on the way (same-basename bind collapse in bake; `RIG_TARGET_PLATFORM` leaking into
> builds from a stale shell export).
> **v0.2.14–0.2.20 (2026-08-20/21) — platform targeting, decommission, suite closure, the vehicle
> kind, stale pins as snapshots, marker identity by default** (ROADMAP §7–§12; plan docs
> `rig-platform-plan.md` + `rig-vehicle-kind-plan.md`, untracked):
> - **v0.2.14 platform**: vehicle.yaml `platform:` (a HOST fact; `/etc/rig` may carry it,
>   `rig provision --platform`) → `RIG_TARGET_PLATFORM` + each service's declared
>   `platform.override_env`; services with a `build.platforms` matrix pull `<image>:<tag>-<platform>`
>   (`images.tag` = VERSION only; the platform-valued tag is deprecated-but-working); build passes the
>   composed tag; certify gains the `platform` check; doctor validates platform-vs-matrix.
> - **v0.2.15 `rig cleanup`**: decommission (images + volumes off the host, never containers/data;
>   `cleanup.sh` in artifacts). **v0.2.16 suite closure**: bare service instances → `services:`
>   members; validate enforces overlay coverage.
> - **v0.2.17 kind `vehicle`**: a suite's instance PLAN (template vehicle.yaml; rows drive the install
>   — custom names/order/enabled/tiers, per-row bindings, N instances per profile; identity markers
>   enforced, fleet defaults literal, row refs unversioned; captured only with its suite via
>   `promote --all --suite S --vehicle V`, installed only through it into an EMPTY tree).
>   **v0.2.18**: hand-authored instances captured as adopted PROFILES, gated on `--adopt` (consent is
>   a flag, never a prompt; without it loud skip). **v0.2.19**: stale exact pins are SNAPSHOTS —
>   validate warns, install serves the pin from git history, a service release never blocks
>   suites/profiles that pin it; `pkg outdated` owns currency. **v0.2.20**: `rig init` scaffolds
>   identity MARKERS by default (`--vehicle-id N` pins a single-vehicle literal).
> **The QoL & registry-currency layer SHIPPED** (v0.2.5–0.2.9, plan doc `rig-qol-plan.md`,
> untracked by request; CHEATSHEET §1.5 "the daily loop", RUNBOOK "registry maintainer loop"):
> - **v0.2.5 discovery & inventory**: no-arg `pkg search` = the catalog (+`--kind`/`--registry`);
>   ONE add grammar under both spellings (`pkg add` takes paths/workspace names; dir-AND-registry
>   ambiguity = hard error, `./`/`@ver` escape); `pkg list` = the FULL inventory (path-added +
>   vendored services as local/unpublished rows — the promotion worklist).
> - **v0.2.6 currency**: `pkg outdated` (drift report across all four kinds, FIX column names
>   repin/rebase, exit 1 on drift, `--quiet`, `--registry NAME|DIR`); `registry sync` prints a
>   package-level delta digest on ff-pulls; ONE namespace resolver
>   (`registries.resolve_namespace/current_version_of`).
> - **v0.2.7 `pkg repin`**: advance declared dependency PINS registry-side (profile requires —
>   caret keeps its caret; overlay authored_against re-stamp, fresh-stamps pre-v0.1.59 packages,
>   warn-only vanished-key check via history; suites refresh EVERY member — (v0.2.19: stale pins are legal snapshots; validate warns, install serves them from git history; pre-0.2.19 the registry law pinned
>   in-registry members at head); payloads stay rebase's job; `--dry-run`.
> - **v0.2.8 `pkg save` + the publish tail**: save = update-in-place porcelain (top-of-stack:
>   bound overlay first — delta recomputed against the pre-overlay base, never patch-composed —
>   else pinned profile via the adopt flow; routed services save their code pointer with a
>   "code only" note); `registry pending|push|discard` (promote/* only, system git, `--pr` =
>   PR creation via gh/glab, discard re-anchors the cwd deployment) and `pkg yank <ref> --from`
>   (previous restored from git history / first publish removed) — both run save's inverse:
>   the delta comes back as LOCAL edits, render byte-identical either direction.
> - **v0.2.9 polish**: `pkg info --versions` (git-history enumeration — @old was installable,
>   now discoverable), `pkg upgrade --dry-run` (the REAL sweep, rolled back — full-fidelity
>   preview), docs sweep.
> **`promote --kind service` SHIPPED** (v0.2.4): publish a routed dev checkout's CODE POINTER
> (origin URL + HEAD; `source.path` derived for collection repos) — the dev-loop counterpart of
> the repo-side registry-release CI job. Guards: clean tree, HEAD on a remote-tracking ref,
> vendored routes refused; `--version X.Y.Z` (service-kind only) or `--bump`; carry-forward of
> hand-added manifest fields (platforms etc.). Scaffold CONTRIBUTING template caught up to
> schema 2 (tuple identity, placement law, reserved names, submodule note).
> **Submodule-carrying services** (v0.2.3): `_fetch_source` now runs `git submodule update
> --init --recursive` on pinned checkouts when `.gitmodules` exists (build-time source only —
> launch surfaces stay submodule-free by design); the superproject commit pins submodule revs,
> so exact-pin holds transitively. Runs on cache reuse too (heals pre-fix caches); loud error
> when submodule URLs are unreachable.
> **Pins-only suite capture** (v0.2.2): `promote --all --suite X` on an all-clean deployment
> emits the suite alone (pinned profiles + existing bindings) instead of exiting "nothing
> dirty"; a deployment with nothing pinned/bound still refuses (EMPTY suite guard).
> **`--adopt` implies `--kind profile`** (v0.2.1): bare `promote <instance> --adopt` no longer
> errors on instances with a pinned base (service-example anchored or profile-pinned) — the flag
> is profile-only, so it IS the kind choice; a dirty pinned instance fork-adopts with the short
> name defaulted from provenance. Explicit `--kind overlay --adopt` still refuses.
> **Profile identity = (service, short) tuple — registry schema 2 SHIPPED** (v0.2.0, CLEAN BREAK,
> no back-compat by user decision — all deployments recreated): a profile's key is
> `service:short` everywhere (refs `[registry/]camera-service:siyi-zr30[@ver]`, index keys, lock
> rows, suite members, based_on), projecting to `profiles/<service>/<short>/` on disk — the
> filesystem enforces tuple uniqueness, so same-short profiles for different services coexist
> (`ouster:generic` + `sbg:generic`). The path is a PROJECTION of the manifest: `name:` holds the
> short half, `requires.service` names the parent dir (placement validated; foreign-service
> profiles use the unqualified name in the path with the qualified pin in requires — validated
> format-only cross-registry, hard at install, lock records the peer registry commit as before).
> Promote takes `--name <short>` (validated: no `/ : @` — also fixes an unguarded nested-dir
> write bug), derives the service half from the instance; rebase takes the compound key. The
> v0.1.69 `<service>:<profile>` porcelain became the ref grammar itself (versioned + qualified
> colon refs now legal); `sensor:`/`project:` prefixes stay safe via the reserved-service-name
> rule. Schema-1 registries are REFUSED with a migration pointer (degrade-not-brick at client:
> skipped with a warning). Pre-move git history of migrated profiles is unreachable by design
> (clean break). rig-registry-public migrated locally in the same session (schema: 2, profiles
> nested, schemas/ regenerated) — PUSH ORDER: rig v0.2.0 release first, then the registry.
> **`<service>:<profile>` porcelain SHIPPED** (v0.1.69, CHEATSHEET §1.5): profiles addressable by
> the service they drive — `rig add ouster:generic` installs the profile NAMED generic whose
> `requires.service` is ouster (unqualified match; exact name ⇒ at most one hit per registry,
> priority order decides across them), `rig pkg search ouster:` lists every profile for a service
> (`ouster:gen*` globs), and free-text search gained the symmetric requires axis (profiles used to
> be invisible to `pkg search ouster` unless the name or a match id contained it — overlays already
> had their by-target axis). Derived entirely from `requires.service`, so nothing new is authored
> and pre-existing profiles are discoverable retroactively; versions/qualifiers are refused with a
> pointer at the explicit ref form. `sensor:` stays the hardware-id porcelain; service names
> `sensor`/`project` are now validation-reserved (they are porcelain prefixes).
> **fleet.yaml = the fleet-vars tier SHIPPED** (v0.1.68, CHEATSHEET §1.6): the deployment-root
> fleet.yaml (pushed by `fleet up`) is now a vars SOURCE — precedence shell > vehicle.local >
> /etc/rig > **fleet.yaml** > vehicle.yaml. `{{fleet_ids}}` is DERIVED from the roster (no more
> dual maintenance), `{{gcs_ip}}`/`{{fleet_mode}}` come from its keys, and its `vars:` section
> carries fleet policy (peer_endpoint: field IPs vs SIL ports = one file). The pushed file
> persists, so mid-test reboots render CURRENT fleet values standalone (latent DDIL gap
> closed); run snapshots already capture it, so per-run fleet provenance is complete.
> **Profile lineage SHIPPED** (v0.1.67, the three-tier workflow: public base → internal org
> profile → project overlays): fork promotes stamp `based_on: parent@ver`; `rig pkg rebase
> <fork> --to <reg>` three-ways the fork onto the parent's current version (old parent payload
> from registry git history, conflicts keep yours loudly, requires adopted + re-qualified);
> `promote --kind profile --adopt` closes the profile round-trip (instance re-pins to the fork,
> working+pin reset, overlays unbound — render identical; hand-authored instances GAIN
> provenance); `pkg list` gained a ROLE column (active vs dependency-of); `pkg info` shows
> lineage + a rebase hint; consumers follow rebases with plain `pkg upgrade`.
> **`rig fleet` verb group SHIPPED** (v0.1.66, CHEATSHEET §1.6 + ROADMAP §4):
> list/status/sync/up/down — the GCS-side ssh loop automated, never a control plane. New
> `rig status --format json` machine contract; correlated run labels (`fleet up --run X`);
> sealed-run harvest into `fleet-runs/<label>/<vehicle>/<run-id>`; roster pushed at up and
> captured by every run snapshot. SIL: local rows = simulated machines
> (`<data_root>/.identity/` via RIG_VEHICLE_LOCAL), shared-run-dir VIEW (symlinks; sync
> materializes the same tree from real vehicles), docker network create/rm +
> RIG_NETWORK/RIG_VEHICLE_IP env contract. First ssh/scp test shims.
> **`{{map}}` + fleet peers SHIPPED** (v0.1.65, CHEATSHEET §1.6): `{{map <list_var>
> <template_var>}}` (whole-scalar, renders a LIST; the template being a VAR makes field vs SIL a
> tiering swap — `tcp/10.160.1.{}:7447` vs `RIG_VAR_peer_endpoint=tcp/127.0.0.1:744{}`) + the
> derived built-in `fleet_peer_ids` (`fleet_ids` minus THIS vehicle_id, string-normalized) —
> zenoh peer endpoints from one fleet artifact, each vehicle excluding itself. MAP-aware fleet
> detection (a map-only deployment still bakes FLEET); map forms hard-error outside configs.
> `rig init` now scaffolds the vars/env convention (gcs_ip worked example) and gitignores
> vehicle.local.yaml + fleet.yaml; the GCS-side fleet.yaml roster is documented (the `rig fleet`
> verb group is a ROADMAP §4 item with settled invariants).
> **pkg UX batch SHIPPED** (v0.1.64): `pkg info` parses `@version`, prints `authored_against`
> and the local install state; `pkg list` marks dirty instances (`*`) next to the upgrade
> column; `config diff` shows the pin + "X.Y.Z available" on both dirty and clean lines;
> free-text `pkg search` covers project tags and overlay targets, prints a header, exits 1 on
> no matches; `overlay list` is a status view (newer-version / masked-keys / missing-payload);
> `pkg lock` reports to stdout and verifies bound overlay payload copies; one `parse_ref`
> helper (rig_cli/refs.py) replaced ~15 hand-rolled ref splits.
> **Git history = the registry version archive SHIPPED** (v0.1.63, ROADMAP §5): git-backed
> registries serve PAST versions read-only from the full-clone cache (`pkg add ns/name@old`,
> `git log`/`git show`, no checkout/tags), and `--locked` re-resolves packages at the locked
> registry commit — reproduction actually reproduces after the registry moves, while the lock's
> hashes still gate (rewritten history fails loudly). Capability-detected: non-git local-dir
> registries keep the exact old behavior.
> **pkg correctness batch SHIPPED** (v0.1.62): `pkg upgrade` now covers bound overlays (rebound in
> place, order kept) and keeps the lock's binding record; upgrade + single-package install are
> all-or-nothing (content-level snapshot/rollback); installing over a different service pin is an
> ERROR pointing at `pkg upgrade` (no more silent shared-service moves / duplicate lock rows);
> overlay hygiene — pin-less instances get no lock anchor (and `pkg lock` self-heals legacy ones),
> re-applying a bound overlay at a new version REBINDS in place, ambiguous remove/reorder refuse,
> pin-less `--clear-local` refuses before mutating, and apply warns on `authored_against` drift
> (the v0.1.59 stamp's first consumer); `--locked` verifies `source.rev` (a rewritten rev under
> the same version now fails); `registry sync` warns on a stale committed index.json; `pkg remove`
> unlinks the instance's stale `var/rendered/` file.
> **Promote update-flow SHIPPED** (v0.1.61): re-promotes carry the existing manifest forward
> (provides/match/overrides_schema survive a bump; authored_against always re-stamped), the profile
> name defaults from the row's provenance, bare promote infers `--kind profile` for hand-authored
> instances (overlay impossible), `--bump` is implied when provenance proves the target IS the
> pinned package (name collisions still refuse), registry-side refs are requalified
> alias→namespace, rollback restores (never deletes) pre-existing packages.
> **Run config snapshots SHIPPED** (v0.1.60, ROADMAP §3c "Config snapshots"): every `rig up` captures
> the effective config (vehicle.yaml + lock + resolved vars + rendered instance configs) into the open
> run at `runs/<id>/.rig/config/<digest12>/` (content-addressed, dedup'd) + an `ups:` event log in the
> run manifest; sealing dirty-checks (flags `config_dirty_at_seal:`, never copies) gated on a
> per-deployment-instance id (`var/deployment-id`) so a stale run rotated away by a freshly untarred
> artifact seals clean. Fail-soft; rig-path only (compose-only up.sh doesn't snapshot — resolved
> artifacts are tag-determined, fleet artifacts already route through bundled rig).
> **The package-registry layer is IMPLEMENTED** (v0.1.35–v0.1.45; plan doc `rig-registry-plan.md`,
> untracked by request; design summary in DESIGN.md): registry model + `registry
> init|validate|index`, live seed registry
> **https://github.com/christomaszewski/rig-registry-public**, `~/.rig` client
> (`setup`/`add`/`sync`, ordered priority, degrade-not-fail), CLI noun taxonomy with permanent
> aliases, one extended `rig.lock`, `pkg add` (install = alias; + `rig add` porcelain: path | name | registry
> ref | `sensor:<id>`) with vendored-at-pin self-contained deployments, the working-copy pipeline
> (`config/.pins/` anchors, `config diff` attribution, `pkg upgrade` three-way), ordered overlay
> bindings (local beats overlays), `pkg promote` (overlay/profile/suite; write+validate, git
> publish stays manual) and atomic suite install with rollback. E2E-verified live: fresh
> `RIG_HOME` → setup → sync → `add public/zenoh-router` + `add sensor:zr30` → doctor 0 errors.
> **M7 distribution SHIPPED** (v0.1.46): release automation on tag (tests → wheel/sdist → arch=all
> deb → GitHub Release; release v0.1.46 live with all three assets), Homebrew tap
> **christomaszewski/homebrew-rig** (verified: `brew install christomaszewski/rig/rig-cli`; formula renamed rig-cli — core's `rig` shadows bare names), tap
> auto-bump wired behind a `TAP_PUSH_TOKEN` secret (unset = bump by hand).
> **Vehicle-local vars + fleet artifacts SHIPPED** (v0.1.47–48, CHEATSHEET §1.6): `{{var}}`
> interpolation (render-time configs + load-time manifest markers, self-marker = mandatory
> per-vehicle), sources shell > vehicle.local.yaml > /etc/rig/vehicle.local.yaml > vehicle.yaml,
> `env:` passthrough on fleet_env, flagless fleet bake (templates in ⇒ templates out, bake blind
> to local sources), `rig provision` (+ artifact provision.sh shim, --force re-identification
> gate). Deferred: `Sensor`→`Instance` dataclass rename (cosmetic, large mechanical diff). Tool at `/Users/ckt/ws/bringup`; run-from-source
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

`ROS_DOMAIN_ID`, `RMW_IMPLEMENTATION`, `VEHICLE_ID`, `RIG_IMAGE_REGISTRY`, `RIG_IMAGE_TAG` (a VERSION,
e.g. `v1.3.0` — composed per-service to `<tag>-<platform>` for build-matrix services, v0.2.14),
`RIG_BASE_IMAGE` (the deployment's ONE base image, v0.2.21 — vehicle.yaml `images.base`, or composed
from a `provides: base` service like fleet-ros; a compose may RUN it directly),
`RIG_TARGET_PLATFORM` (the host's declared hw/OS target from vehicle.yaml `platform:`, e.g. `jp7`;
also mirrored into each service's declared `platform.override_env`, e.g. `CAM_PLATFORM`),
`RIG_DATA_DIR` (recordings/logs host dir), and per-call `COMPOSE_PROJECT_NAME=<name>-vehicle-<vehicle_id>`.
A launcher's compose opts into each (`${RIG_IMAGE_REGISTRY:+…}`, `:${RIG_IMAGE_TAG:-latest}`,
`${RIG_DATA_DIR}/…`), and a launcher honors `COMPOSE_PROJECT_NAME` by **not** passing `-p`.

## rig capabilities (all built/tested — bullets carry their own version tags)

- Lifecycle `up/down(--purge)/cleanup/status/logs/config/doctor` (`cleanup` = decommission: images +
  volumes off the host, v0.2.15); tiered ordering (infra → sensors → autonomy;
  down reversed, so autonomy stops FIRST); tier-aware output ("2 sensors + 2 infra + 1 autonomy").
- `vehicle.yaml`: `vehicle`/`vehicle_id` (→ ROS domain + `VEHICLE_ID`; per-host MARKERS by default
  since v0.2.20 — `rig init --vehicle-id N` pins literals), `ros{rmw,distro}`, `images{registry,tag}`,
  `platform` (→ `RIG_TARGET_PLATFORM`, v0.2.14), `data_dir`, `infra:`, `sensors:`, `autonomy:`.
  Config overrides + nameless profiles (deep-merge).
- `doctor`: one-distro check, launcher-present, host-port clash (enabled-aware `plugins[name=x,enabled=true].params.port`
  selector), **non-ROS-safe name warning** (hyphens → invalid ROS namespace; sensor + autonomy tiers),
  zenoh-router guardrail (SYMMETRIC since v0.2.23 — zenoh rmw with no router, and an enabled
  router under a non-zenoh rmw), autonomy-with-no-enabled-sensors warning ("a brain with no eyes").
- `rig build [-j N] [--registry] [--tag]`: per-unique-service **build** (`rigging.yaml build:`) + **mirror**
  (`mirror:`, via `docker pull/tag/push` so a plain-HTTP registry works). Concurrent with `-j`.
  v0.2.21: **base staging** — a `provides: base` rigging (fleet-ros) builds FIRST (dedup'd across
  riggings sharing one script) and rides into every other build as `RIG_BASE_IMAGE` (`images.base` /
  `--base-image` overrides; conflicting providers = ERROR); **`--no-cache`** exports
  `RIG_BUILD_NO_CACHE=1` (scripts opt in: `docker build ${RIG_BUILD_NO_CACHE:+--no-cache}`), and
  **`RIG_ROS_RMW`** (v0.2.22 — vehicle.yaml `ros.rmw`, so a base build installs the rmw the audit
  then checks for). Providers of one base must agree on image name, build.platforms, and build
  script; disagreement is an error, never an order-dependent guess (v0.2.22). Script agreement is
  judged by CONTENT since v0.2.25 (name + git tree of the script's dir in clean checkouts): two
  pinned checkouts of identical base context are ONE build, and the src cache is (repo, rev)-keyed
  so one collection repo is one clone.
- `rig image audit` (v0.2.21): inspect every image the enabled stacks resolve to (rendered composes →
  `docker run --entrypoint /bin/sh` + dpkg): one ROS distro (= `ros.distro`), the declared rmw package
  installed, and shared ros-* package versions AGREE across images — catches the
  two-images-two-rmw_zenoh_cpp-builds failure before the vehicle does. Run after `rig build`/`pull`.
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
