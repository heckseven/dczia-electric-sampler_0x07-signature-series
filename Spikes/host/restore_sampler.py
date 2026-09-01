"""Put the badge back to being a sampler: CircuitPython 10.2.1, then the build.

Three steps, and the middle one is the one that is easy to forget. `build.py`
only ever copies, so anything already on CIRCUITPY that the build does not
overwrite survives and shadows it - which is how restoring the archived
`Firmware/DCZiaSampler.uf2` left stock DCZia modules sitting in front of the
rework.

The runtime image is untracked. See docs/rewrite-phase0-plan.md for the URL and
sha256 if it is not there.
"""

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import badge as badge_module  # noqa: E402
import flash as flash_module  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
RUNTIME = os.path.join(ROOT, "Firmware", "circuitpython-10.2.1-pico_w.uf2")

# Everything else on the volume is either the previous deploy or somebody's
# stale firmware, and both shadow what is about to be copied on.
#
# `sd` is in here because deleting it silently disables the SD card. CircuitPython's
# storage.mount() needs the mount point to exist as a real directory in the root
# filesystem, and nothing recreates it - not build.py, not setup.py. An earlier
# version of this script swept it away, and the badge then booted from flash
# samples with `setup.py` reporting "No SD Card Found!" while the card itself
# read perfectly. It cost a Task 6 detour to find.
KEEP = {"boot_out.txt", "System Volume Information", "sd"}

# Directories the firmware needs to exist but never creates.
REQUIRED_DIRS = ("sd",)


def clear(mount):
    """Remove everything the build does not own, so nothing shadows it."""
    for name in os.listdir(mount):
        if name in KEEP:
            continue
        subprocess.run(["rm", "-rf", os.path.join(mount, name)], check=False)
    for name in REQUIRED_DIRS:
        path = os.path.join(mount, name)
        if not os.path.isdir(path):
            os.makedirs(path, exist_ok=True)
            print("recreated missing mount point /%s" % name)


def main():
    if not os.path.isfile(RUNTIME):
        raise SystemExit(
            "missing %s - see docs/rewrite-phase0-plan.md for the URL and "
            "checksum" % RUNTIME
        )

    print("flashing CircuitPython 10.2.1")
    flash_module.flash(RUNTIME)
    time.sleep(5)

    mount = badge_module.ensure_circuitpy(timeout=90.0)
    if not mount:
        raise SystemExit("CIRCUITPY did not appear")
    badge_module.ensure_circuitpy_writable()
    print("CIRCUITPY at %s" % mount)

    clear(mount)

    argv = [sys.executable, os.path.join(ROOT, "Tools", "build.py"), "-o", mount]
    mpy = os.path.join(ROOT, "mpy-cross")
    if os.path.exists(mpy):
        argv += ["--mpy-cross", mpy]
    if subprocess.run(argv, cwd=ROOT).returncode != 0:
        raise SystemExit("build.py failed")

    subprocess.run(["sync"], check=False)
    print("restored")
    return 0


if __name__ == "__main__":
    sys.exit(main())
