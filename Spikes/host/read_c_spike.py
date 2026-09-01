"""Read a freshly booted C spike's records. Assumes it is already flashed.

Split out from run_c_spike.py because recovery is not the same job as a run:
after a manual BOOTSEL the image is already on the badge, and all that is left
is to catch what it says on the way up.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import badge as badge_module  # noqa: E402


def main():
    port = badge_module.find_port(timeout=45.0)
    if not port:
        raise SystemExit("badge did not enumerate")
    dev = badge_module.Badge(port)
    dev.open(timeout=20.0)
    try:
        text, matched = dev.read_until("DONE", timeout=90.0)
        print(text)
        if not matched:
            print("!! no DONE")
        return 0 if matched else 1
    finally:
        dev.close()


if __name__ == "__main__":
    sys.exit(main())
