"""Compare encoders with moving synthetic pixels; decode every output frame.

No camera, CAN, vehicle state publication, USB, or display handoff. Timing excludes
test-image construction and decoding. This is not a driving-load benchmark.
"""

import argparse
import json
from pathlib import Path
import statistics
import time


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--encoder", choices=("software", "hardware"), required=True)
  parser.add_argument("--frames", type=int, default=180)
  parser.add_argument("--black-margins", action="store_true", help="Skip conversion of the synthetic 120px black top/bottom bars")
  parser.add_argument("--output", type=Path, required=True)
  args = parser.parse_args()
  if not 30 <= args.frames <= 1800:
    parser.error("Use 30–1800 frames")
  args.output.mkdir(parents=True, exist_ok=False)
  import av
  import numpy as np
  from PIL import Image
  from tools.android_auto.video import nal_units
  if args.encoder == "hardware":
    from tools.android_auto.hardware_encode import HardwareH264Encoder
    encoder = HardwareH264Encoder(1280, 720, margin_height=240 if args.black_margins else 0)
  else:
    from tools.android_auto.live_encode import H264Encoder
    encoder = H264Encoder(1280, 720)
  decoder = av.CodecContext.create("h264", "r")
  samples, cpu, sizes, errors = [], [], [], []
  stages = {}
  keyframes = 0
  try:
    for i in range(args.frames):
      rgba = np.zeros((720, 1280, 4), dtype=np.uint8)
      rgba[:, :, 3] = 255
      x = np.arange(1280, dtype=np.uint16)[None, :]
      y = np.arange(480, dtype=np.uint16)[:, None]
      rgba[120:600, :, 0] = (x // 5 + i * 2) % 256
      rgba[120:600, :, 1] = (y // 2 + i) % 256
      rgba[120:600, :, 2] = ((x + y) // 8) % 256
      left = i * 13 % 1080
      rgba[250:400, left:left + 200, :3] = [0, 210, 100]
      for index, color in enumerate(((255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255), (0, 0, 0))):
        rgba[140:200, index * 200:(index + 1) * 200, :3] = color
      force = i % 47 == 0
      buffer = rgba.tobytes()
      start, cpu_start = time.monotonic(), time.process_time()
      data = encoder.encode_rgba(buffer, force_keyframe=force)
      elapsed, cpu_elapsed = time.monotonic() - start, time.process_time() - cpu_start
      frames = decoder.decode(av.Packet(data))
      if len(frames) != 1 or (frames[0].width, frames[0].height) != (1280, 720):
        raise RuntimeError("Frame missing, reordered, buffered, or wrong dimensions")
      decoded = frames[0].to_ndarray(format="rgb24")
      error = float(np.mean(np.abs(decoded.astype(np.int16) - rgba[:, :, :3].astype(np.int16))))
      if error > 8:
        Image.fromarray(decoded).save(args.output / "invalid-decoded.png")
        pixels = [(rgba[y, x, :3].tolist(), decoded[y, x].tolist()) for x, y in ((50, 50), (500, 300), (1100, 500))]
        raise RuntimeError(f"Decoded image differs excessively: {error:.2f}; expected/actual RGB: {pixels}")
      kinds = [kind for kind, _ in nal_units(data)]
      if 5 in kinds:
        independent = av.CodecContext.create("h264", "r").decode(av.Packet(data))
        if len(independent) != 1 or not {7, 8}.issubset(kinds):
          raise RuntimeError("Keyframe cannot decode independently")
        keyframes += 1
      if force and 5 not in kinds:
        raise RuntimeError("Forced keyframe missing")
      if i == 0:
        Image.fromarray(decoded).save(args.output / "decoded.png")
        (args.output / "first.h264").write_bytes(data)
      if i >= 10:
        samples.append(elapsed)
        cpu.append(cpu_elapsed)
        sizes.append(len(data))
        errors.append(error)
        for name, value in getattr(encoder, "timing", {}).items():
          stages.setdefault(name, []).append(value)
    if decoder.decode(None):
      raise RuntimeError("Decoder buffered output")
  finally:
    encoder.close()
  def summary(values):
    return {"median": statistics.median(values), "p95": sorted(values)[int(.95 * (len(values) - 1))], "max": max(values)}
  result = {"encoder": args.encoder, "black_margins": args.black_margins, "frames": args.frames, "decoded": args.frames, "keyframes": keyframes,
            "encode_ms": summary([s * 1000 for s in samples]), "cpu_ms": summary([s * 1000 for s in cpu]),
            "bytes": summary(sizes), "rgb_mean_absolute_error": summary(errors),
            "stages_ms": {name: summary([s * 1000 for s in values]) for name, values in stages.items()},
            "limit": "Synthetic encode/decode validation; no USB, road UI, or driving workload."}
  (args.output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
  print(json.dumps(result, indent=2))


if __name__ == "__main__":
  main()
