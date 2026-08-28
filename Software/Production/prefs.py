"""Settings that belong to the badge rather than to a song.

Brightness is the first of them. It is not part of a song - loading somebody
else's pattern should not change how bright your panel is - so it lives in
its own small file on the card rather than in the song format.

Written through store.Store, like songs and kits, so the atomic-ish write and
the failure handling are the same ones. Reading and writing are both best
effort: a badge with no card still works, it just forgets between power-ups,
and nothing here raises into the main loop over that.
"""

from store import Store, StoreError

PREF_DIR = "/sd"
NAME = "settings"
SUFFIX = ".prefs"

store = Store(PREF_DIR, SUFFIX, kind="settings")

# What the panel is built at, and what a fresh card gets. Ten NeoPixels on a
# 3V3 rail whose only source is the Pico's own regulator, with 0.6 uF of
# decoupling and no bulk capacitor anywhere - so this is a power decision as
# much as a visual one. See MAX_BRIGHTNESS.
DEFAULT_BRIGHTNESS = 10

# The ceiling, as a percentage. Ten pixels at full white is three channels of
# about 20 mA each, so 600 mA - far past what the regulator will give while
# also running the Pico, the card and the amplifier. Fifty percent is five
# times the default and keeps the worst case near 300 mA, which the measured
# topology can stand. Not a taste limit; do not raise it without measuring.
MAX_BRIGHTNESS = 50
MIN_BRIGHTNESS = 1


def load():
    """Everything saved, as a dictionary. Empty if there is nothing to read."""
    try:
        data = store.load(NAME)
    except StoreError:
        return {}
    return data if isinstance(data, dict) else {}


def save(values):
    """Write the settings. Returns whether they reached the card."""
    try:
        store.save(values, NAME)
        return True
    except StoreError:
        return False


def brightness():
    """The saved panel brightness as a percentage, or the default."""
    return clamp_brightness(load().get("brightness", DEFAULT_BRIGHTNESS))


def set_brightness(percent):
    """Remember a brightness. Returns whether it reached the card."""
    values = load()
    values["brightness"] = clamp_brightness(percent)
    return save(values)


def clamp_brightness(percent):
    """Bound a value that may have come off a card written by anything."""
    try:
        percent = int(percent)
    except (TypeError, ValueError):
        return DEFAULT_BRIGHTNESS
    if percent < MIN_BRIGHTNESS:
        return MIN_BRIGHTNESS
    if percent > MAX_BRIGHTNESS:
        return MAX_BRIGHTNESS
    return percent
