"""Exercise actual rotary wire messages using Google's local Desktop Head Unit.

No vehicle connection and no driving commands. The DHU certificate limitations
in authentication.md apply; this local protocol probe does not assert car TLS.
"""

import argparse
from pathlib import Path
import socket
import subprocess
import time

from tools.android_auto.live_session import LiveVideoSession


def main():
  p = argparse.ArgumentParser(description=__doc__)
  p.add_argument("--dhu", type=Path, default=Path(".cache/automaxxing/dhu-2.1/desktop-head-unit"))
  p.add_argument("--identity", type=Path, default=Path(".cache/automaxxing/imported-identity"))
  p.add_argument("--output", type=Path, required=True)
  args = p.parse_args()
  args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
  binary = args.dhu.resolve(strict=True)
  child = None
  try:
    with socket.socket() as listener, (args.output / "events.jsonl").open("w") as events, (args.output / "dhu.log").open("w") as log:
      listener.bind(("127.0.0.1", 0))
      listener.listen(1)
      listener.settimeout(15)
      child = subprocess.Popen([str(binary), "--headless", f"--adb=127.0.0.1:{listener.getsockname()[1]}",
                                "--config=config/rotary.ini"], cwd=binary.parent, stdin=subprocess.PIPE, stdout=log, stderr=log)
      with listener.accept()[0] as peer:
        peer.settimeout(0.2)
        s = LiveVideoSession(peer, args.identity / "phone-cert.pem", args.identity / "phone-key.pem", events)
        s.authenticate()
        s.open_video(800, 480)

        def pump():
          end = time.monotonic() + 0.35
          while time.monotonic() < end:
            s.pump(0.02)

        for command, expected, steps in (("dpad click", "select", 1), ("dpad rotate right", "rotate", 1),
                                         ("dpad rotate left", "rotate", -1), ("dpad up", "up", 1),
                                         ("dpad down", "down", 1), ("dpad left", "left", 1),
                                         ("dpad right", "right", 1), ("dpad back", "back", 1),
                                         ("keycode home", "home", 1), ("keycode media", "music", 1),
                                         ("keycode navigation", "navigation", 1)):
          child.stdin.write((command + "\n").encode())
          child.stdin.flush()
          pump()
          actions = [(a.name, a.steps) for a in s.input_actions]
          s.input_actions.clear()
          if actions != [(expected, steps)]:
            raise RuntimeError(f"{command}: expected {(expected, steps)}, received {actions}")
          print(f"PASS {command}: {actions}", flush=True)
        s.request_native()
        pump()
        if not s.native_focus_seen:
          raise RuntimeError("DHU did not acknowledge OEM focus request")
        print("PASS native focus release", flush=True)
        s.request_projection()
        pump()
        if not s.focused:
          raise RuntimeError("DHU did not restore video focus")
        print("PASS projection focus return", flush=True)
        s.shutdown()
    return 0
  finally:
    if child is not None:
      try:
        child.communicate(b"quit\n", timeout=3)
      except subprocess.TimeoutExpired:
        child.kill()
        child.communicate()


if __name__ == "__main__":
  raise SystemExit(main())
