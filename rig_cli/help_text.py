"""Workflow guidance shared by command help and the translated noun-group menus."""
from __future__ import annotations

import argparse
import textwrap


_REPLAY_POLICY = """Recipes and instance selection:
  Version 2 uses session.instances for the exact processes to start, including
  infrastructure, cameras and consumers. The bag player is added automatically;
  do not list it. Put source, bags, inputs and playback fields under replay.
  Keep one file in replay/ or sessions/. --recipe NAME checks sessions/NAME.yaml
  first, then replay/NAME.yaml; an explicit YAML path avoids name collisions.
  Version 1 remains supported; its input fields are at the document root and
  its instances mapping binds recorded identities, not process selection.
  --include-service/--skip-service are not accepted with version 2; edit
  session.instances. Its positional names/--live cannot add unlisted instances.

Startup policy without a version-2 recipe (vehicle.yaml instance rows):
  replay: {start: true}   start even when enabled: false
  replay: {start: false}  omit the process; keep its recorded inputs eligible
  Unset: enabled infrastructure, detected recording sources, and the player.
  Instance policy overrides replay.services.SERVICE.start and descriptor defaults.
  replay.instances.NAME.start is the equivalent top-level instance setting.
  Descriptor start: true permits selection; only an explicit instance/service
  true forces a normally disabled consumer to start. The player is managed by Rig.
  Explicit names, --live and --include-service override startup policy.
  --include-service takes service: keys, not name: values, and starts all their
  instances. Repeat it for several services; positional NAMEs select instances.
  --skip-instance/--skip-service also remove proven exclusive recorded outputs and cannot conflict
  with explicit inclusions. Recording-capable instances need recordings or --live.
  Skip all camera feeds with --skip-service camera-service, or one destination
  camera with --skip-instance NAME. Skipped cameras need no recordings/proxies.
  Repeat skips on every check/prepare/replay invocation; recipes do not save them.
  Topic skips require exclusive recorded publisher ownership. A namespace alone
  proves no outputs; without graph evidence use exact playback.exclude entries.
  Shared/unknown outputs remain; keep cannot override a proven skip.

Inputs and recipes:
  Broad playback is the default; graph epochs and pub/sub declarations are
  optional. --selection recorded uses the historical subscription selector.
  Fields below are under replay in version 2, or at the root in version 1:
  bags, instances.NAME.recorded_as/recorded_namespace, recordings.NAME.from/sessions,
  inputs, and playback.topics/exclude/keep/remap/services/calls/image_sources.
  recorded_as selects the recorded instance/files and defaults to NAME.
  recorded_namespace independently binds recorded ROS nodes, e.g. /control/relay;
  a namespace-only entry is valid and overrides the service's rigging.yaml.
  Optional top-level ros_namespace: /control/relay in rigging.yaml declares the
  actual ROS NODE namespace once, without a publisher/subscriber inventory.
  Use a non-root absolute namespace or a quoted "/robot/{name}" template; only
  {name} is supported. Replay expands it with recorded_as, otherwise NAME;
  rig graph uses the current NAME. Undeclared services retain /recorded_as or /NAME.
  This metadata does not launch/remap nodes. Recipe recorded_namespace handles
  historical changes. Distinct instances claiming one fixed namespace are
  ambiguous: use a per-instance template or explicit recipe overrides.
  Neither instance field remaps topics or service calls.
  Use playback.remap for topic changes; service targets keep their recorded names.
  Recording paths are source-run-relative or absolute; call-script paths are
  recipe-relative. Topic names are exact absolute ROS names.
  bags: all is the default. A bags list selects logger/session pairs; logger is
  the recorded instance name beneath RUN/bags/, not a service package name.
  Legacy bag.logger/session selects one session. --session selects camera
  recordings. Windows/call times use the selected aggregate bag timeline start.
  Originals merge by receive time with duplicates intact; no live deduplication.
  An explicit RUN overrides the recipe's source; omit RUN to reuse its binding.
  Bindings belong to each recipe, not the globally last-prepared run. Re-prepare
  after edits; topic-only filters do not require re-exporting proxies.
  To add positional NAMEs with --recipe, supply RUN before the instance names.
  Graph evidence excludes outputs owned exclusively by selected instances,
  including custom status topics. Shared outputs remain eligible. Without graph
  evidence, a namespace cannot distinguish inputs from outputs: exclude known
  live outputs explicitly, e.g. playback.exclude: [/control/relay/status].
  /tf, /tf_static and /diagnostics stay eligible under automatic suppression and
  skips; explicit lists, recorded subscriber selection or exclude may omit them.
  Broad mode never suppresses data solely by namespace, even for skipped sources.
  Both topic modes automatically select recorded calls to unambiguous servers in
  the entire started set, except targets with a known selected live client.
  Omitted/unassigned/ambiguous servers are excluded; no graph means no automatic
  ownership. playback.services: [] disables recorded calls. A nonempty list
  manually selects exact targets with warnings for uncertain ownership/overlap.
  This is a default change: an absent services field now means automatic calls.
  A manual list does not start servers. --calls overrides recipe call selection;
  scripts replace recorded requests before event-content validation.
  Prepare/execute inspect actual request contents in the selected bag/window
  with a bounded read-only helper in the pinned native player image. Check and
  dry-run only read cached evidence; a cache miss reports not_checked. Prepare
  acquires the required image according to --pull before inspection.
  Metadata-only, response-only, wrong-origin, unsupported,
  missing and empty-window requests are excluded in auto mode, errors if manual.
  Player play.services_source chooses service (default) or client origin and is
  frozen for inspection/playback. Only that origin plays; repeated events within
  it remain repeated calls. Inspection reports duplicates without deduplicating.
  JSON reports retain topic decisions, service ownership and request evidence.
  Broad playback does not discover the new deployment's subscriptions.
  Recipes remain local files; registry/suite packaging is deferred.

Originals and proxies:
  inputs selects bags or instance:NAME independently, using exactly one of
  {use: original}, {profile: NAME}, or {proxy: FULL_EXPORT_ID}. Omitted targets
  use originals. --input TARGET=original|profile:NAME|proxy:ID overrides a target;
  repeat for several targets. Explicit selectors do not need --use-proxies.
  Generate selected profiles with: rig run export RUN --recipe analysis
  Bag profiles combine arrival/sent/sensor timing with off/conservative/aggressive
  deduplication. Conservative retains ordinary byte-identical sensor repeats;
  aggressive collapses likely cross-bag matches within its receive-time window.
  Camera-service defines h264; each camera can choose a proxy or its original.
  playback.image_sources.CAMERA: bag keeps bag ROS images while the camera's
  selected video provides preview; camera instead uses that camera's ROS bridge.
  Bag proxies: RUN/exports/inputs/bags/PROFILE/EXPORT_ID/.
  Camera proxies: RUN/exports/services/SERVICE/PROFILE/EXPORT_ID/.
  Check/prepare/replay never generate proxies or fall back when a requested
  proxy is missing, stale or incompatible. Live/skipped instances need none.
  Legacy --use-proxies enables broad service-export selection for that invocation.
  Legacy recipe proxies.services.SERVICE.profile selects a profile;
  proxies.instances.NAME.export pins an export ID, or use: original excludes
  that instance. Preferences do not enable proxies without --use-proxies.
"""

