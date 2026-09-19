"""Bounded H.264 projection experiment against a local, stock Google DHU.

Wire identifiers cross-checked against AACS and observed DHU responses. This is
phone-side TCP only, with no USB transport or live vehicle-data connection.
"""

import argparse
import json
from pathlib import Path
import re
import select
import socket
import struct
import subprocess
import time

from tools.android_auto.session import Session, field, json_fields, one, parse_fields

RESOLUTIONS = {1: (800, 480), 2: (1280, 720), 3: (1920, 1080)}


def access_units(data):
  """Split bounded Annex B H.264 with encoder-inserted access unit delimiters."""
  if not data or len(data) > 64 * 1024 * 1024:
    raise ValueError("Expected an H.264 clip between 1 byte and 64 MiB")
  starts = list(re.finditer(b"\x00\x00(?:\x00)?\x01", data))
  if not starts or starts[0].start() != 0:
    raise ValueError("Expected an Annex B start code at the beginning of the clip")
  units, current = [], bytearray()
  for i, start in enumerate(starts):
    end = starts[i + 1].start() if i + 1 < len(starts) else len(data)
    if start.end() == end:
      raise ValueError("Empty H.264 NAL unit")
    kind = data[start.end()] & 31
    if kind == 9 and current:
      units.append(bytes(current))
      current.clear()
    current.extend(data[start.start():end])
  if current:
    units.append(bytes(current))
  if not units or not all(any(nal_type == 9 for nal_type, _ in nal_units(unit)) for unit in units):
    raise ValueError("Clip must contain AUD-delimited access units (x264 aud=1)")
  if not {5, 7, 8}.issubset({kind for kind, _ in nal_units(units[0])}):
    raise ValueError("Clip must start with an IDR frame and SPS/PPS headers")
  if any(len(unit) > 2 * 1024 * 1024 - 10 for unit in units):
    raise ValueError("Access unit exceeds the session message limit")
  return units


def nal_units(data):
  starts = list(re.finditer(b"\x00\x00(?:\x00)?\x01", data))
  for i, start in enumerate(starts):
    end = starts[i + 1].start() if i + 1 < len(starts) else len(data)
    yield data[start.end()] & 31, data[start.start():end]


def validate_clip(path, width, height):
  if path.stat().st_size > 64 * 1024 * 1024:
    raise ValueError("Clip exceeds the 64 MiB experiment limit")
  result = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,width,height", "-of", "json", str(path)],
                          check=True, capture_output=True, text=True)
  streams = json.loads(result.stdout)["streams"]
  if len(streams) != 1:
    raise ValueError("Expected exactly one video stream")
  stream = streams[0]
  if (stream["codec_name"], stream["width"], stream["height"]) != ("h264", width, height):
    raise ValueError(f"Encoded video does not match requested {width}x{height}: {stream}")
  # Raw Annex B has no container timestamps; --fps defines our presentation
  # timeline. ffprobe's guessed r_frame_rate is not a reliable validation here.


