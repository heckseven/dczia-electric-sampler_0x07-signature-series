"""Flash and watch the Phase 1 firmware.

Separate from run_output.py only because the image lives somewhere else; the
keepalive is the same idea - any byte counts as host traffic, and '.' is not
the 'B' that would send the badge to BOOTSEL.
"""

import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import serial  # noqa: E402
import badge as badge_module  # noqa: E402
import flash as flash_module  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
IMAGE = os.path.join(HERE, "..", "..", "Rewrite", "build", "rt.uf2")


def follow(seconds):
    """Attach to the running firmware and print what it says."""
    ser, deadline = None, time.time() + 30
    while time.time() < deadline and ser is None:
        path = badge_module.target_port()
        if path:
            try:
                ser = serial.Serial(path, 115200, timeout=0.2)
            except Exception:
                ser = None
        time.sleep(0.05)
    if ser is None:
        raise SystemExit("port never appeared")

    buf, last_ping, end = b"", 0.0, time.time() + seconds
    while time.time() < end:
        try:
            buf += ser.read(4096)
        except Exception:
            break
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            text = line.decode("utf-8", "replace").strip()
            if text:
                print(text, flush=True)
        now = time.time()
        if now - last_ping > 5.0:
            try:
                ser.write(b".")
                ser.flush()
            except Exception:
                pass
            last_ping = now
    try:
        ser.close()
    except Exception:
        pass
    return 0


def main():
    args = [a for a in sys.argv[1:] if a != "--watch"]
    watch = float(args[0]) if args else 30.0

    # --watch attaches to whatever is already running. Reflashing to read the
    # log would restart the firmware, which is the opposite of what you want
    # while somebody is playing it.
    if "--watch" in sys.argv:
        return follow(watch)

    mount = flash_module.enter_bootsel(timeout=60)
    if not mount:
        raise SystemExit("could not reach BOOTSEL")
    shutil.copyfile(IMAGE, os.path.join(mount, "rt.uf2"))
    os.sync()
    print("flashed rt", flush=True)

    # By identity, not by port order. The RP2040's flash UID survives the
    # reflash - the board is usb-Raspberry_Pi_Pico_<UID> under rt where it was
    # usb-Raspberry_Pi_Pico_W_<UID> under CircuitPython - so the same handle
    # works either side of a flash, and a foreign board on ttyACM0 cannot be
    # mistaken for ours.
    ser, deadline = None, time.time() + 45
    while time.time() < deadline and ser is None:
        path = badge_module.target_port()
        if path:
            try:
                ser = serial.Serial(path, 115200, timeout=0.2)
            except Exception:
                ser = None
        time.sleep(0.05)
    if ser is None:
        raise SystemExit("port never appeared")

    buf, last_ping, end = b"", 0.0, time.time() + watch
    while time.time() < end:
        try:
            buf += ser.read(4096)
        except Exception as exc:
            print("read ended: %s" % type(exc).__name__, flush=True)
            break
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            text = line.decode("utf-8", "replace").strip()
            if text:
                print(text, flush=True)
        now = time.time()
        if now - last_ping > 5.0:
            try:
                ser.write(b".")
                ser.flush()
            except Exception:
                pass
            last_ping = now
    try:
        ser.close()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
