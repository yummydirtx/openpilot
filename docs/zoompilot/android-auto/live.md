# Live comma-to-Mazda road display

September 19, 2026: the comma now generates, encodes, and sends a live vehicle HUD
directly to the Mazda over USB. Alex confirmed the live `0 mph` display while
parked. The first complete run acknowledged **1,373 of 1,373 frames**. This is a
working parked prototype. The full road view subsequently completed direct
Mazda delivery: **1,360 of 1,360 frames in 174.03 seconds**, with zero drops and
clean shutdown. The remaining validation below has not established onroad readiness.

The default `road` view now combines the real road camera with calibrated native
model path, lane lines, road edges, and lead chevrons. It reuses the native
landscape speed/MAX painters and Inter fonts, wheel/experimental icon, driver
face pose, engagement colors, and text alerts. A `hud` view remains available as
a lower-cost fallback. The scope is the passive 3X onroad display: no settings,
touch interactions, projected alert audio, or head-unit control of the car or
native UI. Native openpilot retains its own display, sounds, and controls.

Camera/model availability and the delivery evidence below are separate. Missing
or expired sources are suppressed and labelled; live rendering never substitutes
a synthetic road or stale model geometry. At the user's request, physical
replug/focus and endurance testing has been deferred while the road view is
implemented. Those gates remain open.

See [the first USB result](in-car.md), [interface design](interface.md),
[authentication](authentication.md), and [validation gates](validation.md) for
the earlier work and hardware constraints.

## Runtime and isolation

The deployed project lives in `/data/automaxxing`. The installed
`/data/openpilot` checkout is unchanged. Its existing hydrated fonts are read
from `/data/openpilot/openpilot/selfdrive/assets`; one copy of the project's
shared `hud_drawing.py` is deployed under `/data/automaxxing/native` so the
experiment does not replace installed UI modules.

| Component | Responsibility |
| --- | --- |
| `live_state.py` | Subscriber-only Cereal access, read-only Params, native speed/unit/status semantics, source freshness |
| `road_state.py` | Conflated VisionIPC subscription, owned NV12 copies, actual sensor/intrinsics/calibration, model and driver snapshots |
| `live_render.py` | CPU Pillow drawing through the shared native painters; no window, EGL, DRM, or native UI instance |
| `road_render.py` | Matched native camera crop and model projection, path/lanes/edges/leads, driver face geometry |
| `live_encode.py` | PyAV 16.1.0 / libx264, baseline H.264, one encoder thread, bounded access units |
| `frame_worker.py` | Spawned renderer/encoder process, one outstanding request, fixed shared frame buffer |
| `live_session.py` | Authenticated video delivery, bounded acknowledgements, focus handling and keyframe requirements |
| `runtime.py` | Session lifecycle, freshness and CPU checks, reconnects, owned USB gadget cleanup |
| `service.py` | Manual transient systemd service, start/stop/status |
| `live_preview.py` | Bounded real-device renderer/encoder probe without USB or head-unit display |
| `monitor.py` | Independent parked coexistence sampling and baseline comparison |

PyAV is installed only under `/data/automaxxing/deps`; Pillow, NumPy, and pyray
already exist in the device's Python environment. No native camera encoder or control
publisher is created. Importing pyray for painter colors does not initialize a
graphics context.

The service and its children use nice level 15, CPU affinity `0 1 2 5 6`, a
384 MiB memory limit, and systemd `KillMode=control-group`. The device exposes
the memory cgroup controller but not a working CPU quota controller. Therefore
**there is no enforced cgroup CPU quota**. The runtime samples cumulative CPU
time for the sender and frame worker and stops an attempt after five consecutive
samples above 85% of one core. This check excludes the USB bridge; independent
monitoring is needed to measure the whole process tree and concurrent openpilot
behavior.

Native planning work uses core 5 and camerad uses core 6; these are shared cores,
not spare capacity. Native FIFO work preempts this normal scheduling class, and
Nice 15 reduces its weight relative to ordinary native processes. Neither
guarantees their deadlines. A cores-0–2-only experiment could not keep successive
camera frames within the unchanged freshness budget. The 8 fps default and
process/health measurements therefore accompany the original wider affinity.
Cores 3, 4, and 7 remain excluded.

