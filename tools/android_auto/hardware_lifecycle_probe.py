"""Validate concurrent, independently decoded hardware sessions and clean closure.

This is encoder resource isolation evidence, not camera/inference coexistence.
"""

import argparse
import gc
import json
from pathlib import Path


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--output", type=Path, required=True)
  args = parser.parse_args()
  import av
  from tools.android_auto.hardware_encode import HardwareH264Encoder
  from tools.android_auto.video import nal_units
  args.output.mkdir(parents=True, exist_ok=False)
  baseline_fds = len(list(Path("/proc/self/fd").iterdir()))
  frames = 0
  for _ in range(3):
    encoders, decoders, inputs = [], [], []
    try:
      for width, height, margin in ((1280, 720, 240), (800, 480, 0)):
        encoders.append(HardwareH264Encoder(width, height, margin_height=margin))
        decoder = av.CodecContext.create("h264", "r")
        decoder.thread_count = 1
        decoders.append(decoder)
        black = bytes((0, 0, 0, 255)) * width * (margin // 2)
        inputs.append(black + bytes((140, 40, 90, 255)) * width * (height - margin) + black)
      for index in range(20):
        for encoder, decoder, pixels in zip(encoders, decoders, inputs, strict=True):
          data = encoder.encode_rgba(pixels, force_keyframe=index in (0, 7))
          decoded = decoder.decode(av.Packet(data))
          if len(decoded) != 1 or (decoded[0].width, decoded[0].height) != (encoder.width, encoder.height):
            raise RuntimeError("Concurrent session lost, buffered, or mixed frames")
          if index in (0, 7) and not {5, 7, 8}.issubset(kind for kind, _ in nal_units(data)):
            raise RuntimeError("Concurrent session keyframe missing")
          frames += 1
    finally:
      for encoder in encoders:
        encoder.close()
        encoder.close()
    gc.collect()
    if len(list(Path("/proc/self/fd").iterdir())) != baseline_fds:
      raise RuntimeError("Hardware session leaked file descriptors")
  result = {"sessions": 6, "simultaneous_sessions": 2, "decoded_frames": frames,
            "descriptor_leaks": 0, "double_close": "passed",
            "limit": "Two synthetic H264 sessions; does not simulate native camera recording or inference."}
  (args.output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
  print(json.dumps(result, indent=2))


if __name__ == "__main__":
  main()
