"""Tests that the vendored libraries are complete.

The host suite replaces every adafruit_* module with a stub, so a library the
firmware imports can be missing from Software/Production/lib entirely and 800
tests still pass. That is exactly how Flashy shipped broken on 2026-08-20:
the bundle had been pruned to the modules the firmware names, and
rainbowchase.mpy subclasses adafruit_led_animation.animation.chase, which was
not among them. Entering Flashy raised ImportError, which drops the badge out
of its main loop and reads as a crash and reboot.

These are static checks on the vendored tree. They catch a module the
firmware names and does not have. They cannot see inside a .mpy, so they
cannot catch a missing transitive dependency - the only honest check for that
is importing on the badge, which is why complete packages are vendored rather
than hand-picked files. That decision is what these tests protect.
"""

import os
import re

from conftest import PRODUCTION_DIR

LIB = os.path.join(PRODUCTION_DIR, "lib")

# Shipped inside the CircuitPython build rather than as a library. Vendoring
# these would shadow a native module with a slower Python one.
BUILT_IN = {"adafruit_pixelbuf"}

# Packages the firmware uses. Vendored whole, not pruned to the modules that
# happen to be named: the modules inside them subclass each other, and the
# saving from pruning adafruit_led_animation was 18 KB against 200 KB free.
WHOLE_PACKAGES = ("adafruit_led_animation", "adafruit_hid", "adafruit_midi")


def firmware_imports():
    """Every adafruit_* or neopixel module the firmware names."""
    found = set()
    for root, _dirs, files in os.walk(PRODUCTION_DIR):
        if "lib" in root.split(os.sep):
            continue
        for name in files:
            if not name.endswith(".py"):
                continue
            with open(os.path.join(root, name)) as handle:
                text = handle.read()
            for match in re.findall(
                r"^\s*(?:from|import)\s+([A-Za-z_][A-Za-z0-9_.]*)", text, re.M
            ):
                if match.startswith("adafruit_") or match == "neopixel":
                    found.add(match)
    return found


def resolves(name):
    """Whether a dotted module name exists in the vendored tree."""
    rel = name.replace(".", os.sep)
    return any(
        os.path.exists(os.path.join(LIB, rel + suffix))
        for suffix in (".mpy", ".py", os.sep + "__init__.mpy", os.sep + "__init__.py")
    )


def test_the_firmware_names_libraries_at_all():
    """A guard on the guard: an empty set would make the next test vacuous."""
    assert len(firmware_imports()) > 10


def test_every_library_the_firmware_imports_is_vendored():
    missing = sorted(
        name
        for name in firmware_imports()
        if name.split(".")[0] not in BUILT_IN and not resolves(name)
    )
    assert missing == [], "the badge will raise ImportError on: %s" % missing


def test_the_animation_package_is_whole():
    """Pruning it is what broke Flashy: the rainbow classes subclass the base ones."""
    animations = os.path.join(LIB, "adafruit_led_animation", "animation")
    present = {name for name in os.listdir(animations) if name.endswith(".mpy")}
    for base in ("chase.mpy", "comet.mpy", "sparkle.mpy", "pulse.mpy"):
        assert base in present, "%s is what rainbowchase and friends subclass" % base


def test_the_packages_the_firmware_uses_are_vendored_whole():
    """Each is a directory with more than the one module that gets named."""
    for package in WHOLE_PACKAGES:
        path = os.path.join(LIB, package)
        assert os.path.isdir(path), "%s is not vendored" % package


def test_no_python_sources_are_vendored_over_the_compiled_bundle():
    """A .py next to a .mpy shadows it, and the .py may be the older version.

    The repository shipped a CircuitPython 8 adafruit_displayio_ssd1306.py
    long after the badge moved to 10, where the class it subclasses no longer
    exists. Nothing caught it because the tests stub that module out.
    """
    stray = []
    for root, _dirs, files in os.walk(LIB):
        for name in files:
            if name.endswith(".py"):
                stray.append(os.path.relpath(os.path.join(root, name), LIB))
    assert stray == [], "vendored .py files shadow the bundle: %s" % stray


def test_nothing_built_into_circuitpython_is_vendored():
    """Vendoring one shadows a native module with a slower Python one."""
    for name in BUILT_IN:
        assert not resolves(name), "%s ships inside CircuitPython" % name
