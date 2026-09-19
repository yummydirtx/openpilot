"""Manually launched, resource-limited, read-only live Android Auto dashboard.

Run via the documented transient systemd unit, whose cgroup contains renderer,
encoder and USB bridge. No manager integration or vehicle command publishers.
"""

import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import time

from tools.android_auto.live_session import LiveVideoSession, PeerRequestedStop
from tools.android_auto.usb import DEVICE, GADGET_ROOT, Gadget, accessory_peer, wait_accessory
from tools.android_auto.viewport import Viewport

LOCK = Path("/run/automaxxing.lock")
LEASE = Path("/run/automaxxing-owned.json")


def atomic_json(path, value):
  temporary = path.with_suffix(".tmp")
  temporary.write_text(json.dumps(value, indent=2) + "\n")
  temporary.replace(path)


def recover_owned_gadget():
  """ExecStopPost cleanup, after systemd has killed all child processes.

  Only a root-owned lease made before our own setup allows recovery. Refuse
  any unexpected function, configuration, descriptor, or symlink target.
  """
  if not LEASE.exists():
    return
  if LEASE.stat().st_uid != 0 or LEASE.stat().st_mode & 0o077:
    raise RuntimeError("Invalid gadget ownership lease permissions")
  lease = json.loads(LEASE.read_text())
  path = GADGET_ROOT / "automaxxing"
  if lease != {"version": 1, "gadget": str(path)}:
    raise RuntimeError("Unknown gadget ownership lease")
  if not path.exists():
    LEASE.unlink()
    return
  if {p.name for p in (path / "functions").iterdir()} - {"accessory.0"}:
    raise RuntimeError("Unexpected USB function; refusing recovery")
  if {p.name for p in (path / "configs").iterdir()} - {"c.1"}:
    raise RuntimeError("Unexpected USB configuration; refusing recovery")
  serial = path / "strings/0x409/serialnumber"
  if serial.exists() and serial.read_text().strip() not in ("", "automaxxing-dev"):
    raise RuntimeError("Unexpected gadget serial; refusing recovery")
  link = path / "configs/c.1/accessory.0"
  if link.is_symlink() and link.resolve() != path / "functions/accessory.0":
    raise RuntimeError("Unexpected gadget link; refusing recovery")
  # No function can still be open: caller must first terminate the cgroup.
  (path / "UDC").write_text("\n")
  if link.is_symlink():
    link.unlink()
  for relative in ("functions/accessory.0", "configs/c.1/strings/0x409", "configs/c.1", "strings/0x409", ""):
    target = path / relative
    if target.exists():
      target.rmdir()
  LEASE.unlink()


