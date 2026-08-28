#!/usr/bin/env python3
"""Compile the firmware to bytecode for deployment.

CIRCUITPY is a 490 KB filesystem and the firmware is 300 KB of source, which
left 25 KB free - not enough for the samples the badge would like to hold, and
not the real problem either.

The real problem is that CircuitPython compiles a module the first time it is
imported, and compiling needs a large contiguous run of heap. The badge died
that way: building the sampler screen at the end of the startup banner, with
the heap in pieces, it could not allocate 195 bytes with 42 KB free. A .mpy
file is already bytecode, so importing one does not run the compiler at all
and that working set never exists.

    source .py    298390 bytes
    compiled       65166 bytes   (21%)

Docstrings are not the reason. CircuitPython's compiler discards them, measured:
5450 bytes of docstring compiled to the same bytecode as a comment saying the
same thing. The saving is the source text itself never reaching the badge.

Usage:
    python3 Tools/build.py                      # -> build/CIRCUITPY
    python3 Tools/build.py -o /media/you/CIRCUITPY
    python3 Tools/build.py --mpy-cross ./mpy-cross

mpy-cross is not vendored - it is a megabyte of platform-specific binary, and
the wrong one is worse than none. See MPY_CROSS_HELP below for where to get the
build that matches, and why the one on PyPI is not it.
"""

import argparse
import os
import shutil
import subprocess
import sys

# CircuitPython looks for code.txt, code.py, main.txt and main.py. It does not
# look for main.mpy, so the entry point stays source however much else is
# compiled. It is small, and it is the one file worth being able to read on a
# badge with no computer attached.
ALWAYS_SOURCE = ("main.py", "boot.py", "code.py")

# Already bytecode, or not code at all.
COPY_AS_IS = ("lib", "samples")

SKIP = ("__pycache__", ".pytest_cache")

# A wedged compiler should fail the build, not hang it. Generous: the largest
# module here compiles in well under a second.
COMPILE_TIMEOUT = 120

MPY_CROSS_HELP = """\
mpy-cross not found.

It must be the CircuitPython build matching the badge's version, not the one on
PyPI: MicroPython's emits a different magic byte (0x4d) from CircuitPython's
(0x43), and the badge rejects the file rather than running something subtly
wrong. Adafruit publish a static binary per release:

  https://adafruit-circuit-python.s3.amazonaws.com/bin/mpy-cross/linux-amd64/\
mpy-cross-linux-amd64-<version>.static

Download the one matching boot_out.txt on the badge, chmod +x it, and pass it
with --mpy-cross (or put it on PATH as mpy-cross).\
"""


def _version_word(text):
    """The first X.Y.Z-looking token. Both versions are scraped from free text."""
    for word in text.split():
        if word[:1].isdigit() and word.count(".") == 2:
            return word
    return None


def _target(out_dir, relative):
    """Where a built file goes, refusing to write through a symlink.

    The output is routinely a mounted CIRCUITPY, and a card that has been out
    of your hands can carry anything. Writing through a symlink - at the file
    or at any directory above it - would put build output somewhere on the host
    instead, silently. FAT cannot hold a symlink, so on a real badge this never
    fires; it fires when the output is an ordinary directory, which is the
    default.
    """
    target = os.path.join(out_dir, relative)
    if os.path.islink(target):
        raise SystemExit("refusing to write through a symlink: %s" % target)
    parent = os.path.dirname(target) or out_dir
    root = os.path.realpath(out_dir)
    if os.path.exists(parent) and not os.path.realpath(parent).startswith(root):
        raise SystemExit("refusing to write outside %s: %s" % (out_dir, parent))
    return target


def plan(source_dir):
    """What the build will do, as a list of (action, relative path).

    Separate from doing it so the decisions can be tested without a compiler
    and without writing anything.
    """
    actions = []
    for root, dirs, files in os.walk(source_dir):
        # In place, because that is the only way os.walk honours the pruning.
        # Sorted so a build is the same twice running.
        dirs[:] = sorted(d for d in dirs if d not in SKIP)
        relative_root = os.path.relpath(root, source_dir)
        if relative_root == ".":
            relative_root = ""
        # Only a top-level lib/ or samples/ is special. A directory of either
        # name nested deeper is ordinary source and gets compiled.
        top = relative_root.split(os.sep)[0]
        verbatim = top in COPY_AS_IS
        for name in sorted(files):
            relative = os.path.join(relative_root, name)
            # A symlink in the source tree would copy whatever it points at,
            # off the host and onto a card that gets carried around. Not ours
            # to follow.
            if os.path.islink(os.path.join(root, name)):
                actions.append(("skip", relative))
            elif verbatim or name in ALWAYS_SOURCE or not name.endswith(".py"):
                actions.append(("copy", relative))
            else:
                actions.append(("compile", relative))
    return actions


