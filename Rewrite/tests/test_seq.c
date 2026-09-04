/* Host tests for the pattern and the transport.
 *
 * These run on the build machine, not the badge. The parts worth testing here -
 * which step owns which tick, how offsets place a hit, whether tracks of
 * different lengths stay independent - are pure arithmetic, and arithmetic is
 * far better checked by enumerating every case than by listening to it.
 *
 * The badge measures what only the badge can: latency, jitter, underruns. This
 * measures what a laptop can do exhaustively in a millisecond.
 */

#include <stdio.h>
#include <string.h>

#include "seq.h"
#include "song.h"

/* --- the audio the transport thinks it is talking to ---------------------- */

struct fired {
    uint8_t track;
    uint64_t frame;
    int16_t velocity;
};

static struct fired log[4096];
static uint32_t log_count;
static uint64_t fake_frames;

uint64_t audio_frames(void) {
    return fake_frames;
}

void audio_trigger_at_frame(uint8_t track, uint64_t at_frame,
                            int16_t velocity) {
    if (log_count < 4096) {
        log[log_count].track = track;
        log[log_count].frame = at_frame;
        log[log_count].velocity = velocity;
        log_count++;
    }
}

void audio_stop_all(void) {}

/* The sync jack, stubbed: what matters here is which ticks ask for a pulse and
 * what frame they name, not what the pin does with it. */
static uint64_t sync_at[512];
static uint32_t sync_count;

/* The frame/time mapping, faked as an exact one: the badge's is exact too,
 * being 62.5 us a frame at 16 kHz, and the only thing this stub leaves out is
 * the block-quantised epoch the real one carries. */
bool audio_frame_at_time_us(uint32_t when_us, uint64_t *frame_out) {
    *frame_out = ((uint64_t)when_us * 2u) / 125u;
    return true;
}

static uint32_t midi_notes_sent;
static uint32_t midi_clocks;

void midi_send_note_on(uint8_t note, uint8_t velocity) {
    (void)note;
    (void)velocity;
    midi_notes_sent++;
}

void midi_clock_at_frame(uint64_t frame) {
    (void)frame;
    midi_clocks++;
}

void sync_pulse_at_frame(uint64_t frame) {
    if (sync_count < 512) {
        sync_at[sync_count++] = frame;
    }
}

/* --- harness -------------------------------------------------------------- */

static int failures;

static void check(bool ok, const char *what) {
    if (!ok) {
        printf("FAIL %s\n", what);
        failures++;
    }
}

/* Run the transport across `steps` steps, feeding it frames the way the audio
 * core would.
 *
 * Stops just short of the closing boundary. A run of exactly N steps fires N+1
 * times - once at frame zero and once on the boundary that ends it - and
 * counting that as N is a fencepost, not a bug in the transport. Asking for
 * "not quite N steps" makes the expected count unambiguous. */
static void run_steps(struct seq *seq, uint32_t steps) {
    log_count = 0;
    fake_frames = 0;
    seq_start(seq);
    uint64_t per_step = (((uint64_t)SAMPLE_RATE * 60u << 32) /
                         ((uint64_t)seq->song->bpm * PPQN)) *
                        song_ticks_per_step(seq->song);
    uint64_t end = ((per_step * steps) >> 32) - BLOCK_FRAMES;
    while (fake_frames < end) {
        seq_update(seq);
        fake_frames += BLOCK_FRAMES;
    }
}

static uint32_t hits_on(uint8_t track) {
    uint32_t n = 0;
    for (uint32_t i = 0; i < log_count; i++) {
        if (log[i].track == track) {
            n++;
        }
    }
    return n;
}

/* --- tests ---------------------------------------------------------------- */

static void test_every_step_fires_once(void) {
    struct song song;
    struct seq seq;
    song_init(&song);
    song_set_length(&song, 0, 8);
    for (uint32_t s = 0; s < 8; s++) {
        song_set_step(&song, 0, s, VELOCITY_DEFAULT, 0);
    }
    for (uint32_t t = 1; t < TRACK_COUNT; t++) {
        song_set_length(&song, (uint8_t)t, 8);
    }
    seq_init(&seq, &song);

    run_steps(&seq, 16);
    check(hits_on(0) == 16, "eight steps fire twice over two cycles");
}

