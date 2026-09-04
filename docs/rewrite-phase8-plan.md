# Rewrite, Phase 8: settings

`SettingsState.py` is 1012 lines, the largest single module in the Python. This
is not a reproduction of it. What is here are the settings that map onto
machinery this firmware already has, arranged the way the rest of the menu is
already arranged.

## Every setting is a list

The menu already has one shape: a title, three rows, Select to move, Play to
take. A setting with three or six legal values *is* a list, and giving it a
bespoke editor would be a second interaction to learn for nothing.

```
SETTINGS
  STEP   1/16          -> the six divisions
  LENGTH 8             -> 1,2,3,4,5,6,7,8,12,16,24,32,64
  LIGHT  10%           -> 1,5,10,25,50
  SYNC   2 PPQ         -> 1,2,4,24
  DELETE SONG          -> the songs on the card
```

Each row carries **what it is currently set to**. That is the whole reason to
have a settings screen rather than more gestures: a glance answers "what is it
now" as well as "what could it be", and a row that only says `STEP` answers
neither.

Doing that means the menu needs to know what the instrument is set to, and it
has no song, no transport and no light strip. So a four-byte `menu_context` is
passed in each frame rather than giving the menu a way to reach into any of
them. Refreshed every frame rather than on each change, because the alternative
is remembering to do it at every place a setting can move - including the
gestures, which never go through the menu at all.

The pattern-length list stops at thirteen entries rather than offering 1 to 64.
A list that long is a minute of turning to cross, and per-track lengths - the
polyrhythms - are still set by Play and the Select knob, where a player already
reaches for them.

## The brightness ceiling is a power limit

The first version of the brightness list went to 100%. `prefs.py` says why that
is wrong, and says it plainly:

> *"Ten pixels at full white is three channels of about 20 mA each, so 600 mA -
> far past what the regulator will give while also running the Pico, the card
> and the amplifier. Fifty percent is five times the default and keeps the worst
> case near 300 mA, which the measured topology can stand. **Not a taste limit;
> do not raise it without measuring.**"*

The rail's only source is the Pico's own regulator, with 0.6 µF of decoupling
and no bulk capacitor anywhere. A brownout there is not a dim panel - it is the
regulator sagging while the SD card is mid-write.

So the list stops at 50, `prefs_decode` clamps on the way in as well as on the
way out - a file edited by hand must not be able to ask for more than the board
can deliver - and a test asserts that nothing outside 1-50 is ever offered.

Brightness is applied *before* anything can light up, so the panel never flashes
at a level the player turned down, even for one frame.

## The settings file, made testable

`brightness` is shared with the Python under that exact key, so both firmwares
mean the same thing by it. The badge reads back `bright=50` from a file the
Python wrote.

Adding a key meant changing the msgpack map header from 2 pairs to 3, and a
header that disagrees with its body is a file no reader recovers from -
including the Python's. So `prefs_encode` and `prefs_decode` were split from
`prefs_save` and `prefs_load`, the same split the song format got and for the
same reason.

**That split found a live bug immediately.** The carry-through for keys this
firmware does not model was copying the encoded value out of the module's own
`buffer` rather than out of the data it had been handed. Those are the same
pointer when `prefs_load` is the only caller, which is why it worked - and it
would have gone on working right up until it did not. The round-trip test failed
on the first run.

Carrying unknown keys through is not decoration. The Python reads every key with
a default, so one dropped on write does not fail to load; it resets, silently,
and looks exactly like nothing happened.

## Tests

Seven suites now. The menu tests were checked by crossing two settings rows to
each other's screens - the failure mode that looks like nothing until the wrong
setting moves - and four assertions failed.

## Not reproduced from SettingsState.py

Kits as saved files, song rename, the MIDI and HID controller screens, and the
screensaver. The first three need machinery that does not exist here yet; the
last needs a display timeout this firmware has no notion of.
