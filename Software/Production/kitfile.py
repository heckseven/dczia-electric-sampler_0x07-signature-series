"""Reading and writing kits on the SD card.

A kit is the eight sample paths a song plays, saved apart from the song so
one set of sounds can be used by several patterns. It is a list rather than
an object, so unlike a song there is nothing in the engine to serialise it -
the coercion that keeps a corrupt file from raising lives here.
"""

from engine.song import (
    DEFAULT_TRACK_VOLUME,
    MAX_TRACK_VOLUME,
    MIN_TRACK_VOLUME,
    TRACK_COUNT,
)
from engine.util import clamp
from store import Store, StoreError

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


def save(paths, name, volumes=None):
    """Write the eight sample paths, and how loud each should be.

    The loudness travels with the sounds rather than with the song: a sample
    that is simply too hot is a property of the sample, and fixing it once
    should fix it everywhere that kit is used.
    """
    data = {"paths": [path or "" for path in paths]}
    if volumes is not None:
        data["volumes"] = [float(value) for value in volumes]
    return store.save(data, name)


def load_volumes(name):
    """The kit's loudness baseline, one per track.

    Missing or unreadable values come back as the default, so a kit written
    before this existed loads as one that simply has no opinion.
    """
    try:
        data = store.load(name)
    except StoreError:
        return [DEFAULT_TRACK_VOLUME] * TRACK_COUNT
    stored = data.get("volumes")
    if not isinstance(stored, (list, tuple)):
        stored = []
    volumes = [DEFAULT_TRACK_VOLUME] * TRACK_COUNT
    for track in range(min(TRACK_COUNT, len(stored))):
        value = stored[track]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        volumes[track] = clamp(float(value), MIN_TRACK_VOLUME, MAX_TRACK_VOLUME)
    return volumes


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
