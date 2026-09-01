"""Run the Task 3 baseline spike and print what came back.

Deploys the spike as bytecode - cross-compiled rather than copied as source,
because compiling ~10 KB of Python on a badge with 30 KB free is a good way to
measure a MemoryError instead of a main loop - then imports it over the REPL and
parses the records.

Usage:
    python3 Spikes/host/run_baseline.py [--mpy-cross ./mpy-cross]
"""

import argparse
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import badge as badge_module  # noqa: E402
import report  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SPIKE_SOURCE = os.path.join(
    os.path.dirname(HERE), "circuitpython", "spike_baseline.py"
)

# Quiet enough not to be a nuisance while a pattern runs for a couple of
# minutes. The spike pins a playing transport on purpose - an idle badge
# collects rarely and reports a tail that does not exist in use - but nobody
# needs to hear it at full level.
TEST_VOLUME = 0.02


def deploy(mpy_cross, mount):
    """Cross-compile the spike onto the badge. Returns the deployed path."""
    target = os.path.join(mount, "spike_baseline.mpy")
    result = subprocess.run(
        [mpy_cross, SPIKE_SOURCE, "-o", target],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SystemExit(
            "mpy-cross failed:\n%s\n%s" % (result.stdout, result.stderr)
        )
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mpy-cross", default="./mpy-cross")
    args = parser.parse_args()

    mount = badge_module.circuitpy_mount()
    if not mount:
        raise SystemExit("CIRCUITPY is not mounted")
    if not badge_module.circuitpy_writable(mount):
        raise SystemExit(
            "CIRCUITPY is not writable. The kernel caches the write-protect "
            "bit at attach, so a remount will not help - reset the badge to "
            "force re-enumeration and try again."
        )

    print("deploying spike to %s" % mount)
    deploy(args.mpy_cross, mount)
    os.sync()
    # Writing to CIRCUITPY triggers auto-reload, so the firmware is restarting
    # right now. Interrupting into the middle of that leaves the host talking
    # to a program that is about to be replaced.
    time.sleep(3.0)

    with badge_module.Badge() as dev:
        print("getting a REPL")
        if not dev.repl():
            raise SystemExit(
                "could not reach a REPL prompt - the badge may still be "
                "auto-reloading, or main.py is not yielding to ctrl-C"
            )

        # The spike imports sequencer, whose module-level restore() reads the
        # card. That is slow and it is the firmware's real boot path, so it is
        # left alone and simply given time.
        print("importing (this reads the card)")
        # The marker is a bare token on its own line. `>>>` would match the
        # prompt in the echo instantly and let the next command run before the
        # import had finished; the trailing newline here is what the echo of
        # print('IMPORTED') does not have, because it is followed by `')`.
        _, imported = dev.line(
            "import spike_baseline; print('IMPORTED')",
            timeout=90,
            marker=r"IMPORTED\s*\r?\n",
        )
        if not imported:
            raise SystemExit(
                "the spike did not import - it reads the card at import time, "
                "so a missing card or a slow one shows up here"
            )
        dev.line(
            "import sequencer as _sq; _sq.engine.volume = %r; print('QUIET')"
            % TEST_VOLUME,
            timeout=15,
            marker=r"QUIET\s*\r?\n",
        )

        print("running cases")
        # DONE cannot appear in the echo of `spike_baseline.run()`, so it is a
        # safe sentinel; matching something the echo contains is how a long run
        # looks like a hang.
        out, matched = dev.line(
            "spike_baseline.run()", timeout=240, marker=r"DONE spike=baseline"
        )
        if not matched:
            print("!! spike did not finish inside the timeout")

    records, noise = report.parse(out)
    print("\n=== records ===")
    for keyword, fields in records:
        print(keyword, " ".join("%s=%s" % kv for kv in sorted(fields.items())))

    states = report.cases(records)
    incomplete = [name for name, state in states.items() if state != "OK"]
    if incomplete:
        print("\n=== cases that did not finish ===")
        for name in incomplete:
            print("  %s: %s" % (name, states[name]))

    interesting = [
        line
        for line in noise
        if "Error" in line or "Traceback" in line or "error" in line
    ]
    if interesting:
        print("\n=== errors ===")
        for line in interesting:
            print(" ", line)

    return 0 if matched and not incomplete else 1


if __name__ == "__main__":
    sys.exit(main())
