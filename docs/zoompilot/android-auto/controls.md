# Display switching and Commander controls

Implemented September 19, 2026. The comma four's home page has a **Use Mazda
display** button. It starts the supervisor if needed, then starts or resumes AA.
While connecting, the native display keeps drawing. Only acknowledged, fresh
projected frames allow native drawing and the backlight to turn off. Touch input
remains active; the panel is not power-gated.

Touch the comma screen to return locally. The entire waking gesture is consumed,
including all touch slots, so it cannot activate an underlying control. Local
selection blocks delayed AA focus grants until another explicit selection.
Critical alerts, expired video freshness, and expired supervisor heartbeats
restore the local screen. The native UI and state updates keep running.

This switches between the native four UI and the existing passive 3X-style
projection compositor. It avoids continuous drawing of both views while
projection is healthy. Replacing Pillow with the actual native GPU road renderer
and hardware encoding remains separate work; these controls do not implement it.

## Projected controls

Press the knob to open the menu. Rotate or push in any direction to move the
blue selection; press to select. The menu sits to the right of the speed display.

| Item / control | Result |
| --- | --- |
| Road view | Camera, model graphics, speed, and alerts |
| Speed and alerts | HUD-only view |
| Use comma display | Restore four UI, pause projection, request OEM video focus |
| Mazda Connect | Yield video focus to OEM without disconnecting USB |
| Back | Close menu; from road view, open menu with Mazda Connect selected |
| Home event | Close menu and return to projected road view |
| Music / Navigation event | Yield to OEM; selecting a particular OEM app is not implemented |

No projected action changes driving parameters or injects native touch events.
Native alerts take precedence over the menu. Projection never requests audio
focus. Verify OEM music continuity physically. Mazda's documented long-press
Home route remains receiver-owned. After an on-screen OEM exit, we resume only
after observing native focus followed by a receiver grant marked unrequested.
The local comma button can always explicitly request projection again. Actual
Mazda focus flags and physical shortcut behavior remain pending verification.

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

- DHU 2.1 emitted actual protocol messages for click, both rotations, all four
  directions, Back, Home, Media, and Navigation. All eleven decoded correctly;
  OEM focus release, projection focus return, and shutdown passed (`input-probe-01`).
- Mazda accepted input binding on channel 5. The parked `controls-car-01` run
  acknowledged **249/249 frames**, zero drops, maximum capture age **104.36 ms**,
  maximum ACK delay **13.88 ms**. No physical buttons were pressed in this run.
- Native UI was restored under normal manager supervision after reload;
  `uiDebug` was alive and no expected manager process was missing.
- **230 tests** cover existing projection behavior plus input bounds/ordering,
  duplicate press suppression, selection, leases, critical alerts, multi-touch
  consumption, launcher failures, and exact installation rollback. Ruff passes.
- Final DHU rerun `input-probe-final` passed all eleven input actions and both
  focus transitions. On-device targeted tests passed 27 cases, with the desktop
  asset-dependent overlay case skipped. The exact supervisor bootstrap command
  recovered after a stop; final native mode is local and projection is stopped.

Still needed while parked: inspect the local button, start AA with it, verify
blanking and tap-to-return, exercise every Commander button and both exits,
test encoder/supervisor failure while projecting, check OEM music, reconnects,
and repeated handoffs. Driving-load validation remains outstanding. These results
do not establish a frame-rate improvement or certify on-road readiness.

Repeat the local protocol test with a fresh output directory:

```sh
.cache/automaxxing/venv/bin/python -m tools.android_auto.input_probe \
  --output .cache/automaxxing/input-probe-new
```