The service is manual and transient: it is not added to openpilot's manager or
enabled at boot. Each run has a duration limit and fresh output directory.

## Deploy an update

Stop the existing transient service before replacing its Python files. The
commands below assume the established `automaxxing-comma` SSH alias and existing
private identity directory from the successful USB experiment.

```sh
ssh automaxxing-comma 'cd /data/automaxxing && sudo -n /usr/local/venv/bin/python -m tools.android_auto.service stop'
```

From the development checkout, stage public project files and install them into
the isolated deployment. No certificate or private key is copied by these
commands:

```sh
automaxxing_stage=$(ssh automaxxing-comma 'mktemp -d /tmp/automaxxing-deploy.XXXXXX')
ssh automaxxing-comma "mkdir '${automaxxing_stage}/modules'"
scp tools/android_auto/*.py "automaxxing-comma:${automaxxing_stage}/modules/"
scp openpilot/selfdrive/ui/onroad/hud_drawing.py "automaxxing-comma:${automaxxing_stage}/hud_drawing.py"
ssh automaxxing-comma "sudo -n install -d -m 755 /data/automaxxing/tools/android_auto /data/automaxxing/native && sudo -n install -m 644 '${automaxxing_stage}'/modules/*.py /data/automaxxing/tools/android_auto/ && sudo -n install -m 644 '${automaxxing_stage}/hud_drawing.py' /data/automaxxing/native/hud_drawing.py"
```

For a new deployment, provision the matching binary PyAV wheel into the isolated
directory using the device's Python 3.12 environment:

```sh
sudo -n /usr/local/venv/bin/python -m pip install \
  --only-binary=:all: --no-deps --target /data/automaxxing/deps av==16.1.0
```

The current device already has this dependency and the private
`identity/phone-cert.pem`, `identity/phone-key.pem`, and `identity/root-cert.pem`.
Identity creation and expiry are covered in [authentication](authentication.md).
These files and raw session artifacts stay outside Git.

## Start, observe, and stop

Keep the car parked for the remaining validation. Leave the normal comma
power/CAN installation connected and connect the Mazda's wired Android Auto port
to the separate comma data port.

On the comma:

```sh
cd /data/automaxxing
sudo -n /usr/local/venv/bin/python -m tools.android_auto.service start \
  --duration 600 --output live-next --view road --fps 8
sudo -n /usr/local/venv/bin/python -m tools.android_auto.service status
sudo -n cat /data/automaxxing/live-next/status.json
```

Use a new output name for each run. The default is `--view road --fps 8`;
`--view hud` selects the telemetry-only fallback. `--fps 10`, `15`, or `30`
requests a higher source update rate; the negotiated H.264 mode remains
30 fps, and actual delivery can be slower under the bounded worker. `--once` exits after a session
failure rather than retrying. The duration covers startup and reconnect time as
well as streaming, so a 600-second service need not contain 600 seconds of video.

Stop explicitly when finished:

```sh
sudo -n /usr/local/venv/bin/python -m tools.android_auto.service stop
```

Service logs and per-attempt evidence:

```sh
sudo -n journalctl -u automaxxing.service --no-pager -n 80
sudo -n cat /data/automaxxing/live-next/summary.json
```

Each `session-NNN` directory contains `events.jsonl`, `bridge.log`, and
`result.json`. The current USB runtime does **not** save PNGs in its frame worker:
first-camera PNG compression caused a startup watchdog failure during testing,
so diagnostics must not delay frame production. Earlier HUD experiment
directories may contain images generated before this change.
`status.json` reports the current phase, counts, acknowledgement and capture-age
maxima, source state, and worker PID. A stopped or failed service does not imply
that a physical reconnect or endurance validation passed; inspect its results.

Before a new road-renderer build uses USB, exercise the same live worker on the
comma and inspect its source images and timings:

```sh
cd /data/automaxxing
PYTHONPATH=/data/automaxxing/deps:/data/automaxxing:/data/openpilot \
  OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 \
  /usr/local/venv/bin/python -m tools.android_auto.live_preview \
  --duration 20 --fps 8 --view road --output preview-next
```

