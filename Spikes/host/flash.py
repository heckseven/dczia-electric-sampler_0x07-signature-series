"""Getting images on and off the badge without anyone touching it.

The RP2040's bootloader appears as a mass-storage volume called RPI-RP2, and
flashing is copying a .uf2 onto it. Getting there is the interesting part,
because it differs by what is currently running:

  - CircuitPython: `microcontroller.on_next_reset(RunMode.BOOTLOADER)`.
  - A Spikes C image: send 'B', which calls `reset_usb_boot()`.
  - Neither, because the image is wedged: hold BOOTSEL while replugging. This
    is the one hands-on step the campaign cannot design away, and every other
    path here exists to make it rare.

`picotool` is deliberately not used. It needs libusb development headers, which
would need root to install, and every C spike already offers the reset over CDC
- so the only thing picotool would add is a dependency.

Where the flash regions are, because it decides what a flash destroys:

    0x10000000  firmware
    ...         CIRCUITPY filesystem, at the TOP of the 2 MB

A 56 KB spike lands at the bottom and does not reach the filesystem, so samples,
songs and settings survive a C session. That is worth knowing before flashing
over a badge somebody uses.
"""

import glob
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import badge as badge_module  # noqa: E402

BOOTSEL_LABEL = "RPI-RP2"
BOOTSEL_TIMEOUT = 30.0


def bootsel_device():
    """The block device of a board sitting in BOOTSEL, or None."""
    result = subprocess.run(
        ["lsblk", "-o", "NAME,LABEL", "-nr"], capture_output=True, text=True
    )
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == BOOTSEL_LABEL:
            return "/dev/" + parts[0]
    return None


def bootsel_mount():
    """Where RPI-RP2 is mounted, or None."""
    for base in ("/run/media", "/media"):
        for user in sorted(glob.glob(os.path.join(base, "*"))):
            path = os.path.join(user, BOOTSEL_LABEL)
            if os.path.isdir(path):
                return path
    return None


def wait_for_bootsel(timeout=BOOTSEL_TIMEOUT):
    """Wait for RPI-RP2, mounting it if udisks does not."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        path = bootsel_mount()
        if path:
            return path
        device = bootsel_device()
        if device:
            subprocess.run(
                ["udisksctl", "mount", "-b", device],
                capture_output=True,
                text=True,
            )
        time.sleep(0.5)
    return None


def enter_bootsel(timeout=BOOTSEL_TIMEOUT):
    """Put whatever is running into BOOTSEL. Returns the mount, or None.

    Tries the C route first and CircuitPython second, because a C spike sitting
    in its pump loop answers immediately while CircuitPython needs interrupting.
    Either way this is best effort - a wedged image answers to neither, and the
    caller has to say so rather than hang.
    """
    path = bootsel_mount()
    if path:
        return path

    port = badge_module.find_port(timeout=3.0)
    if port:
        dev = badge_module.Badge(port)
        try:
            dev.open(timeout=5.0)
            # The C spikes' pump loop takes 'B' as "go to BOOTSEL".
            dev.write("B")
            time.sleep(1.5)
            if bootsel_device() is None:
                # Not a C spike, then. Try CircuitPython's route.
                dev.to_bootloader()
        except badge_module.BadgeGone:
            pass
        finally:
            dev.close()

    return wait_for_bootsel(timeout)


def flash(uf2_path, timeout=BOOTSEL_TIMEOUT):
    """Copy a .uf2 onto a board in BOOTSEL. Returns whether it was written.

    The board reboots as soon as the copy completes, so the volume disappearing
    mid-write is success rather than an error - which is why the usual "did the
    file land" check cannot be used here.
    """
    if not os.path.isfile(uf2_path):
        raise SystemExit("no such image: %s" % uf2_path)

    mount = enter_bootsel(timeout)
    if not mount:
        raise SystemExit(
            "could not reach BOOTSEL. If the badge is wedged, hold BOOTSEL "
            "while replugging it - that is the one step this cannot do."
        )

    target = os.path.join(mount, os.path.basename(uf2_path))
    try:
        shutil.copyfile(uf2_path, target)
        os.sync()
    except OSError:
        # The board resets the instant it has the whole image, so the tail of
        # the copy often fails with EIO or ENODEV. That is the success case.
        pass
    return True


def main():
    if len(sys.argv) != 2:
        raise SystemExit(
            "usage: flash.py <image.uf2>\n"
            "   or: flash.py --restore   (put CircuitPython back)"
        )

    argument = sys.argv[1]
    if argument == "--restore":
        here = os.path.dirname(os.path.abspath(__file__))
        image = os.path.join(
            os.path.dirname(os.path.dirname(here)), "Firmware", "DCZiaSampler.uf2"
        )
    else:
        image = argument

    print("flashing %s" % image)
    flash(image)
    print("copied; waiting for the board to come back")

    port = badge_module.find_port(timeout=BOOTSEL_TIMEOUT)
    print("serial: %s" % (port or "did not reappear"))
    return 0 if port else 1


if __name__ == "__main__":
    sys.exit(main())
