# Driver camera preview

Open **Device → Driver Camera → PREVIEW** in the projected settings. This is the
first Device row, accessible with the Commander. Automaxxing
replaces that menu action's older 3X preview with a landscape adaptation of the
C4 preview: mirrored/enhanced cabin camera, face box, eye and sunglasses
indicators, animated driver pose, awareness percentage, and monitoring alerts.
The camera fills the usable display behind compact translucent controls and a
small monitoring panel in the lower-right corner. On the Mazda this is
1280×480, approximately 1.9× the previous 854×382 camera area. The C4 camera
composition and face box use the same uniform scale; overflow is cropped to
fill the wider display rather than stretched. Mirroring and enhancement remain
the C4's own. The tested C4 supplies a native 1344×760 cabin-camera buffer,
sampled directly by the GPU. The normal physical-display previews are unchanged.

The preview is available either offroad or with fresh vehicle data confirming
**Park, speed below 0.01 m/s, and no active longitudinal or lateral control**.
This supports a demo while the Mazda's ignition powers Android Auto. Leaving
Park, movement, engagement, or stale vehicle state closes the preview.

Turn the Commander to select **Back** or **Reset monitoring** and click normally.
The hardware Back button returns to Device settings; Home returns to the main
view. Music/Navigation closes the preview before yielding to Mazda Connect.
Five minutes without input also closes it.

With the vehicle off, the physical C4 UI owns `IsDriverViewEnabled` and the
preview's `selfdriveState` sound publisher. The projected renderer requests
these through a session-matched, expiring heartbeat. Reset monitoring clears
`DriverTooDistracted`, matching a tap in the C4 preview. Sounds remain on the
comma speaker; Android Auto does not acquire audio focus.

With ignition on in Park, the preview reads the existing camera and monitoring
streams. It does not enable offroad demo mode, clear the distraction lockout, or
publish `selfdriveState`. The reset control is disabled and labeled **Live
monitoring**. The running system retains its normal alert/audio behavior.

Local-screen return, OEM focus loss, or a failed/stalled renderer expires the
offroad preview lease within 0.5 seconds. Back/close revokes it immediately.
An existing physical-display preview keeps ownership. Ignition transition
releases the preview publisher without sending over the running driving-state
publisher. Expired cabin frames are replaced with an unavailable message, and
expired monitoring data hides the face/awareness overlays.

Offroad detection checks fresh raw panda ignition as well as `deviceState`:
the demo parameter blocks the normal started transition, so waiting for
`deviceState.started` alone would prevent driving startup. The preview closes
on ignition; it can be reopened once fresh Park/disengaged data is available.

While the offroad driver-view parameter is set, hardwared keeps the model's
CPU core and the speaker awake even when the local backlight is blank. Manager
waits for core 7 to come online before launching offroad camera/monitoring
processes. Clearing the lease restores the ordinary power policy. Onroad
process startup is unchanged. Offroad demo monitoring accepts the policy's
otherwise invalid envelope (driving inputs are absent), but still requires
fresh valid driver-model output; parked onroad mode requires valid data throughout.

`driver_preview_probe.py` exercises the actual Device-menu Commander target and
camera preview on an offroad C4 using private handoff mailboxes. It checks live
camera/monitoring, reset, lease expiry, Back, Home, OEM exit, inactivity, and
callback cleanup. Its images remain private device diagnostics. The local
regression suite additionally covers the parked-access gates and lease owner.

## Device verification — 2026-09-20

A normal offroad reboot loaded `c3387288f` from the persistent `automaxxing`
branch. The C4 probe opened the actual Device-menu action with a Commander
click and rendered fresh cabin-camera and monitoring data (58 ms source age
at capture). Core 7 was online while previewing. Reset, lease expiry, Back,
Home, Music/OEM exit, the five-minute inactivity deadline, and callback cleanup
all passed. The camera-enable flag was clear afterward; camera and monitoring
processes stopped normally, and the native UI and all expected manager processes
remained healthy. Local regression checks passed: 291 tests and Ruff.

This verification used the C4 with ignition off and an isolated projection
renderer; its landscape layout was visually checked. The ignition-on Park gate
is covered by tests; the new preview still needs a
physical Mazda display/Commander check in Park. Private diagnostic images and
results are under `/data/automaxxing/driver-preview-probe-v5`, outside Git.

The full-screen overlay revision also passed the live C4 probe, including
Commander Reset, Back, Home, Music, idle expiry, and camera cleanup. Its layout
check additionally uses isolated synthetic face/eye/sunglasses/alert data in
the offscreen renderer; those samples are never published to the device's
monitoring or audio services. This checks small-overlay composition separately
from the real live-stream test. Diagnostics are saved privately under
`/data/automaxxing/driver-preview-overlay-probe-v4`.
