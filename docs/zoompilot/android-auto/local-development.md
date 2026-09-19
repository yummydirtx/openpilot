# Local development without the comma or car

## What is ready

Google's Desktop Head Unit (DHU) runs natively on this Apple Silicon Mac. A small
Python sender probe now connects directly to it over loopback TCP, without a phone,
ADB server, Android virtual device, or comma. This is a real DHU protocol exchange,
not a simulated success response in our own mock.

Observed September 18, 2026:

- DHU 2.0-mac-arm64, build `2022-03-30-438482292`, runs on macOS 26.6.2 arm64.
- DHU requests protocol 1.7; our probe replies with 1.5 and success status.
- DHU proceeds by sending a 230-byte TLS ClientHello.
- The original bootstrap probe then deliberately disconnects.
- A second authentication probe completes TLS 1.2 using
  `ECDHE-RSA-AES256-GCM-SHA384`. DHU rejects the generated test certificate
  with status `-3` and `Verify returned: self signed certificate`.
- With a phone identity imported from a verified Android Auto 17.6 APK, stock DHU
  reports `Verify returned: ok`, sends authentication status 0, and returns an
  encrypted service-discovery response advertising seven channels.
- The successful loopback test authenticates the sender to DHU. Optional reverse
  verification fails on DHU's certificate date encoding under OpenSSL 3.6.4.
- Video now passes: a moving test pattern and a synthetic comma 3X HUD are decoded
  by stock DHU, with all frames acknowledged and received screenshots inspected.
  The HUD works at 480p, 720p, and a wide usable viewport.
- Input events, physical reconnect reliability, live camera/model composition,
  and Mazda compatibility remain untested. No actual-car gate has passed.
- The focused Python suite covers transport, authentication, discovery, video
  flow control, shutdown, viewport scaling, and stale presentation.

The previously committed work was planning documentation. This probe is the first
executable experiment. No actual-car milestone has passed.

For the current working demo, go to the [video runbook](video.md). It covers asset
preparation, rendering the scalable 3X preview, and streaming it into DHU. The
commands below preserve the original smaller diagnostics for troubleshooting.

## Run the experiment

From the repository root, using Python 3 (standard library only):

```sh
python3 -m unittest discover -s tools/android_auto -p 'test_*.py' -v
python3 tools/android_auto/probe.py --dhu .cache/automaxxing/dhu-2.0/desktop-head-unit --port 0
```

The probe chooses a free localhost port, starts DHU headlessly, performs the
version exchange, identifies its TLS ClientHello, prints a JSON result, and stops
both ends. It returns nonzero for failure. The result explicitly reports
`authentication_complete: false` and `video_tested: false`. DHU output is in
`.cache/automaxxing/dhu-probe.log` (overwritten on each run). A TLS wait/disconnect
at the end is expected at this stage. Sandbox execution needs loopback networking;
a sandbox denial is not evidence of a protocol failure.

For a visible emulator, run the probe in one terminal:

```sh
python3 tools/android_auto/probe.py --timeout 60
```

Then run DHU in another terminal:

```sh
cd .cache/automaxxing/dhu-2.0
./desktop-head-unit --adb=127.0.0.1:5277 --config=config/rotary.ini
```

The bootstrap probe will not display a dashboard. DHU's `--adb` option connects to
TCP even though its name references ADB: the normal phone workflow supplies an ADB
forwarder, and our local sender supplies the listening socket instead. Type `quit`
in the DHU terminal to close the interactive emulator.

## Authentication experiment

The original phone-certificate rejection has a working local solution. See the
[certificate experiment](authentication.md) for the verified package, repeatable
import, evidence, expiry, and remaining reverse-verification limitation.

The imported identity is already available on this Mac. Repeat the successful
authentication and encrypted discovery test:

```sh
python3 -m tools.android_auto.auth_probe \
  --cert .cache/automaxxing/imported-identity/phone-cert.pem \
  --key .cache/automaxxing/imported-identity/phone-key.pem \
  --output .cache/automaxxing/auth-imported
```

Expected exit code: **0**, with `authentication_complete: true`,
`service_discovery_complete: true`, `head_unit_verified: false`, and
`video_tested: false`. The sender does not validate DHU's identity by default;
the certificate experiment explains the optional `--ca` check and its failure.

### Original negative control

Run the next-stage diagnostic from the repository root:

```sh
python3 -m tools.android_auto.auth_probe
```

It generates a local RSA test identity using OpenSSL, launches DHU, and attempts
the TLS/authentication sequence. Its current expected outcome is **exit code 2**:

```json
{
  "tls_established": true,
  "authentication_complete": false,
  "video_tested": false,
  "error": "Head unit rejected the phone certificate (status -3); TLS alone is not authentication"
}
```

