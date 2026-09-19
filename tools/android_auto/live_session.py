"""Latest-frame video delivery with bounded latency and recoverable focus loss."""

from collections import deque
import select
import struct
import time

from tools.android_auto.session import field, json_fields, one, parse_fields
from tools.android_auto.video import VideoSession, nal_units


class PeerRequestedStop(EOFError):
  pass


class LiveVideoSession(VideoSession):
  def __init__(self, *args, **kwargs):
    super().__init__(*args, **kwargs)
    self.pending = deque()
    self.retired_sessions = deque(maxlen=8)
    self.media_started = False
    self.needs_keyframe = True
    self.focus_epoch = 0
    self.frames_sent = 0
    self.discarded_inflight = 0
    self.max_pending = 0
    self.max_ack_seconds = 0
    self.channels = []

  def discover(self):
    self.channels = super().discover()
    return self.channels

  def open_video(self, width, height):
    super().open_video(width, height)
    config = next(ch for ch in self.channels if ch["id"] == self.video_channel)["video_configs"][self.config_index]
    self.margin_width = one(config, 3, 0)
    self.margin_height = one(config, 4, 0)
    self.window = min(self.window, 2)

  def handle(self, channel, kind, data):
    fields = parse_fields(data)
    if channel == 0 and kind == 11:
      self.send(0, 12, field(1, one(fields, 1)))
    elif channel == 0 and kind == 15:
      self.send(0, 16)
      self.event("peer_requested_shutdown", reason=one(fields, 1))
      raise PeerRequestedStop("Head unit ended projection")
    elif channel == 0 and kind in (14, 19):
      self.event("control_notification", kind=kind, fields=json_fields(fields))
    elif channel == self.video_channel and kind == 0x8008:
      was_focused = self.focused
      self.focused = one(fields, 1) in (1, 4)
      self.event("video_focus", focused=self.focused)
      if self.focused and not was_focused:
        if self.media_started:
          self.retired_sessions.append(self.session_id)
          self.session_id += 1
          self.discarded_inflight += self.unacked
          self.unacked = 0
          self.pending.clear()
        self.media_started = True
        self.needs_keyframe = True
        self.focus_epoch += 1
        self.send(channel, 0x8001, field(1, self.session_id) + field(2, self.config_index))
    elif channel == self.video_channel and kind == 0x8004:
      sid, count = one(fields, 1), one(fields, 2, 0)
      if sid in self.retired_sessions:
        self.event("retired_session_ack", session=sid)
        return
      if sid != self.session_id or not 0 < count <= self.unacked or count > len(self.pending):
        raise ValueError("Invalid live video acknowledgement")
      now = time.monotonic()
      for _ in range(count):
        self.max_ack_seconds = max(self.max_ack_seconds, now - self.pending.popleft())
      self.unacked -= count
      self.acked += count
    else:
      # Input is never opened or forwarded to vehicle/native UI controls.
      raise ValueError(f"Unsupported live message {channel}/{kind:#x}")

  def pump(self, timeout):
    if select.select([self.peer], [], [], max(0, timeout))[0]:
      self.handle(*self.receive())

  def check_progress(self, now=None):
    now = time.monotonic() if now is None else now
    if self.focused and self.pending and now - self.pending[0] > 0.5:
      raise TimeoutError("Live video acknowledgement older than 500 ms")

  def send_frame(self, data, timestamp_us):
    if not self.focused or self.unacked >= self.window:
      raise RuntimeError("Cannot send without video focus/window credit")
    if self.needs_keyframe:
      try:
        kinds = {kind for kind, _ in nal_units(data)}
      except IndexError as error:
        raise ValueError("Malformed encoded access unit") from error
      if not {5, 7, 8}.issubset(kinds):
        raise ValueError("Fresh projection requires SPS/PPS and an IDR frame")
    self.send(self.video_channel, 0, struct.pack(">Q", timestamp_us) + data)
    self.needs_keyframe = False
    self.pending.append(time.monotonic())
    self.unacked += 1
    self.frames_sent += 1
    self.max_pending = max(self.max_pending, self.unacked)

  def shutdown(self):
    self.send(0, 15, field(1, 1))
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
      if select.select([self.peer], [], [], 0.05)[0]:
        channel, kind, data = self.receive()
        if channel == 0 and kind == 16:
          self.event("shutdown_acknowledged")
          return
        if channel == 0 and kind == 15:
          self.send(0, 16)
          self.event("crossing_shutdown_acknowledged")
          return
        if channel == self.video_channel and kind == 0x8008:
          self.event("focus_during_shutdown")
        else:
          self.handle(channel, kind, data)
    raise TimeoutError("Head-unit shutdown acknowledgement missing")
