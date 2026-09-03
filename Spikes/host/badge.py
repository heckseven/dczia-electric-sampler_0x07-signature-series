"""Driving the badge from the host, including when it stops answering.

Every measurement in Phase 0 is taken by firmware that measures itself and
prints over USB serial. That only works unattended if the host can also get the
badge back when a spike wedges it, so most of this file is recovery rather than
communication.

The failure modes here are not hypothetical. Each one cost time during the work
that led to this campaign:

  - CIRCUITPY comes up write-protected, and no remount fixes it. The kernel
    caches the SCSI write-protect bit at device attach, so `udisksctl unmount`
    and `mount` change nothing - the cure is making the badge re-enumerate.
  - The serial port disappears mid-command whenever the badge resets, and
    reappears as a different device a second or two later.
  - The REPL mangles a pasted `for` loop sent line by line. Paste mode - ctrl-E,
    the lines, ctrl-D - is the only reliable way to send a block.
  - A sentinel that also appears in the echoed source ends the read early, which
    is a good way to believe a twenty-second test hung.
  - Arming the watchdog from the REPL resets the badge a couple of seconds
    later, because nothing is feeding it.

None of this is exotic; it is what a USB device does. It is written down here so
each spike does not rediscover it.
"""

import glob
import os
import re
import subprocess
import time

import serial

PORT_GLOB = "/dev/ttyACM*"
BAUD = 115200

# How long to wait for the port to come back after a reset. Measured on this
# badge: re-enumeration takes about three seconds, and the volume mounts a
# little after that. Twenty-five is not tight; it is the difference between a
# flaky campaign and one that finishes.
ENUMERATE_TIMEOUT = 25.0

# How long a spike may go silent before the host gives up on it. Without this a
# wedged badge hangs the whole campaign rather than costing one case.
CASE_TIMEOUT = 120.0


class BadgeGone(Exception):
    """The port went away and did not come back inside the timeout."""


# Which board is ours.
#
# A second RP2040 on the same machine is not hypothetical - a DCZia MK9 badge
# from another project was plugged in alongside the sampler, took /dev/ttyACM0,
# and every tool here aimed at it. Nothing was flashed, because its MicroPython
# REPL answered the CircuitPython bootloader commands with a NameError instead
# of rebooting - luck, not design.
#
# So boards are chosen by identity, never by port order. The RP2040's flash UID
# is stable across firmware, which makes it the one handle that survives
# CircuitPython, a C image and BOOTSEL alike.
TARGET_UID = os.environ.get("BADGE_UID", "E661410403330F22")

# Anything whose by-id string matches these is known to belong to somebody else.
FOREIGN = ("MK9",)

BY_ID = "/dev/serial/by-id"


def _candidates():
    """(device, identity) for every USB serial port, most-preferred first."""
    found = []
    if os.path.isdir(BY_ID):
        for name in sorted(os.listdir(BY_ID)):
            device = os.path.realpath(os.path.join(BY_ID, name))
            found.append((device, name))
    return found


def target_port():
    """The sampler's serial port, or None.

    Refuses to fall back to "the only ACM device" when a foreign board is
    present: guessing is how the wrong badge gets flashed.
    """
    candidates = _candidates()
    if not candidates:
        return None

    for device, name in candidates:
        if TARGET_UID and TARGET_UID in name:
            return device

    ours = [(d, n) for d, n in candidates
            if not any(f in n for f in FOREIGN)]
    if len(ours) == 1:
        return ours[0][0]
    if not ours:
        return None
    # More than one plausible board and no UID match. Say so rather than pick.
    raise BadgeGone(
        "cannot tell which board is the sampler - candidates: %s. "
        "Set BADGE_UID to the right one." % ", ".join(n for _, n in ours)
    )


def foreign_boards():
    """Identities of attached boards that are definitely not ours."""
    return [n for _, n in _candidates() if any(f in n for f in FOREIGN)]


def find_port(timeout=ENUMERATE_TIMEOUT):
    """The sampler's port, waiting for it to appear.

    Identity first: falls back to the old scan only when by-id has nothing to
    say, which is the case on a machine with exactly one board.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = target_port()
        if found:
            return found
        time.sleep(0.2)

    # No blind fallback while somebody else's board is attached.
    #
    # The sampler's by-id entry disappears for a second or two whenever it
    # resets, and a scan during that window returns whatever else is plugged in.
    # That is not theoretical: it handed a MicroPython REPL on a DCZia MK9 back
    # twice, and the second time it was mid-way through writing a file.
    if foreign_boards():
        return None
    return _find_port_by_scan(timeout=0.5)


def _find_port_by_scan(timeout=ENUMERATE_TIMEOUT):
    """The badge's serial port, waiting for it to appear. None on timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        ports = sorted(glob.glob(PORT_GLOB))
        if ports:
            return ports[0]
        time.sleep(0.25)
    return None


