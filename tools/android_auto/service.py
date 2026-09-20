"""Start/stop the isolated Automaxxing transient service on the comma."""

import argparse
from pathlib import Path
import subprocess

ROOT = Path("/data/automaxxing")
PYTHON = "/usr/local/venv/bin/python"
UNIT = "automaxxing.service"


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("action", choices=("start", "stop", "status"))
  parser.add_argument("--duration", type=int, default=600)
  parser.add_argument("--output", default="live-run")
  parser.add_argument("--fps", type=int, choices=(8, 10, 15, 30), default=8)
  parser.add_argument("--view", choices=("road", "hud", "native"), default="road")
  parser.add_argument("--once", action="store_true")
  parser.add_argument("--managed", action="store_true", help="Use the installed local-display supervisor")
  args = parser.parse_args()
  if args.action in ("stop", "status"):
    return subprocess.call(["systemctl", args.action, "--no-pager", UNIT])
  if not 1 <= args.duration <= 7200 or Path(args.output).name != args.output or args.output in (".", ".."):
    parser.error("Use a fresh output directory name and duration 1–7200 seconds")
  if (ROOT / args.output).exists():
    parser.error("Output directory already exists")
  subprocess.run(["systemctl", "reset-failed", UNIT], check=False, capture_output=True)
  env = f"PYTHONPATH={ROOT}/deps:{ROOT}:/data/openpilot"
  command = ["systemd-run", "--unit=automaxxing", "--collect", f"--working-directory={ROOT}", f"--setenv={env}",
             "--setenv=OPENBLAS_NUM_THREADS=1", "--setenv=OMP_NUM_THREADS=1", "--setenv=MKL_NUM_THREADS=1",
             "--property=Nice=15",
             "--property=CPUAffinity=0 1 2 5 6", "--property=MemoryMax=384M",
             "--property=OOMPolicy=stop", "--property=OOMScoreAdjust=500", "--property=KillMode=control-group",
             "--property=TimeoutStopSec=5", "--property=UMask=0077", f"--property=RuntimeMaxSec={args.duration + 20}",
             f"--property=ExecStopPost={PYTHON} -m tools.android_auto.runtime --recover",
             PYTHON, "-u", "-m", "tools.android_auto.runtime", "--duration", str(args.duration),
             "--output", args.output, "--fps", str(args.fps), "--view", args.view]
  if args.once:
    command.append("--once")
  if args.managed:
    command.append("--managed")
  return subprocess.call(command)


if __name__ == "__main__":
  raise SystemExit(main())