static void test_polyrhythm(void) {
    /* The point of per-track lengths.
     *
     * Both tracks step at the same rate - length does not change the rate, it
     * changes how long the pattern is before it repeats. So give each one a
     * single hit at its own step 0: the three-step track fires every third
     * step, the four-step track every fourth, and the two land together only
     * once every twelve. That is the polyrhythm, and filling both tracks with
     * hits on every step would hide it behind a coincidence on every beat. */
    struct song song;
    struct seq seq;
    song_init(&song);
    for (uint32_t t = 0; t < TRACK_COUNT; t++) {
        song_set_length(&song, (uint8_t)t, 4);
    }
    song_set_length(&song, 0, 3);
    song_set_length(&song, 1, 4);
    song_set_step(&song, 0, 0, VELOCITY_DEFAULT, 0);
    song_set_step(&song, 1, 0, VELOCITY_DEFAULT, 0);
    seq_init(&seq, &song);

    run_steps(&seq, 12);
    check(hits_on(0) == 4, "a three-step track fires four times in twelve steps");
    check(hits_on(1) == 3, "a four-step track fires three times in twelve steps");

    uint32_t together = 0;
    for (uint32_t i = 0; i < log_count; i++) {
        for (uint32_t j = 0; j < log_count; j++) {
            if (log[i].track == 0 && log[j].track == 1 &&
                log[i].frame == log[j].frame) {
                together++;
            }
        }
    }
    check(together == 1, "the two cycles coincide once in twelve steps");
}

static void test_offsets_move_hits(void) {
    struct song song;
    struct seq seq;
    song_init(&song);
    song_set_length(&song, 0, 4);
    for (uint32_t t = 1; t < TRACK_COUNT; t++) {
        song_set_length(&song, (uint8_t)t, 4);
    }
    /* 1/16 is six ticks, so the legal offset is +/- 2. */
    song_set_step(&song, 0, 0, VELOCITY_DEFAULT, 0);
    song_set_step(&song, 0, 1, VELOCITY_DEFAULT, 2);   /* late */
    song_set_step(&song, 0, 2, VELOCITY_DEFAULT, -2);  /* early */
    song_set_step(&song, 0, 3, VELOCITY_DEFAULT, 0);
    seq_init(&seq, &song);
    /* Offsets only reach the scheduler at less than full quantise strength -
     * which is the default, since an offset is a record of how a hit was
     * played and the knob decides how much of that survives. This test is
     * about the transport honouring an offset, so it turns the knob off. */
    seq.strength = 0;

    check(song_max_offset(&song) == 2, "1/16 allows two ticks either way");

    run_steps(&seq, 4);
    check(hits_on(0) == 4, "every step still fires exactly once with offsets");

    /* The nudged pair should be closer together than a plain step apart: step 1
     * is two ticks late and step 2 is two ticks early, so four ticks separate
     * them where six would without offsets. */
    uint64_t frames[4];
    uint32_t n = 0;
    for (uint32_t i = 0; i < log_count && n < 4; i++) {
        if (log[i].track == 0) {
            frames[n++] = log[i].frame;
        }
    }
    if (n == 4) {
        uint64_t plain = frames[1] - frames[0];
        uint64_t nudged = frames[2] - frames[1];
        check(nudged < plain, "an early step lands sooner after a late one");
    } else {
        check(false, "four hits to compare");
    }
}

static void test_mute_and_velocity(void) {
    struct song song;
    struct seq seq;
    song_init(&song);
    for (uint32_t t = 0; t < TRACK_COUNT; t++) {
        song_set_length(&song, (uint8_t)t, 2);
    }
    song_set_step(&song, 0, 0, VELOCITY_MAX, 0);
    song_set_step(&song, 1, 0, VELOCITY_MAX, 0);
    song.muted[1] = 1;
    seq_init(&seq, &song);

    run_steps(&seq, 4);
    check(hits_on(0) > 0, "an unmuted track fires");
    check(hits_on(1) == 0, "a muted track does not");

    for (uint32_t i = 0; i < log_count; i++) {
        if (log[i].track == 0) {
            check(log[i].velocity > 0x7000, "full velocity is near full scale");
            break;
        }
    }
}