def wait_for_port_to_go(port, timeout=10.0):
    """Wait for a port to disappear, which is how a reset announces itself.

    Returns whether it went. A reset that is too fast to observe is not an
    error - the caller waits for it to come back either way.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not os.path.exists(port):
            return True
        time.sleep(0.05)
    return False


class Badge:
    """A serial connection that survives the badge restarting under it."""

    def __init__(self, port=None, baud=BAUD):
        self._port = port
        self._baud = baud
        self._serial = None

    # --- connection -------------------------------------------------------

    def open(self, timeout=ENUMERATE_TIMEOUT):
        """Connect, waiting for the port if it is not there yet."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            port = self._port or find_port(timeout=1.0)
            if port:
                try:
                    self._serial = serial.Serial(port, self._baud, timeout=0.3)
                    self._port = port
                    return self
                except (OSError, serial.SerialException):
                    # Present in /dev but not yet ready to open, which is
                    # normal for a second or so after enumeration.
                    pass
            time.sleep(0.25)
        raise BadgeGone("no serial port after %.0fs" % timeout)

    def close(self):
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *_):
        self.close()

    # --- reading ----------------------------------------------------------

    def read_available(self):
        """Whatever is waiting, or "" - never raises when the badge vanishes.

        A disappearing port is the normal consequence of a reset, so it is a
        return value here rather than an exception.
        """
        if self._serial is None:
            return ""
        try:
            waiting = self._serial.in_waiting
        except (OSError, serial.SerialException):
            self.close()
            return ""
        if not waiting:
            return ""
        try:
            return self._serial.read(waiting).decode("utf-8", "replace")
        except (OSError, serial.SerialException):
            self.close()
            return ""

    def read_until(self, pattern, timeout=CASE_TIMEOUT, settle=0.6):
        """Read until `pattern` matches, then a moment longer.

        `pattern` is a regex, and it must not be something that also appears in
        the echo of what was sent - matching the echo rather than the answer is
        how a long test looks like a hang.

        The settle is because a match often lands mid-line: the badge is still
        printing the rest of the record when the regex fires.
        """
        matcher = re.compile(pattern)
        out = ""
        deadline = time.time() + timeout
        while time.time() < deadline:
            chunk = self.read_available()
            if chunk:
                out += chunk
                if matcher.search(out):
                    end = time.time() + settle
                    while time.time() < end:
                        more = self.read_available()
                        if more:
                            out += more
                            end = time.time() + settle
                        else:
                            time.sleep(0.03)
                    return out, True
            else:
                time.sleep(0.05)
        return out, False

    def drain(self, seconds=0.5):
        """Read whatever arrives for a fixed time. For banners and settling."""
        out = ""
        end = time.time() + seconds
        while time.time() < end:
            chunk = self.read_available()
            if chunk:
                out += chunk
                end = time.time() + seconds
            else:
                time.sleep(0.03)
        return out

    # --- writing ----------------------------------------------------------

    def write(self, data):
        if self._serial is None:
            raise BadgeGone("not connected")
        if isinstance(data, str):
            data = data.encode()
        try:
            self._serial.write(data)
        except (OSError, serial.SerialException):
            self.close()
            raise BadgeGone("port went away mid-write")

    def interrupt(self):
        """Ctrl-C twice, which stops a running program and lands in the REPL.

        Twice because the first can be consumed by whatever the program was
        doing, and CircuitPython only acts on it at a bytecode boundary.
        """
        self.write(b"\x03")
        time.sleep(0.3)
        self.write(b"\x03")
        time.sleep(1.0)
        self.drain(0.4)

    def repl(self, attempts=4, timeout=8.0):
        """Get to a REPL prompt and prove it, rather than assuming.

        An interrupt is not enough on its own. Writing any file to CIRCUITPY
        triggers CircuitPython's auto-reload, so deploying a spike restarts
        main.py - and a reload landing just after the interrupt leaves the host
        talking to a running program that ignores it. The symptom is a spike
        that produces no records at all, which reads like the spike being
        broken rather than never having been started.

        So: interrupt, then prove it by asking for a token back. Repeat, because
        the reload may still be in flight on the first attempt.
        """
        token = "REPLOK"
        for _ in range(attempts):
            self.interrupt()
            self.write("print('%s')\r\n" % token)
            out, matched = self.read_until(token + r"\s*\r?\n", timeout=timeout)
            if matched:
                return True
            # Auto-reload may still be restarting the program under us.
            time.sleep(1.5)
        return False

    def line(self, code, timeout=8.0, marker=None):
        """One statement. Returns (text, matched).

        Always a pair, whether or not a marker was given - a call that returns
        a string sometimes and a tuple other times is a bug waiting for the one
        caller who forgets which.  With no marker, `matched` is True: there was
        nothing to wait for.

        Multi-line code does not work this way - the REPL joins the lines and
        produces a syntax error. Use `paste`.
        """
        self.write(code + "\r\n")
        time.sleep(0.15)
        if marker:
            return self.read_until(marker, timeout=timeout)
        return self.drain(min(timeout, 2.0)), True

    def paste(self, code, marker, timeout=CASE_TIMEOUT):
        """A block of code via paste mode. Returns (output, matched).

        `marker` is a regex the badge prints when the block is done. Choose one
        that cannot appear in the echoed source: `RESULT case=x` matches the
        echo of `print("RESULT case=x")` too, whereas `RESULT case=x .*ok=`
        does not.
        """
        self.write(b"\x05")  # ctrl-E: paste mode
        time.sleep(0.3)
        self.drain(0.3)
        for line in code.strip("\n").split("\n"):
            self.write(line + "\r\n")
            time.sleep(0.02)
        self.write(b"\x04")  # ctrl-D: run it
        return self.read_until(marker, timeout=timeout)

    # --- getting it back --------------------------------------------------

    def reset(self, wait=True):
        """Hard reset, and wait for the port to come back.

        A hard reset rather than ctrl-D: only re-enumeration clears a stale USB
        write-protect on CIRCUITPY, and only a fresh boot re-runs setup.py.
        """
        port = self._port
        try:
            self.interrupt()
            self.write("import microcontroller\r\n")
            time.sleep(0.2)
            self.write("microcontroller.reset()\r\n")
        except BadgeGone:
            pass
        self.close()
        if not wait:
            return
        if port:
            wait_for_port_to_go(port, timeout=8.0)
        time.sleep(1.0)
        self._port = None
        self.open()

    def to_bootloader(self):
        """Leave CircuitPython for BOOTSEL, so a C image can be flashed.

        This is what makes the CircuitPython-to-C transition host-driven, and
        so what makes "nobody touches the badge" true rather than aspirational.
        The port does not come back - the board enumerates as RPI-RP2 mass
        storage instead - so this closes the connection and does not reopen it.
        """
        port = self._port
        try:
            self.interrupt()
            self.write("import microcontroller\r\n")
            time.sleep(0.2)
            self.write(
                "microcontroller.on_next_reset("
                "microcontroller.RunMode.BOOTLOADER)\r\n"
            )
            time.sleep(0.3)
            self.write("microcontroller.reset()\r\n")
        except BadgeGone:
            pass
        self.close()
        if port:
            wait_for_port_to_go(port, timeout=10.0)


