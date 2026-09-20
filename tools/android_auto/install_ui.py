"""Explicit, hash-guarded install/rollback of the native four display adapter.

Bundle on the development machine; install on the parked comma. Installation
does not restart processes. Reboot normally while offroad to load all changes.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

LEGACY_FILES = ("openpilot/system/ui/lib/application.py", "openpilot/system/ui/lib/display_handoff.py",
         "openpilot/selfdrive/ui/ui.py", "openpilot/selfdrive/ui/ui_state.py",
         "openpilot/selfdrive/ui/mici/layouts/home.py", "openpilot/selfdrive/ui/automaxxing.py",
         "openpilot/selfdrive/ui/sunnypilot/mici/layouts/settings.py", "openpilot/selfdrive/ui/layouts/main.py")
FILES = (*LEGACY_FILES, "openpilot/system/ui/lib/driver_preview.py", "openpilot/selfdrive/ui/layouts/settings/device.py",
         "openpilot/selfdrive/ui/sunnypilot/layouts/settings/device.py", "openpilot/system/hardware/driver_view.py",
         "openpilot/system/hardware/hardwared.py", "openpilot/system/manager/process_config.py")
SERVICE = Path("/run/systemd/system/automaxxing-display.service")
CHECKOUT = Path("/data/openpilot")
BACKUP = Path("/data/automaxxing/native-ui-backup")
ENABLED = Path("/data/automaxxing/native-ui-enabled")


def start_control():
  # AGNOS /etc is read-only. Recreate this runtime unit on an explicit UI start
  # after boot; no boot scripts, manager process list, or sudo policy changes.
  SERVICE.parent.mkdir(parents=True, exist_ok=True)
  shutil.copyfile(Path(__file__).with_name(SERVICE.name), SERVICE)
  SERVICE.chmod(0o644)
  subprocess.run(["systemctl", "daemon-reload"], check=True)
  subprocess.run(["systemctl", "start", SERVICE.name], check=True)


def digest(path):
  return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def verify_parked():
  import time
  from openpilot.cereal import messaging
  sm = messaging.SubMaster(["carState", "deviceState"])
  end = time.monotonic() + 2
  while time.monotonic() < end:
    sm.update(100)
  offroad = sm.alive["deviceState"] and sm.valid["deviceState"] and not sm["deviceState"].started
  parked = (sm.alive["carState"] and sm.valid["carState"] and sm["carState"].canValid
            and abs(sm["carState"].vEgo) < 0.01 and str(sm["carState"].gearShifter) == "park")
  if not (offroad or parked):
    raise RuntimeError("Native UI installation requires verified offroad or parked state")


def bundle(destination, base="c072c7a"):
  subprocess.run(["git", "rev-parse", "--verify", f"{base}^{{commit}}"], check=True, capture_output=True)
  destination.mkdir(parents=True, exist_ok=False)
  manifest = {}
  for relative in FILES:
    path = Path(relative)
    original = subprocess.run(["git", "show", f"{base}:{relative}"], capture_output=True)
    target = destination / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, target)
    manifest[relative] = {"before": hashlib.sha256(original.stdout).hexdigest() if original.returncode == 0 else None,
                          "after": digest(path)}
  (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


def install(source):
  manifest = json.loads((source / "manifest.json").read_text())
  if set(manifest) != set(FILES):
    raise ValueError("Unexpected UI bundle paths")
  for relative, hashes in manifest.items():
    if digest(source / relative) != hashes["after"] or digest(CHECKOUT / relative) != hashes["before"]:
      raise RuntimeError(f"UI changed or bundle damaged: {relative}")
    compile((source / relative).read_text(), relative, "exec")
  if BACKUP.exists() or SERVICE.exists():
    raise RuntimeError("Existing installation found; rollback before reinstalling")
  verify_parked()
  BACKUP.mkdir(mode=0o700)
  # Record original contents completely before mutating any native file.
  for relative, hashes in manifest.items():
    if hashes["before"] is not None:
      target = BACKUP / relative
      target.parent.mkdir(parents=True, exist_ok=True)
      shutil.copy2(CHECKOUT / relative, target)
  (BACKUP / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
  try:
    for relative in FILES:
      target = CHECKOUT / relative
      temporary = target.with_suffix(".automaxxing.tmp")
      shutil.copyfile(source / relative, temporary)
      temporary.chmod(0o644)
      temporary.replace(target)
    start_control()
    ENABLED.touch(mode=0o644)
  except BaseException:
    rollback(check_parked=False)
    raise


def rollback(*, check_parked=True):
  manifest = json.loads((BACKUP / "manifest.json").read_text())
  if set(manifest) not in (set(FILES), set(LEGACY_FILES)):
    raise ValueError("Unexpected backup paths")
  for relative, hashes in manifest.items():
    if digest(CHECKOUT / relative) not in (hashes["before"], hashes["after"]):
      raise RuntimeError(f"Refusing to overwrite a later native edit: {relative}")
    if hashes["before"] is not None and digest(BACKUP / relative) != hashes["before"]:
      raise RuntimeError(f"Damaged native backup: {relative}")
  if check_parked:
    verify_parked()
  subprocess.run(["systemctl", "disable", "--now", SERVICE.name], check=False)
  subprocess.run(["systemctl", "stop", "automaxxing.service"], check=False)
  for relative, hashes in manifest.items():
    target = CHECKOUT / relative
    if hashes["before"] is None:
      target.unlink(missing_ok=True)
    else:
      temporary = target.with_suffix(".automaxxing.tmp")
      shutil.copy2(BACKUP / relative, temporary)
      temporary.replace(target)
  SERVICE.unlink(missing_ok=True)
  ENABLED.unlink(missing_ok=True)
  subprocess.run(["systemctl", "daemon-reload"], check=True)
  shutil.rmtree(BACKUP)


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("action", choices=("bundle", "install", "rollback", "start-control"))
  parser.add_argument("--bundle", type=Path, default=Path("native-ui"))
  parser.add_argument("--base", default="c072c7a", help="Verified native baseline before display integration")
  args = parser.parse_args()
  if args.action == "bundle":
    bundle(args.bundle, args.base)
  elif os.geteuid() != 0:
    parser.error("Install/rollback requires root on the comma")
  elif args.action == "install":
    install(args.bundle)
  elif args.action == "start-control":
    if not ENABLED.is_file():
      raise RuntimeError("Native display integration has not been installed")
    start_control()
  else:
    rollback()


if __name__ == "__main__":
  main()