This probe neither opens a USB gadget nor verifies anything displayed by the
Mazda. The projection service remains the resource-contained execution path;
do not treat successful preview timings as whole-system coexistence evidence.
The preview saves first-occurrence `first-live.png`, `first-stale.png`,
`first-camera.png`, and `first-model.png` when the corresponding presentation
occurs. Identical first-occurrence images may be hard links to one PNG.

## Camera and model alignment

On this comma four, both observed road streams are `mici`/`os04c10`, 1344×760.
The narrow intrinsics use focal length 1141.5 and optical center `(672, 380)`;
the wide focal length is 425.25. These come from the running camera/device
identity and native `DEVICE_CAMERAS`, not a fallback to older AR0231 hardware.
The reader respects VisionIPC's actual stride and UV offset, copies the shared
buffer into owned memory with a frame-ID consistency check, and converts it to
RGB before rendering. VisionIPC buffers are never retained for asynchronous use.

The renderer follows native `AugmentedRoadView`'s calibrated vanishing-point
crop and zoom in a 1080-high logical viewport. Exactly the same pixel-to-display
transform multiplies the calibrated model projection. Path height is added to
the model path and lead positions; lane and edge coordinates retain the native
zero height offset. Model/camera timestamp and frame-ID checks suppress badly
mismatched overlays. All model drawing is clipped to the camera content area.
Metadata counts actual path/lane/edge/lead polygons drawn; a valid model whose
geometry is wholly outside the view is not reported as a visible model overlay.

Without valid calibration, a fresh camera can remain visible with
“Calibration unavailable” and no model overlays. Missing/stale model data leaves
the fresh camera visible with “Model unavailable.” An expired camera is removed
and the live HUD fallback remains. Driver pose and lead state have their own
freshness checks; missing data never becomes a fabricated icon pose or lead.

## Authentication, reconnects, and focus

Every attempt requires head-unit certificate verification against the imported
root in addition to the head unit accepting the phone identity. Failed checks
stop that attempt; there is no trust or clock bypass. The negotiated Mazda mode
is checked against the renderer: 1280×720, total vertical margin 240, leaving a
1280×480 usable area with 120 black pixels above and below.

The sender continues processing keepalives and focus messages while the worker
builds a frame. It allows at most two unacknowledged frames, even though the
Mazda advertises four. On loss of focus it stops sending. Focus regain creates a
new media session and requires SPS/PPS plus an IDR; a late acknowledgement from a
retired media session cannot release credit in the new one. Dropping an encoded
frame also forces an independently decodable frame before resuming.

Cable/session failures trigger bounded retries within the overall run duration.
Each retry recreates the worker, transport, and two-stage accessory negotiation.
An explicit head-unit ByeBye is acknowledged and ends the run, respecting the
request to leave projection. Unsupported channels/messages fail the attempt;
input and audio channels are not opened.

Normal exit stops the worker and bridge before removing only the owned gadget.
A root-owned lease permits systemd's `ExecStopPost` recovery after its entire
process group has stopped. Recovery refuses unexpected descriptors, functions,
or links. This avoids modifying an unrelated gadget if device state differs
from the tested setup.

## Freshness behavior and its limits

The source adapter checks both receipt and publisher timestamps. The live worker
uses a 350 ms threshold for car state, selfdrive state, Sunnypilot state, and
panda state, reserving part of the 500 ms presentation budget for rendering and
delivery; lower-rate
device state and onroad events have appropriate longer bounds. Missing or invalid
required services produce an unavailable presentation. Fresh text alerts can
remain visible when another stream is missing.

Camera, model, lead, and driver snapshots also have 350 ms source gates and are
checked again at paint time. The frame metadata budgets only sources actually
displayed, including a fresh camera when vehicle telemetry is unavailable.
Experimental-mode/engageable wheel decisions and path throttle-color decisions
also contribute their own source age. Expired mode state produces a neutral
wheel; expired required throttle state resets the path to neutral white
immediately rather than fading a previously active color through expiry.
Road composition reports the oldest absolute source timestamp among the layers
actually drawn. The sender computes age from that timestamp, so camera polling,
drawing, and encoding delay are counted once rather than repeatedly subtracted
from the same 500 ms budget.

