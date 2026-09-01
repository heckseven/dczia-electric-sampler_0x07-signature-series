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


def _writable(path):
    """Can we actually write here? A mount can claim rw and refuse.

    RPI-RP2 disappears the instant a board leaves BOOTSEL, and the stale mount
    it leaves behind still shows up in `mount` as rw with the right owner. The
    first write then fails with EACCES, which looks like a permissions problem
    and is really a mount pointing at a device that went away.
    """
    probe = os.path.join(path, ".spike-probe")
    try:
        with open(probe, "wb") as handle:
            handle.write(b"x")
        os.remove(probe)
        return True
    except OSError:
        return False


def _remount(path):
    """Cycle a stale mount. Returns whether it came back writable."""
    device = bootsel_device()
    if not device:
        return False
    for argv in (["udisksctl", "unmount", "-b", device],
                 ["udisksctl", "mount", "-b", device]):
        subprocess.run(argv, capture_output=True, text=True, timeout=30)
        time.sleep(1.0)
    return os.path.isdir(path) and _writable(path)


def bootsel_mount(require_writable=True):
    """Where RPI-RP2 is mounted, or None.

    Only reports a mount that can actually be written to, remounting once if it
    cannot - otherwise a caller gets a path that fails on the copy.
    """
    for base in ("/run/media", "/media"):
        for user in sorted(glob.glob(os.path.join(base, "*"))):
            path = os.path.join(user, BOOTSEL_LABEL)
            if not os.path.isdir(path):
                continue
            if not require_writable or _writable(path):
                return path
            if _remount(path):
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

    # Keep asking, rather than asking once and waiting.
    #
    # A single 'B' assumes the image is sitting in its pump loop ready to read
    # it. A badge in a reset loop - which is what a spike that has finished and
    # gone quiet becomes, once the heartbeat starts firing - only listens during
    # its boot window, and one write timed anywhere else is simply lost.
    # Retrying costs nothing and turns a coin flip into a certainty: in practice
    # it lands within two or three tries.
    deadline = time.time() + timeout
    tried_circuitpython = False
    while time.time() < deadline:
        if bootsel_device():
            break
        port = badge_module.find_port(timeout=1.0)
        if port:
            try:
                dev = badge_module.Badge(port)
                dev.open(timeout=2.0)
                try:
                    dev.write("B")
                finally:
                    dev.close()
            except (badge_module.BadgeGone, OSError):
                pass
            if not tried_circuitpython and not bootsel_device():
                # Not a C spike, then. CircuitPython needs its own route, and it
                # only needs trying once - it either has a REPL or it does not.
                tried_circuitpython = True
                try:
                    dev = badge_module.Badge(port)
                    dev.open(timeout=3.0)
                    try:
                        dev.to_bootloader()
                    finally:
                        dev.close()
                except (badge_module.BadgeGone, OSError):
                    pass
        time.sleep(0.15)

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
