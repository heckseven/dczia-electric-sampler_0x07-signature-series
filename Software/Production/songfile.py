"""Reading and writing songs on the SD card.

Songs live on the card rather than in onboard flash. That is not a capacity
decision - a song is under two kilobytes - but a workflow one: writing to
CIRCUITPY from the badge needs a boot.py that remounts it, and that makes
the drive read-only to the host, which breaks dragging samples on and off
it. The card is writable by the badge and by a computer, just not at the
same moment, which is the right trade for something the player saves.

Serialisation itself belongs to Song.to_dict and Song.from_dict, in the
engine, where it can be tested without a filesystem. This module is only
the part that needs one: paths, bytes on disk, and the failures that come
with them.

msgpack is used because CircuitPython ships it in the core - no library to
install, no room to find for one - and because it encodes a bytearray as
bytes rather than as a list of numbers. A 64-step pattern for eight tracks
is 512 bytes either way in memory but 25 KB as JSON.
"""

import os

import msgpack

from engine.song import Song

SONG_DIR = "/sd/songs"
SUFFIX = ".song"

# Written first, then renamed over the real name. A song saved on top of
# itself must not be destroyed by a power cut half way through - the badge
# is a Eurorack module and the rack switch is a real thing people press.
TEMP_SUFFIX = ".part"

# A song file is small. Anything much larger is not one, and refusing to
# read it is cheaper than discovering that half way through unpacking.
MAX_BYTES = 16 * 1024


class SongFileError(Exception):
    """The song could not be read or written."""


def path_for(name):
    """The file a song name lives in."""
    return "%s/%s%s" % (SONG_DIR, name, SUFFIX)


def name_of(filename):
    """The song name a filename carries, or None if it is not a song."""
    if not filename.endswith(SUFFIX):
        return None
    return filename[: -len(SUFFIX)]


def available():
    """Whether songs can be read or written at all.

    False when there is no card. The sampler still works without one; it
    just cannot keep anything.
    """
    try:
        os.listdir(SONG_DIR)
        return True
    except OSError:
        pass
    # The directory may simply not exist yet on an otherwise good card.
    try:
        os.mkdir(SONG_DIR)
        return True
    except OSError:
        return False


def songs():
    """The names of the songs on the card, sorted, or an empty list."""
    try:
        entries = os.listdir(SONG_DIR)
    except OSError:
        return []
    found = []
    for entry in entries:
        name = name_of(entry)
        if name:
            found.append(name)
    found.sort()
    return found


def save(song, name):
    """Write a song, replacing any song of that name.

    Raises SongFileError if it cannot be written. The existing file is left
    untouched unless the new one was written in full.
    """
    if not available():
        raise SongFileError("no card")
    final = path_for(name)
    temp = final + TEMP_SUFFIX
    try:
        with open(temp, "wb") as handle:
            msgpack.pack(song.to_dict(), handle)
    except (OSError, ValueError, TypeError) as error:
        _remove(temp)
        raise SongFileError("could not write %s: %s" % (name, error))
    try:
        # No atomic replace on this filesystem, so the old file goes first.
        # The window is between two directory operations rather than across
        # the whole write, which is the best available here.
        _remove(final)
        os.rename(temp, final)
    except OSError as error:
        _remove(temp)
        raise SongFileError("could not replace %s: %s" % (name, error))
    return final


def load(name):
    """Read a song back. Raises SongFileError if it cannot be."""
    path = path_for(name)
    try:
        size = os.stat(path)[6]
    except OSError as error:
        raise SongFileError("no such song %s: %s" % (name, error))
    if size > MAX_BYTES:
        raise SongFileError("%s is %d bytes, too large to be a song" % (name, size))
    try:
        with open(path, "rb") as handle:
            data = msgpack.unpack(handle)
    except (OSError, ValueError, TypeError, EOFError) as error:
        raise SongFileError("could not read %s: %s" % (name, error))
    if not isinstance(data, dict):
        raise SongFileError("%s does not contain a song" % name)
    # Song.from_dict treats everything in here as untrusted, so a corrupt
    # or foreign file loads as a slightly wrong song rather than raising.
    return Song.from_dict(data)


def delete(name):
    """Remove a song. Returns whether there was one to remove."""
    return _remove(path_for(name))


def _remove(path):
    try:
        os.remove(path)
        return True
    except OSError:
        return False
