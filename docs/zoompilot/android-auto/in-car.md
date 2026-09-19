# First direct Mazda projection

Observed September 19, 2026: **the comma four sends the synthetic 3X HUD directly
over USB to the factory Mazda display**. Alex confirmed the replay appeared and
worked. The 60-second run acknowledged all 1,800 frames and shut down cleanly.
Strict verification of the Mazda certificate also passes on the comma.

This proves the accessory byte transport, real-car authentication, video channel,
and first visible pixels. The clip was rendered on the Mac beforehand; live frame
generation on the comma, telemetry integration, and G5 are still pending.

## Observed setup

| Item | Observation |
| --- | --- |
| Vehicle | Alex's 2021 Mazda CX-5, parked throughout; EPS swap irrelevant to infotainment |
| Head unit's self-report | Mazda CX-5, model-year field `2020`; Panasonic `CMU3A`, software `10.21.03` |
| Device | comma four, internal panda type `cuatro`, panda on SPI |
| Software | AGNOS 19.7, Linux 4.9.103 aarch64, September 2, 2026 kernel build |
| Installed openpilot | `develop`, revision `1662aceda`; unchanged by these experiments |
| Python / TLS | Python 3.12.3, OpenSSL 3.0.13 |
| Data-port controller | `a600000.dwc3`, initially unused, no existing configfs gadget |
| Running kernel | Dual-role DWC3, configfs, FunctionFS, and `f_accessory` built in |
| Vehicle connection | Normal installation remained connected; zero vehicle speed and CAN valid |
| Projection USB | High speed; two-stage Android Open Accessory negotiation |
| Video channel | 9; 720p configuration 0; maximum four unacknowledged frames |
| Chosen video | 1280×720 at 30 fps, vertical margin 240: 1280×480 usable viewport |
| Alternative advertised mode | 800×480, vertical margin 180; not yet projected in the car |

Head-unit self-reported model year is recorded separately from the user's vehicle
year. Serial numbers, Bluetooth addresses, raw discovery, and private identities
remain in ignored/private artifacts, not in this document.

## What the real car required

