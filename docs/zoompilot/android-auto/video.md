# Local video and comma 3X preview

Observed September 18, 2026: a phone-free Python sender projects moving H.264 into
**unmodified Google DHU 2.0**. The scalable 3X HUD preview then passes through the
same transport and decoder. The final target remains comma four → wired Mazda;
this runbook describes local TCP development, not USB or actual-car validation.

## Results

| Experiment | Frames sent / acknowledged | Frame rate | Evidence directory under `.cache/automaxxing/` |
| --- | --- | --- | --- |
| Moving test pattern, 800×480 | 180 / 180 | 30 fps | `video-pattern/` |
| Synthetic 3X HUD, 800×480 | 360 / 360 | 30 fps | `video-hud-480/` |
| Synthetic 3X HUD, 1280×720 | 360 / 360 | 30 fps | `video-hud-720/` |
| Synthetic 3X HUD, 1280×500 usable inside 1280×720 | 360 / 360 | 30 fps | `video-hud-wide/` |

Twelve-second HUD sessions took approximately 11.98–12.00 seconds to send and
drain acknowledgements. DHU advertised a window of two unacknowledged frames;
the sender stayed within that limit. The wide run also completed the AA
ByeByeRequest/ByeByeResponse shutdown exchange. These were fresh local sessions,
not physical unplug/replug tests or a ten-minute stability test.

Each directory contains `events.jsonl`, `dhu.log`, `result.json`, and received PNG
screenshots. The screenshots were produced by DHU's own `screenshot` command;
the sent/acknowledged counts alone are not proof of decoded pixels. Final 480p
state captures are in `video-hud-final-480/`. The local renderer's source captures
are in `preview/`, `preview-720/`, and `preview-wide/`.

Authentication retains the [documented limitation](authentication.md): DHU verifies
the sender's imported phone certificate; the default local sender does not verify
DHU's certificate. This experiment neither changes DHU's trust checks nor fixes
the separate reverse-verification date error.

## Prepare the renderer

Run from the repository root. Use a separate Python 3.12 environment for raylib,
matching the repository's supported Python version:

```sh
uv --cache-dir .cache/automaxxing/uv-cache venv --python python3.12 .cache/automaxxing/venv
uv --cache-dir .cache/automaxxing/uv-cache pip install \
  --python .cache/automaxxing/venv/bin/python 'comma-deps-raylib==6.0.0.1.post101'
python3 -m tools.android_auto.prepare_assets
```

FFmpeg with libx264 and ffprobe must be on PATH. This Mac used FFmpeg 9.0.1.
Raylib uses a hidden macOS graphics context; under the desktop sandbox it needs
permission to access WindowServer. The sender needs loopback networking. A
sandbox denial does not establish a renderer or protocol failure.

The current imported phone identity and DHU installation must already exist;
see [authentication](authentication.md) and [local development](local-development.md).

## Render and project

```sh
.cache/automaxxing/venv/bin/python -m tools.android_auto.preview
python3 -m tools.android_auto.video \
  --video .cache/automaxxing/preview/preview.h264 \
  --output .cache/automaxxing/video-hud-demo
```

The renderer writes a twelve-second, 30 fps Annex B H.264 clip, plus source PNGs
at 0, 2, 7, and 10 seconds. It uses the shared native HUD code and exact native
assets; camera/model content is explicitly synthetic. See [interface](interface.md).

The sender starts DHU headlessly, authenticates, discovers an advertised mode
matching the requested dimensions, opens the video channel, negotiates setup,
waits for focus, and sends paced timestamped access units. It services keepalive
requests and acknowledgements, limits in-flight frames, and fails on a five-second
streaming stall. It captures three received frames and performs orderly shutdown.
Use `--visible` to show DHU's window during the run. Processes are cleaned up on
completion or failure. Logs and generated files in the selected directory may be
overwritten; use distinct output directories to retain evidence.

For 720p:

```sh
.cache/automaxxing/venv/bin/python -m tools.android_auto.preview \
  --width 1280 --height 720 --output .cache/automaxxing/preview-720
python3 -m tools.android_auto.video \
  --video .cache/automaxxing/preview-720/preview.h264 \
  --width 1280 --height 720 --config config/default_720p.ini \
  --output .cache/automaxxing/video-hud-720
```

For the wide fixture, add `--margin-height 220` to the renderer, use
`config/default_wide.ini` for DHU, and choose separate output directories.
The full encoded image remains 1280×720; the usable area is 1280×500 with 110-pixel
top/bottom margins. Actual negotiated margins are logged but not yet passed
automatically into a live renderer.

To make a convenient playable copy of the same projected stream:

```sh
ffmpeg -y -r 30 -i .cache/automaxxing/preview/preview.h264 \
  -c:v copy -r 30 -movflags +faststart .cache/automaxxing/preview/preview.mp4
```

The output was checked as 360 frames and 12.000 seconds. Raw Annex B has no
container timestamps. The sender uses the explicit `--fps` presentation timeline;
ffprobe's guessed raw-stream frame rate is not used as a reliable clock. Use the
same frame rate when generating the clip and sending it.

## Protocol evidence and limits

For stock DHU's tested configurations:

1. ChannelOpenRequest `7` on the advertised video channel, with channel-specific
   flag set; response `8` carries success status `0`.
2. Media setup `0x8000`, H.264 selector `3`; response `0x8003` advertises ready
   status `2`, window `2`, and configuration index `0`.
3. Video focus `0x8008` indicates focus `1`; sender starts session ID `1` with
   `0x8001` and selected configuration `0`.
4. Media `0` contains a big-endian 64-bit microsecond timestamp followed by an
   Annex B access unit. SPS/PPS and IDR start the clip, and periodic headers and
   keyframes permit clean fresh sessions.
5. Ack `0x8004` identifies the session and released frame count. Wrong-session,
   zero, and excessive acknowledgements are rejected.
6. Control ping `11` gets response `12`; final request `15`, reason `1`, gets
   shutdown response `16`.

Identifiers and the initial sequence were cross-checked with
[AACS's sender](https://github.com/tomasz-grobelny/AACS/blob/faa1cf208feb5dfe1cb9535be16daeac4f08da0c/AAServer/src/VideoChannelHandler.cpp)
and the inspected Android Auto package, then validated empirically against DHU.
No AACS implementation was vendored. Refer to
[Google's DHU configuration documentation](https://developer.android.com/training/cars/testing/dhu#configure-the-dhu)
for the resolution/margin fixtures and screenshot command.

The sender is deliberately narrow: clips are capped at 64 MiB, must begin with
AUD/SPS/PPS/IDR, and encoded dimensions must match the requested/advertised mode.
It does not yet implement input, audio, every optional control message, live
encoding, keyframe restart after mid-session focus loss, USB, or reconnect on the
same running receiver. Unexpected messages fail visibly instead of disappearing.

## Verification

```sh
.cache/automaxxing/venv/bin/python -m unittest discover -s tools/android_auto -p 'test_*.py' -v
```

The focused suite covers framing, protobuf bounds, authentication rejection,
discovery logging, H.264 grouping, acknowledgement accounting, focus/keepalive,
bounded stalls, shutdown response, viewport/margins, and stale presentation.
Ruff checks also cover the new code and both native HUD files.

A separate comparison against the original `hud_renderer.py` checked every draw
argument in **80 cases**: metric/imperial, five status values, set/unset cruise,
and four logical widths. All matched after extracting the shared painters. This
does not replace a full native UI integration test on the device.
