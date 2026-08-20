"""Reading and writing kits on the SD card.

A kit is the eight sample paths a song plays, saved apart from the song so
one set of sounds can be used by several patterns. It is a list rather than
an object, so unlike a song there is nothing in the engine to serialise it -
the coercion that keeps a corrupt file from raising lives here.
"""

from engine.song import TRACK_COUNT
from store import Store

KIT_DIR = "/sd/kits"
SUFFIX = ".kit"

# Public, because the tests point it at a temporary directory and the
# firmware never has cause to move it.
store = Store(KIT_DIR, SUFFIX, kind="kit")

path_for = store.path_for
name_of = store.name_of
available = store.available
kits = store.names
delete = store.delete
rename = store.rename


def save(paths, name):
    """Write the eight sample paths under a name."""
    return store.save({"paths": [path or "" for path in paths]}, name)


def load(name):
    """Read a kit back as eight paths, missing ones being None.

    Everything in the file is treated as untrusted: a kit written by an
    older firmware, or half overwritten by a power cut, has to load as a
    kit with some empty tracks rather than raise into the main loop.
    """
    data = store.load(name)
    stored = data.get("paths")
    if not isinstance(stored, (list, tuple)):
        stored = []
    paths = [None] * TRACK_COUNT
    for track in range(min(TRACK_COUNT, len(stored))):
        path = stored[track]
        if isinstance(path, bytes):
            path = path.decode()
        if isinstance(path, str) and path:
            paths[track] = path
    return paths