Evidence is in `.cache/automaxxing/session/events.jsonl` and `dhu.log`. Keys remain
in that ignored directory and are not printed in logs. Files are overwritten on
rerun; use `--output .cache/automaxxing/another-run` to retain separate evidence.
The probe cleans up its emulator process after success or failure.

The first generated certificate used an arbitrary subject, which DHU rejected as
an invalid peer name. Using the phone-role subject from the AACS reference reached
the signing-authority check and was rejected as self-signed. A process-local
`SSL_CERT_FILE` experiment did not change that outcome and is not retained in the
launcher. No system trust store, system clock, or DHU binary was changed.

AACS's published phone certificate expired on August 24, 2022. Its maintainer
confirms this in [issue 28](https://github.com/tomasz-grobelny/AACS/issues/28).
Adopting that project's bundled identity therefore did not solve the failure.
The current imported certificate now passes stock DHU; acceptance by the Mazda
remains a separate hardware experiment alongside USB transport and video.

## Recreate the local emulator installation

The emulator is already extracted under `.cache/automaxxing/dhu-2.0/` on this Mac.
That directory is ignored by Git; Google's binaries are not vendored into the
project. Existing Android Studio/SDK settings were not changed.

On another machine, the normal installation is Android Studio → SDK Manager →
SDK Tools → Android Auto Desktop Head Unit Emulator. Use the binary beneath
`SDK_LOCATION/extras/google/auto/` with `--dhu`; retain its adjacent libraries and
configuration directory. This requires Google's SDK terms through the installer.

For reproducibility, this experiment used the stable Apple Silicon package listed
in Google's [SDK catalog](https://dl.google.com/android/repository/repository2-3.xml):

| Item | Value |
| --- | --- |
| Archive | [desktop-head-unit-darwin-aarch64_r02.0.zip](https://dl.google.com/android/repository/desktop-head-unit-darwin-aarch64_r02.0.zip) |
| Size | 7,443,824 bytes |
| Catalog SHA-1, verified before extraction | `4e90abf932e512d6ca954b5126dd3a11e7a279e2` |
| Version/channel | 2.0 / stable |

The catalog also lists 2.1 on its beta channel; this baseline intentionally uses
the stable entry. No Rosetta, Docker, Linux VM, or full openpilot build is needed
for the bootstrap experiment.

## Tonight's next increments

Completed: channel setup/focus, H.264 test pattern, paced bounded sending, orderly
shutdown, and the synthetic scalable 3X HUD. See [video evidence](video.md).

1. Connect the shared renderer to an encoder during the session, then add a
   read-only live telemetry adapter. Keep synthetic/live/replay modes explicit.
2. Resolve sender-side head-unit certificate verification; the current success
   proves only the head unit's acceptance of our sender.
3. Reuse native camera/model/alert renderers as described in the
   [interface plan](interface.md), measuring on-device resource use.
4. Exercise rotary input, focus regain with keyframe restart, and reconnect to
   the same running receiver. Fresh local sessions are already working.

The original
`probe.py` is intentionally limited to plaintext bootstrap. The new `session.py`
adds TLS memory BIOs, bounded message reassembly, protobuf fields, authentication
status checks, and discovery. Its encrypted discovery exchange now passes against
stock DHU; `video.py` adds the working streaming experiment. This remains a
prototype, not a complete session library.

## What tomorrow still needs to prove

DHU emulates the head unit, not the comma's Qualcomm hardware, AGNOS kernel, USB
controller, or Mazda firmware. It cannot establish peripheral/host role switching,
Android Open Accessory enumeration, the working physical data port, simultaneous
vehicle power/CAN connectivity, hardware encoding capacity, or Mazda-specific
negotiation and display modes. Its 800×480 rotary configuration is a development
fixture, not a measurement of the Mazda screen.

Bring a USB-A to USB-C **data** cable long enough to reach the factory Android Auto
port from the comma's separate data port. Verify the actual connector path and
running gadget/controller state before configuring USB. Start tomorrow with G0/G1
in the [implementation plan](implementation-plan.md), even if projection into DHU
works tonight. The final runtime remains comma → Mazda, with no laptop or phone.

## References

- [Google DHU documentation](https://developer.android.com/training/cars/testing/dhu):
  installation, TCP/USB options, display configuration, and rotary controls.
- [AACS protocol identifiers](https://github.com/tomasz-grobelny/AACS/blob/master/include/enums.h)
  and [communicator](https://github.com/tomasz-grobelny/AACS/blob/faa1cf208feb5dfe1cb9535be16daeac4f08da0c/AAServer/src/AaCommunicator.cpp):
  reference for the phone-side bootstrap. No third-party implementation is vendored
  by this experiment. Review licensing before importing/adapting larger components.
