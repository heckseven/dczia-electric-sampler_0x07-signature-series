"""Named files of packed data on the SD card.

Songs and kits are the same problem twice: a directory, a suffix, a name the
player chose, and a dictionary to write into it. This holds that once, so a
third kind of saved thing is a few lines rather than another copy of the
rename dance and the failure handling.

Everything lives on the card rather than in onboard flash. That is not a
capacity decision - a song is under two kilobytes - but a workflow one:
writing to CIRCUITPY from the badge needs a boot.py that remounts it, and
that makes the drive read-only to the host, which breaks dragging samples on
and off it. The card is writable by the badge and by a computer, just not at
the same moment, which is the right trade for something the player saves.

Turning an object into a dictionary and back belongs to whatever is being
saved, in the engine, where it can be tested without a filesystem. This
module is only the part that needs one: paths, bytes on disk, and the
failures that come with them.

msgpack is used because CircuitPython ships it in the core - no library to
install, no room to find for one - and because it encodes a bytearray as
bytes rather than as a list of numbers. A 64-step pattern for eight tracks
is 512 bytes either way in memory but 25 KB as JSON.
"""

import os

import msgpack

# Written first, then renamed over the real name. A song saved on top of
# itself must not be destroyed by a power cut half way through - the badge
# is a Eurorack module and the rack switch is a real thing people press.
TEMP_SUFFIX = ".part"

# A saved file is small. Anything much larger is not one, and refusing to
# read it is cheaper than discovering that half way through unpacking.
#
# This bounds the file, not what the file claims. A msgpack header inside a
# 16 KB file can declare an array of four billion entries, and the unpacker
# allocates before it can discover the bytes are not there - which is why
# MemoryError is caught alongside the parse errors below.
MAX_BYTES = 16 * 1024

# Most names a directory will be read for. A listing is not something the
# badge controls: the card is written by a computer, and a folder with tens
# of thousands of files in it would be turned into a list of strings before
# anything could look at its length. The limit is far above any real
# collection of songs and far below what the heap can hold.
MAX_ENTRIES = 512

# What a name may contain. The card is FAT, the badge writes these paths
# without escaping them, and a name arriving from a file listing or an old
# save is not something to trust: a slash would write outside the directory
# and a dot pair would climb out of it. The name entry screen cannot produce
# anything outside this set, so this catches the paths that did not come
# from it.
ALLOWED = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_ "
MAX_NAME = 24


class StoreError(Exception):
    """The file could not be read or written."""


def valid_name(name):
    """Whether a name can be used as a filename on the card."""
    if not name or len(name) > MAX_NAME:
        return False
    for character in name:
        if character not in ALLOWED:
            return False
    return True


class Store:
    """One directory of named files sharing a suffix."""

    def __init__(self, directory, suffix, kind="file"):
        self.directory = directory
        self.suffix = suffix
        # What to call it in an error the player will read.
        self.kind = kind

    def path_for(self, name):
        return "%s/%s%s" % (self.directory, name, self.suffix)

    def name_of(self, filename):
        """The name a filename carries, or None if it is not one of ours."""
        if not filename.endswith(self.suffix):
            return None
        return filename[: -len(self.suffix)]

    def available(self):
        """Whether anything can be read or written at all.

        False when there is no card. The sampler still works without one; it
        just cannot keep anything.
        """
        try:
            os.listdir(self.directory)
            return True
        except OSError:
            pass
        # The directory may simply not exist yet on an otherwise good card.
        try:
            os.mkdir(self.directory)
            return True
        except OSError:
            return False

    def names(self, limit=MAX_ENTRIES):
        """The names on the card, sorted, or an empty list.

        Bounded, because the card is written by a computer and the length of
        a listing is not something the badge decides. MemoryError is caught
        for the same reason: it is neither OSError nor anything else the
        callers handle, so without this a card with too much on it takes the
        badge down rather than showing a shorter list.
        """
        try:
            entries = os.listdir(self.directory)
        except (OSError, MemoryError):
            return []
        found = []
        try:
            for entry in entries:
                name = self.name_of(entry)
                if name:
                    found.append(name)
                    if len(found) >= limit:
                        break
        except MemoryError:
            pass
        found.sort()
        return found

    def save(self, data, name):
        """Write a dictionary, replacing anything of that name.

        Raises StoreError if it cannot be written. The existing file is left
        untouched unless the new one was written in full.
        """
        if not valid_name(name):
            raise StoreError("bad name")
        if not self.available():
            raise StoreError("no card")
        final = self.path_for(name)
        temp = final + TEMP_SUFFIX
        try:
            with open(temp, "wb") as handle:
                msgpack.pack(data, handle)
        except (OSError, ValueError, TypeError, MemoryError) as error:
            self._remove(temp)
            raise StoreError("could not write %s: %s" % (name, error))
        try:
            # No atomic replace on this filesystem, so the old file goes
            # first. The window is between two directory operations rather
            # than across the whole write, which is the best available here.
            self._remove(final)
            os.rename(temp, final)
        except OSError as error:
            self._remove(temp)
            raise StoreError("could not replace %s: %s" % (name, error))
        return final

    def load(self, name):
        """Read a dictionary back. Raises StoreError if it cannot be."""
        if not valid_name(name):
            raise StoreError("bad name")
        path = self.path_for(name)
        try:
            size = os.stat(path)[6]
        except OSError as error:
            raise StoreError("no such %s %s: %s" % (self.kind, name, error))
        if size > MAX_BYTES:
            raise StoreError(
                "%s is %d bytes, too large to be a %s" % (name, size, self.kind)
            )
        try:
            with open(path, "rb") as handle:
                data = msgpack.unpack(handle)
        except (OSError, ValueError, TypeError, EOFError, MemoryError) as error:
            # MemoryError is caught deliberately, the same way the WAV reader
            # in sequencer.py catches it. The size check above bounds the
            # file; it does not bound what the file claims about itself. A
            # header declaring a four billion entry array is a few bytes
            # long, and the unpacker allocates for it before it can find out
            # the bytes are missing. Without this that escapes every handler
            # up to the main loop, which drops the badge to the REPL in the
            # middle of a performance.
            raise StoreError("could not read %s: %s" % (name, error))
        if not isinstance(data, dict):
            raise StoreError("%s does not contain a %s" % (name, self.kind))
        return data

    def delete(self, name):
        """Remove one. Returns whether there was one to remove."""
        if not valid_name(name):
            return False
        return self._remove(self.path_for(name))

    def rename(self, old, new):
        """Give a saved file a new name. Returns whether it moved.

        A rename rather than a save under the new name and a delete of the
        old, so the data is never held only in memory: a failure here leaves
        both files where they were.

        The source is checked before the destination is touched. Renaming
        onto an existing name has to remove it first - this filesystem has
        no atomic replace - and doing that before finding out whether there
        is anything to move would destroy a good song to make room for one
        that is not there. A stale name is not far-fetched: the card can be
        edited by a computer between one save and the next.
        """
        if not valid_name(old) or not valid_name(new):
            raise StoreError("bad name")
        if old == new:
            return False
        source = self.path_for(old)
        try:
            os.stat(source)
        except OSError as error:
            raise StoreError("no such %s %s: %s" % (self.kind, old, error))
        try:
            self._remove(self.path_for(new))
            os.rename(source, self.path_for(new))
        except OSError as error:
            raise StoreError("could not rename %s: %s" % (old, error))
        return True

    @staticmethod
    def _remove(path):
        try:
            os.remove(path)
            return True
        except OSError:
            return False