1. **Accessory negotiation before accessory IDs.** Exposing `18d1:2d00` immediately
   produced one short high-speed enumeration, then disconnect and no AA version
   request. The working attempt first exposes compatibility IDs `12d1:107e`,
   as in the pinned [AACS ModeSwitcher](https://github.com/tomasz-grobelny/AACS/blob/faa1cf208feb5dfe1cb9535be16daeac4f08da0c/AAServer/src/ModeSwitcher.cpp).
   AGNOS's existing accessory driver handles AOA control requests. The host sends
   manufacturer `Android`, model `Android Auto`, then START. We unbind, change to
   `18d1:2d00`, and rebind. Other initial IDs have not been compared.
2. **User acceptance.** Alex accepted the new device as an Android Auto device in
   the Mazda during the first successful connection.
3. **Explicit display focus.** The Mazda accepted video setup but did not send the
   unsolicited focus grant seen in DHU. Sending VideoFocusRequest `0x8007`, with
   field 2 = PROJECTED (1), field 3 = USER_SELECTION (4), resolved the timeout.
   Repeated grants do not restart an already-focused media session.
4. **Buffered accessory reads.** The inspected AGNOS `f_accessory` driver lacks
   `poll` and can discard the remainder of a USB transfer after a short read.
   A separate process reads 16 KiB buffers and bridges to a bounded socket pair.
   The existing framing/TLS/video code therefore retains its partial-read and
   timeout behavior. The bridge is stopped before removing the function.

Reference behavior is documented in the
[AOA specification](https://source.android.com/docs/core/interaction/accessories/aoa)
and the inspected [AGNOS accessory driver](https://github.com/commaai/agnos-kernel-sdm845/blob/master/drivers/usb/gadget/function/f_accessory.c).
The source reference is not a claim that its current master exactly matches this
device's kernel build; the running configuration and successful ioctls were
verified on the device separately.

## Results and evidence

Evidence is under `.cache/automaxxing/car-2026-09-19/` on the development Mac and
`/data/automaxxing/` on the comma. Each USB attempt has events, bridge diagnostics,
and a result JSON. Keys and raw discovery never belong in Git.

| Attempt | Result |
| --- | --- |
| `usb-probe-01` | No attachment in 30 seconds; temporary gadget removed |
| `usb-probe-02` | After physical replug, high-speed configuration then disconnect; no version request; gadget removed |
| `usb-auth-03` | Two-stage AOA, phone certificate accepted, encrypted discovery of nine channels; gadget removed |
| `usb-video-04` | Authentication/setup passed; timed out waiting for unsolicited video focus; gadget removed |
| `usb-video-05` | Explicit focus request; 360/360 frames in 11.969 seconds, maximum one pending; orderly shutdown and cleanup |
| `usb-mutual-06` | Required and verified Mazda certificate against the imported root; phone accepted; discovery passed |
| `usb-video-07` | 1,800/1,800 frames in 59.971 seconds, maximum one pending; orderly shutdown and cleanup; Alex confirmed visible working HUD |
| `usb-video-08-verified` | Strict mutual authentication plus 360/360 video frames in 11.968 seconds; orderly shutdown and cleanup |

The first video runs used the previous one-way verification setting. After the
strict authentication result, the device CLI now requires head-unit verification
by default; `usb-video-08-verified` exercises this default end to end. The DHU's certificate-date failure did not reproduce with the Mazda
and this device's OpenSSL; these are different peers and TLS versions/builds, so
the result does not isolate which difference accounts for it.

A separate comma-to-Mac DHU smoke test over an SSH loopback tunnel passed 360/360
frames. It validates the ARM Python/TLS sender but is not direct-car evidence.
After the focus change, the Mac-to-DHU regression also passed 360/360 frames.
The focused Python suite passes 32 tests covering framing, authentication status,
flow control, explicit/duplicate focus, short writes, and guarded cleanup.

Five-second telemetry snapshots before/after the first video showed all selected
services alive/valid, CAN valid, speed zero, no managed process PID/state changes,
and thermal status `ok`. The panda fault `interruptRateCan2` was present before
USB setup and unchanged afterward. CPU temperatures were approximately 67–73°C,
GPU 66–74°C, and memory usage 70–71% in these snapshots. These observations are
not a control-deadline audit or proof of onroad performance.

## Repeat the parked experiment

The deployed prototype is isolated in `/data/automaxxing`; it does not replace
`/data/openpilot` or install a manager service. The identity directory is private.
SSH alias: `automaxxing-comma`.

On the development Mac, render and validate a new clip before copying it:

```sh
.cache/automaxxing/venv/bin/python -m tools.android_auto.preview \
  --width 1280 --height 720 --margin-height 240 \
  --output .cache/automaxxing/preview-mazda
python3 -c 'from pathlib import Path; from tools.android_auto.video import validate_clip; validate_clip(Path(".cache/automaxxing/preview-mazda/preview.h264"),1280,720)'
```

With the parked car connected, run on the comma from `/data/automaxxing`:

```sh
sudo -n python3 -u -m tools.android_auto.usb \
  --parked --negotiate --mode video \
  --video media-mazda/preview.h264 --width 1280 --height 720 \
  --attach-timeout 60 --output usb-video-next
```

Use a fresh output directory for every attempt. `--mode probe` stops before TLS;
`--mode auth` tests strict authentication and discovery. The default trust root
is `identity/root-cert.pem`; `--ca` selects another root. A failed certificate
check stops the experiment. No clock or trust-validation bypass is used.

`media-mazda/demo-60s.h264` contains five complete copies of the validated
twelve-second clip, each restarting with SPS/PPS and an IDR. The sender assigns
continuous timestamps. Replace the `--video` path above to replay it for a minute.
The device CLI refuses clips longer than 60 seconds and has an overall deadline.

If attachment does not occur, reconnect only the data cable and observe the
Mazda's permission prompt. The normal power/CAN connection stays connected.

`SIGINT`, `SIGTERM`, normal completion, and caught failures run cleanup: terminate
the bridge, unbind the UDC, unlink/remove only the owned accessory function and
configuration. Setup refuses any pre-existing gadget/accessory device. No sysfs
role/power setting, kernel, CAN configuration, or openpilot process is changed.
After a killed process or reboot, inspect actual state before retrying; setup
deliberately refuses to overwrite leftovers. A reboot was not needed in these
experiments, and no experiment is configured to start automatically.

For a filtered health snapshot:

```sh
PYTHONPATH=/data/openpilot /usr/local/venv/bin/python \
  /data/automaxxing/tools/android_auto/health.py
```

## Remaining gates

- G4: frames generated on the comma; live telemetry, units, alerts, and stale-data
  behavior. The displayed speed/path in these tests were synthetic.
- G5: ten-minute run, three counted physical unplug/replug trials, failure/focus
  recovery, measured concurrent rendering/encoding load, and a saved demo video.
- Confirm head-unit input handling and focus regain with a fresh keyframe. The
  current minimal sender intentionally reports unsupported messages as errors.
- Full native camera/model composition remains an additional renderer task.
