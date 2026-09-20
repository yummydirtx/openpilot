"""Bounded native GPU rendering, sharing the existing worker IPC/watchdog."""

import os
from pathlib import Path
import time

from tools.android_auto.frame_worker import MAX_FRAME_BYTES, receive_control, send_control


def run(connection, frame_buffer, config):
  # Must precede all imports of the native frontend (fonts/layout constants
  # are chosen at module import). The manager's four UI is another process.
  os.environ["BIG"] = "1"
  os.environ["SUNNYPILOT_UI"] = "1" if config["sunnypilot"] else "0"
  renderer = encoder = None
  try:
    from tools.android_auto.native_identity import native_identity
    native_identity(config["output"])
    from tools.android_auto.native_renderer import NativeRenderer
    from tools.android_auto.live_encode import create_native_encoder
    renderer = NativeRenderer(config["viewport"])
    viewport = config["viewport"]
    # Only skip known black rows with chroma-aligned margin boundaries.
    margin = viewport.margin_height if viewport.margin_height % 4 == 0 else 0
    encoder, encoder_fallback = create_native_encoder(viewport.width, viewport.height, config.get("encoder", "software"),
                                                      margin_height=margin)
    end = time.monotonic() + 2
    while True:
      image = renderer.render(raw=True)
      encoder.encode_rgba(image.buffer, force_keyframe=True)
      image.close()
      if time.monotonic() >= end or renderer.state.started and not renderer.metadata(time.monotonic())["stale"]:
        break
      time.sleep(.02)
    send_control(connection, ("ready", time.process_time()))
    first = True
    while True:
      command = receive_control(connection)
      if command == ("stop",):
        break
      if not isinstance(command, tuple) or len(command) not in (2, 3) or command[0] != "frame" or type(command[1]) is not bool:
        raise ValueError("Invalid native frame request")
      request = command[2] if len(command) == 3 else {}
      actions = request.get("actions", ())
      if len(actions) > 32:
        raise ValueError("Too many native UI actions")
      renderer.command = None
      captured_at, cpu_start = time.monotonic(), time.process_time()
      image = renderer.render(actions, raw=True)
      try:
        metadata = renderer.metadata(captured_at)
        if metadata["stale"]:
          raise TimeoutError("Native UI source data is unavailable or stale")
        encode_started = time.monotonic()
        data = encoder.encode_rgba(image.buffer, force_keyframe=first or command[1])
        metadata.update(renderer.frame_timing, encode_seconds=time.monotonic() - encode_started)
        metadata.update(getattr(encoder, "timing", {}), encoder=getattr(encoder, "backend", "libx264"))
        if encoder_fallback is not None:
          metadata["encoder_fallback"] = encoder_fallback
        if first and config["output"]:
          output = Path(config["output"])
          output.mkdir(parents=True, exist_ok=True)
          image.pil().save(output / "first-native.png", compress_level=1)
      finally:
        image.close()
      if not 0 < len(data) <= MAX_FRAME_BYTES:
        raise ValueError("Native encoded frame exceeds shared buffer")
      first = False
      memoryview(frame_buffer).cast("B")[:len(data)] = data
      metadata.update(cpu_seconds=time.process_time(), render_encode_cpu_seconds=time.process_time() - cpu_start,
                      render_encode_seconds=time.monotonic() - captured_at)
      send_control(connection, ("frame", len(data), metadata))
  except (EOFError, BrokenPipeError):
    pass
  except Exception as error:
    try:
      send_control(connection, ("error", f"{type(error).__name__}: {str(error)[:400]}"))
    except (OSError, ValueError):
      pass
  finally:
    if encoder is not None:
      encoder.close()
    if renderer is not None:
      renderer.close()
    connection.close()
