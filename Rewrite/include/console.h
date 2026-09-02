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

#endif /* CONSOLE_H */