def find_mpy_cross(explicit=None):
    if explicit:
        if not os.path.isfile(explicit) or not os.access(explicit, os.X_OK):
            raise SystemExit("not an executable: %s\n\n%s" % (explicit, MPY_CROSS_HELP))
        return explicit
    found = shutil.which("mpy-cross")
    if found is None:
        raise SystemExit(MPY_CROSS_HELP)
    return found


def badge_version(out_dir):
    """The CircuitPython version the target is running, if it can be seen.

    Only meaningful when building straight onto a mounted badge. A .mpy built
    by the wrong compiler imports as "Incompatible .mpy file", which is a
    confusing thing to discover on hardware when it could be caught here.
    """
    marker = os.path.join(out_dir, "boot_out.txt")
    try:
        with open(marker) as handle:
            first = handle.readline()
    except OSError:
        return None
    # "Adafruit CircuitPython 10.2.1 on 2026-05-13; Raspberry Pi Pico W..."
    return _version_word(first)


def compiler_version(mpy_cross):
    try:
        out = subprocess.run(
            [mpy_cross, "--version"], capture_output=True, text=True, timeout=30
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return _version_word(out)


def build(source_dir, out_dir, mpy_cross, actions=None, verbose=True):
    """Compile and copy. Returns (compiled, copied, skipped, source, built).

    The plan is taken as an argument so the tree is walked once for a build
    rather than once per thing the caller wants to know, and so the summary
    describes the files that were actually built rather than whatever the
    directory looks like by the time it is printed.
    """
    if actions is None:
        actions = plan(source_dir)
    compiled = copied = skipped = 0
    source_bytes = built_bytes = 0
    for action, relative in actions:
        source = os.path.join(source_dir, relative)
        if action == "skip":
            skipped += 1
            print("  skipped symlink %s" % relative)
            continue
        if action == "copy":
            target = _target(out_dir, relative)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            try:
                shutil.copy2(source, target)
            except OSError as error:
                # The output is usually a mounted badge: unplugged mid-build,
                # or full, is the ordinary way this fails.
                raise SystemExit("could not write %s: %s" % (target, error))
            copied += 1
            continue
        target = _target(out_dir, relative[:-3] + ".mpy")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        try:
            result = subprocess.run(
                [mpy_cross, "-o", target, source],
                capture_output=True,
                text=True,
                timeout=COMPILE_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            raise SystemExit("mpy-cross hung on %s" % source)
        except OSError as error:
            raise SystemExit("could not run %s: %s" % (mpy_cross, error))
        if result.returncode != 0:
            raise SystemExit("%s\n%s" % (source, result.stderr.strip()))
        compiled += 1
        before = os.path.getsize(source)
        after = os.path.getsize(target)
        source_bytes += before
        built_bytes += after
        if verbose:
            print("  %-34s %6d -> %5d" % (relative, before, after))
    return compiled, copied, skipped, source_bytes, built_bytes


def main():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parser = argparse.ArgumentParser(
        description="Compile the firmware to bytecode for deployment."
    )
    parser.add_argument(
        "-s",
        "--source",
        default=os.path.join(here, "Software", "Production"),
        help="firmware source directory",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=os.path.join(here, "build", "CIRCUITPY"),
        help="where to write the build; a mounted CIRCUITPY works",
    )
    parser.add_argument("--mpy-cross", dest="mpy_cross", help="path to mpy-cross")
    parser.add_argument("--quiet", action="store_true", help="only print the summary")
    args = parser.parse_args()

    if not args.output:
        raise SystemExit("--output must name a directory")

    mpy_cross = find_mpy_cross(args.mpy_cross)
    wanted = badge_version(args.output)
    have = compiler_version(mpy_cross)
    if wanted and have and wanted != have:
        raise SystemExit(
            "mpy-cross is %s but the badge runs CircuitPython %s.\n"
            "Bytecode from the wrong version imports as "
            '"Incompatible .mpy file".\n\n%s' % (have, wanted, MPY_CROSS_HELP)
        )

    os.makedirs(args.output, exist_ok=True)
    actions = plan(args.source)
    compiled, copied, skipped, source_bytes, built_bytes = build(
        args.source, args.output, mpy_cross, actions, verbose=not args.quiet
    )

    print("\n%d compiled, %d copied -> %s" % (compiled, copied, args.output))
    if skipped:
        print("%d symlinks skipped" % skipped)
    print(
        "%d bytes of source became %d (%d%%), saving %d"
        % (
            source_bytes,
            built_bytes,
            built_bytes * 100 // max(source_bytes, 1),
            source_bytes - built_bytes,
        )
    )


if __name__ == "__main__":
    main()
