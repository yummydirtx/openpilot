"""Build the standalone projection encoder on the C4 without rebuilding openpilot."""

import argparse
from pathlib import Path
import platform
import subprocess


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--output", type=Path, default=Path(__file__).with_name("libaa_encoder.so"))
  args = parser.parse_args()
  if platform.machine() != "aarch64" or not Path("/AGNOS").exists():
    parser.error("Build on an AGNOS device with Qualcomm V4L2/ION headers")
  target = args.output.resolve()
  temporary = target.with_suffix(".building.so")
  test_binary = target.with_suffix(".conversion-test")
  try:
    subprocess.run(["clang++", "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror",
                    str(Path(__file__).with_name("test_rgba_to_nv12.cc")), "-o", str(test_binary)], check=True)
    subprocess.run([str(test_binary)], check=True, timeout=10)
    subprocess.run(["clang++", "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror", "-fPIC", "-shared",
                    str(Path(__file__).with_name("hardware_encoder.cc")), "-o", str(temporary)], check=True)
    temporary.replace(target)
  finally:
    temporary.unlink(missing_ok=True)
    test_binary.unlink(missing_ok=True)
  print(target)


if __name__ == "__main__":
  main()