The worker has one request and one shared buffer, so old video cannot accumulate
in a frame queue. Each request polls current data. Its 500 ms production watchdog
is enforced by the parent, which can terminate a hung encoder. Before delivery,
the parent discards captures older than 250 ms and active frames whose source
age plus pipeline age exceeds 500 ms. Poll duration is included conservatively
in source age. It also checks the current presentation's source-expiry budget
and a 500 ms acknowledgement bound; violations terminate the attempt and run
cleanup/reconnect.

These are software freshness checks, **not a guarantee that the Mazda screen
becomes blank within exactly 500 ms**. Scheduling, socket/TLS operations, process
termination, USB teardown, and the receiver's behavior can extend the time an
old frame remains visible. Capture age measures the local snapshot-to-send path,
and acknowledgements measure receiver responses; neither is a measured
end-to-end screen latency. A parked SIGSTOP trial exercised the failure path;
additional visual failure tests remain part of validation.

## Measured results so far

### Full road view

`road-live-08` used the real camera/model subscriptions, default 8 fps source
pacing, the original wider affinity, and strict Mazda authentication. It sent and
received acknowledgements for 1,360 frames over 174.03 seconds, approximately
7.8 fps. Maximum local snapshot-to-send age was 100.31 ms; maximum acknowledgement
delay was 27.85 ms. No frame was discarded and shutdown was acknowledged.
All 173 nonempty one-second status samples showed live camera and model drawing.

Final-code confirmation `road-live-09` acknowledged 729/729 frames in 93.25
seconds, with zero drops, maximum local capture age 98.01 ms and acknowledgement
38.43 ms. Strict authentication, orderly shutdown, and owned-gadget removal passed.
The installed `/data/openpilot` checkout remained unchanged.

The inspected real-camera preview faces a nearby wall. It shows the camera,
native speed/MAX, wheel, and driver graphics; that scene does not demonstrate
road path or lead placement. Current geometry counts show a lane ribbon, with
no visible path or lead in that parked scene. Native-math and labeled fixture
tests cover those renderers; physical road-scene alignment remains unverified.
Alex has not yet visually confirmed this full view on the Mazda screen.

Earlier attempts exposed and fixed camera-PNG diagnostic latency in the live
path and double-counted road processing time in freshness accounting. PNGs now
stay in the separate preview tool; visible road sources use absolute timestamps.
The 500 ms budget was retained. Restricting all rendering to cores 0–2 also
failed that budget, so it is not the deployed configuration.

### Earlier HUD-only milestone

These delivery results are from the HUD-only milestone and do not measure the
additional camera/model workload.

| Evidence | Observation |
| --- | --- |
| `live-01`, direct Mazda USB | 1,373 sent / 1,373 acknowledged in 53.77 seconds; all delivered frames marked live |
| Source pacing | Requested 30 updates/s; first-run effective delivery approximately 25–26 frames/s |
| Local capture age | Maximum 143.35 ms before send |
| Receiver acknowledgement | Maximum 38.56 ms |
| Predelivery discard | One frame discarded; next delivery required a fresh IDR |
| Visible display | Alex confirmed live `0 mph` on the Mazda while parked |
| Worker SIGSTOP | Freshness failure detected; automatic session recovery took approximately seven seconds |
| Main-process SIGKILL | Service process-group cleanup and owned-gadget lease recovery passed |
| Later HUD-only completion | 6,306 / 6,306 frames in 234.01 seconds; maximum capture age 82.8 ms, maximum acknowledgement 49.77 ms, one predelivery discard; clean shutdown |
| Isolated copied-subscription invalidation | Unavailable HUD encoded at 457 ms; real subscription remained active and recovered. No USB or head-unit timing evidence |

