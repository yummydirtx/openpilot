# Implementation plan

Current phase: G0, baseline and device inventory. No hardware gate has passed.
Objective and completion criteria: [project brief](README.md).

The gates below are sequential where they depend on hardware evidence. A laptop
render preview can be developed independently, but it must not delay proving the
direct USB path. Time boxes are proposed investigation budgets; the remaining
hackathon schedule has not been confirmed.

## Milestones

| Gate | Work | Exit evidence | Initial time box |
| --- | --- | --- | --- |
| G0: baseline | Record device/software/head-unit versions and cabling; inspect running gadget/role state; capture normal openpilot resource use | Saved inventory and known-good restore procedure | 30–45 min |
| G1: USB peripheral | Establish accessory enumeration through the comma data port while normal vehicle power/CAN remain connected | Host/device traces showing the correct physical connection and working byte transport | 60–90 min |
| G2: session | Build/port minimal car-facing sender; complete version/authentication/service discovery and required channel setup | Negotiated services/video capabilities and a sustained session log | 90–120 min |
| G3: first pixels | Send a pre-encoded pattern in an accepted video mode; handle focus, timing, acknowledgements, and session restart | Moving pattern or counter on the real Mazda screen, with no intermediary | 60–90 min |
| G4: dashboard | Render synthetic states, then attach local telemetry; implement units, validity, stale state, and labels | Readable live dashboard plus clearly labeled parked transition demo | 2–3 h |
| G5: demo hardening | Reconnect, failure isolation, resource comparison, launch/cleanup instructions | Ten-minute session, three reconnect trials, failure checks, and recorded demonstration | 60–90 min |

A gate's time box triggers diagnosis and reprioritization, not automatic success
or abandonment. If a gate blocks the direct architecture, save the last successful
stage and failure evidence before making further changes. Scope reductions should
first remove camera video, model graphics, input navigation, and automatic startup.

## First work session

- [ ] Confirm time remaining, available cables, device SSH access, and the exact
      head-unit firmware/market/trim.
- [ ] Record the deployed Zoompilot and AGNOS/kernel revisions. Do not infer them
      from this checkout or the published kernel configuration.
- [ ] Run the read-only device inventory in the [runbook](validation.md).
- [ ] Identify which controller and gadget configuration belong to the separate
      comma data port; inventory existing USB users before claiming a controller.
- [ ] Inspect AACS's USB implementation, negotiation, video ingress, and build
      dependencies. Select and pin a minimal sender path; record the choice in D4.
- [ ] Define setup and cleanup together, then attempt G1 with timestamped logs.

The first artifact after planning should be a short device inventory and USB
experiment result, not a polished dashboard mockup.

## Implementation work packages

### Transport and session

- Separate USB/accessory setup from protocol handling so either can be diagnosed.
- Log state transitions, negotiated capabilities, failures, and disconnect causes.
- Support fresh initialization after unplug/replug and sender restart.
- Handle required services without assuming that video-only application scope
  means every non-video protocol message can be ignored.
- Stop cleanly and restore the USB state owned by the experiment. Do not overwrite
  unrelated gadget configuration or change CAN/safety settings.

### Video and dashboard

- Provide pattern, synthetic, replay, and live sources behind one explicit mode.
- Verify accepted codec configuration and keyframe behavior before optimizing.
- Use bounded queues and measure frame age as well as nominal frame rate.
- Build a small state adapter with existing speed/setpoint semantics and an
  explicit stale state; attach current alert text and severity.
- Add hardware encoding only after identifying buffer-format and concurrency
  requirements. Keep the baseline test pattern available for regression diagnosis.

### Integration and reproducibility

- Record dependency revisions, build/deploy instructions, device launch arguments,
  environment requirements, and setup/cleanup steps as they become known.
- Keep the prototype outside manager startup until manual lifecycle passes G5.
- Preserve native UI/audio alerts and avoid changes to vehicle-control behavior.
- Save a short demo recording showing the cable path and the phone-free runtime.
- Document limitations: tested head unit/firmware, video mode, missing input/audio,
  resource use, reconnect behavior, and whether onroad coexistence was tested.

## Unknowns and experiments

| Unknown | Experiment | Consequence if unresolved |
| --- | --- | --- |
| Data port exposes a usable peripheral controller while vehicle-connected | Inspect live role/gadget state; enumerate against a development host, then Mazda | Direct USB architecture remains blocked |
| Published kernel capabilities match running image | Compare running config, UDCs, and available functions | Transport adaptation or a targeted kernel change may be needed; do not assume a full reflash is necessary |
| AACS works with AGNOS and this head unit | Minimal build and staged handshake capture | Adapt transport/protocol implementation on comma |
| Mazda's video mode, focus, and timing requirements | Service discovery and pattern projection | Adjust codec/mode/session handling before dashboard work |
| Encoder can accept UI frames and coexist with recording | Synthetic frame encode and baseline comparison | Reduce workload or evaluate another on-device encoder path |
| Input is rotary, keys, touch, or a combination | Log negotiated input capabilities and parked events | Keep core demo passive; defer page navigation |
| Fork-specific state/setpoint behavior is represented correctly | Compare UI and telemetry, including MADS and invalid data | Simplify labels until semantics are verified |

## Validation scope

For each gate run the specific check that proves its exit. Software tests should
target framing/partial reads, malformed or unexpected messages, teardown/reconnect,
backpressure, stale telemetry, and unit/state conversions where implemented.
Passing mocks or an emulated head unit does not close an actual-car gate.

For code integration, run the repository checks appropriate to the files changed.
Vehicle-control tests are needed if those paths change; this plan intends to avoid
those changes. For these planning documents, verify links and diff formatting only.

## Progress log

| Date | Change or experiment | Result / next step |
| --- | --- | --- |
| 2026-09-18 | Agreed direct comma-to-Mazda objective; created planning documents | Source research only. Next: G0 device inventory and G1 peripheral validation |

For later entries include the gate, tested revision, evidence location, outcome,
and next discriminating experiment. Keep failed attempts; they prevent repeating
the same hardware or protocol assumptions in a later work session.