def circuitpy_mount():
    """Where CIRCUITPY is mounted, or None."""
    for base in ("/run/media", "/media"):
        for user in sorted(glob.glob(os.path.join(base, "*"))):
            path = os.path.join(user, "CIRCUITPY")
            if os.path.isdir(path):
                return path
    return None


def circuitpy_device():
    """The block device holding CIRCUITPY, or None.

    Found by label rather than by device node: the badge is not always /dev/sda,
    and after a reset it may not be the node it was a minute ago.
    """
    result = subprocess.run(
        ["lsblk", "-o", "NAME,LABEL", "-nr"],
        capture_output=True,
        text=True,
    )
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "CIRCUITPY":
            return "/dev/" + parts[0]
    return None


def ensure_circuitpy(timeout=ENUMERATE_TIMEOUT):
    """Where CIRCUITPY is mounted, mounting it first if it is not.

    Resetting the badge repeatedly leaves the volume present but unmounted -
    udisks auto-mounts on attach, and an attach it decides is a repeat of one
    it has already seen does not always get one. The campaign resets the badge
    for every spike, so this happens often enough that failing with "CIRCUITPY
    is not mounted" would stop a run that has nothing wrong with it.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        path = circuitpy_mount()
        if path:
            return path
        device = circuitpy_device()
        if device:
            subprocess.run(
                ["udisksctl", "mount", "-b", device],
                capture_output=True,
                text=True,
            )
            # Whether that succeeded or reported it was already mounted, the
            # answer is the same question asked again.
            path = circuitpy_mount()
            if path:
                return path
        time.sleep(1.0)
    return None


def ensure_circuitpy_writable(attempts=3):
    """A mounted, writable CIRCUITPY - resetting the badge if that is what it takes.

    The volume can come up write-protected at the block layer, and no remount
    fixes it: the kernel caches the SCSI write-protect bit at device attach, so
    `udisksctl unmount` and `mount` change nothing. The only cure is making the
    badge re-enumerate.

    It happens often enough during a campaign that resets the badge for every
    spike that failing with "permission denied" would stop runs which have
    nothing wrong with them.
    """
    for attempt in range(attempts):
        path = ensure_circuitpy()
        if path and circuitpy_writable(path):
            return path
        if attempt + 1 == attempts:
            break
        # Re-enumerate. Nothing softer than this clears the cached bit.
        try:
            dev = Badge()
            dev.open(timeout=ENUMERATE_TIMEOUT)
            dev.reset()
            dev.close()
        except BadgeGone:
            pass
        time.sleep(3.0)
    return None


def circuitpy_writable(path=None):
    """Whether CIRCUITPY can actually be written to.

    Not the same question as whether it is mounted. The badge can come up with
    the volume write-protected at the block layer while the badge itself also
    cannot write it - both sides believing the other owns it - and nothing
    short of re-enumeration resolves that.
    """
    path = path or circuitpy_mount()
    if not path:
        return False
    probe = os.path.join(path, ".writable-probe")
    try:
        with open(probe, "w") as handle:
            handle.write("x")
        os.remove(probe)
        return True
    except OSError:
        return False
