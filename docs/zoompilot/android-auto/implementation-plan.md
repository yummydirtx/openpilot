# Implementation plan

Current phase: stock DHU projects moving video and the scalable 3X HUD preview.
Sender-side verification of DHU, live composition, and Mazda compatibility remain
unproven. See the [video runbook](video.md) and [interface plan](interface.md).
G0 device inventory awaits hardware access.
No hardware gate has passed.
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

Without hardware tonight, use the [local development guide](local-development.md).
The Mac DHU and phone-side probe have completed version exchange, TLS 1.2,
receiver acceptance of the imported phone certificate, encrypted discovery,
video channel/focus setup, bounded frame acknowledgement, and decoded H.264.
The synthetic 3X HUD preview passes at 480p, 720p, and a wide viewport. Next,
connect live rendering/encoding, resolve sender-side verification of the head
unit's certificate, and start hardware inventory when the comma is available. These
are preparatory experiments for G2/G3, not completion of their actual-car exits.
The first hardware session still starts with device inventory and USB validation.

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
| Phone certificate accepted by Mazda | Stock DHU acceptance now demonstrated; repeat on Mazda with the imported identity | A rejection needs the Mazda's actual failure stage; self-signed and expired certificates are not substitutes |
| Sender validates head-unit identity | `auth_probe --ca` fails on DHU 2.0/2.1 date encoding under OpenSSL 3.6.4; inspect peer certificate and repeat on Mazda | Current loopback prototype authenticates the phone to DHU only; mutual verification remains work for the runtime |
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
| 2026-09-18 | Installed stable Apple Silicon DHU locally; ran phone-free TCP bootstrap probe and four framing/error tests | DHU requested 1.7, accepted a 1.5 reply, sent 230-byte TLS ClientHello. Authentication/video not tested. Next: TLS and service discovery locally; G0/G1 tomorrow |
| 2026-09-18 | Extended local sender through TLS and explicit authentication status; twelve tests pass | TLS 1.2 established; DHU rejected self-signed identity with -3. Service discovery/video remain untested. Next: certificate compatibility; G0/G1 still pending hardware |
| 2026-09-18 | Verified Android Auto 17.6 APK signer; recovered and matched its phone key/certificate; added repeatable importer | Stock DHU 2.0 reports `Verify returned: ok`, auth status 0, encrypted discovery with seven channels. Thirteen tests pass. Identity expires December 23, 2026. Video and hardware gates still pending |
| 2026-09-18 | Enabled optional sender-side CA verification and tested stock DHU 2.0 and 2.1 | OpenSSL rejects the head-unit certificate's `notBefore` encoding; optional verification fails closed. Successful local discovery currently uses one-way authentication. See certificate experiment |
| 2026-09-18 | Implemented video setup/focus, pacing, acknowledgement window, and shutdown | Stock DHU decoded a six-second 800×480 pattern; all 180 frames acknowledged and received screenshots inspected |
| 2026-09-18 | Shared native 3X HUD painters, fetched hash-verified UI assets, and built a scalable synthetic preview | All 360 frames acknowledged at 480p, 720p, and a wide 1280×500 usable viewport; received pixels inspected. Eighty native HUD drawing-argument comparisons pass. Live camera/model composition and hardware gates remain pending |
| 2026-09-18 | Captured engaged, override, and stale states from DHU; ran final focused checks | 24 tests, Ruff, Python compilation, local documentation links, and diff formatting pass. Final 480p session acknowledges all 360 frames and orderly shutdown |

For later entries include the gate, tested revision, evidence location, outcome,
and next discriminating experiment. Keep failed attempts; they prevent repeating
the same hardware or protocol assumptions in a later work session.