_REPLAY_WORKFLOW = """Workflow (also available as rig run replay ...):
  rig replay check RUN
  rig replay prepare RUN --recipe bench
  rig replay check --recipe bench --format json
  rig replay --recipe bench --auto-end
  rig replay --recipe bench --fast
  rig replay RUN planner --from 120 --to 300 --dry-run
  rig replay check RUN --skip-service camera-service
  rig replay prepare RUN --recipe no-cameras --skip-service camera-service
  rig replay --recipe no-cameras --skip-service camera-service

  --fast reuses local hashes from an earlier replay/prepare when file identity,
  size and timestamps are unchanged. New/changed files are hashed normally;
  recipes, inventories, images and startup checks still run. Omit --fast for a
  fresh integrity scan. Check and dry-run can read the cache but never write it.
  The separate legacy startup delay can be shortened with --start-delay 0.

  Use `rig replay check -h` and `rig replay prepare -h` for readiness and import.
  For a source literally named check or prepare, use: rig replay -- check
"""

_DESCRIPTIONS = {
    "run-export": "Export recorded inputs offline. --recipe builds the selected bag and "
                  "per-instance proxies; --service exports one service's recordings. Both "
                  "preserve originals. Without either selector, use the legacy whole-run export.",
    "replay": "Replay recorded inputs into the current deployment's services and configs. "
              "Hand-built deployments, renamed instances and new consumers are supported; "
              "a captured deployment is not required. With data_dir configured, execution "
              "opens a linked output run.",
    "replay-check": "Inspect replay selection, source bindings and destination readiness without "
                    "writing files or starting containers. Uses the same plan as prepare and replay.",
    "replay-prepare": "Prepare a run for replay on this deployment without starting deployment "
                      "services. Save a local recipe/binding, adapt legacy inputs and optionally "
                      "add a missing bag player. Pull missing selected runtime images by default; "
                      "--pull always refreshes them and --pull never requires cached images. "
                      "Original recordings are preserved.",
    "build": "Build and publish service images, or mirror declared upstream images, into the "
             "registry. The default release build covers all declared runtime variants and "
             "supported CPU architectures, including disabled deployment rows.",
    "bake": "Freeze a deployment artifact for a selected runtime and CPU. Preserve the release "
            "image catalog, including replay companions, and optionally bundle deployment images "
            "for offline startup.",
    "reconstruct": "Extract a run's verified deployment capture, overlay its saved configs and "
                   "localize execution for this machine. Requires a captured or retrofitted "
                   "artifact; use replay prepare for legacy inputs on a hand-built deployment.",
}

