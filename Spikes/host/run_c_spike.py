"""Flash a C spike, capture its records, and hand the badge back.

The C spikes talk first and answer little: they print RESULT lines as they go
and then sit in a pump loop. So this is deliberately dumber than
`run_baseline.py`, which has to drive a REPL - here the only interaction is
reading until DONE, and then sending 'B' so the next thing to run can be
CircuitPython.

Usage:  run_c_spike.py <name>          # builds nothing; flashes build/<name>.uf2
        run_c_spike.py <name> --stay   # leave it in C, do not return to BOOTSEL
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import badge as badge_module  # noqa: E402
import flash as flash_module  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.join(os.path.dirname(HERE), "c", "build")

# Generous: a spike that measures 200-block mixes at 24 voices is still only
# milliseconds of work, but the boot window alone is 3 seconds.
DONE_TIMEOUT = 90.0


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    name = sys.argv[1]
    stay = "--stay" in sys.argv

    image = os.path.join(BUILD, name + ".uf2")
    if not os.path.isfile(image):
        raise SystemExit("no image: %s (build it first)" % image)

    print("flashing %s" % os.path.basename(image))
    flash_module.flash(image)

    port = badge_module.find_port(timeout=flash_module.BOOTSEL_TIMEOUT)
    if not port:
        raise SystemExit("badge did not reappear after flashing")

    dev = badge_module.Badge(port)
    dev.open(timeout=20.0)
    try:
        text, matched = dev.read_until("DONE", timeout=DONE_TIMEOUT)
        print(text)
        if not matched:
            print("!! no DONE within %.0fs" % DONE_TIMEOUT)
        if not stay:
            # Straight back to BOOTSEL, so the badge is never left in C.
            dev.write("B")
            time.sleep(1.0)
        return 0 if matched else 1
    finally:
        dev.close()


if __name__ == "__main__":
    sys.exit(main())
