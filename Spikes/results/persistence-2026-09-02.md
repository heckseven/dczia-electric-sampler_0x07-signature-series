# Phase 3: writing to the card - 2026-09-02

## The ordering, which is the whole design

The Python does what it can, and says so:

> *"No atomic replace on this filesystem, so the old file goes first. The window is
> between two directory operations rather than across the whole write."*

That leaves a moment where neither the old file nor the new one exists. In C the
directory entry is ours, so the order can be better:

1. Allocate free clusters and write the data. Nothing existing is touched.
2. Write the chain into every copy of the FAT. Nothing references it yet.
3. Rewrite the directory entry - one 32-byte change inside one 512-byte sector, a single
   block write.
4. Free the old chain.

Power loss before step 3 leaves the old file whole and leaks some clusters. After it, the
new file is whole and the old clusters leak. **At no point does the file not exist.**

Everything that is filesystem structure - FAT entries, directory sectors - is written
through `sd_write_verified`, which reads the block back and compares. A wrong byte there
is not a wrong song, it is a card that will not mount.

## Measured

Save and reload, twice, with values chosen so nothing can look right by accident:

```
round=1 saved=ok loaded=ok save_us=30524 bpm=127->127 failures=0
round=2 saved=ok loaded=ok save_us=30711 bpm=164->164 failures=0
entry=csave.song bytes=1234
entry=ctest.song bytes=1259
```

Two rounds on purpose: the first creates the file, taking the "find free slots" path; the
second overwrites it, taking the "rewrite the entry, free the old chain" path - the one
that can cross-link a filesystem if the ordering is wrong. **Zero mismatches across every
field**, and the directory afterwards holds exactly the two files it should.

A save costs about 30 ms.

## The format is compatible in both directions

Phase 2 proved CircuitPython writes and C reads. This proves the reverse, which is the
half that matters for not stranding anybody's work:

```
LIST  ['csave.song', 'ctest.song']
BPM   164          DIV 1/8T
LEN   [5, 6, 7, 8, 9, 10, 11, 12]
MUTED [False, True, False, True, False, True, False, True]
VOL   [0.5, 0.625, 0.75, 0.875, 1.0, 1.125, 1.25, 1.375]
```

Every value what the C wrote, read by CircuitPython's own `songfile.load`.

## Four bugs, and one of them reached the card

**8.3 truncation, three times over.** `csave.song` is stored as the short name
`CSAVE~1.SON` with a long-name run in front of it. Three separate places compared against
the short form and so never matched: the lookup that decides whether to overwrite, the
delete, and the load. The first of those is the one that did damage - **a second save
created a second directory entry beside the first instead of replacing it**, which is
exactly the corruption this phase exists to avoid, on the player's own card.

Two entries of the same name were removed with a one-off image before going further. The
fix is that all three now match through one long-name-aware locator.

**The float encoder produced exactly twice its input.** The exponent bias was one too
high, so 0.5 was written as 1.0 - and read back as 1.0, which is a plausible track volume
rather than an error. Caught by comparing against the IEEE-754 bit patterns rather than
against the decoder, which agreed with it.

**Long names were built from uninitialised memory.** The leaf buffer was
null-terminated but not zeroed, and the writer reads past the terminator to decide where
padding begins.

The lesson is the same in all four: **every one produced a plausible result rather than an
error.** A duplicated directory entry still lists. A doubled volume still plays. A short
name still saves and loads, as long as nothing else ever looks for the long one.

## Still to do

- Persistence across a power cycle, and the card mounted on a host afterwards.
- A thousand save cycles, then a check for leaked clusters.
- Saving while the transport runs.
- Pulling power mid-write. The one criterion that needs a person and a cable.
