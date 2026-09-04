cmake_minimum_required(VERSION 3.13)

# Same toolchain discovery as the Spikes build: nothing is on the system path,
# so both the SDK and the ARM toolchain are pointed at explicitly.
if(NOT DEFINED ENV{PICO_SDK_PATH})
    set(ENV{PICO_SDK_PATH} "$ENV{HOME}/toolchains/pico-sdk")
endif()
if(NOT DEFINED ENV{PICO_TOOLCHAIN_PATH})
    file(GLOB _arm_toolchain "$ENV{HOME}/toolchains/arm-gnu-toolchain-*")
    if(_arm_toolchain)
        list(GET _arm_toolchain 0 _arm_toolchain_first)
        set(ENV{PICO_TOOLCHAIN_PATH} "${_arm_toolchain_first}")
    endif()
endif()

set(PICO_BOARD pico_w)
include($ENV{PICO_SDK_PATH}/external/pico_sdk_import.cmake)

project(dczia_rt C CXX ASM)
set(CMAKE_C_STANDARD 11)
pico_sdk_init()

add_executable(rt
    src/main.c
    src/audio.c
    src/console.c
    src/sd.c
    src/fat.c
    src/kit.c
    src/input.c
    src/display.c
    src/font.c
    src/menu.c
    src/song.c
    src/seq.c
    src/msgpack.c
    src/songfile.c
    src/prefs.c
    src/sync.c
    src/pixels.c
    src/anim.c
    src/midi.c
    src/midi_port.c
    src/usb_descriptors.c
)
target_include_directories(rt PRIVATE ${CMAKE_CURRENT_LIST_DIR}/include)
pico_generate_pio_header(rt ${CMAKE_CURRENT_LIST_DIR}/src/i2s.pio)
pico_generate_pio_header(rt ${CMAKE_CURRENT_LIST_DIR}/src/ws2812.pio)
target_link_libraries(rt
    pico_stdlib
    pico_multicore
    hardware_clocks
    hardware_dma
    hardware_pio
    hardware_i2c
    hardware_spi
    hardware_watchdog
    hardware_uart
)
# CDC *and* MIDI on the one USB device.
#
# pico_enable_stdio_usb would give CDC only: its interface library adds both
# stdio_usb.c and stdio_usb_descriptors.c to the target unconditionally, and the
# descriptor file has no MIDI in it and no way to opt out of it. Since the three
# descriptor callbacks are compiled straight into the target rather than into a
# static library, defining them here as well is a duplicate symbol rather than
# an override.
#
# So the driver is taken and the descriptors are not: stdio_usb.c is added
# directly, src/usb_descriptors.c supplies the callbacks it needs, and
# stdio_usb_init is called by hand because stdio_init_all only knows to call it
# when the library above was linked.
target_sources(rt PRIVATE
    ${PICO_SDK_PATH}/src/rp2_common/pico_stdio_usb/stdio_usb.c
)
# Ours first: include/tusb_config.h has to be found before the SDK's, which
# disables itself when tinyusb_device is linked directly. See that file.
target_include_directories(rt BEFORE PRIVATE
    ${CMAKE_CURRENT_LIST_DIR}/include
    ${PICO_SDK_PATH}/src/rp2_common/pico_stdio_usb/include
)
target_link_libraries(rt
    pico_stdio
    pico_time
    pico_unique_id
    pico_usb_reset
    tinyusb_device
)
# Without this the device never enumerates at all, and nothing says so.
#
# stdio_usb_init only calls tusb_init() when PICO_STDIO_USB_ENABLE_TINYUSB_INIT
# is set, and that defaults to 0 "if the application is not using tinyUSB
# directly" - which linking tinyusb_device above is precisely the signal for.
# The other branch is `assert(tud_inited())`, and asserts are compiled out of a
# release build, so initialisation is skipped in silence: the firmware runs, the
# audio plays, the lights move, and the host sees no device on the bus.
#
# Cost of finding that out the hard way: one badge recovered by holding Function
# while plugging it in.
#
# Both of these default to 0 for the same reason, in the same header, four lines
# apart: "1 if the application is not using tinyUSB directly", and linking
# tinyusb_device is what the SDK reads as using it directly. Setting one and not
# the other gets a device that initialises and is then never serviced - which
# enumerates exactly as badly as one that was never initialised at all.
target_compile_definitions(rt PRIVATE
    PICO_STDIO_USB_ENABLE_TINYUSB_INIT=1
    PICO_STDIO_USB_ENABLE_IRQ_BACKGROUND_TASK=1
)

