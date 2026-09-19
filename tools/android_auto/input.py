"""Bounded rotary input decoding; wire fields cross-checked against aasdk proto.

Reference: f1xpl/aasdk 046b3b381595509d0939fa84b14a90978f46ff63.
No vehicle commands or native touch injection are produced here.
"""

from dataclasses import dataclass

from tools.android_auto.session import one, parse_fields

KEYS = {2: "menu", 3: "home", 4: "back", 19: "up", 20: "down", 21: "left", 22: "right", 23: "select",
        66: "select", 84: "voice", 85: "play_pause", 87: "next", 88: "previous", 126: "play", 127: "pause",
        209: "music", 65536: "rotary", 65537: "music", 65538: "navigation"}
# 65537/65538 are AA-specific media/navigation shortcuts; 209 is Android MUSIC.


def repeated_integers(values):
  """Handle protobuf packed and unpacked repeated varints."""
  result = []
  for value in values:
    if isinstance(value, int):
      result.append(value)
    elif isinstance(value, bytes):
      # Reuse the bounded varint decoder by prefixing a repeated field tag.
      pos = 0
      while pos < len(value):
        end = pos
        while end < len(value) and value[end] & 128:
          end += 1
        end += 1
        result.append(one(parse_fields(b"\x08" + value[pos:end]), 1))
        pos = end
    else:
      raise ValueError("Invalid keycode list")
    if len(result) > 256:
      raise ValueError("Too many input keycodes")
  return result


@dataclass(frozen=True)
class InputAction:
  name: str
  steps: int = 1


class InputDecoder:
  def __init__(self):
    self.held = set()

  def reset(self):
    self.held.clear()

  def decode(self, data):
    if len(data) > 16_384:
      raise ValueError("Input message exceeds limit")
    fields = parse_fields(data)
    result = []
    count = 0
    for group in fields.get(4, []):
      for raw in parse_fields(group).get(1, []):
        count += 1
        if count > 64:
          raise ValueError("Too many input events")
        event = parse_fields(raw)
        code, pressed = one(event, 1, 0), one(event, 2, 0)
        if not isinstance(code, int) or pressed not in (0, 1):
          raise ValueError("Invalid button event")
        if not pressed:
          self.held.discard(code)
          continue
        if code in self.held:
          continue
        if len(self.held) >= 64:
          raise ValueError("Too many held buttons")
        self.held.add(code)
        # Leave long-press Home to the receiver's OEM switch. No repeat-clicks.
        if not one(event, 4, 0) and code in KEYS and code != 65536:
          result.append(InputAction(KEYS[code]))
    for group in fields.get(6, []):
      for raw in parse_fields(group).get(1, []):
        count += 1
        if count > 64:
          raise ValueError("Too many input events")
        event = parse_fields(raw)
        if one(event, 1, 0) != 65536:
          continue
        delta = one(event, 2, 0)
        if not isinstance(delta, int):
          raise ValueError("Invalid rotary delta")
        # int32 uses two's-complement varints (negative values may use 10 bytes).
        delta &= 0xffffffff
        if delta >= 0x80000000:
          delta -= 0x100000000
        if delta:
          result.append(InputAction("rotate", max(-20, min(20, delta))))
    return result
