# Scalable comma 3X interface

Decision, September 18, 2026: target the familiar **landscape comma 3X onroad
interface**, scaled and adapted to the Mazda's negotiated viewport. Alex prefers
this to a new dashboard design. Reuse the existing renderers and assets wherever
possible. Camera/model integration is part of the desired interface; its resource
cost and hardware compatibility still need measurement.

## Current implementation

The live renderer and local preview reuse the native landscape speed readout and MAX box through
`openpilot/selfdrive/ui/onroad/hud_drawing.py`. This is shared production drawing
code extracted from `hud_renderer.py`; the native renderer calls the same functions.
Its Cereal state updates, unit conversions, and interactions retain their original
call path. Sunnypilot's overridden painters remain in their subclasses.

The renderers use the checkout's exact Inter fonts, steering-wheel icon, and driver
face icon. `tools/android_auto/prepare_assets.py` downloads only those five files
from the configured Git LFS server, verifies the pointer hashes, and caches them
outside Git. It works without the `git-lfs` executable.

`preview.py` remains an explicitly **synthetic fixture** for protocol regression.
Its background road, path, lead, and changing speed are not live data. The
twelve-second clip exercises disengaged, engaged, override, stale, and recovery.
That source label remains visible in every frame.

The separate live pipeline runs entirely on the comma. `live_state.py` preserves
native speed, units, setpoint, MADS, and alert semantics. `live_render.py` uses a
passive Pillow backend; `frame_worker.py` encodes each fresh snapshot with PyAV.
Alex confirmed the live HUD on the Mazda. See [live evidence](live.md).

The full passive road view adds `road_state.py` and `road_render.py`: latest
VisionIPC camera frames, actual device/sensor intrinsics and extrinsics, model
lane/path/edge ribbons, lead chevrons, and native icons. It follows the existing
3X camera crop and model projection mathematics without launching another UI
window or GPU context. The camera and model share a single transform. This is a
read-only onroad display; settings, touchscreen/Commander interaction, fork-specific
extra widgets, and audio remain outside this renderer.

## Scaling contract

Use a logical height of 1080, matching the existing 3X layout, with uniform scaling
in x and y. Adapt logical width to the usable aspect ratio so the HUD's relative
anchors keep working. This avoids stretching fonts/icons and makes use of wide
head units. Reserve negotiated video margins before calculating scale.

```text
usable_width  = video_width  - total_horizontal_margin
usable_height = video_height - total_vertical_margin
scale         = usable_height / 1080
logical_width = usable_width / scale
origin        = (total_horizontal_margin / 2, total_vertical_margin / 2)
```

The prototype's `Viewport` implements symmetric margins. Asymmetric content
insets and pixel-aspect ratios beyond this contract need separate testing. The
actual Mazda selected 1280×720 with total vertical margin 240: 1280×480 usable,
with 120 black pixels above and below. Other local configurations remain fixtures.

Native HUD dimensions and BIG UI font scaling are retained. The actual drawing
area gets the existing 30-unit border. Speed remains centered; MAX and steering
icon stay anchored near their corners. Road/camera overlays must ultimately share
one viewport and calibration transform.

## Reuse map for the final renderer

| Existing code | Role in projection |
| --- | --- |
| `openpilot/system/ui/lib/application.py` | `BIG=1` selects the existing 2160×1080 landscape layout; scaling and render-texture recording already exist |
| `openpilot/selfdrive/ui/onroad/hud_drawing.py` | Shared native speed and MAX painters; used by the working local preview |
| `openpilot/selfdrive/ui/onroad/augmented_road_view.py` | Camera, model, HUD, alerts, and driver-state composition; reuse its layout/calibration approach |
| `openpilot/selfdrive/ui/onroad/cameraview.py` | Camera subscription and GPU drawing; use actual stream/device calibration |
| `openpilot/selfdrive/ui/onroad/model_renderer.py` | Native ribbon, path, gradient, and lead formulas followed by the passive CPU compositor |
| `openpilot/selfdrive/ui/onroad/alert_renderer.py` | Preserve native alert text, severity, and sizing when adding the live adapter |
| `openpilot/selfdrive/ui/sunnypilot/onroad/` | Fork-specific visual/state extensions to evaluate after the base 3X view is working |

`BIG=1` is a useful existing selection mechanism, not a finished projection
launcher. `OFFSCREEN=1` currently disables FPS limiting but still initializes a
window. `MainLayout` also owns settings, bookmarking, and interactive controls.
The projection process should compose passive renderers into its own offscreen
target, with independent initialization and cleanup. Do not launch a second
fullscreen native UI on the comma's display as a shortcut.

## Freshness and resource contract

The camera subscriber conflates frames, validates acquisition timestamps and
camera-state metadata, then copies the selected NV12 buffer before conversion.
It checks the producer's frame ID around the copy to detect reuse. No camera
server, vehicle publisher, or parameter writer is created.

Camera and model have independent 350 ms freshness gates. A model overlay also
needs valid calibration and camera/model acquisition times within 150 ms and
frame IDs within three frames. Unknown intrinsics or dimensions suppress the
road view; expired calibration suppresses model graphics. Missing camera/model
data leaves the fresh HUD available with an explicit unavailable indication.
Fresh driver/radar data is gated separately. The sender's 500 ms budget includes
the age of the sources actually drawn, including camera when HUD data is stale.

The current default is 8 source frames/s in the proven 30 fps codec mode after
CPU profiling. Measure the whole projection process tree and keep `--view hud`
available. The worker uses one
outstanding request, a fixed encoded-frame buffer, and a parent watchdog; the
service retains its CPU warning/stop policy and memory cap. Hardware acceleration
can be evaluated later if measured CPU load warrants it.

See [the live runbook](live.md) for current commands and hardware evidence, and
[the local video runbook](video.md) for the independent synthetic regression path.
Physical focus/reconnect tests are deferred while Alex is away from the car.