static void test_division_changes_clamp_offsets(void) {
    /* A shorter step means a smaller legal offset, and a hit left beyond the
     * new limit would sit closer to a neighbour's grid line than its own. */
    struct song song;
    song_init(&song);
    song_set_division(&song, DIVISION_QUARTER); /* 24 ticks, +/- 11 */
    song_set_step(&song, 0, 0, VELOCITY_DEFAULT, 11);
    check(song_offset(&song, 0, 0) == 11, "a quarter allows eleven ticks");

    song_set_division(&song, DIVISION_THIRTYSECOND); /* 3 ticks, +/- 1 */
    check(song_max_offset(&song) == 1, "1/32 allows one tick");
    check(song_offset(&song, 0, 0) == 1, "the old offset is clamped, not kept");
}

static void test_recording_finds_the_nearest_step(void) {
    /* Recording asks where the transport is *audibly*, which is behind where it
     * has already booked. These are the cases that decide whether a hit lands
     * on the beat the player heard or the one after it.
     *
     * At 125 BPM a tick is exactly 320 frames (16000 * 60 / (125 * 24)), so the
     * arithmetic here is not itself rounding and the test measures seq_now
     * rather than its own constants. */
    struct song song;
    song_init(&song);
    song_set_bpm(&song, 125); /* 1/16 -> 6 ticks a step, 320 frames a tick */
    struct seq seq;
    seq_init(&seq, &song);

    fake_frames = 0;
    seq_start(&seq);

    const uint64_t frames_per_tick = 320;
    uint32_t step = 99;
    int32_t offset = 99;

    /* Dead on step 3. */
    fake_frames = seq.start_frame + 18 * frames_per_tick;
    check(seq_now(&seq, 0, &step, &offset), "a running transport has a now");
    check(step == 3, "on the boundary of step 3");
    check(offset == 0, "and no offset");

    /* Two ticks late. */
    fake_frames = seq.start_frame + 20 * frames_per_tick;
    seq_now(&seq, 0, &step, &offset);
    check(step == 3, "two ticks late still belongs to step 3");
    check(offset == 2, "recorded as two ticks late");

    /* Two ticks early - the nearest step, not the one just passed. */
    fake_frames = seq.start_frame + 16 * frames_per_tick;
    seq_now(&seq, 0, &step, &offset);
    check(step == 3, "two ticks early belongs to step 3, not step 2");
    check(offset == -2, "recorded as two ticks early");

    /* Past halfway it is the next step, early, rather than this one very
     * late - which is the whole reason for rounding rather than truncating. */
    fake_frames = seq.start_frame + 21 * frames_per_tick;
    seq_now(&seq, 0, &step, &offset);
    check(step == 4, "past halfway it rounds up to step 4");
    check(offset == -3, "as three ticks early");

    /* Half a tick late still counts as on the beat: an offset is a whole
     * number of ticks, so the nearest one is the only answer available. */
    fake_frames = seq.start_frame + 18 * frames_per_tick + 159;
    seq_now(&seq, 0, &step, &offset);
    check(step == 3 && offset == 0, "just under half a tick rounds to the beat");

    /* And it wraps on the track's own length rather than running off. */
    fake_frames = seq.start_frame + 48 * frames_per_tick;
    seq_now(&seq, 0, &step, &offset);
    check(step == 0, "step 8 of an eight-step track wraps to 0");

    seq_stop(&seq);
    check(!seq_now(&seq, 0, &step, &offset), "a stopped transport has no now");
}

static void test_quantize_strength_is_applied_on_playback(void) {
    /* The offset in the song is what the player did; strength decides how much
     * of it survives to the scheduler. Numbers checked against
     * engine/quantize.py's effective_offset, which this reproduces. */
    check(seq_effective_offset(5, STRENGTH_MAX) == 0, "full strength snaps");
    check(seq_effective_offset(5, 0) == 5, "zero strength plays as performed");
    check(seq_effective_offset(5, 10) == 3, "half of 5 rounds up to 3");
    check(seq_effective_offset(-5, 10) == -3, "and symmetrically the other way");
    check(seq_effective_offset(0, 0) == 0, "no offset stays no offset");
    check(seq_effective_offset(2, 15) == 1, "a quarter of 2 rounds to 1");
    check(seq_effective_offset(1, 19) == 0, "a twentieth of 1 rounds to none");

    /* And the transport actually uses it: the same song, unmodified, schedules
     * its hit at two different frames under two strengths. */
    struct song song;
    song_init(&song);
    song_set_length(&song, 0, 1);
    song_set_step(&song, 0, 0, VELOCITY_DEFAULT, 2); /* two ticks late */
    for (uint32_t t = 1; t < TRACK_COUNT; t++) {
        song.lengths[t] = 0;
    }

    struct seq seq;
    seq_init(&seq, &song);
    check(seq.strength == STRENGTH_DEFAULT, "starts fully quantised");

    run_steps(&seq, 4);
    check(log_count > 0, "the pattern fires at full strength");
    uint64_t snapped = log[0].frame;

    seq_stop(&seq);
    seq.strength = 0;
    run_steps(&seq, 4);
    check(log_count > 0, "and at zero strength");
    uint64_t as_played = log[0].frame;

    check(as_played > snapped, "at zero strength the hit keeps its late offset");
    check(song_offset(&song, 0, 0) == 2, "and the song itself never changed");
}

