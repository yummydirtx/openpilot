"""Local phone-side Android Auto bootstrap probe; stops before TLS authentication.

Wire identifiers cross-checked against https://github.com/tomasz-grobelny/AACS.
No Android, openpilot, protobuf, or third-party Python dependencies are required.
"""

import argparse
import json
from pathlib import Path
import socket
import struct
import subprocess
import sys


def read_exact(peer, size):
  data = bytearray()
  while len(data) < size:
    chunk = peer.recv(size - len(data))
    if not chunk:
      raise EOFError(f"Peer disconnected after {len(data)}/{size} bytes")
    data.extend(chunk)
  return bytes(data)


def read_control(peer):
  channel, flags, size = struct.unpack(">BBH", read_exact(peer, 4))
  # This bootstrap only accepts complete, plaintext control frames. Reject other
  # formats explicitly instead of silently misparsing encrypted/fragmented data.
  if channel != 0 or flags != 3 or size < 2:
    raise ValueError(f"Unsupported bootstrap frame: channel={channel} flags={flags:#x} size={size}")
  payload = read_exact(peer, size)
  return struct.unpack(">H", payload[:2])[0], payload[2:]


def probe(peer):
  kind, body = read_control(peer)
  if kind != 1 or len(body) != 4:
    raise ValueError(f"Expected VersionRequest; got type={kind} body={body.hex()}")
  major, minor = struct.unpack(">HH", body)
  if major != 1:
    raise ValueError(f"Unsupported protocol major {major}")
  # Limit this experiment to the 1.5 bootstrap documented by AACS. Negotiating
  # a newer version requires inspecting its subsequent protocol differences.
  selected_minor = min(minor, 5)
  reply = struct.pack(">HHHH", 2, 1, selected_minor, 0)
  peer.sendall(struct.pack(">BBH", 0, 3, len(reply)) + reply)
  kind, body = read_control(peer)
  if kind != 3 or len(body) < 9 or body[0] != 22 or body[1] != 3 or body[5] != 1:
    raise ValueError(f"Expected TLS ClientHello; got type={kind} prefix={body[:16].hex()}")
  return {
    "result": "version_exchange_and_tls_client_hello",
    "head_unit_version": [major, minor],
    "selected_version": [1, selected_minor],
    "tls_bytes_received": len(body),
    "authentication_complete": False,
    "video_tested": False,
  }


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--dhu", type=Path, help="Launch this DHU binary headlessly; omit for a separate interactive DHU")
  parser.add_argument("--port", type=int, default=5277, help="Loopback TCP port; 0 chooses a free port")
  parser.add_argument("--timeout", type=float, default=15, help="Accept/read timeout in seconds")
  parser.add_argument("--log", type=Path, default=Path(".cache/automaxxing/dhu-probe.log"))
  args = parser.parse_args()
  if not 0 <= args.port <= 65535 or args.timeout <= 0:
    parser.error("Port must be 0–65535 and timeout must be positive")
  child = None
  try:
    with socket.socket() as listener:
      listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
      listener.bind(("127.0.0.1", args.port))
      listener.listen(1)
      listener.settimeout(args.timeout)
      port = listener.getsockname()[1]
      print(f"Listening on 127.0.0.1:{port}", file=sys.stderr, flush=True)
      if args.dhu:
        binary = args.dhu.resolve(strict=True)
        args.log.parent.mkdir(parents=True, exist_ok=True)
        with args.log.open("w") as log:
          child = subprocess.Popen([str(binary), "--headless", f"--adb=127.0.0.1:{port}", "--config=config/rotary.ini"],
                                   cwd=binary.parent, stdin=subprocess.PIPE, stdout=log, stderr=log)
      with listener.accept()[0] as peer:
        peer.settimeout(args.timeout)
        print(json.dumps(probe(peer), indent=2))
    return 0
  except (OSError, EOFError, ValueError) as error:
    print(f"Probe failed: {error}. DHU log (if launched): {args.log}", file=sys.stderr)
    return 1
  finally:
    if child is not None:
      try:
        child.communicate(b"quit\n", timeout=3)
      except subprocess.TimeoutExpired:
        child.terminate()
        try:
          child.communicate(timeout=3)
        except subprocess.TimeoutExpired:
          child.kill()
          child.communicate()


if __name__ == "__main__":
  sys.exit(main())
