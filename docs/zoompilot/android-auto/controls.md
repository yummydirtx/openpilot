# Display switching and Commander controls

The comma four launcher is now the first **android auto** category in its
Sunnypilot settings menu, using the same `SettingsBigButton` as adjacent categories.
It starts the supervisor if needed and requests projection. The native display
keeps drawing until the head unit acknowledges a fresh projected frame. Touch
input remains active while local drawing and the backlight are suppressed.

Touch the comma screen to return locally. The entire gesture is consumed,
including all touch slots. Local selection blocks late AA focus grants until
another explicit selection. Critical alerts, expired video freshness, and expired
supervisor heartbeats restore the local screen. State updates keep running.

## Native 3X frontend

`--view native` renders the installed `MainLayout`, its original road view,
sidebar, settings panels, dialogs, and keyboard. It uses a separate EGL pbuffer
on the Adreno render node, without opening a window or acquiring DRM master.
The four retains physical-display ownership; its drawing stops during healthy
projection. The worker drops to the `comma` Unix user before importing the frontend;
normal UI cache/default writes must never create root-only parameter files. The native worker updates UI state with `update_display=False`, so
it never competes for brightness or display power. Bookmark requests go back to
the four's existing publisher instead of opening a second publisher.

The Mazda's 1280×480 usable area uses a 2880×1080 logical viewport. Camera import,
scaling, widgets, and the output flip use the GPU. Final RGBA readback goes directly
to the existing bounded H.264 encoder; hardware encoding is not implemented.
Projection hides the path while disengaged and rotates the wheel using live
`steeringAngleDeg`. The installed 3X source normally draws a narrower disengaged
path; hiding it is Alex's explicit projection preference.

| Commander control | Native frontend behavior |
| --- | --- |
| Click/rotate on road view | Open the full native settings interface |
| Rotate / Up / Down | Move focus; scroll offscreen categories and items into view |
| Left / Right | Move focus between columns |
| Click | Reveal hidden focus; otherwise run the selected native widget's original action |
| Back | Close a dialog, return from settings to road, then yield to Mazda Connect |
| Home | Return to the native road/home view |
| Music / Navigation | Yield video focus to OEM; opening a particular OEM app is not implemented |
| Android Auto settings category | Road view, Mazda Connect, or use comma display |

These are the real settings callbacks, with their original visibility, enabled,
offroad and engagement conditions. They can change parameters when the user
selects a setting. Rotary activation is confined to the selected projected widget;
no physical touchscreen input is injected. The old read-only compositor remains
available as `--view road` or `--view hud`.

Focus remains fully visible for three seconds after Commander input, fades over
250 ms, then stays hidden until the next input. Selection is remembered. The first
click after idle only reveals it; rotation both reveals and moves it. Home hides
the indicator immediately. Focus follows the native frontend's painted category,
toggle, and segment bounds independently of its touch rectangles. Scroll clipping
clips the outline without shrinking or moving it onto a different control.

These focus changes have local regression coverage; installation and visual
verification on the Mazda are pending because SSH is currently unreachable.
`native_ui_probe` now captures category focus and its idle disappearance while
continuing telemetry updates, without activating vehicle settings.

The user confirmed that the full native frontend looks good on the Mazda. That
confirms appearance, not a maximum achievable frame rate. The last isolated
render/encode measurement was 26.07 fps; it excluded USB and receiver decoding.
The live request ceiling remains 30 fps, with the existing adaptive CPU budget.
Status now reports actual interval `sent_fps` and `acked_fps`, alongside
`target_fps` and `ack_window`. Native frame metadata separates UI updates, draw
submission, GPU readback (including GPU completion waits), and software encoding.
ACK rate measures receiver acceptance, not physical screen refresh. Hardware
encoding is a candidate for more headroom, but has not been integrated or timed.

Projection never requests audio focus. Verify OEM music continuity physically.
After an OEM exit, resume requires observed native focus followed by a receiver
grant marked unrequested. A local comma selection can request projection explicitly.

## Heartbeat timeout correction

The user reported a return to the four after 30–60 seconds. The first interactive
session ended with `Display supervisor heartbeat expired`, after **348/348 frames
were acknowledged**. Readers sampled time before opening the heartbeat file;
a concurrent atomic replacement could therefore appear to come from the future.
The UI, runtime, and supervisor now sample time after each read. A deterministic
regression exercises this exact race. The 500 ms freshness limits remain intact.
Supervisor mode changes are honored even if the token stays the same, and an
already-local client no longer repeatedly changes its token on a local reply.

High CPU use now reduces the requested cadence before the sustained-load guard
terminates projection. Ten samples of spare capacity are required before increasing
it. The minimum-rate overload guard and all freshness watchdogs remain active.

## Installation and recovery

