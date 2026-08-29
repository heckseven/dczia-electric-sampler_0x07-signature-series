"""Settings that belong to the badge rather than to a song.

Brightness is the first of them, joined since by the screensaver line, the
chosen animation, and what the badge was last playing. None of them is part of
a song - loading somebody else's pattern should not change how bright your
panel is, what it says, or what it is doing - so they live in their own small
file on the card rather than in the song format.

Written through store.Store, like songs and kits, so the atomic-ish write and
the failure handling are the same ones. Reading and writing are both best
effort: a badge with no card still works, it just forgets between power-ups,
and nothing here raises into the main loop over that.
"""

from store import Store

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
    """Everything saved, as a dictionary. Empty if there is nothing to read.

    Catches everything, not just StoreError, and the reason is the badge
    starting at all. Sequencer.restore reads this at import - before the main
    loop, before the state machine, before anything exists to contain a
    failure - so an exception escaping here does not silence a setting, it
    fails `import sequencer` and the badge never boots.

    StoreError alone was not enough for that. store.load converts a fixed list
    of exceptions from msgpack.unpack, and store.MAX_BYTES bounds how *large*
    settings.prefs can be but not how deeply *nested*: a few hundred bytes of
    repeated map headers is thousands of levels, and what comes back from that
    is a RecursionError, which store.load does not convert and this would not
    have caught. The file comes off a card written by an arbitrary computer,
    which makes that a real shape rather than a theoretical one.

    Anything unreadable is the same answer as nothing saved: use the defaults.
    """
    try:
        data = store.load(NAME)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save(values):
    """Write the settings. Returns whether they reached the card.

    Broad for the same reason as load: this is reached from the sequencer's
    tick loop when the volume knob settles, and a note to self about where a
    knob was left must never be the thing that stops a badge playing.
    """
    try:
        store.save(values, NAME)
        return True
    except Exception:
        return False


def _set(key, value):
    """Read, change one key, write back. Returns whether it reached the card.

    Every setter here has this shape, and writing it out each time is one more
    place to pair the wrong load with the wrong save.
    """
    values = load()
    values[key] = value
    return save(values)


def _forget(key):
    """Drop a key entirely, so reading it back says "never saved".

    Not the same as storing an empty value: see last_kit, where "the player
    silenced every track" and "nothing has been remembered yet" are different
    answers and have to survive a power cycle as different answers.
    """
    values = load()
    values.pop(key, None)
    return save(values)


def brightness():
    """The saved panel brightness as a percentage, or the default."""
    return clamp_brightness(load().get("brightness", DEFAULT_BRIGHTNESS))


def set_brightness(percent):
    """Remember a brightness. Returns whether it reached the card."""
    return _set("brightness", clamp_brightness(percent))


# What the panel says while the lights are running. Empty means "say
# nothing", which is the default: a badge should not arrive with somebody
# else's words on it.
DEFAULT_TEXT = ""
MAX_TEXT = 21  # one row of the display


def text():
    """The screensaver line, or empty if none has been set."""
    return clean_text(load().get("text", DEFAULT_TEXT))


def set_text(value):
    """Remember what to show while an animation is up."""
    return _set("text", clean_text(value))


# Which light animation the badge was left showing.
#
# Stored by name rather than by index, so reordering the list in
# engine/animation.py cannot silently change what a saved badge comes back
# as. Empty means "whatever the default is", which is what a fresh card gets.
#
# Nothing here validates the name against the list: engine.animation.by_name
# already falls back for one it does not know, and that is where the list
# lives. This module bounds the string and leaves the meaning to the caller,
# exactly as it does for text().
DEFAULT_ANIMATION = ""
MAX_ANIMATION = 21


def animation_name():
    """The animation last chosen, or empty if none has been."""
    return clean_animation(load().get("animation", DEFAULT_ANIMATION))


def set_animation(name):
    """Remember which animation the badge is showing."""
    return _set("animation", clean_animation(name))


# What the badge was last playing, so a power cycle picks it up rather than
# starting over. Two things, because they change independently: the song is
# a name on the card, and the samples are the paths a player has swapped in
# since - which may be nothing to do with any saved kit.
#
# The kit is bounded to eight, mirroring engine.song.TRACK_COUNT. Named here
# rather than imported so this module keeps knowing nothing about what a
# track is, exactly as it knows nothing about what an animation is.
DEFAULT_SONG = ""
MAX_SONG = 21
MAX_KIT_TRACKS = 8
MAX_PATH = 96


def last_song():
    """The song last saved or loaded, or empty if there is none."""
    return _bounded(load().get("song", DEFAULT_SONG), MAX_SONG, DEFAULT_SONG)


def set_last_song(name):
    """Remember which song the badge is on. Empty forgets it."""
    return _set("song", _bounded(name, MAX_SONG, DEFAULT_SONG))


def last_kit():
    """The sample paths last in use, or None if none were remembered.

    None and [] are different answers and both are real. None means nothing
    has been remembered and the caller should use its own default; a list of
    eight Nones means the player silenced every track and meant it, and a
    badge that came back making noise after that would be ignoring them.

    Every entry is bounded the way the rest of this file bounds what it reads:
    the list comes off a card and may have been written by anything. A slot
    that is not a usable path comes back as None, which is a silent track
    rather than a badge that will not boot.
    """
    value = load().get("kit")
    if not isinstance(value, (list, tuple)):
        return None
    kit = []
    for entry in value[:MAX_KIT_TRACKS]:
        cleaned = _bounded(entry, MAX_PATH, "")
        kit.append(cleaned or None)
    return kit


def set_last_kit(paths):
    """Remember the samples in use. `paths` may hold None for a silent track.

    Passing None forgets the kit entirely rather than storing an empty one -
    see last_kit for why the two have to stay distinguishable.
    """
    if paths is None:
        return _forget("kit")
    return _set(
        "kit", [_bounded(path, MAX_PATH, "") for path in list(paths)[:MAX_KIT_TRACKS]]
    )


# Where the volume knob was left, as a knob position rather than a level -
# the position is what the firmware actually holds, and the level is derived
# from it so the steps are even to the ear. See engine.util.
#
# -1 means "never saved", which is how a fresh badge gets the firmware's own
# default rather than a silent one: 0 is a real position meaning silence, so
# it cannot double as "unset".
NO_VOLUME = -1


def volume_position():
    """The saved knob position, or NO_VOLUME if none has been saved."""
    value = load().get("volume", NO_VOLUME)
    try:
        value = int(value)
    except (TypeError, ValueError):
        return NO_VOLUME
    return value if value >= 0 else NO_VOLUME


def set_volume_position(position):
    """Remember where the knob was left."""
    try:
        position = max(0, int(position))
    except (TypeError, ValueError):
        return False
    return _set("volume", position)


def _bounded(value, limit, default):
    """Bound anything that came off a card written by who knows what.

    terminalio is ASCII only - anything outside it draws as a blank box - and
    a row is 21 characters, so both are enforced here rather than trusted
    from the file.
    """
    if not isinstance(value, str):
        return default
    kept = "".join(c for c in value if 32 <= ord(c) < 127)
    return kept[:limit]


def clean_text(value):
    return _bounded(value, MAX_TEXT, DEFAULT_TEXT)


def clean_animation(value):
    return _bounded(value, MAX_ANIMATION, DEFAULT_ANIMATION)


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
