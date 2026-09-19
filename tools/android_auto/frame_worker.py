"""One outstanding live frame in a separately terminable worker process.

Frames use one fixed shared buffer. The control pipe only carries small status
messages, so a worker blocked in rendering/encoding cannot strand the Android
Auto event loop in a multi-megabyte pipe receive. No worker owns USB or controls.
"""

import math
import multiprocessing
from pathlib import Path
import pickle
import time

MAX_FRAME_BYTES = 2 * 1024 * 1024 - 10
MAX_CONTROL_BYTES = 2048


def frame_metadata(state, captured_at, elapsed, *, cpu_seconds=0.0, render_encode_cpu_seconds=0.0, poll_seconds=0.0, road=None):
  source_age = state.age_seconds + poll_seconds if math.isfinite(state.age_seconds) else None
  # Budget every displayed high-rate source, including a fresh road camera
  # when telemetry has gone unavailable. Calibration has its own low-rate gate.
  displayed_ages = [] if state.stale else [source_age]
  if state.alert is not None and state.alert.size:
    displayed_ages.append(state.alert_age_seconds + poll_seconds)
  if road is not None and road.get("display_age_seconds") is not None:
    # The road renderer records the oldest actually drawn source's absolute
    # timestamp. Its diagnostic age already includes part of paint time; adding
    # poll/render time again would prematurely expire perfectly fresh frames.
    if road.get("display_source_timestamp") is not None:
      displayed_ages.append(max(0, captured_at - road["display_source_timestamp"]))
    else:
      displayed_ages.append(road["display_age_seconds"] + poll_seconds)
  return {"captured_at": captured_at, "render_encode_seconds": elapsed, "stale": state.stale,
          "status": state.display_status, "speed": None if state.stale else state.speed,
          "set_speed": None if state.stale else state.set_speed, "units": state.unit_text,
          "missing_services": list(state.missing_services),
          # A reader may record its snapshot clock before reading Params. Add
          # the whole poll duration conservatively so that slow reads cannot
          # make a nearly expired source look fresh at the post-poll timestamp.
          "source_age_seconds": source_age,
          "alert_age_seconds": state.alert_age_seconds + poll_seconds if state.alert is not None and state.alert.size else None,
          "display_source_age_seconds": max(displayed_ages) if displayed_ages else None,
          "road": road,
          "cpu_seconds": cpu_seconds, "render_encode_cpu_seconds": render_encode_cpu_seconds}


def send_control(connection, message):
  # Keep header + pickle below POSIX PIPE_BUF's usual 4096-byte atomic write.
  data = pickle.dumps(message, protocol=4)
  if len(data) > MAX_CONTROL_BYTES:
    raise ValueError("Frame worker control message exceeds the fixed limit")
  connection.send_bytes(data)


def receive_control(connection):
  # This pipe is private to our own spawned child, not an untrusted interface.
  return pickle.loads(connection.recv_bytes(MAX_CONTROL_BYTES))


def _worker_main(connection, frame_buffer, config):
  encoder = None
  road_reader = None
  try:
    from tools.android_auto.live_encode import H264Encoder
    from tools.android_auto.live_render import LiveRenderer
    from tools.android_auto.live_state import LiveStateReader
    reader = LiveStateReader(sunnypilot=config["sunnypilot"], stale_after=0.35)
    if config["view"] == "road":
      from tools.android_auto.road_state import RoadStateReader
      road_reader = RoadStateReader(sunnypilot=config["sunnypilot"])
    viewport = config["viewport"]
    renderer = LiveRenderer(viewport, config["assets"], hud_path=config["hud_path"])
    encoder = H264Encoder(viewport.width, viewport.height, fps=config["fps"], threads=1)
    end = time.monotonic() + 1.5
    while True:
      state = reader.poll()
      road = None if road_reader is None else road_reader.poll()
      if time.monotonic() >= end or not state.stale and (road_reader is None or road.camera is not None):
        break
      time.sleep(0.02)
    # Load lazy drawing modules/assets, connect VisionIPC, and initialize codec
    # work before declaring readiness. This stays inside the startup watchdog;
    # no warmup image is sent. The first requested frame forces a fresh IDR.
    encoder.encode(renderer.render(state, road=road), force_keyframe=True)
    output = Path(config["output"]) if config["output"] is not None else None
    if output is not None:
      output.mkdir(parents=True, exist_ok=True)
    saved = set()
    first_frame = True
    send_control(connection, ("ready", time.process_time()))
    while True:
      command = receive_control(connection)
      if command == ("stop",):
        return
      if not isinstance(command, tuple) or len(command) != 2 or command[0] != "frame" or type(command[1]) is not bool:
        raise ValueError("Invalid frame worker request")
      poll_started = time.monotonic()
      state = reader.poll()
      road = None if road_reader is None else road_reader.poll()
      captured_at = time.monotonic()
      render_cpu_started = time.process_time()
      image = renderer.render(state, road=road)
      data = encoder.encode(image, force_keyframe=command[1] or first_frame)
      first_frame = False
      if not 0 < len(data) <= MAX_FRAME_BYTES:
        raise ValueError("Encoded frame exceeds the shared buffer")
      # A few first-occurrence diagnostics, never a video or screenshot stream.
      # Writes are covered by the parent's watchdog.
      labels = ["stale" if state.stale else "live"]
      road_metadata = renderer.road_metadata if road_reader is not None else None
      if road_metadata:
        labels += [name for name in ("camera", "model") if road_metadata.get(f"{name}_displayed")]
      saved_image = None
      for label in labels:
        if output is not None and label not in saved:
          target = output / f"first-{label}.png"
          if saved_image is None:
            image.save(target, compress_level=1)
            saved_image = target
          else:
            target.hardlink_to(saved_image)
          saved.add(label)
      memoryview(frame_buffer).cast("B")[:len(data)] = data
      cpu_seconds = time.process_time()  # Cumulative user + system time across all worker threads.
      metadata = frame_metadata(state, captured_at, time.monotonic() - captured_at, cpu_seconds=cpu_seconds,
                                render_encode_cpu_seconds=cpu_seconds - render_cpu_started, poll_seconds=captured_at - poll_started,
                                road=road_metadata)
      send_control(connection, ("frame", len(data), metadata))
  except (EOFError, BrokenPipeError):
    pass
  except Exception as error:
    try:
      send_control(connection, ("error", f"{type(error).__name__}: {str(error)[:400]}"))
    except (OSError, ValueError):
      pass
  finally:
    try:
      if road_reader is not None:
        road_reader.close()
    finally:
      if encoder is not None:
        try:
          encoder.close()
        except Exception:
          pass
      connection.close()


