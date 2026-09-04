/* TinyUSB configuration: CDC and MIDI together.
 *
 * The SDK ships one of these in pico_stdio_usb/include, and it cannot be used
 * here. Its entire body is wrapped in
 *
 *     #if !defined(LIB_TINYUSB_HOST) && !defined(LIB_TINYUSB_DEVICE)
 *
 * which is exactly the case that does not apply once tinyusb_device is linked
 * directly - and linking it directly is what taking the SDK's CDC driver
 * without its CDC-only descriptors requires. With that file in the include path
 * and the guard failing, CFG_TUD_CDC is never defined, tusb.h leaves out the
 * class headers, and the descriptor macros are simply missing. The error that
 * produces names TUD_CONFIG_DESC_LEN and says nothing about USB at all.
 *
 * So this file replaces it, and says the same things unconditionally, with MIDI
 * added.
 */

#ifndef TUSB_CONFIG_H
#define TUSB_CONFIG_H

#include "pico/stdio_usb.h"

#define CFG_TUSB_RHPORT0_MODE (OPT_MODE_DEVICE)
#define CFG_TUSB_OS (OPT_OS_PICO)

#ifndef CFG_TUSB_MEM_ALIGN
#define CFG_TUSB_MEM_ALIGN __attribute__((aligned(4)))
#endif

#define CFG_TUD_ENDPOINT0_SIZE 64

#define CFG_TUD_CDC 1
#define CFG_TUD_MIDI 1
#define CFG_TUD_MSC 0
#define CFG_TUD_HID 0
#define CFG_TUD_VENDOR 0

#define CFG_TUD_CDC_RX_BUFSIZE 64
#define CFG_TUD_CDC_TX_BUFSIZE 64
#define CFG_TUD_CDC_EP_BUFSIZE 64

/* Two full packets of headroom each way. At MIDI rates - 31250 baud on the
 * wire, and USB is far faster - that is more than the main loop can fall
 * behind by between passes. */
#define CFG_TUD_MIDI_RX_BUFSIZE 128
#define CFG_TUD_MIDI_TX_BUFSIZE 128
#define CFG_TUD_MIDI_EP_BUFSIZE 64

#endif /* TUSB_CONFIG_H */
