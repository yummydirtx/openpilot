"""Parked AGNOS accessory experiment, using its existing f_accessory driver.

Reference: commaai/agnos-kernel-sdm845 drivers/usb/gadget/function/f_accessory.c
and include/uapi/linux/usb/f_accessory.h. No kernel or USB role changes.
"""

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time

from tools.android_auto.probe import probe
from tools.android_auto.session import Session
from tools.android_auto.video import VideoSession, access_units

GADGET_ROOT = Path("/sys/kernel/config/usb_gadget")
UDC = "a600000.dwc3"
DEVICE = "/dev/usb_accessory"


def wait_accessory(gadget, timeout, report=print):
  """Negotiate using the existing kernel driver; return only after configuration."""
  deadline = time.monotonic() + timeout
  negotiating = True
  last = None
  result = {}
  monitor = os.open(DEVICE, os.O_RDWR)
  try:
    while True:
      state = (Path("/sys/class/udc") / UDC / "state").read_text().strip()
      if state != last:
        report(f"USB state: {state}")
        last = state
      if fcntl.ioctl(monitor, 0x4D07):
        result["aoa_start"] = True
        report("AOA START received")
        gadget.switch_to_accessory()
        negotiating = False
        deadline = time.monotonic() + 30
        continue
      if state == "configured" and not negotiating:
        result["usb_speed"] = (Path("/sys/class/udc") / UDC / "current_speed").read_text().strip()
        return result
      if time.monotonic() > deadline:
        raise TimeoutError("USB attachment/negotiation timed out")
      time.sleep(0.025)
  finally:
    os.close(monitor)


def write_all(fd, data):
  view = memoryview(data)
  while view:
    count = os.write(fd, view)
    if count <= 0:
      raise OSError("Accessory write made no progress")
    view = view[count:]


def bridge(socket_fd):
  """Isolate blocking, non-pollable kernel I/O in a terminable process.

  Always read a complete 16 KiB driver buffer: short userspace reads can discard
  the remainder of a USB transfer. The socket supplies buffering and select().
  """
  peer = socket.socket(fileno=socket_fd)
  fd = os.open(DEVICE, os.O_RDWR)

  def receive():
    try:
      while data := os.read(fd, 16384):
        peer.sendall(data)
    except OSError as error:
      print(f"Accessory reader stopped: {error}", file=sys.stderr, flush=True)
    finally:
      peer.shutdown(socket.SHUT_RDWR)

  threading.Thread(target=receive, daemon=True).start()
  try:
    while data := peer.recv(16384):
      write_all(fd, data)
  except OSError as error:
    print(f"Accessory writer stopped: {error}", file=sys.stderr, flush=True)
  finally:
    # The process owns both the fd and the thread. Exiting kills a still-blocked
    # reader before the parent removes the kernel function.
    os._exit(0)


class Gadget:
  def __init__(self, root=GADGET_ROOT):
    self.root = root
    self.path = root / "automaxxing"
    self.created = []
    self.linked = False
    self.bound = False

  def mkdir(self, relative):
    path = self.path / relative
    path.mkdir()
    self.created.append(path)

  def write(self, relative, value):
    (self.path / relative).write_text(str(value) + "\n")

  def setup(self, negotiate=False):
    if list(self.root.iterdir()) or Path(DEVICE).exists():
      raise RuntimeError("An existing gadget/accessory is present; refusing to displace it")
    if not (Path("/sys/class/udc") / UDC).exists():
      raise RuntimeError("Expected AGNOS data-port UDC is absent")
    self.mkdir("")
    # Initial compatibility IDs from the pinned AACS ModeSwitcher experiment.
    # They are an experiment, not a proven requirement of every head unit.
    self.write("idVendor", "0x12d1" if negotiate else "0x18d1")
    self.write("idProduct", "0x107e" if negotiate else "0x2d00")
    self.write("bcdUSB", "0x0200")
    self.write("bcdDevice", "0x0100")
    self.mkdir("strings/0x409")
    self.write("strings/0x409/manufacturer", "Automaxxing")
    self.write("strings/0x409/product", "Parked projection experiment")
    self.write("strings/0x409/serialnumber", "automaxxing-dev")
    self.mkdir("configs/c.1")
    self.write("configs/c.1/MaxPower", 100)
    self.write("configs/c.1/bmAttributes", "0xc0")
    self.mkdir("configs/c.1/strings/0x409")
    self.write("configs/c.1/strings/0x409/configuration", "Android accessory")
    self.mkdir("functions/accessory.0")
    (self.path / "configs/c.1/accessory.0").symlink_to(self.path / "functions/accessory.0")
    self.linked = True
    self.write("UDC", UDC)
    self.bound = True

  def switch_to_accessory(self):
    self.write("UDC", "")
    self.bound = False
    self.write("idVendor", "0x18d1")
    self.write("idProduct", "0x2d00")
    time.sleep(0.5)
    self.write("UDC", UDC)
    self.bound = True

  def cleanup(self):
    if self.bound:
      self.write("UDC", "")
      self.bound = False
    if self.linked:
      (self.path / "configs/c.1/accessory.0").unlink()
      self.linked = False
    while self.created:
      self.created[-1].rmdir()
      self.created.pop()