# A desk-only safety net for changes to the USB configuration.
#
# A firmware that cannot enumerate cannot be told anything: not by flash.py,
# not by the console, not at all. The way back is to hold Function while
# plugging the badge in, and that costs a person walking over to it.
#
# With this on, a build that has not been configured by a host within
# RT_USB_PROBATION_MS of boot returns to the bootloader by itself. That is only
# correct when the badge is known to be plugged into a computer, which is true
# at a bench and false on a battery at a conference - so it is off by default
# and turned on explicitly for the build being experimented with.
option(RT_USB_PROBATION "Reboot to BOOTSEL if USB never enumerates (bench only)" OFF)
if (RT_USB_PROBATION)
    target_compile_definitions(rt PRIVATE RT_USB_PROBATION=1)
    message(STATUS "rt: USB probation ON - this build reboots to BOOTSEL if it does not enumerate")
endif()
# The badge's UART pins are the MIDI jack, and stdio on them would put debug
# text down a MIDI cable.
pico_enable_stdio_uart(rt 0)
pico_add_extra_outputs(rt)
# MIDI alongside the CDC console. TinyUSB defaults CFG_TUD_MIDI to 0 and the
# SDK's tusb_config.h never mentions it, so a command-line define is what turns
# the class on.
target_link_options(rt PRIVATE "-Wl,-Map=${CMAKE_CURRENT_BINARY_DIR}/rt.map")

# --- criterion 2: trigger-to-output, measured at the pin --------------------
#
# Links the shipped audio.c rather than a copy, so the number describes the
# instrument. Never flashed as the instrument: it adds a second PIO program
# whose only job is to watch the data pin.
add_executable(rt_latency
    measure/measure_latency.c
    src/audio.c
    src/console.c
)
target_include_directories(rt_latency PRIVATE ${CMAKE_CURRENT_LIST_DIR}/include)
pico_generate_pio_header(rt_latency ${CMAKE_CURRENT_LIST_DIR}/src/i2s.pio)
pico_generate_pio_header(rt_latency ${CMAKE_CURRENT_LIST_DIR}/measure/watch.pio)
target_link_libraries(rt_latency
    pico_stdlib
    pico_multicore
    hardware_clocks
    hardware_dma
    hardware_pio
    hardware_watchdog
)
pico_enable_stdio_usb(rt_latency 1)
pico_enable_stdio_uart(rt_latency 0)
pico_add_extra_outputs(rt_latency)

# --- criterion 6: the null test, against the shipped mixer and driver -------
add_executable(rt_null
    measure/measure_null.c
    src/audio.c
    src/console.c
    src/display.c
    src/font.c
)
target_include_directories(rt_null PRIVATE ${CMAKE_CURRENT_LIST_DIR}/include)
pico_generate_pio_header(rt_null ${CMAKE_CURRENT_LIST_DIR}/src/i2s.pio)
target_link_libraries(rt_null
    pico_stdlib
    pico_multicore
    hardware_clocks
    hardware_dma
    hardware_i2c
    hardware_pio
    hardware_watchdog
)
pico_enable_stdio_usb(rt_null 1)
pico_enable_stdio_uart(rt_null 0)
pico_add_extra_outputs(rt_null)

# --- Phase 2 criterion 2: sequenced step timing, measured at the pin --------
add_executable(rt_timing
    measure/measure_timing.c
    src/audio.c
    src/console.c
    src/song.c
    src/seq.c
)
target_include_directories(rt_timing PRIVATE ${CMAKE_CURRENT_LIST_DIR}/include)
pico_generate_pio_header(rt_timing ${CMAKE_CURRENT_LIST_DIR}/src/i2s.pio)
pico_generate_pio_header(rt_timing ${CMAKE_CURRENT_LIST_DIR}/measure/watch.pio)
target_link_libraries(rt_timing
    pico_stdlib
    pico_multicore
    hardware_clocks
    hardware_dma
    hardware_pio
    hardware_watchdog
)
pico_enable_stdio_usb(rt_timing 1)
pico_enable_stdio_uart(rt_timing 0)
pico_add_extra_outputs(rt_timing)

# --- Phase 3: the write path, exercised end to end -------------------------
add_executable(rt_save
    measure/measure_save.c
    src/console.c
    src/sd.c
    src/fat.c
    src/song.c
    src/songfile.c
    src/msgpack.c
)
target_include_directories(rt_save PRIVATE ${CMAKE_CURRENT_LIST_DIR}/include)
target_link_libraries(rt_save
    pico_stdlib
    hardware_clocks
    hardware_dma
    hardware_spi
    hardware_watchdog
)
pico_enable_stdio_usb(rt_save 1)
pico_enable_stdio_uart(rt_save 0)
pico_add_extra_outputs(rt_save)

