# Rewrite, Phase 3: persistence

Phase 1 made it play. Phase 2 made it sequence, with steps landing inside a microsecond
of where they were asked for. Neither can save anything, so every pattern the player
writes dies at power-off. That is the gap this phase closes, and it is the last one
between the rewrite and something usable as an instrument rather than a demonstration.

## Why this is its own phase

It needs FAT32 **writes**, on the card holding the player's work.

Phase 1's storage layer is read-only on purpose. A reader that misunderstands a structure
returns a wrong song; a writer that misunderstands one corrupts a filesystem, and the
first anybody knows of it is a card that will not mount. Writing means allocating
clusters, updating two copies of the FAT, and rewriting directory entries - three
structures that have to agree, on a device that can lose power between any two sectors.

## The safety argument, which is the point of the phase

The Python does what it can:

> *"No atomic replace on this filesystem, so the old file goes first. The window is
> between two directory operations rather than across the whole write."*

That leaves a window where neither the old file nor the new one exists. In C the
directory entry is ours to write, so the ordering can be better than that:

1. **Allocate free clusters and write the data into them.** Nothing existing is touched.
   The old file is still whole and still referenced.
2. **Write the chain into both FAT copies.** Still nothing references the new clusters, so
   they are merely reserved.
3. **Rewrite the existing directory entry** to point at the new first cluster and the new
   length. One 32-byte change inside one 512-byte sector - a single block write, which is
   the smallest unit this device can fail to complete.
4. **Free the old chain.**

Power loss before step 3 leaves the old file intact and some clusters leaked. Power loss
after it leaves the new file intact and the old clusters leaked. **At no point does the
file not exist**, and no failure produces a cross-linked or truncated filesystem.

Leaked clusters are the accepted cost. They waste space until the card is checked, and
they are the failure a player can live with - unlike the alternatives.

## Scope

**In:** single-block writes to the card; cluster allocation; song save in the format the
Python reads; kit save; `settings.prefs`; and delete and rename, which the menu needs.

**Out:** directory creation, files growing past their initial allocation, long-name
*writing* (a new file gets an 8.3 name it can be found by), and any write while the
transport is running - Phase 0 measured a flash erase at 35 ms with the UI frozen, and an
SD write is the same shape of problem.

**Explicitly not in scope: FAT12 or FAT16.** The card is FAT32 and the reader already
refuses anything else.

## Success criteria

1. A song saved by the C firmware loads in **CircuitPython** - the reverse of Phase 2's
   test, and the one that proves the format is genuinely shared rather than merely
   readable.
2. A song saved, the badge power-cycled, and the song loaded back identical.
3. Overwriting an existing song leaves the card mountable, checked by mounting it on the
   host and reading the file back.
4. A thousand save cycles without the filesystem degrading - `fsck` clean at the end.
5. Saving while audio plays does not underrun. Phase 0's flash result says the audio path
   survives 35 ms of frozen UI; this checks the same for the card.
6. Interrupting a save - power pulled mid-write - leaves either the old song or the new
   one, never neither. The one criterion that needs a person to pull the cable.

## Order of work

1. `sd_write`, single block, with read-back verification behind a flag.
2. Cluster allocation and the FAT chain, both copies.
3. Directory entry rewrite - the ordering above is the whole design.
4. A msgpack writer, matching what `store.Store` produces closely enough that
   CircuitPython reads it.
5. Song save, then kit and prefs.
6. The soak, and the power-pull test.
