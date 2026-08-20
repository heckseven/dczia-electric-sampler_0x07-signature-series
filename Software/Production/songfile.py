"""Reading and writing songs on the SD card.

The filesystem part is store.Store; what a song looks like as a dictionary
is Song.to_dict and Song.from_dict, in the engine. This is the seam between
them, and the place that knows songs live in /sd/songs.
"""

from engine.song import Song
from store import MAX_BYTES, Store, StoreError  # noqa: F401

SONG_DIR = "/sd/songs"
SUFFIX = ".song"

# Public, because the tests point it at a temporary directory and the
# firmware never has cause to move it.
store = Store(SONG_DIR, SUFFIX, kind="song")

# The old name for the failure, kept because it reads better where it is
# caught and because callers already name it.
SongFileError = StoreError

path_for = store.path_for
name_of = store.name_of
available = store.available
songs = store.names
delete = store.delete
rename = store.rename


def save(song, name):
    """Write a song, replacing any song of that name."""
    return store.save(song.to_dict(), name)


def load(name):
    """Read a song back. Raises SongFileError if it cannot be."""
    # Song.from_dict treats everything in here as untrusted, so a corrupt
    # or foreign file loads as a slightly wrong song rather than raising.
    return Song.from_dict(store.load(name))
