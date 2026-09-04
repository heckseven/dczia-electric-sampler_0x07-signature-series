/* USB descriptors: CDC and MIDI on one device.
 *
 * The SDK's pico_stdio_usb ships its own descriptors, CDC only, in
 * stdio_usb_descriptors.c. They are not weak symbols, so they cannot be
 * overridden one at a time - but all three live in a single object file, and a
 * static library object is only pulled in when it resolves a symbol nothing
 * else has. Defining all three of its callbacks here means that object is never
 * linked and these are used instead.
 *
 * That is the whole trick, and it is worth stating plainly because doing it by
 * halves gives a duplicate-symbol error at link time - which is the good
 * outcome. The bad one is a descriptor that enumerates as something the host
 * only half understands.
 *
 * The CDC half is copied from the SDK's file rather than reworked. It is the
 * console this firmware is flashed and measured through, and it should keep
 * working exactly as it did; MIDI is added after it so the CDC interface
 * numbers and endpoints do not move.
 */

#include "pico/unique_id.h"
#include "tusb.h"

#define USBD_VID (0x2E8A) /* Raspberry Pi */
#define USBD_PID (0x000A) /* Pico SDK CDC, kept so the host tools still match */

#define USBD_MANUFACTURER "DC Zia"
#define USBD_PRODUCT "0x07 Sampler"

/* CDC at interfaces 0 and 1, exactly where the SDK put it. MIDI takes 2 and 3
 * after it. */
#define USBD_ITF_CDC (0)
#define USBD_ITF_MIDI (2)
#define USBD_ITF_MAX (4)

#define USBD_CDC_EP_CMD (0x81)
#define USBD_CDC_EP_OUT (0x02)
#define USBD_CDC_EP_IN (0x82)
#define USBD_CDC_CMD_MAX_SIZE (8)
#define USBD_CDC_IN_OUT_MAX_SIZE (64)

/* Endpoint 3, because 1 and 2 belong to the CDC. An endpoint number used twice
 * in one configuration gives a device that enumerates and then behaves oddly
 * under load, rather than one that fails cleanly. */
#define USBD_MIDI_EP_OUT (0x03)
#define USBD_MIDI_EP_IN (0x83)

#define USBD_STR_0 (0x00)
#define USBD_STR_MANUF (0x01)
#define USBD_STR_PRODUCT (0x02)
#define USBD_STR_SERIAL (0x03)
#define USBD_STR_CDC (0x04)
#define USBD_STR_MIDI (0x05)

#define USBD_DESC_LEN \
    (TUD_CONFIG_DESC_LEN + TUD_CDC_DESC_LEN + TUD_MIDI_DESC_LEN)

static const tusb_desc_device_t usbd_desc_device = {
    .bLength = sizeof(tusb_desc_device_t),
    .bDescriptorType = TUSB_DESC_DEVICE,
    .bcdUSB = 0x0200,
    /* Miscellaneous / IAD: this is two classes at once, and the host needs the
     * interface association descriptors to tell them apart. */
    .bDeviceClass = TUSB_CLASS_MISC,
    .bDeviceSubClass = MISC_SUBCLASS_COMMON,
    .bDeviceProtocol = MISC_PROTOCOL_IAD,
    .bMaxPacketSize0 = CFG_TUD_ENDPOINT0_SIZE,
    .idVendor = USBD_VID,
    .idProduct = USBD_PID,
    .bcdDevice = 0x0100,
    .iManufacturer = USBD_STR_MANUF,
    .iProduct = USBD_STR_PRODUCT,
    .iSerialNumber = USBD_STR_SERIAL,
    .bNumConfigurations = 1,
};

static const uint8_t usbd_desc_cfg[USBD_DESC_LEN] = {
    TUD_CONFIG_DESCRIPTOR(1, USBD_ITF_MAX, USBD_STR_0, USBD_DESC_LEN, 0, 250),

    TUD_CDC_DESCRIPTOR(USBD_ITF_CDC, USBD_STR_CDC, USBD_CDC_EP_CMD,
                       USBD_CDC_CMD_MAX_SIZE, USBD_CDC_EP_OUT, USBD_CDC_EP_IN,
                       USBD_CDC_IN_OUT_MAX_SIZE),

    TUD_MIDI_DESCRIPTOR(USBD_ITF_MIDI, USBD_STR_MIDI, USBD_MIDI_EP_OUT,
                        USBD_MIDI_EP_IN, 64),
};

static char usbd_serial_str[PICO_UNIQUE_BOARD_ID_SIZE_BYTES * 2 + 1];

static const char *const usbd_desc_str[] = {
    [USBD_STR_MANUF] = USBD_MANUFACTURER,
    [USBD_STR_PRODUCT] = USBD_PRODUCT,
    [USBD_STR_SERIAL] = usbd_serial_str,
    [USBD_STR_CDC] = "Board CDC",
    [USBD_STR_MIDI] = "0x07 Sampler MIDI",
};

const uint8_t *tud_descriptor_device_cb(void) {
    return (const uint8_t *)&usbd_desc_device;
}

const uint8_t *tud_descriptor_configuration_cb(uint8_t index) {
    (void)index;
    return usbd_desc_cfg;
}

#define USBD_DESC_STR_MAX (32)

const uint16_t *tud_descriptor_string_cb(uint8_t index, uint16_t langid) {
    (void)langid;
    static uint16_t desc_str[USBD_DESC_STR_MAX];

    /* The serial number is the flash id, which is how the host tools here tell
     * this badge from any other board on the same machine. */
    if (!usbd_serial_str[0]) {
        pico_get_unique_board_id_string(usbd_serial_str,
                                        sizeof(usbd_serial_str));
    }

    uint8_t len;
    if (index == 0) {
        desc_str[1] = 0x0409; /* English */
        len = 1;
    } else {
        if (index >= sizeof(usbd_desc_str) / sizeof(usbd_desc_str[0])) {
            return NULL;
        }
        const char *str = usbd_desc_str[index];
        for (len = 0; len < USBD_DESC_STR_MAX - 1 && str[len]; ++len) {
            desc_str[1 + len] = str[len];
        }
    }

    desc_str[0] = (uint16_t)((TUSB_DESC_STRING << 8) | (2 * len + 2));
    return desc_str;
}