def claim_gadget():
  if LEASE.exists() or list(GADGET_ROOT.iterdir()) or Path(DEVICE).exists():
    raise RuntimeError("Existing gadget/lease found; stop the prior service and recover it first")
  fd = os.open(LEASE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
  with os.fdopen(fd, "w") as out:
    json.dump({"version": 1, "gadget": str(GADGET_ROOT / "automaxxing")}, out)


def project_live(session, worker, deadline, fps, status_path, display_control=None):
  from tools.android_auto.menu import ProjectionMenu
  menu = ProjectionMenu()
  start = time.monotonic()
  next_frame = start
  last_status = start - 2
  requested = None
  requested_epoch = 0
  max_age = 0
  dropped = 0
  live_frames = stale_frames = 0
  last_metadata = {}
  cpu_sample = None
  cpu_over_budget = 0
  worker_cpu = getattr(worker, "initial_cpu_seconds", 0.0)
  last_sent = start
  valid_until = None
  focus_epoch = session.focus_epoch
  while time.monotonic() < deadline:
    session.pump(0.005)
    if display_control is not None:
      display_control.sync(session)
    while getattr(session, "input_actions", ()):
      command = menu.handle(session.input_actions.popleft())
      if command in ("exit", "local"):
        session.request_native(resume_from_head_unit=command == "exit")
    if not session.focused:
      menu.reset()
    now = time.monotonic()
    if session.focus_epoch != focus_epoch:
      focus_epoch = session.focus_epoch
      last_sent, valid_until = now, None
    if session.focused and (now - last_sent > 0.5 or valid_until is not None and now >= valid_until):
      session.event("freshness_expired", frames_sent=session.frames_sent, frames_acked=session.acked,
                    since_last_send_ms=round((now - last_sent) * 1000, 2),
                    expired_by_ms=None if valid_until is None else round((now - valid_until) * 1000, 2), display=last_metadata)
      raise TimeoutError("Displayed live frame exceeded its 500 ms freshness budget")
    session.check_progress(now)
    if worker.busy:
      result = worker.poll(0)
      if result is not None:
        data, metadata = result
        worker_cpu = metadata["cpu_seconds"]
        now = time.monotonic()
        age = now - metadata["captured_at"]
        source_age = metadata.get("display_source_age_seconds",
                                  None if metadata["stale"] else metadata.get("source_age_seconds", 0))
        too_old = age > 0.25 or (source_age is not None and source_age + age > 0.5)
        if too_old or not session.focused or requested_epoch != session.focus_epoch or session.unacked >= session.window:
          dropped += 1
          session.needs_keyframe = True
          session.event("frame_discarded", capture_age_ms=round(age * 1000, 2), source_age_seconds=source_age,
                        too_old=too_old, focused=session.focused, focus_epoch_changed=requested_epoch != session.focus_epoch)
        else:
          session.send_frame(data, round((now - start) * 1_000_000))
          last_sent = now
          valid_until = None if source_age is None else metadata["captured_at"] + max(0, 0.5 - source_age)
          max_age = max(max_age, age)
          last_metadata = metadata
          if session.frames_sent <= 3:
            session.event("first_frame_timing", frame=session.frames_sent, capture_age_ms=round(age * 1000, 2),
                          send_ms=round((time.monotonic() - now) * 1000, 2), display=metadata)
          if metadata["stale"]:
            stale_frames += 1
          else:
            live_frames += 1
        requested = None
      elif now - requested > 0.5:
        raise TimeoutError("Renderer/encoder did not produce a frame within 500 ms")
    if not worker.busy and session.focused and session.unacked < session.window and now >= next_frame:
      requested_epoch = session.focus_epoch
      if getattr(session, "input_channel", None) is not None:
        worker.request(force_keyframe=session.needs_keyframe, menu=menu.snapshot())
      else:
        worker.request(force_keyframe=session.needs_keyframe)
      requested = now
      # Never catch up overdue frames; sample current telemetry at each request.
      next_frame = now + 1 / fps
    if display_control is not None:
      display_control.publish(session, valid_until=valid_until, stale=last_metadata.get("stale", True))
    if now - last_status >= 1:
      summary = {"phase": "streaming" if session.focused else "native_display", "frames_sent": session.frames_sent,
                 "frames_acked": session.acked, "max_pending": session.max_pending,
                 "max_ack_ms": round(session.max_ack_seconds * 1000, 2), "max_frame_age_ms": round(max_age * 1000, 2),
                 "dropped_frames": dropped, "live_frames": live_frames, "stale_frames": stale_frames,
                 "elapsed_seconds": round(now - start, 2), "display": last_metadata, "head_unit_verified": True}
      summary["worker_pid"] = worker.pid
      cpu = time.process_time() + worker_cpu
      if cpu_sample is not None:
        cores = max(0, cpu - cpu_sample[1]) / (now - cpu_sample[0])
        summary["renderer_and_sender_cpu_cores"] = round(cores, 3)
        cpu_over_budget = cpu_over_budget + 1 if cores > 0.85 else 0
        if cpu_over_budget >= 5:
          raise RuntimeError("Projection exceeded 85% of one CPU core for five samples")
      cpu_sample = (now, cpu)
      atomic_json(status_path, summary)
      session.event("live_status", **summary)
      last_status = now
  drain_deadline = time.monotonic() + 0.5
  while session.unacked and time.monotonic() < drain_deadline:
    session.pump(0.01)
  if session.unacked and session.focused:
    raise TimeoutError("Final live video acknowledgements missing")
  return {"frames_sent": session.frames_sent, "frames_acked": session.acked, "max_frame_age_ms": round(max_age * 1000, 2),
          "max_ack_ms": round(session.max_ack_seconds * 1000, 2), "dropped_frames": dropped,
          "discarded_inflight": session.discarded_inflight, "live_frames": live_frames, "stale_frames": stale_frames,
          "elapsed_seconds": round(time.monotonic() - start, 2)}


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--recover", action="store_true", help="Cleanup only; call after killing the experiment's entire cgroup")
  parser.add_argument("--duration", type=float, default=600)
  parser.add_argument("--attach-timeout", type=int, default=30)
  parser.add_argument("--identity", type=Path, default=Path("identity"))
  parser.add_argument("--assets", type=Path, default=Path("/data/openpilot/openpilot/selfdrive/assets"))
  parser.add_argument("--hud-path", type=Path, default=Path("native/hud_drawing.py"))
  parser.add_argument("--output", type=Path, default=Path("live-run"))
  parser.add_argument("--width", type=int, default=1280)
  parser.add_argument("--height", type=int, default=720)
  parser.add_argument("--margin-height", type=int, default=240)
  parser.add_argument("--fps", type=int, choices=(8, 10, 15, 30), default=8, help="Source update rate; negotiated codec mode is 30 fps")
  parser.add_argument("--view", choices=("road", "hud"), default="road", help="Full passive onroad view, or HUD-only fallback")
  parser.add_argument("--stock", action="store_true", help="Use stock openpilot instead of Sunnypilot state semantics")
  parser.add_argument("--once", action="store_true", help="Exit on a session failure instead of reconnecting")
  parser.add_argument("--managed", action="store_true", help="Require local-display supervisor heartbeats")
  args = parser.parse_args()
  if os.geteuid() != 0:
    parser.error("USB setup requires root; launch using the documented limited service")
  os.umask(0o077)
  with LOCK.open("a") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if args.recover:
      recover_owned_gadget()
      return 0
    if not 1 <= args.duration <= 7200 or not 1 <= args.attach_timeout <= 120:
      parser.error("Duration must be 1–7200 seconds and attachment timeout 1–120 seconds")
    from tools.android_auto.frame_worker import FrameWorker

    args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    viewport = Viewport(args.width, args.height, 0, args.margin_height)
    for sig in (signal.SIGINT, signal.SIGTERM):
      signal.signal(sig, lambda signum, frame: (_ for _ in ()).throw(KeyboardInterrupt()))
    end = time.monotonic() + args.duration
    attempt = 0
    errors = 0
    results = []
    display_control = None
    if args.managed:
      from tools.android_auto.display_control import DisplayControl
      display_control = DisplayControl()
    try:
      while time.monotonic() < end:
        attempt += 1
        out = args.output / f"session-{attempt:03d}"
        out.mkdir()
        gadget = Gadget()
        worker = None
        session = None
        claimed = False
        delay = 0
        try:
          atomic_json(args.output / "status.json", {"phase": "initializing", "attempt": attempt})
          # PNG diagnostics belong to live_preview. Compressing/writing a
          # camera snapshot must never delay the live display's first frame.
          worker = FrameWorker(viewport, args.assets, args.hud_path, sunnypilot=not args.stock, view=args.view)
          claim_gadget()
          claimed = True
          gadget.setup(negotiate=True)
          with (out / "events.jsonl").open("w") as events, (out / "bridge.log").open("w") as bridge_log:
            wait_accessory(gadget, min(args.attach_timeout, max(1, end - time.monotonic())), report=lambda s: print(s, flush=True))
            with accessory_peer(bridge_log) as peer:
              peer.settimeout(0.1)
              session = LiveVideoSession(peer, args.identity / "phone-cert.pem", args.identity / "phone-key.pem", events,
                                         ca=args.identity / "root-cert.pem", receive_timeout=0.1, send_timeout=0.1)
              session.authenticate()
              session.open_video(args.width, args.height)
              if (session.margin_width, session.margin_height) != (0, args.margin_height):
                raise ValueError("Negotiated margins differ from renderer viewport")
              result = project_live(session, worker, end, args.fps, args.output / "status.json", display_control)
              session.shutdown()
              result.update(head_unit_verified=True, shutdown_acknowledged=True)
              atomic_json(out / "result.json", result)
              results.append(result)
              errors = 0
        except (OSError, EOFError, ValueError, RuntimeError) as error:
          errors += 1
          result = {"error": str(error), "attempt": attempt}
          if session is not None:
            result.update(frames_sent=getattr(session, "frames_sent", 0), frames_acked=getattr(session, "acked", 0))
          atomic_json(out / "result.json", result)
          results.append(result)
          print(json.dumps(result), flush=True)
          atomic_json(args.output / "status.json", {"phase": "disconnected", **result})
          if args.once:
            break
          # Respect the user's request to leave projection; a manual start is
          # needed after an explicit AA ByeBye. Plain cable failures retry.
          if isinstance(error, PeerRequestedStop):
            break
          delay = min(10, 1 + errors)
        finally:
          if display_control is not None:
            try:
              display_control.clear()
            except OSError:
              # An unwritable mailbox still expires locally. Never skip worker
              # termination or owned-gadget cleanup because status publishing failed.
              pass
          try:
            if worker is not None:
              worker.close()
          finally:
            gadget.cleanup()
            if claimed:
              if gadget.path.exists():
                raise RuntimeError("Gadget remains after cleanup; ownership lease retained for recovery")
              LEASE.unlink(missing_ok=True)
        time.sleep(min(delay, max(0, end - time.monotonic())))
    except KeyboardInterrupt:
      print("Projection stopped", flush=True)
    finally:
      atomic_json(args.output / "summary.json", {"attempts": attempt, "sessions": results, "stopped": True})
      atomic_json(args.output / "status.json", {"phase": "stopped", "attempts": attempt})
    return int(not any("error" not in r and r.get("frames_sent", 0) > 0 for r in results))


if __name__ == "__main__":
  raise SystemExit(main())