_EPILOGS = {
    "run-export": """Recipe-selected inputs (recommended):
  rig run export RUN --recipe replay/analysis.yaml --dry-run
  rig run export RUN --recipe replay/analysis.yaml
  rig replay prepare RUN --recipe replay/analysis.yaml
  rig replay --recipe replay/analysis.yaml

  The same recipe chooses processes, bag inputs and per-camera representations.
  Version 2 puts inputs under replay; version 1 puts them at the root.
  Use --input bags=profile:sensor-conservative or
  --input instance:cam_front=profile:h264 to override a target; repeatable.
  Omit RUN when the recipe supplies a source or has a prepared local binding.
  Original targets need no conversion; exact proxy IDs verify existing artifacts.
  Recipe export accepts --force and --dry-run, but not --service, --profile,
  --instance, --in-place or --lossy. It never starts deployment services.
  Bag proxies go under RUN/exports/inputs/bags/PROFILE/EXPORT_ID/.
  Check/prepare/replay do not create proxies; export first, then prepare.

Service-only exports:
  rig run export RUN --service camera-service
  rig run export RUN --service camera-service --profile h264 --instance cam_front
  rig run export RUN --service camera-service --dry-run
  rig replay check RUN --use-proxies
  rig replay RUN --use-proxies

  --instance uses ORIGINAL recorded names, even when absent from this deployment.
  No vehicle.yaml encoding profile is needed. Service-only exports preserve all
  originals, contain no bags or unchanged services, and are reusable after copying
  the run. --force regenerates without deleting older generations. Multiple
  compatible generations require an explicit export pin in the replay recipe.
  Profiles declare lossiness and supported inputs; unsupported inputs are errors.
  For a new conversion, missing native images declared by an updated service
  helper are pulled automatically at their exact reference/digest, then checked
  again. Docker credentials apply. No builds or cached-image replacements occur.
  --dry-run reports planned pulls without downloading; reuse needs no image pull.
  --in-place/--lossy cannot be combined with --service.

Legacy whole-run exports:
  Without --recipe or --service, --profile selects vehicle.yaml export_profiles (implicit
  when exactly one exists), under RUN/exports/PROFILE/. --force replaces that
  directory. --in-place explicitly rewrites originals with existing safeguards.
  The service-export namespace 'services' is reserved for new writes.

Sharing:
  rig fleet sync --include-service-exports
  Transfers committed camera/service exports, preserving nesting. This flag
  does not transfer bag proxies; copy the full run or regenerate them there.
  Existing fleet sync --profile remains for whole-run exports only.
""",
    "replay": _REPLAY_POLICY + """
Execution:
  Multiple bags/proxies require a plan-v2/multi-input player and matching native
  images (released baseline: rig-infra 1.20.0). Newer profile revisions need their
  matching provider bundle. Prepare and replay acquire selected runtime images
  according to --pull; neither builds images. Check and dry-run never pull.
  Recorded calls default to automatic selected-server policy; explicitly set
  playback.services: [] to disable them. Source-only replay uses wall time; bag replay
  publishes /clock by default, including when no data topics remain selected.
  Use --auto-end with a non-looping bag, or stop with rig down --end-run.

""" + _REPLAY_WORKFLOW,
    "replay-check": """Readiness:
  Reports ready_to_prepare and ready_to_run separately. Missing Docker/images,
  incompatible player capabilities and busy destinations prevent run readiness.
  Exit 0: ready to run. Exit 1: errors or unresolved checks. CLI errors: exit 2.
  --json and --format json expose the resolved plan and diagnostics.
  Review recorded namespace bindings, service call targets, skipped service
  events and regenerated output exclusions before execution.
  Request inspection uses valid cached evidence only; a cache miss is not_checked.
  Use prepare or replay to acquire required native images and inspect requests.
  Execution flags are assessed only; no run is opened and no calls are sent.
  This checks available evidence, not compatibility with every new subscriber.

""" + _REPLAY_POLICY + "\n" + _REPLAY_WORKFLOW,
    "replay-prepare": """Files and legacy support:
  --recipe NAME checks sessions/NAME.yaml then replay/NAME.yaml; an explicit path
  also works. New recipes default to version 1 under replay/. Without
  --recipe, prepare chooses a source-derived name and prints the replay command.
  Local source bindings live in var/replay/NAME/bindings.yaml (with a path suffix
  for recipes outside replay/). Generated plans,
  configs, reports and derived sources live in var/replay/NAME/<generation>/.
  Existing recipes are preserved; changed inputs produce a new generation.
  Accepts canonical runs without captures/epochs, standalone ROS 2 MCAP/SQLite
  bags, and camera aliases/external recordings configured in the recipe.
  Standalone bags and recordings for older services may be copied. Direct source
  bindings (including camera proxies) need no hybrid run tree. Missing/invalid bag indexes
  are rebuilt on the copy using a bounded, network-disabled local ROS container.
  Split bags must be supplied as a directory. No ROS 1/schema conversion.

Runtime images:
  Default --pull missing acquires absent native images from this recipe's runtime
  inventory before image-dependent preparation. --pull always refreshes selected
  references; --pull never requires cached images. No deployment services start.
  --dry-run only reports proposed pulls and writes nothing, even with --pull always.
  Offline bundles use their recorded local images and never pull from a registry.

Adding a player:
  rig replay prepare RUN --recipe bench --player public/ros2-bag-player@1.20.0
  rig replay prepare RUN --recipe bench --player /path/to/rig-infra/ros2-bag-player
  Only --player requests wiring: adds bag_player to vehicle.yaml with
  enabled: false, order: 999, its services.yaml route and player config (plus
  vendoring/lock state for a registry ref). Path wiring may print a manual
  snippet for YAML layouts it cannot safely edit. Existing rows are preserved;
  upgrade an old player with rig swap. Multiple bags need plan-v2/multi-input.
  Normal rig up leaves the player off; replay starts it when a bag is selected.

Readiness:
  Does not build images. May finish preparation but exit 1 when runtime
  prerequisites remain missing; inspect ready_to_prepare and ready_to_run.
  Exit 0: ready to run, or a valid --dry-run preview. CLI errors: exit 2.
  --dry-run writes nothing and starts no containers. Startup flags are
  assessed only; no output run is opened and no deployment services are started.

""" + _REPLAY_POLICY,
    "build": """Targets and scopes:
  --platform selects a runtime variant (e.g. jp6m, dev); --arch selects a Linux
  CPU (arm64, amd64). Deployment platform/arch do not narrow a release build.
  all          every referenced service, runtime and supported CPU (default)
  deployment   enabled services at the declared deployment target
  host         enabled services at the provisioned host runtime + Docker CPU
  replay       startup-policy candidates + player, at each service's replay
               runtime and all supported CPUs; a build superset, not a RUN plan
  Narrow scopes include shared build providers. Combine with commas or repeated
  --scope; all cannot be combined with narrower scopes. Targets are deduplicated.
  --platform/--arch filter all, or override only the deployment part of a scope
  union. They require deployment when used with narrower scopes; host/replay
  resolve independently. Host runtime falls back to an agreed service replay
  default or unambiguous matrix; provision --platform sets it explicitly.

Publication:
  Uses Buildx; the builder must support every requested CPU (native or emulated).
  Publishes base, messages, then services; verifies digests/CPUs in rig.lock.
  Matrix tags are <tag>-<runtime>; generic services use <tag>. Without images.tag,
  matrix tags are the runtime name and generic tags are latest.
  Partial builds cannot overwrite a tag while dropping its other CPUs; use a
  fresh iteration tag and set images.tag to it before running/baking that build.
  --no-cache does not repin vendored sources. Logs: var/build/<session>/.

Building some services:
  `rig build SERVICE...` builds only those (service or instance names); every
  other service keeps the images already in the registry. A named consumer
  builds FROM the base already published; naming the base provider rebuilds the
  base and its msgs overlay, not the consumers. After a failure, rig prints the
  command that retries only the services that failed or never ran.

Offline builds:
  Git sources (rig.lock service pins, msgs overlay sources, build.sources) come
  from local mirrors in $RIG_HOME/cache/git; `rig src sync` fills them online.
  --offline checks every source and mirrored image first and builds nothing if
  one is missing; --offline --check stops after that report. Image layers come
  from the build cache: a step it lacks (a new apt package) needs the network once.

Examples:
  rig image build --dry-run
  rig image build sbg novatel --tag release-1
  rig image build --scope host --tag laptop-1
  rig image build --scope deployment,replay --tag release-1
  rig image build --platform dev --arch amd64 --tag bench-1
  rig image build --scope deployment --platform jp6m --arch arm64 --tag jetson-1
""",
    "bake": """Target selection:
  Uses vehicle.yaml platform/arch unless explicitly overridden. Missing values
  are inferred only when service matrices have one common choice; the builder's
  CPU never chooses the vehicle CPU. Local identity/target overrides are excluded.
  --bundle-images exports enabled deployment stacks at that runtime/CPU.
  --include-replay adds replay service bundles and a missing disabled player to
  the artifact; --replay-player selects only the source for that added player.
  With both flags, replay companion images for the baked CPU are bundled too.
  Cross-CPU export requires Docker API >= 1.48. Other companion CPUs remain in
  catalog metadata; pull/load their images separately when replaying there.
  Build/publish needed companions first; baking does not build them.

Examples:
  rig image build --scope deployment,replay --tag release-1
  rig artifact bake --tag release-1 --include-replay --bundle-images
  rig artifact bake --platform dev --arch amd64 --tag bench-1
  rig artifact show release-1 --format json
  --tag names the artifact; set vehicle.yaml images.tag to choose image versions.
""",
    "reconstruct": """Replay target:
  Runtime defaults to destination provisioning, then service replay defaults,
  then the recorded runtime. --platform recorded retains the source runtime.
  CPU follows the destination Docker engine (native host fallback if unavailable),
  independently of the recorded CPU. Catalog companions allow native replay on
  another CPU/runtime; legacy captures without a catalog cannot verify them.
  Registry overrides change the endpoint, not release digests or source pins.
  Build/publish compatible images first. Replay prepare pulls selected missing
  runtime images by default; replay check only reports readiness.
  Existing player rows are preserved; upgrade old launchers with rig swap.

Examples:
  rig reconstruct /data/runs/RUN --into ./bench --platform dev
  rig --root ./bench replay prepare RUN --recipe bench --player /path/to/rig-infra
  rig --root ./bench replay --recipe bench
""",
    "run-retrofit": """This command modifies historical runs: attach a matching artifact for
reconstruction, or adopt external camera recordings (MOVE by default; --copy
preserves the originals). It is not required for replay on a hand-built tree.
Use rig replay prepare with a recipe for source-preserving legacy bag/camera
adaptation without a historical deployment capture.
""",
    "provision": """Target roles:
  --platform is this machine's runtime, used by host builds and reconstruction.
  --arch asserts the Docker execution CPU; it does not enable cross compilation.
  Release builds still default to the full matrix. Deployment builds and baked
  targets come from vehicle.yaml or explicit build/bake overrides.

Handover:
  --export writes this machine's file as a record (no sudo): `values` holds its
  vars and env, `machine` every other key. --apply merges a record's values into
  this machine's file and prints each key's old and new value. --only / --except
  pick values by name; --with-identity also applies the machine block, for a
  replaced computer, never for a spare that runs beside the original. A run
  directory, id or label works as the source: it stands for the machine file
  its last `up` captured.

Examples:
  sudo rig provision --platform dev --arch amd64
  rig image build --scope host --tag laptop-1
  rig provision --export uav-03.yaml
  sudo rig provision --apply uav-03.yaml --only altitude_band --dry-run
  sudo rig provision --apply ~/fleet-runs/survey/uav-03/20261112T101500Z_survey
""",
    "certify": """Portable build checks:
  Checks declared runtime/CPU matrices, image override propagation and the build
  contract (Buildx/platform arguments and digest handoff) where available.
  Build-contract probes use isolation without network/Docker access; they do not
  compile or execute every CPU target. Missing isolation/context can skip a probe;
  --require-build-check makes that a failure for services declaring a build.
  Use doctor --deep for deployment preflight plus launcher certification.

Examples:
  rig service certify --repo . --require-build-check
  rig doctor --deep
""",
    "doctor": """For run-specific source bindings, player capabilities and replay readiness,
use rig replay check RUN. For portable launcher/build contracts, use --deep or
rig service certify --repo PATH --require-build-check.
""",
    "pull": """Pulls images for the selected instances' effective execution configuration,
including local overrides. The default selection is all normally enabled rows;
name disabled replay participants explicitly when pre-pulling them.
This does not fetch the entire release matrix or every replay companion.

Examples:
  rig image pull --dry-run
  rig image pull bag_player planner
  rig replay check RUN
""",
    "graph": """Graph epochs and interface declarations are optional for broad replay.
Use --selection recorded on replay to request historical subscription selection.
Graph evidence describes recorded behavior; it cannot infer every new consumer.
""",
    "config": """Related commands: rig config show [NAME ...], rig config render [NAME ...],
rig config diff [NAME ...]. Bare rig config retains the show-all behavior.
""",
}

