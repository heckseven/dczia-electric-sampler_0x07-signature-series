"""Drive the torn-write test across repeated power pulls.

One cycle is: wait for the badge to be present, read the verdict it prints
about whatever the previous pull left behind, tell it to start writing, then
wait for the port to go away - which is the pull.

The two things this gets wrong if written casually, both of which it did:

  A cycle must not begin until the port has genuinely gone. Starting the next
  one the instant a read fails means opening the symlink that is still
  disappearing, failing again, and counting one pull as two - which silently
  consumed eight real pulls and reported one verdict.

  A read failing is not the same as the badge being unplugged. It also happens
  while the device is still settling after enumeration.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import serial  # noqa: E402
import badge as badge_module  # noqa: E402


def wait_present(timeout):
    end = time.time() + timeout
    while time.time() < end:
        if badge_module.target_port() is not None:
            return True
        time.sleep(0.1)
    return False


def wait_absent(timeout):
    """Wait for the port to go, and stay gone - a pull, not a flicker."""
    end = time.time() + timeout
    while time.time() < end:
        if badge_module.target_port() is None:
            time.sleep(0.4)
            if badge_module.target_port() is None:
                return True
        time.sleep(0.1)
    return False


def run_cycle(index, total, letter=b"S"):
    print("=== cycle %d of %d: waiting for the badge ===" % (index, total),
          flush=True)
    if not wait_present(300):
        print("badge never appeared", flush=True)
        return None

    # Settle before opening: a port that has just enumerated can still throw.
    time.sleep(1.0)
    ser = None
    for _ in range(10):
        path = badge_module.target_port()
        if path:
            try:
                ser = serial.Serial(path, 115200, timeout=0.2)
                break
            except Exception:
                ser = None
        time.sleep(0.3)
    if ser is None:
        print("could not open the port", flush=True)
        return None

    verdict = None
    started = False
    already_writing = False
    buf = b""
    deadline = time.time() + 25
    try:
        while time.time() < deadline and not started:
            try:
                buf += ser.read(4096)
            except Exception:
                break
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                text = line.decode("utf-8", "replace").strip()
                if not text:
                    continue
                if "saves=" in text or "writing - pull" in text:
                    # Already past the verdict: this cycle attached mid-session
                    # rather than after a reboot, so there is nothing to read
                    # and the only useful thing to do is wait for the pull.
                    already_writing = True
                for part in text.split():
                    if part.startswith("verdict=") and verdict is None:
                        verdict = part.split("=", 1)[1]
                        print(text, flush=True)
                    if part.startswith("fs_problems=") and part != "fs_problems=0":
                        print("!! %s" % text, flush=True)
            if already_writing:
                started = True
                break
            if verdict is not None:
                try:
                    ser.write(letter)
                    ser.flush()
                    started = True
                except Exception:
                    break
    finally:
        try:
            ser.close()
        except Exception:
            pass

    if verdict is None:
        if not already_writing:
            print("no verdict read this cycle", flush=True)
            return None
        print("(already writing - this pull's verdict comes next cycle)",
              flush=True)

    print(">>> PULL THE USB CABLE NOW <<<" if letter == b"S"
          else "-- badge will reset itself --", flush=True)
    if not wait_absent(300):
        print("no pull seen", flush=True)
    else:
        print("-- power gone --", flush=True)
    return verdict


def main():
    total = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    # "self" tells the badge to reset itself instead of waiting for a person,
    # which exercises this script's reconnect path without costing anybody a
    # cable pull. It cannot show a torn write - the card keeps its power.
    letter = b"R" if (len(sys.argv) > 2 and sys.argv[2] == "self") else b"S"
    verdicts = []
    for i in range(total):
        v = run_cycle(i + 1, total, letter)
        if v is not None:
            verdicts.append(v)
    bad = [v for v in verdicts if v not in ("A", "B")]
    print("VERDICTS %s" % (verdicts,), flush=True)
    print("clean=%d torn_or_absent=%d" % (len(verdicts) - len(bad), len(bad)),
          flush=True)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