The checked native painter change also preserved all drawing arguments across
80 comparisons of units, status, cruise-set state, and viewport width. Renderer
tests include black margins, unavailable data, preserved critical alerts,
partial-engagement colors, actual H.264 decode, forced IDR, and worker hangs and
crashes. Road-renderer checks additionally compare 36 camera/calibration
transforms, nine polygon cases, and lead geometry against the native source,
verify exact native driver landmarks, and check camera pixel alignment, scissor
boundaries, stale-source fallback, and age accounting for visible leads/driver
pose. Camera frames bypass the static HUD cache so unchanged speed cannot freeze
the road picture.

Final focused check: **202 tests pass**, with Ruff and diff formatting clean.

Run the focused suite from the development checkout after preparing the local
assets and optional renderer dependencies described in
[local development](local-development.md):

```sh
.cache/automaxxing/venv/bin/python -m unittest discover \
  -s tools/android_auto -p 'test_*.py'
```

### Parked coexistence observations

The observed `interruptRateCan2` fault and approximately 101–102
`safetyTxBlocked` increments per second were already present in the baseline.
They cannot be attributed to projection merely because the cumulative counters
continued increasing during a run.

The raw `cumLagMs` metric grew approximately 4.5 ms/s in baseline and 4.7 ms/s
during the first active sample. It is a cumulative phase offset from a fixed
100 Hz schedule, **not message latency or instantaneous control execution
time**. Compare trends from adjacent captures in the same boot/control session,
and investigate changes alongside service validity, events, process continuity,
temperatures, and complete process-tree resource use. These short parked
observations do not establish control-deadline or onroad safety margins.

`monitor.py record` accepts repeated `--pid` arguments for process-tree sampling;
record adjacent baseline and projection captures and use `monitor.py compare`
to inspect their differences. Its report explicitly records the limits of these
measurements.

Earlier captures, including `coexistence-reconnect-03` and
`coexistence-road-live-08`, observed only the main PID. The running kernel omits
`/proc/PID/task/PID/children` even for root. These captures cannot establish total
projection CPU usage. The monitor now falls back to one bounded PPID scan of
`/proc/*/stat` per sample and explicitly reports complete/incomplete tree coverage.
It reads no process command lines or environment values.

The final `coexistence-road-live-09` capture covers all four service processes
in all 60 samples. Their combined CPU use averaged 71.25% of one core, with a
93.80% one-second maximum; summed RSS peaked at 150.92 MiB (shared pages can be
counted more than once). The runtime's five-sample sender/worker CPU guard did
not trip. Maximum reported CPU/GPU temperatures were 68.3°C/67.0°C, thermal state
remained `ok`, and all 1,093 observed model-drop samples were zero. Against the
preceding projection-off capture there were no new service/CAN-validity failures,
native process changes, or timing/communication events. Bus 2's error counter
did not increase during this final capture. These are stationary measurements;
control-loop deadline and driving-load margins remain unmeasured.

During the 90-second `road-live-08` health capture, service validity, CAN validity,
native process identities, and timing/communication events had no new failures.
Bus 2's cumulative error counter increased once; it also increased once during
the subsequent 30-second projection-off capture. The existing
`interruptRateCan2` fault and approximately 101 blocked transmits/s persisted.
These observations do not establish a causal effect of projection or resolve
the pre-existing vehicle/interface conditions.

## Deferred validation and remaining work

- A completed ten-minute streaming/endurance run with adjacent baseline and
  active health/resource captures; startup time must not be mistaken for video
  time.
- Three counted physical unplug/replug trials, plus physical Mazda/native-screen
  focus loss and return, with acknowledgement and fresh-IDR evidence.
- Confirmation that the displayed speed, units, setpoint, MADS states, alerts,
  and stale transitions match the actual device across relevant operating
  states. The confirmed live reading so far is stationary `0 mph`.
- Review of the pre-existing device/CAN observations and the remaining
  [validation gates](validation.md) before deciding to proceed onroad.
- Visually inspect real camera/model alignment on the Mazda. Full-road delivery
  timings above do not establish end-to-end display latency or driving performance.
- Compare passive road-view fidelity against the native 3X layout across
  calibration, wide/narrow camera switches, leads, driver pose, and alerts.
  Settings, interactions, and fork-specific extensions are outside this view.
