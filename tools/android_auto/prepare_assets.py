"""Cache five native UI assets, verifying the checkout's Git LFS object hashes."""

import hashlib
import json
from pathlib import Path
import subprocess
import urllib.request

REPO = Path(__file__).resolve().parents[2]
SOURCE = REPO / "openpilot/selfdrive/assets"
OUTPUT = REPO / ".cache/automaxxing/ui-assets"
NAMES = ["fonts/Inter-Bold.ttf", "fonts/Inter-Medium.ttf", "fonts/Inter-SemiBold.ttf", "icons/chffr_wheel.png", "icons/driver_face.png"]


def main():
  pending = {}
  for name in NAMES:
    content = (SOURCE / name).read_bytes()
    target = OUTPUT / name
    target.parent.mkdir(parents=True, exist_ok=True)
    if not content.startswith(b"version https://git-lfs.github.com/spec/v1\n"):
      target.write_bytes(content)
      continue
    lines = content.decode().splitlines()
    oid, size = lines[1].split(":", 1)[1], int(lines[2].split()[1])
    if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == oid:
      continue
    pending[oid] = (name, size)
  if pending:
    remote = subprocess.check_output(["git", "config", "-f", str(REPO / ".lfsconfig"), "lfs.url"], text=True).strip()
    if not remote.startswith("https://"):
      raise ValueError("This development helper requires an HTTPS LFS endpoint")
    body = {"operation": "download", "transfers": ["basic"], "objects": [{"oid": oid, "size": size} for oid, (_, size) in pending.items()]}
    request = urllib.request.Request(remote.rstrip("/") + "/objects/batch", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/vnd.git-lfs+json", "Accept": "application/vnd.git-lfs+json"})
    with urllib.request.urlopen(request, timeout=30) as response:
      objects = json.load(response)["objects"]
    for obj in objects:
      name, size = pending[obj["oid"]]
      action = obj.get("actions", {}).get("download")
      if action is None or not action["href"].startswith("https://"):
        raise ValueError(f"No HTTPS download available for {name}")
      request = urllib.request.Request(action["href"], headers=action.get("header", {}))
      with urllib.request.urlopen(request, timeout=30) as response:
        content = response.read(size + 1)
      if len(content) != size or hashlib.sha256(content).hexdigest() != obj["oid"]:
        raise ValueError(f"LFS integrity check failed for {name}")
      (OUTPUT / name).write_bytes(content)
      print(f"Verified {name} ({size} bytes)")
    if any(not (OUTPUT / name).exists() for name, _ in pending.values()):
      raise ValueError("LFS response omitted a requested asset")
  print(OUTPUT)


if __name__ == "__main__":
  main()
