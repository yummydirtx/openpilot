# Hardware validation and experiment runbook

Status: all device/car experiments pending. Follow the gate order in the
[implementation plan](implementation-plan.md).

## Test setup

- Park the Mazda for protocol and display development. Keep replay/synthetic
  transitions visibly labeled; they do not represent actual engagement.
- Power comma four through the normal installation. Connect the separate data port
  to Mazda's Android Auto USB port using an appropriate data-capable cable. OBD-C
  carries vehicle signals and is not the infotainment USB connection.
- Use a development laptop over the existing management connection for logs and
  deployment. Remove it from the final demonstration's runtime dependencies.
- Record normal behavior before making USB changes. Preserve a management route
  that does not depend on the USB function being reconfigured.

An Android phone may optionally verify the head unit/cable baseline or supply
reference captures. It is not required by the design and must not participate in
the final projection session.

## G0: read-only device inventory

Run on the **comma**, not on the development Mac. These commands inspect state;
they do not create a gadget, switch a role, or transmit vehicle commands. Missing
paths are evidence to record, not an instruction to create them.

```sh
uname -a
cat /etc/os-release
ls -l /sys/class/udc
ls -l /sys/class/usb_role
ls -l /sys/class/dual_role_usb
ls -l /sys/class/typec
ls -l /sys/kernel/config/usb_gadget
mount | grep -E 'configfs|functionfs'
lsusb -t
```

Where available, inspect the running kernel configuration:

```sh
zcat /proc/config.gz | grep -E 'CONFIG_USB_(GADGET|DWC3|CONFIGFS|F_FS|F_ACC)'
```

Collect recent USB/controller messages with `dmesg` where permitted. Inspect the
roles, bound UDC, functions, and descriptors of any existing gadget individually
after identifying the relevant paths. Save that state before modifying it.

Record device model, deployed software revision, kernel/AGNOS version, head-unit
firmware, cable/port identity, existing gadget users, baseline CAN health, process
health, CPU/GPU/memory/thermal metrics, and encoder use under the tested workload.
Kernel configuration flags alone do not pass G1.

## G1–G3: connection and first pixels

1. Establish which controller maps to the separate data port. Verify enumeration
   against a development host if useful, then test the real Mazda.
2. Implement setup and cleanup as a pair. Record prior state and only claim the
   controller/functions identified for the experiment. Restore state after failure.
3. Capture accessory negotiation and the transition to usable transport. Record
   descriptors and endpoint behavior from evidence; do not invent a universal set.
4. Capture protocol negotiation, authentication outcome, service discovery,
   advertised display modes, video focus, and channel startup.
5. Send a supported H.264 test pattern containing a moving counter. A static image
   alone does not establish sustained video or working acknowledgements.
6. Stop/restart the sender, then unplug/replug the cable. Record whether a fresh
   session and decodable video return without rebooting the comma or Mazda.

After a failure, identify the last completed stage. Examples: no UDC; wrong port
role; enumeration but no transport; transport but failed authentication; negotiated
session but rejected video; first frame then stalled acknowledgements. Change one
variable at a time. Keep a known-good pattern independent of the live renderer.

Use logs and captures from the user's own setup. Keep credentials, private keys,
VINs, account identifiers, and unrelated route data out of committed evidence.
Store detailed captures in the existing private research location or another
explicitly chosen local directory; commit sanitized findings and artifact pointers.

## G4–G5: dashboard acceptance matrix

All rows below are pending. Ten minutes, three reconnects, and the 500 ms stale
threshold are initial project targets and may be revised explicitly with evidence.

| Check | Procedure | Pass condition |
| --- | --- | --- |
| Direct runtime | Disconnect development helpers; show physical cable path | Comma-generated dashboard remains on Mazda; no phone, dongle, or companion computer |
| Live source | Display local live telemetry while parked | Values agree with current telemetry/native UI and are labeled live |
| State semantics | Feed recorded/synthetic enabled, disabled, overriding, and alert cases | Correct state/priority; persistent replay/synthetic label |
| Units/set speed | Exercise mph/km/h, unset setpoint, and valid zero speed | Same conversions and validity conventions as current fork; no misleading setpoint |
| Stale source | Stop or invalidate required telemetry in an isolated demo source | Within configured threshold, dependent values/state become unavailable; no frozen active indicator |
| Sustained video | Run for ten minutes with changing content | No unintended disconnect, unbounded queue growth, or accumulating frame delay |
| Reconnection | Unplug/replug three times | Fresh session and current data return each time without reboot |
| Sender failure | Stop/crash experimental sender | Openpilot control/CAN processes and native alerts continue; experimental USB state can be restored |
| Head-unit interruption | Leave AA or invoke a parked head-unit interruption | Focus/session is handled cleanly; return behavior and any limitation are documented |
| Resource coexistence | Compare equivalent workloads with projection off/on | No new control deadline misses, CAN faults, or process restarts; memory, temperatures, and encoder behavior are recorded |
| Cleanup | Stop prototype and execute documented cleanup | Prior USB setup and normal device/head-unit operation restored |

A parked test cannot establish driving-load performance. Use representative
replay/bench workloads for early concurrency checks and mark onroad coexistence
unverified unless actually measured. The hackathon display supplements native
openpilot indications; it is not the only alert surface.

## Experiment record template

```text
Experiment ID / gate:
Date and operator:
Hypothesis:
Device, AGNOS/kernel, Zoompilot revision:
Sender/dependency revisions and build options:
Mazda/head-unit firmware, cable, ports, power arrangement:
Input mode (pattern / synthetic / replay / live):
Preconditions and baseline:
One change made:
Exact commands/procedure:
Last successful USB/protocol stage:
Observed result and repeatability:
Negotiated mode / timing / resource measurements:
Logs, capture, and photo/video locations:
Cleanup and restoration result:
Conclusion (observed / inferred / still unknown):
Next experiment:
```

Before declaring the project complete, link the actual G5 evidence from the
implementation plan and record remaining limitations in the project brief.
