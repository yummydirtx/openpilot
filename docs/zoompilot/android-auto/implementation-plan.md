# Implementation plan

Current phase: **native 3X frontend and full-menu integration**. Alex confirmed
the prior implementation worked and supplied six follow-up findings. The new
backend uses the real GPU widgets; a device-only benchmark reached 26.07 fps,
and a navigation probe reached all 16 settings categories without changing settings.
The four's launcher moves to its first native settings card. Heartbeat validation
now avoids a concurrent-write clock race. Sustained AA and driving-load checks
remain open; see [controls and evidence](controls.md).
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
stage and failure evidence before making further changes. Keep a measured HUD-only
fallback, but camera/model graphics are now explicitly requested work. Input
navigation is implemented; automatic startup remains deferred.

## First work session

- [ ] Confirm time remaining, available cables, device SSH access, and the exact
      head-unit firmware/market/trim.
- [x] Record the deployed Zoompilot and AGNOS/kernel revisions. Do not infer them
      from this checkout or the published kernel configuration.
- [x] Run the read-only device inventory in the [runbook](validation.md).
- [x] Identify which controller and gadget configuration belong to the separate
      comma data port; inventory existing USB users before claiming a controller.
- [x] Inspect AACS's USB implementation, negotiation, video ingress, and build
      dependencies. Select and pin a minimal sender path; record the choice in D4.
- [x] Define setup and cleanup together, then attempt G1 with timestamped logs.

For work without hardware, use the [local development guide](local-development.md).
The Mac DHU and phone-side probe have completed version exchange, TLS 1.2,
receiver acceptance of the imported phone certificate, encrypted discovery,
video channel/focus setup, bounded frame acknowledgement, and decoded H.264.
The synthetic 3X HUD preview passes at 480p, 720p, and a wide viewport. Live
rendering/encoding now runs on the comma. The September 19 [hardware session](in-car.md)
separately proved actual-car USB transport, mutual authentication, and pixels;
DHU results alone never establish those hardware exits.

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
| Repeated accessory startup on this hardware | Two-stage AOA passed; run counted physical reconnects | Reconnect reliability remains unproven |
| Portability beyond this AGNOS image | Running 19.7 kernel supports the implemented gadget; test other builds separately | Do not generalize the result to untested controllers/images |
| Phone identity refresh | Mazda accepts current imported identity; refresh before December 23 expiry | Later identities need independent import and car validation |
| DHU reverse certificate verification | Mazda verification passes on device; DHU 2.0/2.1 date encoding still fails on Mac | Keep emulator limitation separate from verified car behavior |
| Sender focus/input interruptions | Basic video and clean shutdown pass; exercise interruptions and Commander messages | Implement required responses and restart at a keyframe before claiming robust recovery |
| Other Mazda video modes | 720p with explicit focus passed; 480p is advertised but untested | Use the proven mode until another is measured |
| Full camera/model rendering can coexist with recording | Profile CPU rendering and PyAV encoding; current default 8 fps, with adjacent native workload comparison | Keep HUD fallback; optimize measured bottlenecks before raising resource limits |
| Commander input and local handoff | DHU input/focus tests pass; Mazda accepted binding and 249/249 video frames | Menu and local switch implemented; physical button, wake, and exit tests remain in [controls](controls.md) |
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
| 2026-09-18 | Installed stable Apple Silicon DHU locally; ran phone-free TCP bootstrap probe and four framing/error tests | DHU requested 1.7, accepted a 1.5 reply, sent 230-byte TLS ClientHello. Authentication/video not tested. Next: TLS and service discovery locally; G0/G1 tomorrow |
| 2026-09-18 | Extended local sender through TLS and explicit authentication status; twelve tests pass | TLS 1.2 established; DHU rejected self-signed identity with -3. Service discovery/video remain untested. Next: certificate compatibility; G0/G1 still pending hardware |
| 2026-09-18 | Verified Android Auto 17.6 APK signer; recovered and matched its phone key/certificate; added repeatable importer | Stock DHU 2.0 reports `Verify returned: ok`, auth status 0, encrypted discovery with seven channels. Thirteen tests pass. Identity expires December 23, 2026. Video and hardware gates still pending |
| 2026-09-18 | Enabled optional sender-side CA verification and tested stock DHU 2.0 and 2.1 | OpenSSL rejects the head-unit certificate's `notBefore` encoding; optional verification fails closed. Successful local discovery currently uses one-way authentication. See certificate experiment |
| 2026-09-18 | Implemented video setup/focus, pacing, acknowledgement window, and shutdown | Stock DHU decoded a six-second 800×480 pattern; all 180 frames acknowledged and received screenshots inspected |
| 2026-09-18 | Shared native 3X HUD painters, fetched hash-verified UI assets, and built a scalable synthetic preview | All 360 frames acknowledged at 480p, 720p, and a wide 1280×500 usable viewport; received pixels inspected. Eighty native HUD drawing-argument comparisons pass. Live camera/model composition and hardware gates remain pending |
| 2026-09-18 | Captured engaged, override, and stale states from DHU; ran final focused checks | 24 tests, Ruff, Python compilation, local documentation links, and diff formatting pass. Final 480p session acknowledges all 360 frames and orderly shutdown |
| 2026-09-19 | Inventoried comma over SSH; implemented isolated AGNOS accessory transport | Two-stage AOA works while normal power/CAN remain connected. Initial direct-accessory-ID attempt failed and cleaned up. Deployed openpilot remains unchanged |
| 2026-09-19 | Authenticated directly with Mazda, discovered nine channels, verified Mazda certificate | Strict verification passes with device OpenSSL 3.0.13; the earlier DHU certificate-date failure did not reproduce |
| 2026-09-19 | Added explicit video-focus request; projected 720p synthetic 3X HUD | 360/360 frames, then 1,800/1,800 in 59.971 seconds. Alex confirmed working display. Orderly shutdown and gadget cleanup pass; monitored processes unchanged. G4/G5 remain pending |
| 2026-09-19 | Added subscriber-only live state, shared native painters, spawned CPU renderer/PyAV encoder, limited transient service | Alex confirmed live 0 mph/disengaged HUD. First run 1,373/1,373 frames; later 6,306/6,306 over 234 seconds. Strict authentication and orderly cleanup pass |
| 2026-09-19 | Injected SIGSTOP into isolated frame worker and SIGKILL into sender | Freshness watchdog ended hung session; fresh authenticated session returned in about seven seconds. systemd removed children, owned gadget, and lease after sender kill |
| 2026-09-19 | Invalidated only a copied subscriber view | Unavailable HUD encoded at 457 ms; actual subscription recovered. Local render/encode evidence only |
| 2026-09-19 | Alex deferred physical reconnect/focus tests and prioritized the complete 3X road display | Add real camera, calibrated model path/lanes/edges/leads, and passive native icons; retain independent layer freshness and measured resource bounds |
| 2026-09-19 | Implemented real VisionIPC camera/model composition and native geometry parity tests; profiled and optimized CPU rendering | Full-road `road-live-08`: 1,360/1,360 frames in 174.03 seconds, zero drops, maximum local capture age 100.31 ms, maximum acknowledgement 27.85 ms. Clean shutdown. Full-road physical visual/alignment and driving-load checks remain pending |

For later entries include the gate, tested revision, evidence location, outcome,
and next discriminating experiment. Keep failed attempts; they prevent repeating
the same hardware or protocol assumptions in a later work session.
