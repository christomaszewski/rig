# Rig documentation

Current operator references cover Rig **v0.2.76**: offline and selected-service builds, shared
sessions and explicitly **experimental** Isaac attachment. Use the Rig executable shipped with your deployment;
updating another installation does not upgrade a baked artifact.

| Task | Start here |
| --- | --- |
| Install Rig and understand the launcher contract | [Project README](../README.md) |
| Prepare Docker, identity, devices and networking | [Host setup](HOST_SETUP.md) |
| Author/build/bake a deployment | [Cheat sheet](CHEATSHEET.md) |
| Follow the worked Orin deployment | [Runbook](RUNBOOK.md) |
| Build portable images and reconstruct across hosts | [Builds and replay](BUILD_AND_REPLAY.md) |
| Build some services, or build without the network | [Builds and replay](BUILD_AND_REPLAY.md#building-some-services-and-building-offline), [offline builds plan](offline-builds-plan.md) |
| Replay a run in an existing deployment; prepare recipes/proxies | [Replay](REPLAY.md) |
| Replay from a shipped artifact, including offline bundles | [Onboard replay](ONBOARD_REPLAY.md) |
| Hold replay producers until consumers are ready | [Coordinated startup](replay-coordination.md) |
| Attach selected services to a running Isaac ROS graph | [Simulation reference](SIMULATION.md) (experimental) |
| Prepare an offline Isaac machine and record→replay | [Offline walkthrough](ISAAC_OFFLINE.md) and [example](../examples/isaac-attach/README.md) |
| Inspect current evidence and remaining Isaac acceptance | [Preparation report](ISAAC_PREP_REPORT.md) |
| Resume development or understand decisions | [State](STATE.md), [design](DESIGN.md), [roadmap](ROADMAP.md) |

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
- [Coordinated replay readiness, 2026-10-27](releases/coordinated-replay-readiness-2026-10-27.md)

The release readiness report establishes replay evidence on its stated machines/versions.
It does not establish live Isaac, GPU, physics or disconnected scene acceptance.
