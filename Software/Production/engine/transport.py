"""Play, stop and record state.

Transport is deliberately just state and transitions - it owns no clock, no
audio and no time. The sequencer wires it to the clock, which keeps the rules
here small enough to read and to test exhaustively.

The behaviour it encodes:

* Play starts and stops from anywhere, in any mode. It is the one control that
  always means the same thing.
* Function+Play arms recording. If the pattern is already running, recording
  latches on straight away. If the transport is stopped, it arms both play and
  record and waits: the first pad hit in LIVE both starts the pattern and is
  itself recorded, so a take begins on the beat the player intends rather than
  on a count-in they have to anticipate.
* Recording only ever adds. Nothing is wiped by looping round again.

Waiting to start on a pad hit only makes sense in LIVE, where pads trigger
sounds. In SEQ the pads edit steps instead, so there is nothing to punch in
with and Play starts the pattern normally with record already latched.

This module imports nothing from CircuitPython.
"""

# Record states
OFF = "off"
ARMED = "armed"  # waiting for the first pad hit to start the take
ON = "on"

# Modes the pads can be in, which decides whether a pad hit can punch in.
LIVE = "live"
SEQ = "seq"


class Transport:
    def __init__(self):
        self.playing = False
        self.record = OFF

    # --- queries ----------------------------------------------------------

    @property
    def recording(self):
        """True only while hits are actually being captured."""
        return self.record == ON

    @property
    def armed(self):
        """Waiting for a pad hit to start the take."""
        return self.record == ARMED

    @property
    def stopped(self):
        return not self.playing

    # --- transitions ------------------------------------------------------

    def start(self):
        """Start playing. Returns True if the playhead should reset."""
        if self.playing:
            return False
        self.playing = True
        if self.record == ARMED:
            self.record = ON
        return True

    def stop(self):
        """Stop, and disarm.

        Recording does not survive a stop. Leaving it armed would mean the
        next press of Play silently starts capturing, which is the kind of
        hidden state that ruins a take.
        """
        self.playing = False
        self.record = OFF

    def toggle_play(self):
        """Play button. Returns True if the playhead should reset."""
        if self.playing:
            self.stop()
            return False
        return self.start()

    def toggle_record(self):
        """Function+Play.

        Running: latch recording on, or off again if it was already on.
        Stopped: arm, so the next pad hit in LIVE starts the take. Pressing
        again while armed cancels, rather than leaving the badge waiting.
        """
        if self.playing:
            self.record = OFF if self.record == ON else ON
        else:
            self.record = OFF if self.record == ARMED else ARMED
        return self.record

    def pad_hit(self, mode=LIVE):
        """A pad was struck. Returns True if this hit started the transport.

        Only LIVE can punch in: in SEQ the pads are editing steps, not
        triggering sounds, so there is nothing to record.
        """
        if self.record == ARMED and mode == LIVE and not self.playing:
            self.playing = True
            self.record = ON
            return True
        return False

    def should_capture(self, mode=LIVE):
        """Whether a pad hit right now should be written into the pattern."""
        return self.playing and self.record == ON and mode == LIVE
