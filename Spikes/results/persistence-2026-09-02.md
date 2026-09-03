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

## A thousand saves, while playing

**measured**, 1,000 save-and-reload cycles with the transport running and the mixer
sounding, each round changing the tempo and a step so a save that quietly did nothing
would show up as a mismatch:

| | |
|---|---|
| rounds | 1,000 |
| save failures | **0** |
| load failures | **0** |
| field mismatches | **0** |
| mean save | 36.1 ms |
| worst save | 57.4 ms |
| **free clusters before** | **905,784** |
| **free clusters after** | **905,784** |
| **leaked** | **0** |
| underruns | **0** |

Clusters are 32 KB here, so a 1.2 KB song occupies one. Allocating a new cluster and
freeing the old one balances exactly over a thousand overwrites, which is the answer to
the question the leak-rather-than-corrupt policy raises: it costs nothing when nothing
goes wrong.

## Two failures that look nothing alike

The first run of this soak reported **zero underruns and 476 late hits out of 826**. Both
numbers are about the same 36 ms, and they are completely different problems.

The audio never noticed. It is on the other core with two blocks of buffer ahead of it,
and the card does not touch either. **The sequencer noticed enormously**: it books hits
about 16 ms ahead and it cannot book anything while the main loop is sitting inside
`sd_write` waiting for the card to finish programming. Fifty-eight percent of the hits in
that run arrived at whatever moment the loop next got a turn.

Raising the lookahead does not fix it. It would have to exceed 36 ms, and at 300 BPM a
1/32 step is 25 ms - a lookahead longer than a step books a track's voices before its
previous hit has finished, which trades late hits for missing ones.

What does fix it is noticing where the time actually goes. Almost all of those 36 ms are
the card holding MISO low while it programs a block, and during that the CPU has nothing
to do. `sd_set_idle_hook` runs the caller's own work in that gap - the sequencer's
scheduler, here.

**measured**, same soak with the hook set:

```
rounds=1000 save_fail=0 load_fail=0 mismatch=0 mean_save_us=36062 worst_save_us=55756
free_before=905784 free_after=905784 leaked=0 underruns=0 late=0 seqhits=824
```

**Late hits: 476 to 0.** Saves cost the same. The work was always there to be done; the
loop was just standing still while the card thought about it.

The general shape is worth keeping: a slow peripheral does not cost time, it costs
*attention*, and the two are only the same if nothing else has a deadline.

## Surviving a reset, and a filesystem that stays sound

**measured**, in two passes driven from a scratch register so it runs unattended: save a
song with distinctive values, audit the filesystem, reset the chip through the watchdog,
then load the song back on the other side.

```
pass=1 mounted=1  saved=ok bpm=211  files=68 crosslinks=0 bad_chains=0
-- port dropped, reconnecting --
pass=2 mounted=1  loaded=ok bpm=211->211 wrong=0  files=68 crosslinks=0 bad_chains=0
```

**Every field identical, across a reset that cleared RAM.** `wrong=0` covers tempo,
division, all eight lengths and mutes, and all 512 steps and offsets.

**Said plainly: a watchdog reset is not a power cycle.** It does not interrupt the card's
supply, and this does not pretend otherwise. What it does establish is that the song came
off the card rather than out of memory, because memory does not survive it. Pulling the
cable mid-write is a different question and needs a person.

The audit is a small fsck over `/songs` and `/samples` - 68 files. For each: follow the
chain to the end and confirm the bytes read match the length the directory claims, and
confirm no file starts on a cluster another file already claimed. A cross-link is what a
bad write ordering produces and it is invisible until something reads the wrong bytes,
which makes it exactly the thing worth checking after a thousand overwrites.

Zero cross-links and zero short chains, before the reset and after it.
