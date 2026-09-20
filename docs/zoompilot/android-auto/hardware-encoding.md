# Hardware encoding on comma four

Automaxxing can use a dedicated Qualcomm V4L2 H.264 session for the full native
3X frontend. Resolution, camera/path rendering, alerts, settings, rotary input,
and display handoff are unchanged. GPU drawing and RGBA readback remain in place.

## Implementation

The pipeline is native GPU rendering → RGBA readback → ARM NEON conversion into
an ION NV12 buffer → hardware H.264 → the existing Android Auto transport.
Conversion uses BT.601 limited range and averages each 2×2 chroma block. The
negotiated black top/bottom margins are initialized once when their boundaries
are chroma-aligned; only actual UI rows need conversion each frame. Both the
1280×720 video size and its 1280×480 usable viewport are preserved.

`hardware_encoder.cc` follows the installed loggerd V4L2 and msgq ION conventions,
but owns a separate device session and allocations. It opens no messaging
publisher, borrows no camera buffers, changes no vehicle-control process, and
does not request realtime encoder priority. The driver advertises RGB input,
but that path produced corrupted decoded images during testing and is not used.

The stream is H.264 baseline, level 3.1, 30 fps, nominal 6 Mbps VBR, with no B
frames. Each call returns exactly one access unit. IDRs carry SPS/PPS headers;
an AUD is normalized to the front of every frame. Forced IDRs are required on
first delivery and resumption, as in the software path.

Only one input frame is queued, even though the driver reserves multiple buffer
slots. Input storage is reused only after both input completion and encoded
output arrive with the matching timestamp. The hardware poll deadline is 200 ms;
the existing separately terminable worker still has its 500 ms deadline. The
existing freshness checks and adaptive CPU budget are unchanged.

## Selection and fallback

The service/runtime default is `--encoder auto`. For the native frontend this
tries the hardware library and validates a forced keyframe before readiness.
Missing/incompatible libraries and hardware initialization errors fall back to
libx264 before the session starts. Metadata records `encoder` and any
`encoder_fallback` reason. Hardware failures during projection terminate that
session through the existing recovery path; codecs are not swapped midstream.

`--encoder software` forces the old encoder. `--encoder hardware` makes hardware
errors explicit and is intended for validation. The passive road/HUD renderer
continues using software. Unsupported hardware dimensions fall back in auto mode.

Build on the C4, as `comma`:

```sh
cd /data/automaxxing
PYTHONPATH=/data/automaxxing/deps:/data/automaxxing:/data/openpilot \
  /usr/local/venv/bin/python -m tools.android_auto.build_hardware_encoder
```

The build runs a scalar-versus-SIMD regression over aligned widths and remainder
lanes, including buffer padding, then atomically replaces `libaa_encoder.so`.
The Python wrapper checks the library ABI before calling it. No openpilot rebuild
or native UI reinstall is needed; the next projection session loads the backend.

Installed on the C4 after verifying a fresh offroad state, inactive projection,
and old/new source hashes. Previous runtime sources are saved at
`/data/automaxxing/hardware-backup-20260919-v1.tar.gz`. The installed-directory
smoke test selected `qcom-v4l2` automatically and completed 284 frames over ten
seconds without fallback. Its first native UI access unit decoded at 1280×720
with exact black margins. No native UI or manager restart was needed.

## Desk validation, 2026-09-19

The C4 was powered in Alex's room, offroad, without the Mazda. Normal native
processes and the local display remained active. Full-UI benchmarks used the
projection service's `Nice=15` and CPU affinity `0,1,2,5,6`. They requested 30 fps
through the same bounded frame worker. These runs do not include USB, head-unit
decoding, road camera/model rendering, or the driving/recording workload.

| Native offroad UI run | Frames / duration | Delivered to preview | Worker CPU |
| --- | --- | --- | --- |
| Software baseline `native-software-01` | 541 / 30.23 s | 17.90 fps | 0.952 cores |
| Hardware, full-frame conversion `native-hardware-02` | 3,350 / 120.22 s | 27.87 fps | 0.787 cores |
| Hardware, known black margins `native-hardware-03` | 3,464 / 120.20 s | **28.82 fps** | **0.722 cores** |

The final run delivered approximately **61% more frames per second** than the
software baseline while using **24% less worker CPU**. Excluding the first PNG
diagnostic frame, median production time fell from **53.31 ms to 30.32 ms**;
median encoding fell from **36.84 ms to 12.51 ms**. Final p95 production time was
**36.29 ms**, so this is near 30 fps, not a guarantee that every frame meets its
33.3 ms interval. The 120-second run completed without a worker timeout or stale
source. Normal source/CPU protections were not relaxed.

The separate moving synthetic encode/decode probe verified 180/180 frames and
eight independently decodable keyframes for both full-frame and black-margin
hardware conversion. Decoded RGB mean absolute error stayed below one level per
channel on the gradient/moving-rectangle test. The black-margin run produced the
same encoded-byte size distribution and image errors as full-frame conversion.
Actual native UI access units also decoded at 1280×720 with black video margins.

The final probe added red, green, blue, white, and black patches: all 180 frames
decoded, with maximum mean RGB error **0.94/255** and median encoding **12.50 ms**.
The lifecycle probe decoded **120 frames across six sessions**, including two
simultaneous sessions at 1280×720 and 800×480. Repeated/double closure passed and
the process's file-descriptor count returned to baseline each round. This checks
session isolation; it does not simulate the camera recording workload.

**271 local tests** pass, including hardware packet framing, forced-keyframe
requirements, startup fallback/cleanup, and all existing UI/transport regressions.
The on-device C++ conversion regression passes across nine widths, covering SIMD
lanes, scalar tails, row padding, and output bounds.

The initial hardware approach using PyAV RGBA→NV12 conversion remained expensive;
the NEON converter replaced it. Stage metadata now separates color conversion,
cache synchronization/queueing, and hardware wait from total encoding time.

Reproduce a bounded benchmark with a fresh output directory:

```sh
PYTHONPATH=/data/automaxxing/deps:/data/automaxxing:/data/openpilot \
  OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 nice -n 15 taskset -c 0,1,2,5,6 \
  /usr/local/venv/bin/python -m tools.android_auto.live_preview \
  --view native --encoder hardware --fps 30 --duration 120 --output native-hw-new

PYTHONPATH=/data/automaxxing/deps:/data/automaxxing:/data/openpilot \
  /usr/local/venv/bin/python -m tools.android_auto.encoder_probe \
  --encoder hardware --black-margins --frames 180 --output encoder-new
```

`live_preview` saves its first native image and first encoded access unit, scalar
per-frame timings, and a summary. Initial PNG diagnostic writing contributes to
the maximum first-frame time; the live projection path does not save PNGs.
`encoder_probe` decodes every frame, checks dimensions and colors, and verifies
that each IDR can be decoded independently.

## Remaining physical validation

The next Mazda test must verify this new hardware bitstream, text/color quality,
actual sent/acknowledged cadence, focus changes/IDRs, and coexistence with normal
camera recording and inference. The earlier source-data failure immediately
after focus resumption remains a separate open issue. Alex reported unplugging
the C4 before shutting the car off; that may explain the last disconnect, but
does not establish the cause of both earlier resume failures.

Thirty fps is the advertised Mazda mode and target. Desk throughput is not a
claim of sustained 30 fps on the head unit or under driving load. GPU/shared-buffer
input could remove further readback/conversion work if later measurements warrant it.