class FrameWorker:
  """Bounded live frame production; call poll while servicing the AA connection.

  ``poll`` raises on a crashed or >500 ms worker and returns ``None`` while it
  is still working. Consumers additionally reject stale captures before sending
  them. The shared frame is copied once upon receipt, before another request is
  allowed. No backlog can form. Construct before opening a USB session.
  """

  def __init__(self, viewport, assets, hud_path=None, sunnypilot=True, *, fps=30, output=None, startup_timeout=10.0,
               frame_timeout=0.5, view="road", _target=None):
    if not 0 < startup_timeout <= 30 or not 0 < frame_timeout <= 0.5:
      raise ValueError("Use startup <=30 seconds and frame watchdog <=500 ms")
    if view not in ("road", "hud"):
      raise ValueError("View must be road or hud")
    context = multiprocessing.get_context("spawn")
    self._frame_buffer = context.RawArray("B", MAX_FRAME_BYTES)
    self._connection, child_connection = context.Pipe(duplex=True)
    self._requested_at = None
    self.frame_timeout = frame_timeout
    self.initial_cpu_seconds = 0.0
    self.closed = False
    config = {"viewport": viewport, "assets": str(assets), "hud_path": None if hud_path is None else str(hud_path),
              "sunnypilot": sunnypilot, "fps": fps, "view": view, "output": None if output is None else str(output)}
    self._child = context.Process(target=_target or _worker_main, args=(child_connection, self._frame_buffer, config),
                                  name="automaxxing-frames", daemon=True)
    try:
      self._child.start()
      child_connection.close()
      if not self._connection.poll(startup_timeout):
        raise TimeoutError("Live frame worker did not become ready")
      message = receive_control(self._connection)
      if isinstance(message, tuple) and len(message) == 2 and message[0] == "ready":
        self.initial_cpu_seconds = float(message[1])
        if not math.isfinite(self.initial_cpu_seconds) or self.initial_cpu_seconds < 0:
          raise ValueError("Invalid worker CPU baseline")
      elif message != ("ready",):
        raise RuntimeError(f"Live frame worker initialization failed: {message}")
    except BaseException:
      child_connection.close()
      self.close()
      raise

  @property
  def busy(self):
    return self._requested_at is not None

  @property
  def pid(self):
    return self._child.pid

  def request(self, force_keyframe=False):
    if self.closed:
      raise RuntimeError("Live frame worker is closed")
    if self.busy:
      raise RuntimeError("A live frame is already outstanding")
    if type(force_keyframe) is not bool:
      raise ValueError("force_keyframe must be a boolean")
    if not self._child.is_alive():
      self.close()
      raise RuntimeError("Live frame worker exited")
    self._requested_at = time.monotonic()
    try:
      send_control(self._connection, ("frame", force_keyframe))
    except BaseException:
      self.close()
      raise

  def poll(self, timeout=0):
    if self.closed:
      raise RuntimeError("Live frame worker is closed")
    if not self.busy:
      return None
    remaining = self.frame_timeout - (time.monotonic() - self._requested_at)
    if remaining <= 0:
      self.close()
      raise TimeoutError("Live frame generation exceeded the watchdog")
    try:
      if not self._connection.poll(min(max(0, timeout), remaining)):
        if not self._child.is_alive():
          raise RuntimeError("Live frame worker exited before returning a frame")
        if time.monotonic() - self._requested_at >= self.frame_timeout:
          raise TimeoutError("Live frame generation exceeded the watchdog")
        return None
      message = receive_control(self._connection)
      if not isinstance(message, tuple) or len(message) != 3 or message[0] != "frame":
        raise RuntimeError(f"Live frame worker failed: {message}")
      _, length, metadata = message
      if type(length) is not int or not 0 < length <= MAX_FRAME_BYTES or not isinstance(metadata, dict):
        raise ValueError("Live frame worker returned an invalid frame")
      if not isinstance(metadata.get("captured_at"), (int, float)) or not math.isfinite(metadata["captured_at"]):
        raise ValueError("Live frame worker omitted its capture time")
      data = bytes(memoryview(self._frame_buffer).cast("B")[:length])
      self._requested_at = None
      return data, metadata
    except BaseException:
      self.close()
      raise

  def close(self):
    if self.closed:
      return
    self.closed = True
    self._requested_at = None
    # Do not wait for the child to process an in-band stop if it is wedged.
    try:
      if self._child.pid is not None:
        if self._child.is_alive():
          try:
            self._child.terminate()
          except ProcessLookupError:
            pass  # A crash raced the signal; join still reaps the child.
        self._child.join(timeout=0.2)
        if self._child.is_alive():
          try:
            self._child.kill()
          except ProcessLookupError:
            pass
          self._child.join(timeout=0.2)
    finally:
      self._connection.close()
