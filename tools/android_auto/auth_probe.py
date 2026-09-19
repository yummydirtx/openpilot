"""Probe AA TLS/authentication against DHU; its certificate check is expected to reject a self-signed identity."""

import argparse
import json
from pathlib import Path
import socket
import subprocess
import sys

from tools.android_auto.session import AuthenticationRejected, Session


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--dhu", type=Path, default=Path(".cache/automaxxing/dhu-2.0/desktop-head-unit"))
  parser.add_argument("--output", type=Path, default=Path(".cache/automaxxing/session"))
  parser.add_argument("--cert", type=Path, help="Phone certificate accepted by the head unit (requires --key)")
  parser.add_argument("--key", type=Path, help="Private key matching --cert; never written to logs")
  parser.add_argument("--ca", type=Path, help="Verify the head unit's certificate against this CA")
  args = parser.parse_args()
  if bool(args.cert) != bool(args.key):
    parser.error("--cert and --key must be supplied together")
  args.output.mkdir(parents=True, exist_ok=True)
  cert = args.cert or args.output / "phone-cert.pem"
  key = args.key or args.output / "localhost-key.pem"
  if args.cert is None and (not cert.exists() or not key.exists()):
    # Correct protocol identity, locally generated key. DHU verifies the signing
    # authority too, so this intentionally diagnoses rejection, not compatibility.
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-nodes", "-days", "7",
                    "-subj", "/C=US/ST=California/L=Mountain View/O=CarService/OU=53", "-keyout", str(key), "-out", str(cert)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    key.chmod(0o600)
  binary = args.dhu.resolve(strict=True)
  child = None
  try:
    with socket.socket() as listener, (args.output / "events.jsonl").open("w") as events, (args.output / "dhu.log").open("w") as log:
      listener.bind(("127.0.0.1", 0))
      listener.listen(1)
      listener.settimeout(15)
      port = listener.getsockname()[1]
      child = subprocess.Popen([str(binary), "--headless", f"--adb=127.0.0.1:{port}", "--config=config/rotary.ini"],
                               cwd=binary.parent, stdin=subprocess.PIPE, stdout=log, stderr=log)
      with listener.accept()[0] as peer:
        peer.settimeout(10)
        session = Session(peer, cert, key, events, ca=args.ca)
        session.authenticate()
        channels = session.discover()
        print(json.dumps({"tls_established": True, "authentication_complete": True,
                          "head_unit_verified": args.ca is not None,
                          "service_discovery_complete": True, "video_tested": False,
                          "channels": channels}, indent=2), flush=True)
    return 0
  except AuthenticationRejected as error:
    print(json.dumps({"tls_established": True, "authentication_complete": False, "video_tested": False, "error": str(error)}, indent=2))
    print(f"DHU certificate diagnostics: {args.output / 'dhu.log'}", file=sys.stderr)
    return 2
  except (OSError, EOFError, ValueError) as error:
    print(f"Experiment failed: {error}; see {args.output}", file=sys.stderr)
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
