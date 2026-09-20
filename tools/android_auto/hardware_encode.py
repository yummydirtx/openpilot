"""One synchronous hardware-encoded H.264 frame, inside the existing worker.

An ARM SIMD conversion writes RGBA directly into the encoder's NV12 ION buffer.
Separate buffers belong only to projection; GPU/shared-buffer input is future work.
"""

import ctypes
from pathlib import Path
import time

from tools.android_auto.frame_worker import MAX_FRAME_BYTES
from tools.android_auto.video import nal_units


def normalize_access_unit(data, *, keyframe):
  """Normalize driver Annex B output to the same contract as libx264."""
  if not data.startswith((b"\x00\x00\x01", b"\x00\x00\x00\x01")) or len(data) > MAX_FRAME_BYTES:
    raise RuntimeError("Expected a bounded Annex B hardware frame")
  try:
    units = list(nal_units(data))
  except IndexError as error:
    raise RuntimeError("Empty hardware NAL unit") from error
  kinds = [kind for kind, _ in units]
  if kinds.count(1) + kinds.count(5) != 1 or kinds.count(9) > 1:
    raise RuntimeError("Hardware encoder did not produce one access unit")
  if keyframe and not {5, 7, 8}.issubset(kinds):
    raise RuntimeError("Hardware encoder did not produce an independently decodable keyframe")
  # Qualcomm may emit an AUD; put exactly one first, ahead of codec headers.
  result = b"\x00\x00\x00\x01\x09\xf0" + b"".join(unit for kind, unit in units if kind != 9)
  if len(result) > MAX_FRAME_BYTES:
    raise RuntimeError("Hardware access unit exceeds shared buffer")
  return result


class HardwareH264Encoder:
  backend = "qcom-v4l2"

  def __init__(self, width, height, fps=30, bitrate=6000000, library=None, margin_height=0):
    if not 0 < width <= 1280 or not 0 < height <= 720 or width % 2 or height % 2 or fps != 30:
      raise ValueError("Hardware encoder supports even dimensions up to 1280x720 at 30 fps")
    if not 1000000 <= bitrate <= 20000000:
      raise ValueError("Hardware bitrate must be 1–20 Mbps")
    if not 0 <= margin_height < height or margin_height % 4:
      raise ValueError("Known black vertical margins must be divisible by four")
    self.width, self.height, self.fps = width, height, fps
    self.frame_index = 0
    self.handle = None
    self.lib = ctypes.CDLL(str(library or Path(__file__).with_name("libaa_encoder.so")))
    self.lib.aa_encoder_abi.argtypes = []
    self.lib.aa_encoder_abi.restype = ctypes.c_int
    if self.lib.aa_encoder_abi() != 2:
      raise RuntimeError("Rebuild the hardware encoder: incompatible ABI")
    pointer = ctypes.c_void_p
    self.lib.aa_encoder_create.argtypes = [ctypes.c_int] * 5 + [pointer, ctypes.c_size_t]
    self.lib.aa_encoder_create.restype = pointer
    self.lib.aa_encoder_encode.argtypes = [pointer, pointer, ctypes.c_size_t, ctypes.c_int,
                                          pointer, ctypes.c_size_t, pointer, ctypes.c_size_t]
    self.lib.aa_encoder_encode.restype = ctypes.c_int
    self.lib.aa_encoder_destroy.argtypes = [pointer]
    self.lib.aa_encoder_destroy.restype = None
    self.lib.aa_encoder_timing.argtypes = [pointer, ctypes.POINTER(ctypes.c_double)]
    self.lib.aa_encoder_timing.restype = None
    self.error = ctypes.create_string_buffer(512)
    self.output = ctypes.create_string_buffer(MAX_FRAME_BYTES)
    self._timing = (ctypes.c_double * 3)()
    self.handle = self.lib.aa_encoder_create(width, height, fps, bitrate, margin_height, self.error, len(self.error))
    if not self.handle:
      raise RuntimeError(self.error.value.decode("utf8", "replace"))
    self.timing = {}

  def encode_rgba(self, buffer, *, force_keyframe=False):
    if not self.handle:
      raise RuntimeError("Encoder has been closed")
    if len(buffer) != self.width * self.height * 4:
      raise ValueError("Expected tightly packed RGBA at negotiated dimensions")
    started = time.monotonic()
    if isinstance(buffer, bytes):
      rgba = ctypes.c_char_p(buffer)
    else:
      view = memoryview(buffer)
      if not view.contiguous or view.nbytes != len(buffer):
        raise ValueError("Expected a contiguous byte buffer")
      array = ctypes.c_ubyte * len(buffer)
      rgba = array.from_buffer_copy(view) if view.readonly else array.from_buffer(view)
    force = self.frame_index == 0 or force_keyframe
    size = self.lib.aa_encoder_encode(self.handle, rgba, len(buffer),
                                      force, self.output, len(self.output), self.error, len(self.error))
    if size < 0:
      raise RuntimeError(self.error.value.decode("utf8", "replace"))
    data = normalize_access_unit(self.output[:size], keyframe=force)
    self.timing = {"hardware_encode_seconds": time.monotonic() - started}
    self.lib.aa_encoder_timing(self.handle, self._timing)
    self.timing.update(zip(("color_convert_seconds", "hardware_queue_seconds", "hardware_wait_seconds"), self._timing, strict=True))
    self.frame_index += 1
    return data

  def close(self):
    if self.handle:
      self.lib.aa_encoder_destroy(self.handle)
      self.handle = None
