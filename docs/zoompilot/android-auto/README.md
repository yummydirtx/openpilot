# Direct Android Auto projection

Status: the direct comma four → Mazda connection works and Alex has tested it.
The new `native` backend ports the installed 3X frontend, including all settings
categories, onto an offscreen Adreno surface. Its isolated render/encode benchmark
reached 26.07 fps, compared with the previous 8 fps CPU compositor. The settings
launcher, disengaged path, steering icon, and heartbeat race are addressed in this
revision. Long-session and driving-load validation remain open. See
[controls and evidence](controls.md).

## Objective

Project a readable openpilot dashboard **directly from comma four to the factory
display in Alex's 2021 Mazda CX-5 over wired Android Auto**. The comma must generate
the display and implement the phone side of the projection connection itself.

There must be no Android phone, wireless adapter, or companion computer in the
runtime path. A development laptop may build software, collect logs, and emulate
a head unit during development. The final demonstration must work without it.

The car has wired Android Auto and a swapped EPS that behaves like a 2022 for
openpilot. That swap does not establish anything about infotainment compatibility;
the target remains the actual 2021 head unit. Trim, market,
and available development time still need recording. Deployed AGNOS and the
head unit's own firmware report are recorded in the [in-car runbook](in-car.md).

## Documents and authority

- [Architecture](architecture.md): proposed components, interfaces, and decisions.
- [Implementation plan](implementation-plan.md): dependency order, milestone exits,
  and the current backlog.
- [Validation runbook](validation.md): device inspection, experiment records, and
  the final demonstration checks.
- [Local development](local-development.md): Mac head-unit emulator setup, verified
  bootstrap probe, and the work possible without physical hardware.
- [Certificate experiment](authentication.md): working phone identity import,
  reproducible authentication check, expiry, and remaining verification limits.
- [Interface](interface.md): scalable comma 3X design, existing code reuse, and
  the next steps toward real camera/model composition.
- [Video runbook](video.md): working local projection commands and measured results.
- [In-car runbook](in-car.md): working direct USB setup, real Mazda results,
  strict authentication, cleanup, and repeat commands.
- [Live runbook](live.md): on-device rendering, isolated deployment, start/stop,
  freshness and failure behavior, measured load, and remaining validation.
- [Controls and display handoff](controls.md): Commander menu, OEM exit, local
  four button, tap-to-return, backed-up native installation, and pending checks.

This brief owns the objective and scope. The implementation plan owns progress;
the runbook owns experimental evidence. Label new statements as observed,
source-backed, or proposed. A source-backed capability is not a passed hardware
test. Update the documents when evidence changes a decision.

## Hackathon deliverable

A parked demonstration in the real Mazda, with comma four powered through its
normal vehicle connection and connected to the infotainment USB port through its
separate data port. The display shows:

- Connection/data freshness and whether data is live or replayed.
- Large vehicle speed and cruise set speed, with explicit units.
- Openpilot state and current alert text, preserving their meaning in this fork.

The visual target is the existing landscape comma 3X onroad interface, adapted to
the Mazda's measured 1280×480 usable viewport within 1280×720 video. The live HUD
shares native painters and assets. Real camera/model composition is implemented;
the old local preview remains an explicitly synthetic regression fixture.

Core acceptance criteria:

1. The factory head unit opens an Android Auto projection session with the comma.
2. Pixels are produced and transmitted on the comma, with no runtime intermediary.
3. Live telemetry is displayed; recorded/synthetic data is visibly labeled and is
   used only to demonstrate transitions unavailable while parked.
4. The dashboard runs for ten minutes and reconnects in three consecutive unplug/
   replug trials without a device reboot. These are proposed demo targets, not
   measurements already achieved.
5. Stale telemetry loses its active/healthy presentation. Failure or removal of
   projection does not interrupt openpilot's control processes, CAN interface, or
   native alerts.
6. A reproducible start/stop procedure and evidence of the actual demonstration
   are saved. Any unmet criterion is reported rather than silently relaxed.

A stable test pattern on the Mazda display is the first major success, but it is
an intermediate milestone rather than completion of the dashboard objective.

## Scope

Required: wired USB transport, Android Auto negotiation, video projection, a
compact landscape dashboard, reconnect behavior, and useful diagnostic logs.
Implement protocol responses needed by this Mazda even when the associated user
feature is outside the demo scope.

Implemented interface: real camera and native camera/model projection geometry,
with path/lanes/edges/leads, passive driver graphics, HUD, and text alerts. Keep a
HUD-only fallback while measuring load and evaluating further rendering performance.
Commander menu navigation and a local button to start/switch displays are now
implemented; see the controls runbook for evidence and pending physical checks.
Further work includes native GPU rendering, hardware encoding, and driving-load
validation.

Deferred: remote start, door locks, HVAC control, wireless Android Auto, phone
proxying, general Android apps, media/navigation integration, and replacing the
native openpilot UI or alerts. Projection is a read-only consumer of driving data;
head-unit input may navigate presentation pages but must not command the vehicle.

If direct projection proves blocked, document the exact blocker and revisit scope
with Alex. A phone or companion board is not an implicit fallback for this project.

## Evidence at kickoff

| Finding | Evidence | Limit |
| --- | --- | --- |
| The car supports wired Android Auto | Alex's report | Comma-to-car session not attempted |
| comma four has a separate USB 3.1 data port | [Product specifications](https://comma.ai/shop/comma-four) | Peripheral operation on the deployed image remains untested |
| Published AGNOS kernel config enables dual-role USB, gadget, FunctionFS, and Android accessory functions | [Pinned kernel configuration](https://github.com/commaai/agnos-kernel-sdm845/blob/c368754c26c7b9659de187addc6cccedc6cfb0a0/arch/arm64/configs/tici_defconfig) | Does not identify the running kernel, port routing, or working gadget setup |
| AACS implements the car-facing endpoint and reports arbitrary video projection | [AACS](https://github.com/tomasz-grobelny/AACS) | Older Odroid/Ubuntu setup; compatibility and dependencies require inspection |
| AA protocol references and proxy implementations exist | [open-android-auto](https://github.com/mrmees/open-android-auto), [aa-proxy-rs](https://github.com/aa-proxy/aa-proxy-rs) | References/proxies do not supply a validated standalone comma sender |
| This checkout exposes vehicle/system telemetry and hardware H.264 encoder code | [Architecture source map](architecture.md#existing-code-to-inspect) | Encoder input, negotiated format, and concurrent capacity remain untested |

The table above records kickoff research. Subsequent [hardware experiments](in-car.md)
established the two-stage AOA descriptors, authenticated command sequence, and
720p video mode. Protocol names such as "car control" do not prove Mazda support
for those optional services.
