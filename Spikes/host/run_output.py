"""Flash a C spike and read it while keeping the heartbeat fed.

The heartbeat resets the badge if the host goes quiet for SPIKE_HEARTBEAT_MS,
which is correct for an unattended campaign and fatal for a spike that runs
longer than that while the host only listens. So this one talks back: any byte
counts as traffic, and '.' is not 'B'.
"""

import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import serial  # noqa: E402
import flash as flash_module  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else "spike_output"
    image = os.path.join(HERE, "..", "c", "build", name + ".uf2")

    mount = flash_module.enter_bootsel(timeout=60)
    if not mount:
        raise SystemExit("could not reach BOOTSEL")
    shutil.copyfile(image, os.path.join(mount, os.path.basename(image)))
    os.sync()
    print("flashed %s" % name, flush=True)

    # Grab the port the instant it enumerates, so the boot banner is not missed.
    ser, deadline = None, time.time() + 45
    while time.time() < deadline:
        for path in ("/dev/ttyACM0", "/dev/ttyACM1"):
            if os.path.exists(path):
                try:
                    ser = serial.Serial(path, 115200, timeout=0.2)
                except Exception:
                    ser = None
                if ser:
                    break
        if ser:
            break
        time.sleep(0.02)
    if not ser:
        raise SystemExit("port never appeared")

    buf, last_ping = b"", 0.0
    end = time.time() + int(os.environ.get("SPIKE_TIMEOUT", "180"))
    while time.time() < end:
        try:
            buf += ser.read(4096)
        except Exception as exc:
            print("read ended: %s" % type(exc).__name__, flush=True)
            break
        if b"DONE spike=" in buf:
            break
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
    print(buf.decode("utf-8", "replace"))
    return 0 if b"DONE spike=" in buf else 1


if __name__ == "__main__":
    sys.exit(main())