`install_ui.py` checks every original/source hash, compiles sources, verifies
Park or offroad state, and backs up all native files before modifying them.
Rollback refuses to overwrite subsequent user edits. The original device
baseline is `1662aceda`; affected native files match repository `c072c7a`.

Build, then copy the bundle and `tools/android_auto` to `/data/automaxxing`:

```sh
.cache/automaxxing/venv/bin/python -m tools.android_auto.install_ui bundle \
  --base c072c7a --bundle .cache/automaxxing/native-ui-bundle
```

Place the bundle at `/data/automaxxing/native-ui`, then on the comma:

```sh
cd /data/automaxxing
sudo -n env PYTHONPATH=/data/automaxxing:/data/openpilot \
  /usr/local/venv/bin/python -m tools.android_auto.install_ui install
```

Reload through a normal **parked manager restart or reboot** after installation
or rollback. Do not kill only the UI: this fork's manager does not respawn a
child whose process object still exists. The installer changes no manager source,
CAN code, driving parameters, or sudo policy.

AGNOS `/etc` is read-only. `automaxxing-display.service` lives in
`/run/systemd/system` and is recreated by the button's fixed launcher after
reboot. Projection never starts at boot. The existing bounded worker remains
`automaxxing.service`. The launcher uses the device's existing passwordless sudo.
Diagnostic supervisor start:

```sh
sudo -n /usr/local/venv/bin/python \
  /data/automaxxing/tools/android_auto/install_ui.py start-control
```

Requests contain only local/project selection, a token, and a monotonic heartbeat.
Root-owned status carries the same token and an absolute video lease deadline;
forwarding status never extends that deadline. The native client rejects old
tokens/replies, oversized or malformed files, symlinks, and non-regular files.
Launcher failure leaves the display local. Mailboxes are under
`/run/automaxxing-display` and `/run/automaxxing-display-request`.

Rollback while parked, then reload the native UI as described above:

```sh
cd /data/automaxxing
sudo -n env PYTHONPATH=/data/automaxxing:/data/openpilot \
  /usr/local/venv/bin/python -m tools.android_auto.install_ui rollback
```

Backup: `/data/automaxxing/native-ui-backup`.

## Evidence and remaining checks

- Alex confirmed the previous projection, then reported launcher placement,
  path/wheel behavior, incomplete menus, low cadence, and a 30–60 second exit.
- The new EGL pbuffer initialized on Adreno 630 while the four retained its
  display. A real-camera native 3X preview was captured and visually inspected.
- `native-preview-03`: **529 frames in 20.29 seconds (26.07 fps)** at a requested
  30 fps, about **0.877 CPU core** for the worker. This is renderer/encoder evidence,
  without USB; it does not establish a sustained on-road rate. The first diagnostic
  PNG accounts for a 388 ms maximum; live sessions do not write screenshots.
- `native-menu-01`: rotary navigation reached all **16 categories**, including
  offscreen entries; Back to road and Back to OEM passed. No setting was changed.
- The complete native settings preview was visually inspected: native sidebar,
  category selection, display actions and rotary focus fit the negotiated viewport.
- The first root-run menu probe created four unreadable parameter/cache files.
  Their contents were preserved and ownership restored to `comma`. Native workers
  now drop privileges before UI imports, and the renderer refuses root execution.
  After recovery/reload, manager, UI, device, car and selfdrive topics were all
  seen and alive; Park/0 mph/disengaged were confirmed with no missing expected process.
- **246 local tests** pass, including heartbeat read ordering, disengaged path
  suppression, steering rotation, GPU RGBA H.264 decoding, rotary navigation,
  bounded overload adaptation, and existing transport/install/failure regressions.
- Earlier DHU 2.1 checks decoded all eleven physical input commands and passed
  both focus transitions. The earlier Mazda control run acknowledged 249/249 frames.

Still needed: longer native-frontend AA delivery, visual confirmation on the Mazda,
physical Commander checks in the new menus, repeated handoffs, reconnects, OEM music,
and driving-load validation. The isolated benchmark does not establish on-road
readiness. Keep the car parked for installation and these pending checks.

Reproduce the native checks on the comma (use fresh output paths):

```sh
PYTHONPATH=/data/automaxxing/deps:/data/automaxxing:/data/openpilot \
  /usr/local/venv/bin/python -m tools.android_auto.live_preview \
  --view native --fps 30 --duration 20 --output native-preview-new

PYTHONPATH=/data/automaxxing/deps:/data/automaxxing:/data/openpilot \
  /usr/local/venv/bin/python -m tools.android_auto.native_ui_probe \
  --output native-menu-new
```

Run these under the same resource limits as `service.py` when measuring performance.
The native-menu probe requires a started, parked car and visits category navigation
only. Previews do not claim USB or head-unit delivery.
