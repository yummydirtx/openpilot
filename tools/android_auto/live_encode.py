"""Bounded one-frame-at-a-time software H.264 encoding using an isolated PyAV.

No subprocess, growing frame queue, GPU context, or native camera encoder is
owned here. The transport should call encode only when it can send a new frame.
"""

from fractions import Fraction

from tools.android_auto.video import nal_units


def create_native_encoder(width, height, preference="auto", *, margin_height=0):
  """Choose once at startup; never switch codecs during a live media session."""
  if preference not in ("auto", "software", "hardware"):
    raise ValueError("Encoder must be auto, software, or hardware")
  fallback = None
  if preference != "software":
    encoder = None
    try:
      from tools.android_auto.hardware_encode import HardwareH264Encoder
      encoder = HardwareH264Encoder(width, height, margin_height=margin_height)
      # Exercise the driver and independently-decodable packet contract before
      # declaring readiness. All frames actually sent get another forced IDR.
      encoder.encode_rgba(bytes(width * height * 4), force_keyframe=True)
      return encoder, None
    except (OSError, RuntimeError, ValueError, AttributeError) as error:
      if encoder is not None:
        encoder.close()
      if preference == "hardware":
        raise
      fallback = f"{type(error).__name__}: {str(error)[:160]}"
  return H264Encoder(width, height), fallback


class H264Encoder:
  backend = "libx264"

  def __init__(self, width, height, fps=30, threads=1):
    if not (0 < width <= 1920 and 0 < height <= 1080) or width % 2 or height % 2:
      raise ValueError("H.264 requires even dimensions no larger than 1920x1080")
    if fps not in (15, 30, 60) or not 1 <= threads <= 2:
      raise ValueError("Use 15/30/60 fps and one or two encoder threads")
    import av
    self.av = av
    self.width, self.height = width, height
    self.fps = fps
    self.frame_index = 0
    self.closed = False
    self.codec = av.CodecContext.create("libx264", "w")
    self.codec.width = width
    self.codec.height = height
    self.codec.pix_fmt = "yuv420p"
    self.codec.time_base = Fraction(1, fps)
    self.codec.framerate = Fraction(fps)
    self.codec.thread_count = threads
    self.codec.gop_size = fps
    self.codec.max_b_frames = 0
    level = ("4.0" if fps <= 30 else "4.2") if height > 720 else ("3.1" if fps <= 30 else "3.2")
    self.codec.options = {"preset": "ultrafast", "tune": "zerolatency", "profile": "baseline", "level": level, "crf": "25",
                          "forced-idr": "1", "x264-params": f"aud=1:repeat-headers=1:annexb=1:keyint={fps}:scenecut=0"}
    self.codec.open()

  def encode(self, image, *, force_keyframe=False):
    if self.closed:
      raise RuntimeError("Encoder has been closed")
    if image.mode != "RGB" or image.size != (self.width, self.height):
      raise ValueError("Encoder expects an RGB image with the negotiated dimensions")
    frame = self.av.VideoFrame.from_image(image)
    return self._encode_frame(frame, force_keyframe)

  def encode_rgba(self, buffer, *, force_keyframe=False):
    """Encode the GPU's final RGBA readback without intermediate RGB images."""
    if self.closed:
      raise RuntimeError("Encoder has been closed")
    frame = self.av.VideoFrame(self.width, self.height, "rgba")
    if frame.planes[0].line_size != self.width * 4 or len(buffer) != self.width * self.height * 4:
      raise ValueError("GPU readback must be tightly packed RGBA at the negotiated dimensions")
    frame.planes[0].update(buffer)
    return self._encode_frame(frame, force_keyframe)

  def _encode_frame(self, frame, force_keyframe):
    frame.pts = self.frame_index
    frame.time_base = self.codec.time_base
    if force_keyframe:
      frame.pict_type = self.av.video.frame.PictureType.I
    packets = self.codec.encode(frame)
    if len(packets) != 1:
      raise RuntimeError(f"Low-latency encoder buffered or split a frame ({len(packets)} packets)")
    data = bytes(packets[0])
    kinds = [kind for kind, _ in nal_units(data)]
    if len(data) > 2 * 1024 * 1024 - 10 or kinds.count(9) != 1:
      raise RuntimeError("Encoder did not produce one bounded AUD-delimited access unit")
    if (self.frame_index == 0 or force_keyframe) and not {5, 7, 8}.issubset(kinds):
      raise RuntimeError("Encoder did not produce an independently decodable keyframe")
    self.frame_index += 1
    return data

  def close(self):
    if not self.closed:
      # Zerolatency guarantees no pending packets; discard none silently.
      self.closed = True
      if self.codec.encode(None):
        raise RuntimeError("Low-latency encoder unexpectedly retained pending frames")
      self.codec = None
