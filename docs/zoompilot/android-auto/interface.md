# Scalable comma 3X interface

Decision, September 18, 2026: target the familiar **landscape comma 3X onroad
interface**, scaled and adapted to the Mazda's negotiated viewport. Alex prefers
this to a new dashboard design. Reuse the existing renderers and assets wherever
possible. Camera/model integration is part of the desired interface; its resource
cost and hardware compatibility still need measurement.

## What works tonight

The local preview reuses the native landscape speed readout and MAX box through
`openpilot/selfdrive/ui/onroad/hud_drawing.py`. This is shared production drawing
code extracted from `hud_renderer.py`; the native renderer calls the same functions.
Its Cereal state updates, unit conversions, and interactions retain their original
call path. Sunnypilot's overridden painters remain in their subclasses.

The preview uses the checkout's exact Inter fonts, steering-wheel icon, and driver
face icon. `tools/android_auto/prepare_assets.py` downloads only those five files
from the configured Git LFS server, verifies the pointer hashes, and caches them
outside Git. It works without the `git-lfs` executable.

The background road, green path, lead marker, and changing speed are **synthetic
fixtures**. They are not camera footage or outputs from the real ModelRenderer.
The source label remains visible in every frame. The twelve-second clip exercises
disengaged, engaged, override, stale, and recovery states. Stale state removes the
green path/status, replaces speed and set speed with dashes, and shows
"Data unavailable". This fixture does not yet implement a live telemetry adapter
or the full native alert renderer.

The encoded clip is streamed into stock Google DHU. Received screenshots prove
the renderer → H.264 → encrypted AA → head-unit decoder path, beyond simply
sending packets or viewing a local drawing window. The clip is pre-rendered;
live rendering/encoding during the session is a next integration step.

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
insets, pixel-aspect ratio, and display-specific constraints must be inspected in
the actual Mazda response before extending this contract. Today's 480p, 720p, and
wide configurations are test fixtures, not measurements of the Mazda display.

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
| `openpilot/selfdrive/ui/onroad/model_renderer.py` | Real lane/path/lead drawing; replace the preview's synthetic road/path fixture |
| `openpilot/selfdrive/ui/onroad/alert_renderer.py` | Preserve native alert text, severity, and sizing when adding the live adapter |
| `openpilot/selfdrive/ui/sunnypilot/onroad/` | Fork-specific visual/state extensions to evaluate after the base 3X view is working |

`BIG=1` is a useful existing selection mechanism, not a finished projection
launcher. `OFFSCREEN=1` currently disables FPS limiting but still initializes a
window. `MainLayout` also owns settings, bookmarking, and interactive controls.
The projection process should compose passive renderers into its own offscreen
target, with independent initialization and cleanup. Do not launch a second
fullscreen native UI on the comma's display as a shortcut.

## Next integration steps

1. Prove USB and negotiated video on the Mazda with the known-good clip.
2. Feed rendered frames into a bounded encoder/sender path during the session,
   using the head unit's selected resolution, frame rate, and usable viewport.
3. Attach read-only Cereal snapshots with age/validity handling. Reuse the fork's
   actual speed, cruise, MADS, and alert semantics rather than the synthetic
   fixture's simplified state names.
4. Subscribe to VisionIPC and compose the existing ModelRenderer using the real
   comma four camera/calibration data. Keep a HUD-only fallback if the added
   encoding or readback load is too high for the demo.
5. Measure GPU contexts, readback, encoder load, and coexistence with the native
   comma four UI. Keep native alerts and control processes independent.

See [the local video runbook](video.md) for commands, artifacts, and measured
results. No actual-car milestone has passed from these local tests.