@contextmanager
def accessory_peer(log):
  parent, child = socket.socketpair()
  process = None
  try:
    process = subprocess.Popen([sys.executable, "-m", "tools.android_auto.usb", "--bridge-fd", str(child.fileno())],
                               pass_fds=(child.fileno(),), stderr=log)
    child.close()
    parent.settimeout(15)
    yield parent
  finally:
    parent.close()
    child.close()
    if process is not None:
      process.terminate()
      try:
        process.wait(timeout=3)
      except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--bridge-fd", type=int, help=argparse.SUPPRESS)
  parser.add_argument("--mode", choices=("probe", "auth", "video"), default="probe")
  parser.add_argument("--identity", type=Path, default=Path("identity"))
  parser.add_argument("--ca", type=Path, help="Head-unit trust root (default: IDENTITY/root-cert.pem)")
  parser.add_argument("--video", type=Path, help="Previously validated AUD-delimited H.264 clip")
  parser.add_argument("--width", type=int, default=800)
  parser.add_argument("--height", type=int, default=480)
  parser.add_argument("--fps", type=int, choices=(30, 60), default=30)
  parser.add_argument("--attach-timeout", type=int, default=30, choices=range(1, 121), metavar="SECONDS")
  parser.add_argument("--negotiate", action="store_true", help="Wait for AOA START before changing to accessory IDs")
  parser.add_argument("--output", type=Path, required=False, default=Path("usb-result"))
  parser.add_argument("--parked", action="store_true", help="Confirm vehicle will stay parked throughout this experiment")
  args = parser.parse_args()
  if args.bridge_fd is not None:
    bridge(args.bridge_fd)
    return 0
  if not args.parked or os.geteuid() != 0:
    parser.error("Requires root and explicit --parked confirmation")
  frames = None
  if args.mode == "video":
    if args.video is None:
      parser.error("--video is required")
    frames = access_units(args.video.read_bytes())
    if len(frames) > args.fps * 60:
      parser.error("This experiment is limited to 60 seconds of video")
  args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
  gadget = Gadget()
  result = {"mode": args.mode, "authentication_complete": False, "head_unit_verified": False, "video_tested": False}

  def stop(signum, frame):
    raise KeyboardInterrupt(f"Signal {signum}")

  for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGALRM):
    signal.signal(sig, stop)
  signal.alarm(args.attach_timeout + 90)
  try:
    with (args.output / "events.jsonl").open("w") as events, (args.output / "bridge.log").open("w") as log:
      gadget.setup(negotiate=args.negotiate)
      print("Temporary accessory gadget bound", flush=True)
      deadline = time.monotonic() + args.attach_timeout
      last = None
      negotiating = args.negotiate
      monitor = os.open(DEVICE, os.O_RDWR)
      try:
        while True:
          state = (Path("/sys/class/udc") / UDC / "state").read_text().strip()
          if state != last:
            print(f"USB state: {state}", flush=True)
            last = state
          if fcntl.ioctl(monitor, 0x4D07):  # ACCESSORY_IS_START_REQUESTED
            host = {}
            for number, name in ((1, "manufacturer"), (2, "model")):
              value = bytearray(256)
              fcntl.ioctl(monitor, 0x41004D00 | number, value)
              host[name] = value.split(b"\0", 1)[0].decode("utf-8", "replace")
            result["aoa_start"] = host
            print(f"AOA START: {host}", flush=True)
            gadget.switch_to_accessory()
            negotiating = False
            deadline = time.monotonic() + 30
            continue
          if state == "configured" and not negotiating:
            break
          if time.monotonic() > deadline:
            raise TimeoutError("Timed out waiting for AOA START" if negotiating else "Timed out waiting for USB configuration")
          time.sleep(0.025)
      finally:
        os.close(monitor)
      result["usb_configured"] = True
      result["usb_speed"] = (Path("/sys/class/udc") / UDC / "current_speed").read_text().strip()
      with accessory_peer(log) as peer:
        if args.mode == "probe":
          result.update(probe(peer))
        else:
          cls = VideoSession if args.mode == "video" else Session
          ca = args.ca or args.identity / "root-cert.pem"
          session = cls(peer, args.identity / "phone-cert.pem", args.identity / "phone-key.pem", events, ca=ca)
          session.authenticate()
          result.update(authentication_complete=True, head_unit_verified=True)
          if args.mode == "video":
            session.open_video(args.width, args.height)
            result.update(session.project(frames, args.fps))
            result["video_tested"] = True
            session.shutdown()
            result["shutdown_acknowledged"] = True
          else:
            result["channels"] = session.discover()
          result["service_discovery_complete"] = True
  except (OSError, EOFError, ValueError, RuntimeError, KeyboardInterrupt) as error:
    result["error"] = str(error)
  finally:
    signal.alarm(0)
    try:
      gadget.cleanup()
      result["gadget_removed"] = not gadget.path.exists()
    except OSError as error:
      result["cleanup_error"] = str(error)
    (args.output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2), flush=True)
  return int("error" in result or "cleanup_error" in result)


if __name__ == "__main__":
  raise SystemExit(main())
