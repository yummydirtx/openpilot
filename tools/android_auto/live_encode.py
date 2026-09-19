"""Bounded one-frame-at-a-time software H.264 encoding using an isolated PyAV.

No subprocess, growing frame queue, GPU context, or native camera encoder is
owned here. The transport should call encode only when it can send a new frame.
"""

from fractions import Fraction

from tools.android_auto.video import nal_units


class H264Encoder:
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