static void test_sync_out_lands_on_the_right_ticks(void) {
    /* A pulse has to sit on a tick the sequencer actually visits, and it has to
     * name the frame the tick is *heard* on rather than the moment it was
     * booked - the sequencer runs a lookahead ahead of the audio, so those are
     * sixteen milliseconds apart. */
    struct song song;
    song_init(&song);
    for (uint32_t t = 0; t < TRACK_COUNT; t++) {
        song.lengths[t] = 0; /* nothing playing; this is about the clock */
    }
    struct seq seq;
    seq_init(&seq, &song);
    check(seq.sync_ppqn == SYNC_PPQN_DEFAULT, "defaults to the Volca rate");

    /* Four beats at 2 PPQN is eight pulses, one every twelve ticks. */
    sync_count = 0;
    run_steps(&seq, 16); /* 16 sixteenths = 4 beats */
    check(sync_count == 8, "two pulses a beat at the default rate");

    /* The first is on the downbeat, and that is the transport's start frame -
     * not the frame the main loop happened to call seq_update on. */
    check(sync_at[0] == seq.start_frame, "the first pulse is the downbeat");
    check(sync_at[0] > 0, "which is ahead of where the audio already was");

    /* Evenly spaced, at exactly the half-beat. */
    uint64_t gap = sync_at[1] - sync_at[0];
    for (uint32_t i = 2; i < sync_count; i++) {
        uint64_t this_gap = sync_at[i] - sync_at[i - 1];
        /* A frame either way: a tick boundary is 333.33 frames at 120 BPM and
         * the whole part alternates. */
        int64_t drift = (int64_t)this_gap - (int64_t)gap;
        check(drift <= 1 && drift >= -1, "pulses are evenly spaced");
    }

    /* 24 PPQN is a pulse a tick - MIDI clock and DIN sync. */
    seq_stop(&seq);
    check(seq_set_sync_ppqn(&seq, 24), "24 is a rate the jack speaks");
    sync_count = 0;
    run_steps(&seq, 4); /* 4 sixteenths = 24 ticks */
    check(sync_count == 24, "one pulse a tick at 24 PPQN");

    /* And a rate that does not divide PPQN is refused rather than accepted and
     * quietly rounded - every fifth pulse would land 4.8 ticks along, which is
     * somewhere the sequencer never visits. */
    check(!seq_set_sync_ppqn(&seq, 5), "5 does not divide 24, so it is refused");
    check(seq.sync_ppqn == 24, "and the old rate is kept");
}

/* Microseconds between sync pulses at a given tempo and rate. */
static uint32_t pulse_gap_us(uint32_t bpm, uint32_t ppqn) {
    return (uint32_t)(60000000ull / ((uint64_t)bpm * ppqn));
}

static void test_external_sync_takes_the_tempo(void) {
    struct song song;
    song_init(&song); /* 120 BPM internally */
    for (uint32_t t = 0; t < TRACK_COUNT; t++) {
        song.lengths[t] = 0;
    }
    struct seq seq;
    seq_init(&seq, &song);
    check(!seq.external, "starts on its own clock");
    check(seq_effective_bpm(&seq) == 120, "and its own tempo");

    /* A master at 100 BPM, 2 PPQN - the Volca rate. */
    fake_frames = 0;
    seq_start(&seq);
    uint32_t gap = pulse_gap_us(100, 2);
    uint32_t at = 1000000;
    for (uint32_t i = 0; i < 8; i++) {
        seq_external_pulse(&seq, at, 2);
        at += gap;
        fake_frames = ((uint64_t)at * 2u) / 125u;
    }
    check(seq.external, "one pulse latches it to external");
    uint32_t measured = seq_effective_bpm(&seq);
    check(measured >= 99 && measured <= 101, "and it plays the master's tempo");
    check(song.bpm == 120, "without changing the song's own tempo");

    /* The song's tempo is ignored while synced - that is what synced means. */
    song_set_bpm(&song, 200);
    check(seq_effective_bpm(&seq) == measured, "the song's tempo is overridden");

    /* Stopping lets go, which is engine/clock.py's rule. */
    seq_stop(&seq);
    check(!seq.external, "stopping returns to the internal clock");
    check(seq_effective_bpm(&seq) == 200, "and to the song's tempo");
}