_GROUP_GUIDANCE = {
    "run": "Replay readiness/import: rig replay check -h; rig replay prepare -h.\n"
           "Playback accepts a local recipe; no captured deployment is required.",
    "image": "Build defaults to the full runtime/CPU matrix. Use build --scope for\n"
             "deployment, host or replay iterations; rig image build -h explains selection.",
    "artifact": "Bake selects a deployment runtime/CPU; bundles contain that target's images.\n"
                "Replay companion images remain in the catalog. See rig artifact bake -h.",
    "service": "Use certify --require-build-check in CI to require isolated portable-build probes.",
}


def configure(parser: argparse.ArgumentParser) -> None:
    """Keep menu summaries visible on leaf -h pages, including nested commands."""
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        for menu in action._choices_actions:
            child = action.choices[menu.dest]
            if not child.description:
                child.description = _DESCRIPTIONS.get(menu.dest, menu.help)
            if menu.dest in _EPILOGS:
                child.epilog = _EPILOGS[menu.dest]
                child.formatter_class = argparse.RawDescriptionHelpFormatter
                child.description = textwrap.fill(child.description, width=78)
            if menu.dest in ("replay-check", "replay-prepare"):
                menu.metavar = menu.dest.replace("-", " ", 1)
            configure(child)


def group_help(noun: str, verbs: dict[str, str], parser: argparse.ArgumentParser) -> str:
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    summaries = {a.dest: a.help for a in sub._choices_actions}
    lines = [f"usage: rig {noun} <verb> [options]", "", "verbs:"]
    seen = set()
    for verb, command in verbs.items():
        if command in seen:
            continue  # aliases parse forever, but menus teach one spelling
        seen.add(command)
        lines.append(textwrap.fill(summaries[command], width=80,
                                   initial_indent=f"  {verb:<10}", subsequent_indent=" " * 12))
    lines += ["", f"Use rig {noun} <verb> -h for options and examples."]
    if noun in _GROUP_GUIDANCE:
        lines += ["", _GROUP_GUIDANCE[noun]]
    return "\n".join(lines)
