"""Drive the fault matrix, which deliberately kills the badge three times.

Different from every other runner here in two ways, both required:

  - It reconnects. Each leg ends in a reset, so the port disappears and comes
    back; a runner that opens once sees the first leg and nothing after it.

  - It stays silent. The `usb_dead` leg is caught by the host-silence
    heartbeat, so a keepalive - the thing run_output.py sends to keep long runs
    alive - would prevent the very failure this is testing.
"""

import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import serial  # noqa: E402
import flash as flash_module  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
# Three legs: watchdog at 8 s twice, then the 30 s heartbeat, plus a 3 s boot
# window each. Roughly 70 s of real time; this is slack, not an estimate.
DEADLINE = 240.0


def grab(timeout):
    """Open the port as soon as it exists. Returns a Serial or None."""
    end = time.time() + timeout
    while time.time() < end:
        for path in ("/dev/ttyACM0", "/dev/ttyACM1"):
            if os.path.exists(path):
                try:
                    return serial.Serial(path, 115200, timeout=0.2)
                except Exception:
                    pass
        time.sleep(0.02)
    return None


def main():
    image = os.path.join(HERE, "..", "c", "build", "spike_fault.uf2")
    mount = flash_module.enter_bootsel(timeout=60)
    if not mount:
        raise SystemExit("could not reach BOOTSEL")
    shutil.copyfile(image, os.path.join(mount, os.path.basename(image)))
    os.sync()
    print("flashed spike_fault", flush=True)

    seen = []
    end = time.time() + DEADLINE
    while time.time() < end:
        ser = grab(min(45.0, max(1.0, end - time.time())))
        if not ser:
            break
        buf = b""
        try:
            while time.time() < end:
                chunk = ser.read(4096)
                if chunk:
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        text = line.decode("utf-8", "replace").strip()
                        if text:
                            print(text, flush=True)
                            seen.append(text)
                if b"complete legs=" in b"\n".join(s.encode() for s in seen):
                    return 0
        except Exception:
            # The port going away IS the measurement: a leg just took the
            # badge down. Reconnect and keep listening.
            print("-- port dropped, reconnecting --", flush=True)
        finally:
            try:
                ser.close()
            except Exception:
                pass
        if any("complete legs=" in s for s in seen):
            return 0
    print("!! matrix did not complete", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
