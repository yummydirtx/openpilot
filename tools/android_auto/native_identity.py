"""Run native widgets under the same Unix identity as the device's own UI."""

import os
import pwd
from pathlib import Path


def native_identity(output=None):
  if os.geteuid() != 0:
    return
  user = pwd.getpwnam("comma")
  if output is not None:
    path = Path(output)
    if path.is_symlink():
      raise ValueError("Native diagnostic directory must not be a symlink")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chown(path, user.pw_uid, user.pw_gid)
  os.initgroups(user.pw_name, user.pw_gid)
  os.setgid(user.pw_gid)
  os.setuid(user.pw_uid)
  os.environ["HOME"] = user.pw_dir
  os.environ["USER"] = os.environ["LOGNAME"] = user.pw_name
  os.umask(0o022)