class VideoSession(Session):
  def open_video(self, width, height):
    channels = self.discover()
    matches = [(ch, index) for ch in channels for index, config in enumerate(ch.get("video_configs", []))
               if RESOLUTIONS.get(one(config, 1)) == (width, height)]
    if not matches:
      raise ValueError(f"Receiver did not advertise a {width}x{height} video mode")
    channel, config = matches[0]
    self.video_channel = channel["id"]
    self.focused = False
    self.unacked = 0
    self.acked = 0
    self.session_id = 1
    self.send(self.video_channel, 7, field(1, 0) + field(2, self.video_channel), control=True)
    opened = parse_fields(self.wait_for(self.video_channel, 8))
    if one(opened, 1) != 0:
      raise ValueError("Receiver rejected opening the video channel")
    self.send(self.video_channel, 0x8000, field(1, 3))
    setup = parse_fields(self.wait_for(self.video_channel, 0x8003))
    # Stock DHU: field 1 = READY(2), field 2 = max unacknowledged frames,
    # field 3 = available configuration indices. Reject unrecognized responses.
    self.window = one(setup, 2, 0)
    if one(setup, 1) != 2 or config not in setup.get(3, []) or not 1 <= self.window <= 32:
      raise ValueError(f"Unsupported video setup response: {setup}")
    self.config_index = config
    self.event("video_setup", channel=self.video_channel, width=width, height=height, window=self.window, config=config)
    deadline = time.monotonic() + 5
    while not self.focused:
      if time.monotonic() >= deadline:
        raise TimeoutError("No video focus received")
      self.pump(min(0.1, deadline - time.monotonic()))

  def pump(self, timeout):
    if not select.select([self.peer], [], [], max(0, timeout))[0]:
      return
    channel, kind, data = self.receive()
    fields = parse_fields(data)
    if channel == 0 and kind == 11:
      self.send(0, 12, field(1, one(fields, 1)))
    elif channel == self.video_channel and kind == 0x8008:
      self.focused = one(fields, 1) == 1
      self.event("video_focus", focused=self.focused)
      if self.focused:
        self.send(channel, 0x8001, field(1, self.session_id) + field(2, self.config_index))
    elif channel == self.video_channel and kind == 0x8004:
      count = one(fields, 2, 0)
      if one(fields, 1) != self.session_id or not 0 < count <= self.unacked:
        raise ValueError(f"Invalid video acknowledgement: {fields}; pending={self.unacked}")
      self.unacked -= count
      self.acked += count
    else:
      raise ValueError(f"Unhandled streaming message {channel}/{kind:#x}: {data.hex()}")

  def project(self, frames, fps, screenshot=None):
    start = time.monotonic()
    max_pending = 0
    for index, frame in enumerate(frames):
      deadline = start + index / fps
      stalled_until = time.monotonic() + 5
      while time.monotonic() < deadline or self.unacked >= self.window or not self.focused:
        if time.monotonic() >= stalled_until:
          raise TimeoutError("Video focus or acknowledgement stalled")
        wait = min(0.05, max(0, deadline - time.monotonic())) if self.unacked < self.window and self.focused else 0.05
        self.pump(wait)
      self.send(self.video_channel, 0, struct.pack(">Q", round(index * 1_000_000 / fps)) + frame)
      self.unacked += 1
      max_pending = max(max_pending, self.unacked)
      self.pump(0)
      if screenshot is not None and index in (len(frames) // 6, len(frames) * 7 // 12, len(frames) * 5 // 6):
        screenshot(index)
    deadline = time.monotonic() + 5
    while self.unacked:
      if time.monotonic() >= deadline:
        raise TimeoutError("Final video acknowledgements did not arrive")
      self.pump(0.05)
    result = {"frames_sent": len(frames), "frames_acked": self.acked, "max_pending": max_pending,
              "elapsed_seconds": round(time.monotonic() - start, 3), "fps": fps}
    self.event("video_complete", **result)
    return result

  def shutdown(self):
    # Phone-side ByeByeRequest/Response; reason 1 is USER_SELECTION.
    self.send(0, 15, field(1, 1))
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
      if not select.select([self.peer], [], [], 0.1)[0]:
        continue
      channel, kind, data = self.receive()
      if channel == 0 and kind == 16:
        self.event("shutdown_acknowledged")
        return
      if channel == 0 and kind == 11:
        self.send(0, 12, field(1, one(parse_fields(data), 1)))
      elif channel == self.video_channel and kind == 0x8008:
        self.event("video_focus_during_shutdown", fields=json_fields(parse_fields(data)))
      else:
        raise ValueError(f"Unexpected message during shutdown: {channel}/{kind:#x}")
    raise TimeoutError("Receiver did not acknowledge shutdown")


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--video", type=Path, required=True, help="AUD-delimited Annex B H.264, starting with SPS/PPS and an IDR")
  parser.add_argument("--width", type=int, default=800)
  parser.add_argument("--height", type=int, default=480)
  parser.add_argument("--fps", type=int, default=30)
  parser.add_argument("--dhu", type=Path, default=Path(".cache/automaxxing/dhu-2.0/desktop-head-unit"))
  parser.add_argument("--config", default="config/rotary.ini", help="DHU configuration, relative to the DHU directory or absolute")
  parser.add_argument("--identity", type=Path, default=Path(".cache/automaxxing/imported-identity"))
  parser.add_argument("--output", type=Path, default=Path(".cache/automaxxing/video"))
  parser.add_argument("--visible", action="store_true", help="Show DHU's window (needed for screenshots on some builds)")
  args = parser.parse_args()
  if args.fps not in (30, 60) or (args.width, args.height) not in RESOLUTIONS.values():
    parser.error("Supported experiment modes: 800x480, 1280x720, 1920x1080 at 30 or 60 fps")
  try:
    validate_clip(args.video, args.width, args.height)
    frames = access_units(args.video.read_bytes())
  except (OSError, ValueError, subprocess.CalledProcessError) as error:
    parser.error(str(error))
  args.output.mkdir(parents=True, exist_ok=True)
  binary = args.dhu.resolve(strict=True)
  child = None
  try:
    with socket.socket() as listener, (args.output / "events.jsonl").open("w") as events, (args.output / "dhu.log").open("w") as log:
      listener.bind(("127.0.0.1", 0))
      listener.listen(1)
      listener.settimeout(15)
      command = [str(binary), f"--adb=127.0.0.1:{listener.getsockname()[1]}", f"--config={args.config}"]
      if not args.visible:
        command.append("--headless")
      child = subprocess.Popen(command, cwd=binary.parent, stdin=subprocess.PIPE, stdout=log, stderr=log)
      screenshots = []

      def screenshot(index):
        path = (args.output / f"received-{index:04d}.png").resolve()
        child.stdin.write(f"screenshot {path}\n".encode())
        child.stdin.flush()
        screenshots.append(path)

      with listener.accept()[0] as peer:
        peer.settimeout(5)
        session = VideoSession(peer, args.identity / "phone-cert.pem", args.identity / "phone-key.pem", events)
        session.authenticate()
        session.open_video(args.width, args.height)
        result = session.project(frames, args.fps, screenshot)
        session.shutdown()
        result.update(authentication_complete=True, head_unit_verified=False, service_discovery_complete=True)
        result["shutdown_acknowledged"] = True
        result["screenshots_saved"] = [str(path) for path in screenshots if path.exists()]
        (args.output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2))
    return 0
  except (OSError, EOFError, ValueError) as error:
    print(f"Video experiment failed: {error}; see {args.output}")
    return 1
  finally:
    if child is not None:
      try:
        child.communicate(b"quit\n", timeout=3)
      except subprocess.TimeoutExpired:
        child.terminate()
        try:
          child.communicate(timeout=3)
        except subprocess.TimeoutExpired:
          child.kill()
          child.communicate()


if __name__ == "__main__":
  raise SystemExit(main())
