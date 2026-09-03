"""Run a spike that resets itself partway, and keep listening across it.

Same shape as run_fault.py: the port disappears when the badge reboots, so a
runner that opens once sees the first pass and nothing after it.
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


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else "rt_check"
    deadline = time.time() + (float(sys.argv[2]) if len(sys.argv) > 2 else 180.0)

    image = os.path.join(HERE, "..", "..", "Rewrite", "build", target + ".uf2")
    mount = flash_module.enter_bootsel(timeout=60)
    if not mount:
        raise SystemExit("could not reach BOOTSEL")
    shutil.copyfile(image, os.path.join(mount, target + ".uf2"))
    os.sync()
    print("flashed %s" % target, flush=True)

    seen = []
    while time.time() < deadline:
        ser = None
        while time.time() < deadline and ser is None:
            path = badge_module.target_port()
            if path:
                try:
                    ser = serial.Serial(path, 115200, timeout=0.2)
                except Exception:
                    ser = None
            time.sleep(0.05)
        if ser is None:
            break

        buf, last_ping = b"", 0.0
        try:
            while time.time() < deadline:
                buf += ser.read(4096)
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    text = line.decode("utf-8", "replace").strip()
                    if text:
                        print(text, flush=True)
                        seen.append(text)
                if any("DONE spike=" in s for s in seen[-3:]):
                    return 0
                now = time.time()
                if now - last_ping > 5.0:
                    try:
                        ser.write(b".")
                        ser.flush()
                    except Exception:
                        pass
                    last_ping = now
        except Exception:
            print("-- port dropped, reconnecting --", flush=True)
        finally:
            try:
                ser.close()
            except Exception:
                pass
    return 1


if __name__ == "__main__":
    sys.exit(main())
