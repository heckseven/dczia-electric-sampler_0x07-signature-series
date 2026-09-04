/* Keys and encoders.
 *
 * The key numbering is the CircuitPython matrix's, because the whole UI design
 * in engine/controls.py is written against it and there is nothing to gain by
 * renumbering: 0-7 are the pads, 8 is Play, 9 is Function, and 10 and 11 are
 * the two encoder pushes.
 *
 * Two things differ from the Python, both for latency.
 *
 * The scan runs every millisecond rather than every twenty. `keypad.KeyMatrix`
 * uses a 20 ms interval, which on its own is twice the whole 10 ms budget for a
 * pad strike - the single largest input-side cost in the current firmware, and
 * it was noted during Phase 0 without being addressed.
 *
 * And a press fires on the leading edge, with a lockout afterwards, rather than
 * waiting for N agreeing samples. Waiting to be sure adds its debounce window
 * to every hit; firing first and then refusing to believe the switch for a few
 * milliseconds rejects the same bounce and costs nothing. Release is debounced
 * the ordinary way, because nobody can hear a late note-off.
 */

#ifndef INPUT_H
#define INPUT_H

#include <stdbool.h>
#include <stdint.h>

#include "board.h"

#define KEY_PAD_FIRST 0
#define KEY_PAD_LAST 7
#define KEY_PLAY 8
#define KEY_FUNCTION 9
#define KEY_SELECT_PUSH 10
#define KEY_VOLUME_PUSH 11

/* Long enough to outlast the bounce on a tactile switch, short enough that a
 * deliberate double-tap still reads as two hits. */
#define KEY_LOCKOUT_MS 8

/* Is this key held right now, read directly rather than through the queue.
 *
 * For the one question that has to be answered before the firmware is properly
 * running: whether to go straight back to the bootloader. See the note at its
 * only caller. */
bool input_read_key_now(uint8_t key);

enum input_event_kind {
    INPUT_NONE = 0,
    INPUT_KEY_DOWN,
    INPUT_KEY_UP,
    INPUT_SELECT_TURN,
    INPUT_VOLUME_TURN,
};

struct input_event {
    enum input_event_kind kind;
    uint8_t key;   /* for KEY_DOWN / KEY_UP */
    int32_t delta; /* detents, for the turns */
};

void input_init(void);

/* Scan once. Cheap enough to call from the main loop every pass. */
void input_poll(void);

/* Take the next event, or false when the queue is empty. */
bool input_next(struct input_event *event);

/* Whether a key is down right now, for the held-modifier gestures. */
bool input_held(uint8_t key);

#endif /* INPUT_H */