add_executable(rt_clean
    measure/measure_clean.c
    src/console.c
    src/sd.c
    src/fat.c
)
target_include_directories(rt_clean PRIVATE ${CMAKE_CURRENT_LIST_DIR}/include)
target_link_libraries(rt_clean pico_stdlib hardware_clocks hardware_dma
                      hardware_spi hardware_watchdog)
pico_enable_stdio_usb(rt_clean 1)
pico_enable_stdio_uart(rt_clean 0)
pico_add_extra_outputs(rt_clean)

# --- Phase 3: a thousand saves, while playing ------------------------------
add_executable(rt_soak
    measure/measure_soak.c
    src/audio.c
    src/console.c
    src/sd.c
    src/fat.c
    src/song.c
    src/seq.c
    src/songfile.c
    src/msgpack.c
)
target_include_directories(rt_soak PRIVATE ${CMAKE_CURRENT_LIST_DIR}/include)
pico_generate_pio_header(rt_soak ${CMAKE_CURRENT_LIST_DIR}/src/i2s.pio)
target_link_libraries(rt_soak
    pico_stdlib
    pico_multicore
    hardware_clocks
    hardware_dma
    hardware_pio
    hardware_spi
    hardware_watchdog
)
pico_enable_stdio_usb(rt_soak 1)
pico_enable_stdio_uart(rt_soak 0)
pico_add_extra_outputs(rt_soak)

# --- Phase 3: survives a reset, and the filesystem stays sound -------------
add_executable(rt_check
    measure/measure_check.c
    src/console.c
    src/sd.c
    src/fat.c
    src/song.c
    src/songfile.c
    src/msgpack.c
)
target_include_directories(rt_check PRIVATE ${CMAKE_CURRENT_LIST_DIR}/include)
target_link_libraries(rt_check pico_stdlib hardware_clocks hardware_dma
                      hardware_spi hardware_watchdog)
pico_enable_stdio_usb(rt_check 1)
pico_enable_stdio_uart(rt_check 0)
pico_add_extra_outputs(rt_check)

# --- Phase 3: the torn-write test, which needs a person and a cable --------
add_executable(rt_pull
    measure/measure_pull.c
    src/console.c
    src/sd.c
    src/fat.c
    src/song.c
    src/songfile.c
    src/msgpack.c
)
target_include_directories(rt_pull PRIVATE ${CMAKE_CURRENT_LIST_DIR}/include)
target_link_libraries(rt_pull pico_stdlib hardware_clocks hardware_dma
                      hardware_spi hardware_watchdog)
pico_enable_stdio_usb(rt_pull 1)
pico_enable_stdio_uart(rt_pull 0)
pico_add_extra_outputs(rt_pull)

add_executable(rt_scan
    measure/measure_scan.c
    src/console.c
    src/sd.c
    src/fat.c
)
target_include_directories(rt_scan PRIVATE ${CMAKE_CURRENT_LIST_DIR}/include)
target_link_libraries(rt_scan pico_stdlib hardware_clocks hardware_dma
                      hardware_spi hardware_watchdog)
pico_enable_stdio_usb(rt_scan 1)
pico_enable_stdio_uart(rt_scan 0)
pico_add_extra_outputs(rt_scan)

add_executable(rt_prefs
    measure/measure_prefs.c
    src/console.c
    src/sd.c
    src/fat.c
    src/msgpack.c
    src/prefs.c
)
target_include_directories(rt_prefs PRIVATE ${CMAKE_CURRENT_LIST_DIR}/include)
target_link_libraries(rt_prefs pico_stdlib hardware_clocks hardware_dma
                      hardware_spi hardware_watchdog)
pico_enable_stdio_usb(rt_prefs 1)
pico_enable_stdio_uart(rt_prefs 0)
pico_add_extra_outputs(rt_prefs)

add_executable(rt_session
    measure/measure_session.c
    src/console.c
    src/sd.c
    src/fat.c
    src/song.c
    src/songfile.c
    src/msgpack.c
    src/prefs.c
)
target_include_directories(rt_session PRIVATE ${CMAKE_CURRENT_LIST_DIR}/include)
target_link_libraries(rt_session pico_stdlib hardware_clocks hardware_dma
                      hardware_spi hardware_watchdog)
pico_enable_stdio_usb(rt_session 1)
pico_enable_stdio_uart(rt_session 0)
pico_add_extra_outputs(rt_session)
