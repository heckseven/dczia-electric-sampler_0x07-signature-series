/* Loading samples off the card into the arena.
 *
 * WAV parsing is deliberately strict: the badge's samples are all 16 kHz mono
 * 16-bit PCM, and a file that is not gets refused with a reason rather than
 * played at the wrong speed. Resampling at load time is a real feature, but it
 * is not this phase's, and silently playing a 44.1 kHz file at 16 kHz is the
 * kind of bug that gets blamed on the mixer.
 */

#ifndef KIT_H
#define KIT_H

#include <stdbool.h>
#include <stdint.h>

enum kit_result {
    KIT_OK = 0,
    KIT_NO_FILE,
    KIT_NOT_WAV,
    KIT_WRONG_FORMAT, /* not 16 kHz mono 16-bit PCM */
    KIT_NO_ROOM,      /* the arena is full */
};

/* Read a WAV into the arena and point `track` at it. */
enum kit_result kit_load_track(uint8_t track, const char *path,
                               uint32_t *frames_out);

const char *kit_result_name(enum kit_result result);

#endif /* KIT_H */