static void test_external_sync_survives_a_bad_cable(void) {
    struct song song;
    song_init(&song);
    for (uint32_t t = 0; t < TRACK_COUNT; t++) {
        song.lengths[t] = 0;
    }
    struct seq seq;
    seq_init(&seq, &song);

    fake_frames = 0;
    seq_start(&seq);
    uint32_t gap = pulse_gap_us(120, 2);
    uint32_t at = 1000000;
    for (uint32_t i = 0; i < 8; i++) {
        seq_external_pulse(&seq, at, 2);
        at += gap;
        fake_frames = ((uint64_t)at * 2u) / 125u;
    }
    uint32_t settled = seq_effective_bpm(&seq);
    check(settled >= 119 && settled <= 121, "settled on the master");

    /* A glitch: two edges a hair apart. Rejected as noise rather than read as
     * a tempo of several thousand BPM. */
    uint32_t before = seq.ext_rejected;
    seq_external_pulse(&seq, at, 2);
    seq_external_pulse(&seq, at + 500, 2); /* half a millisecond later */
    check(seq.ext_rejected > before, "a gap too short to be music is refused");
    check(seq_effective_bpm(&seq) >= 119 && seq_effective_bpm(&seq) <= 121,
          "and the tempo already measured is kept");

    /* The master pauses for two and a half seconds, then comes back. That is
     * inside the range a gap may take, so it is not noise - but it is a
     * restart, not a master that slowed to 12 BPM. The tempo already measured
     * has to survive it. */
    at += 2500000;
    fake_frames = ((uint64_t)at * 2u) / 125u;
    seq_external_pulse(&seq, at, 2);
    check(seq_effective_bpm(&seq) >= 119 && seq_effective_bpm(&seq) <= 121,
          "a pause is a restart, not a tempo of 12 BPM");
    check(seq.running, "and the transport is still running");

    /* A gap beyond the outer bound is refused outright. */
    before = seq.ext_rejected;
    at += 4000000;
    fake_frames = ((uint64_t)at * 2u) / 125u;
    seq_external_pulse(&seq, at, 2);
    check(seq.ext_rejected > before, "a gap past the outer bound is refused");

    /* Back to normal, and it re-synchronises. */
    for (uint32_t i = 0; i < 8; i++) {
        at += gap;
        fake_frames = ((uint64_t)at * 2u) / 125u;
        seq_external_pulse(&seq, at, 2);
    }
    uint32_t again = seq_effective_bpm(&seq);
    check(again >= 119 && again <= 121, "and picks the master back up");
}

static void test_a_pulse_starts_a_stopped_transport(void) {
    struct song song;
    song_init(&song);
    for (uint32_t t = 0; t < TRACK_COUNT; t++) {
        song.lengths[t] = 0;
    }
    struct seq seq;
    seq_init(&seq, &song);
    fake_frames = 1000;
    check(!seq.running, "stopped to begin with");
    seq_external_pulse(&seq, 1000000, 2);
    check(seq.running, "a master sending clock starts the transport");
    check(seq.external, "on the external clock");
}

int main(void) {
    test_every_step_fires_once();
    test_polyrhythm();
    test_offsets_move_hits();
    test_mute_and_velocity();
    test_division_changes_clamp_offsets();
    test_recording_finds_the_nearest_step();
    test_quantize_strength_is_applied_on_playback();
    test_sync_out_lands_on_the_right_ticks();
    test_external_sync_takes_the_tempo();
    test_external_sync_survives_a_bad_cable();
    test_a_pulse_starts_a_stopped_transport();

    if (failures == 0) {
        printf("ok - all sequencer tests passed\n");
        return 0;
    }
    printf("%d failure(s)\n", failures);
    return 1;
}
