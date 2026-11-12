# Rig documentation

These references describe the current checkout. Rig **v0.2.80** introduced multiple-bag replay
and per-input proxy selection; it requires rig-infra **v1.20.0** for those bag features.
Rig **v0.2.81** adds `rig replay --fast` hash reuse and supports the sensor-profile revision-2
TF scheduling fixes in rig-infra **v1.20.1**. Check both installed versions before using these
features.
Rig **v0.2.82** keeps the published digest of an image that a build did not change, so a repeated
`rig build` no longer rebuilds the images above an unchanged base.
Rig **v0.2.83** makes real `replay prepare` pull missing recipe-selected runtime
images by default, honoring `--pull missing|always|never`; check and dry-run remain read-only.
Rig **v0.2.84** adds the [service contract](SERVICE_CONTRACT.md) and the
[rigification guide](RIGIFICATION.md), and makes `rig certify` fail fixed container names,
relocated build images and hard-coded ROS settings that it used to pass.
Use the Rig executable shipped with your deployment; updating another installation does not
upgrade a baked artifact. Isaac attachment remains explicitly **experimental**.

| Task | Start here |
| --- | --- |
| Install Rig and get an overview | [Project README](../README.md) |
| Look up what Rig requires of a service | [Service contract](SERVICE_CONTRACT.md) |
| Make existing software a Rig-compliant service | [Rigification guide](RIGIFICATION.md) |
| Prepare Docker, identity, devices and networking | [Host setup](HOST_SETUP.md) |
| Author/build/bake a deployment | [Cheat sheet](CHEATSHEET.md) |
| Follow the worked Orin deployment | [Runbook](RUNBOOK.md) |
| Build portable images and reconstruct across hosts | [Builds and replay](BUILD_AND_REPLAY.md) |
| Build some services, or build without the network | [Builds and replay](BUILD_AND_REPLAY.md#building-some-services-and-building-offline), [offline builds plan](offline-builds-plan.md) |
| Select services, bags, camera recordings and proxies in one recipe | [Replay](REPLAY.md) |
| Replay from a shipped artifact, including offline bundles | [Onboard replay](ONBOARD_REPLAY.md) |
| Hold replay producers until consumers are ready | [Coordinated startup](replay-coordination.md) |
| Attach selected services to a running Isaac ROS graph | [Simulation reference](SIMULATION.md) (experimental) |
| Prepare an offline Isaac machine and record→replay | [Offline walkthrough](ISAAC_OFFLINE.md) and [example](../examples/isaac-attach/README.md) |
| Attach selected services to another running simulator | [External simulator](SIMULATION.md#an-external-simulator) and [example](../examples/external-attach/README.md) (experimental) |
| Inspect current evidence and remaining Isaac acceptance | [Preparation report](ISAAC_PREP_REPORT.md) |
| Resume development or understand decisions | [State](STATE.md), [design](DESIGN.md), [roadmap](ROADMAP.md) |

## Tentative future work

- [Windows replay without WSL](windows-replay-plan.md): proposed Docker Desktop Hyper-V
  controller and offline baked-artifact packaging; not implemented or supported yet.

## Historical plans and release evidence

These preserve their original checkpoints and tradeoffs. Their old pending items and test
totals should not be read as current operator requirements:

- [Build/replay portability plan](cross-platform-replay-plan.md)
- [Replay preparation plan](replay-preparation-plan.md)
- [Replay proxy/export plan](replay-proxy-exports-plan.md)
- [Replay reliability plan](replay-reliability-plan.md)
- [v0.2.71 release notes](releases/v0.2.71.md)
- [v0.2.72 release notes](releases/v0.2.72.md)
- [v0.2.73 release notes](releases/v0.2.73.md)
- [v0.2.74 release notes](releases/v0.2.74.md) and [pre-release review](releases/v0.2.74-review.md) (fixed and known issues)
- [v0.2.75 release notes](releases/v0.2.75.md)
- [v0.2.76 release notes](releases/v0.2.76.md)
- [v0.2.77 release notes](releases/v0.2.77.md)
- [v0.2.78 release notes](releases/v0.2.78.md)
- [v0.2.80 release notes](releases/v0.2.80.md) (multiple bags and per-input proxies)
- [v0.2.81 release notes](releases/v0.2.81.md) (faster repeat replay and sensor-time TF handling)
- [v0.2.82 release notes](releases/v0.2.82.md) (unchanged images keep their digest across builds)
- [v0.2.83 release notes](releases/v0.2.83.md) (preparation acquires replay runtime images)
- [v0.2.84 release notes](releases/v0.2.84.md) (service contract, rigification guide and stricter certify)
- [Coordinated replay readiness, 2026-10-27](releases/coordinated-replay-readiness-2026-10-27.md)

The release readiness report establishes replay evidence on its stated machines/versions.
It does not establish live Isaac, GPU, physics or disconnected scene acceptance.
