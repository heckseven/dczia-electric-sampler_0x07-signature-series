#ifndef CONSOLE_H
#define CONSOLE_H

/* Long enough for a host to notice the badge and get a byte in, short enough
 * that nobody waits for it. */
#define CONSOLE_WINDOW_MS 3000

/* Long enough that slow work is not killed, short enough that a wedge costs
 * seconds rather than minutes. */
#define CONSOLE_WATCHDOG_MS 8000

void console_begin(const char *name);
void console_pump(void);

/* Anything typed that is not 'B' goes here.
 *
 * For driving the badge from the host when the thing being measured is not the
 * buttons: sync output timing needs a running transport and nothing else, and
 * waiting for someone to press Play makes a measurement that could be taken in
 * a script into one that needs a person in the room. 'B' stays where it is,
 * because a hook that could swallow the way back into BOOTSEL would be a hook
 * that can brick the badge. */
void console_set_command_hook(void (*hook)(char));

#endif /* CONSOLE_H */
