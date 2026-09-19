"""Bounded on-device renderer/encoder probe without USB or a head unit.

Uses the same spawned worker as projection. Diagnostics contain a handful of
first-occurrence images and scalar metrics; it does not record a driving video.
"""

import argparse
import json
from pathlib import Path
import time

from tools.android_auto.frame_worker import FrameWorker
from tools.android_auto.runtime import atomic_json
from tools.android_auto.viewport import Viewport


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--duration", type=float, default=20)
  parser.add_argument("--fps", type=int, choices=(8, 10, 15, 30), default=8)
  parser.add_argument("--view", choices=("road", "hud"), default="road")
  parser.add_argument("--output", type=Path, required=True)
  parser.add_argument("--assets", type=Path, default=Path("/data/openpilot/openpilot/selfdrive/assets"))
  parser.add_argument("--hud-path", type=Path, default=Path("native/hud_drawing.py"))
  parser.add_argument("--stock", action="store_true")
  args = parser.parse_args()
  if not 1 <= args.duration <= 120:
    parser.error("Preview duration must be 1–120 seconds")
  args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
  worker = FrameWorker(Viewport(1280, 720, 0, 240), args.assets, args.hud_path,
                       sunnypilot=not args.stock, output=args.output, view=args.view)
  started = time.monotonic()
  end = started + args.duration
  next_frame = started
  frames = camera_frames = model_frames = total_bytes = 0
  max_frame_age = max_render_time = 0
  cpu_first = cpu_last = None
  last = None
  try:
    with (args.output / "frames.jsonl").open("w") as events:
      while time.monotonic() < end:
        now = time.monotonic()
        if not worker.busy and now >= next_frame:
          worker.request(force_keyframe=frames == 0)
          next_frame = now + 1 / args.fps
        result = worker.poll(0.002)
        if result is None:
          time.sleep(0.001)
          continue
        data, metadata = result
        frames += 1
        total_bytes += len(data)
        road = metadata.get("road") or {}
        camera_frames += bool(road.get("camera_displayed"))
        model_frames += bool(road.get("model_displayed"))
        max_frame_age = max(max_frame_age, time.monotonic() - metadata["captured_at"])
        max_render_time = max(max_render_time, metadata["render_encode_seconds"])
        cpu_last = time.monotonic(), metadata["cpu_seconds"]
        if cpu_first is None:
          cpu_first = cpu_last
        last = metadata
        events.write(json.dumps(metadata) + "\n")
  finally:
    worker.close()
  elapsed = time.monotonic() - started
  cpu = None if cpu_first == cpu_last else (cpu_last[1] - cpu_first[1]) / (cpu_last[0] - cpu_first[0])
  result = {"view": args.view, "requested_fps": args.fps, "frames": frames, "camera_frames": camera_frames,
            "model_frames": model_frames, "seconds": elapsed, "fps": frames / elapsed, "worker_cpu_cores": cpu,
            "max_capture_age_ms": max_frame_age * 1000, "max_render_encode_ms": max_render_time * 1000,
            "encoded_bytes": total_bytes, "last_display": last,
            "limit": "Renderer/encoder probe only; no USB or head-unit display evidence."}
  atomic_json(args.output / "result.json", result)
  print(json.dumps(result, indent=2))
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
