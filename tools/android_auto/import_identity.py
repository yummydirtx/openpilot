"""Import the phone TLS identity from the verified Android Auto 17.6 test APK.

Development-time tool only; requires jadx, Android SDK apksigner, and cryptography.
No APK, decompiled source, certificate, or key is distributed with this tool.
Method reference: https://github.com/tomasz-grobelny/AACS/issues/15
The layout and algorithm were checked against the APK pinned below.
"""

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


APK_SHA256 = "d9cdcf6db33056a2c586182c0b93fbd75768260426134f231848186067603078"
SIGNER_SHA256 = "1ca8dcc0bed3cbd872d2cb791200c0292ca9975768a82d676b8b424fb65b5295"
APK_VERSION = "17.6.663454-release"


def certificate_literal(source):
  matches = re.findall(r'"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----\\n"', source)
  if len(matches) != 1:
    raise ValueError("Expected exactly one certificate in each provider class")
  return json.loads(matches[0]).encode("utf-8")


def derive_aes_material(cert, root, mask):
  state = bytearray(48)

  def mix(data):
    for pos in range(len(data)):
      for i in range(48):
        b = state[i]
        # Read data[pos] inside the inner loop: data aliases state in later
        # rounds. JADX's enhanced-for output changes this bytecode behavior.
        state[i] = ((((b >> 7) | (b + b)) + 33) ^ mask[i % len(mask)] ^ data[pos]) & 255

  mix(cert)
  mix(root)
  for _ in range(7):
    mix(state)
  return bytes(state)


def recover(provider_source, security_source):
  from cryptography import x509
  from cryptography.hazmat.primitives import padding, serialization
  from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

  cert = certificate_literal(provider_source)
  root = certificate_literal(security_source)
  arrays = {}
  for name, values in re.findall(r'public static final byte\[\] (\w+) = \{(.*?)\};', security_source, re.S):
    numbers = [int(value.strip()) for value in values.split(",")]
    if not all(-128 <= value <= 127 for value in numbers):
      raise ValueError("Invalid Java byte literal")
    arrays[name] = bytes(value & 255 for value in numbers)
  if len(arrays.get("b", b"")) != 256 or len(arrays.get("c", b"")) != 1712:
    raise ValueError("Unrecognized identity provider layout")
  material = derive_aes_material(cert, root, arrays["b"])
  decryptor = Cipher(algorithms.AES(material[:32]), modes.CBC(material[32:])).decryptor()
  unpadder = padding.PKCS7(128).unpadder()
  plain = unpadder.update(decryptor.update(arrays["c"]) + decryptor.finalize()) + unpadder.finalize()
  key = serialization.load_pem_private_key(plain, password=None)
  leaf = x509.load_pem_x509_certificate(cert)
  authority = x509.load_pem_x509_certificate(root)
  leaf.verify_directly_issued_by(authority)
  now = datetime.now(UTC)
  if not all(item.not_valid_before_utc <= now <= item.not_valid_after_utc for item in (leaf, authority)):
    raise ValueError("Identity or root has expired or is not yet valid; obtain and inspect a newer APK")
  if leaf.public_key().public_numbers() != key.public_key().public_numbers():
    raise ValueError("Recovered key does not match the phone certificate")
  metadata = {"apk_version": APK_VERSION, "apk_sha256": APK_SHA256, "apk_signer_sha256": SIGNER_SHA256,
              "subject": leaf.subject.rfc4514_string(), "expires": leaf.not_valid_after_utc.isoformat(),
              "certificate_sha256": hashlib.sha256(leaf.public_bytes(serialization.Encoding.DER)).hexdigest(),
              "key_matches": True}
  pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
  return {"phone-cert.pem": cert, "root-cert.pem": root, "phone-key.pem": pem}, metadata


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--apk", type=Path, required=True, help="Base APK extracted from the 17.6.663454 XAPK")
  parser.add_argument("--apksigner", default="apksigner", help="Android SDK build-tools/apksigner path")
  parser.add_argument("--jadx", default="jadx")
  parser.add_argument("--output", type=Path, default=Path(".cache/automaxxing/imported-identity"), help="New private output directory")
  args = parser.parse_args()
  apk = args.apk.resolve(strict=True)
  if args.output.exists():
    parser.error("Output already exists; use its existing identity or choose a new directory")
  if hashlib.sha256(apk.read_bytes()).hexdigest() != APK_SHA256:
    parser.error("APK differs from the inspected build; inspect its provider code before updating this importer")
  verification = subprocess.run([args.apksigner, "verify", "--print-certs", str(apk)], check=True, capture_output=True, text=True)
  signers = re.findall(r"^Signer #\d+ certificate SHA-256 digest: ([0-9a-f]+)$", verification.stdout, re.M)
  if signers != [SIGNER_SHA256]:
    parser.error("APK does not match the trusted Google signer from the SDK emulator stub")
  with tempfile.TemporaryDirectory(prefix="automaxxing-jadx-") as temporary:
    sources = []
    for name in ("jcf", "jch"):
      destination = Path(temporary) / f"{name}.java"
      subprocess.run([args.jadx, "--no-res", "--no-replace-consts", "--log-level", "error",
                      "--single-class", f"defpackage.{name}", "--single-class-output", str(destination), str(apk)],
                     check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
      sources.append(destination.read_text())
    files, metadata = recover(*sources)
  args.output.mkdir(mode=0o700, parents=True)
  for name, content in files.items():
    with os.fdopen(os.open(args.output / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as output:
      output.write(content)
  (args.output / "provenance.json").write_text(json.dumps(metadata, indent=2) + "\n")
  print(json.dumps({**metadata, "directory": str(args.output)}, indent=2))


if __name__ == "__main__":
  main()
