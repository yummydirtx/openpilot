# Driver camera preview

Open **Device → Driver Camera Preview** in the projected settings. Automaxxing
replaces that menu action's older 3X preview with a landscape adaptation of the
C4 preview: mirrored/enhanced cabin camera, face box, eye and sunglasses
indicators, animated driver pose, awareness percentage, and monitoring alerts.
The camera retains the C4 crop and aspect ratio; status information occupies a
separate column beside it. The normal physical-display previews are unchanged.

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

`driver_preview_probe.py` exercises the actual Device-menu Commander target and
camera preview on an offroad C4 using private handoff mailboxes. It checks live
camera/monitoring, reset, lease expiry, Back, Home, OEM exit, inactivity, and
callback cleanup. Its images remain private device diagnostics. The local
regression suite additionally covers the parked-access gates and lease owner.
