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
    src/song.c
    src/seq.c
    src/msgpack.c
    src/songfile.c
)
target_include_directories(rt PRIVATE ${CMAKE_CURRENT_LIST_DIR}/include)
pico_generate_pio_header(rt ${CMAKE_CURRENT_LIST_DIR}/src/i2s.pio)
target_link_libraries(rt
    pico_stdlib
    pico_multicore
    hardware_clocks
    hardware_dma
    hardware_pio
    hardware_i2c
    hardware_spi
    hardware_watchdog
)
# CDC only. The badge's UART pins are the MIDI jack, and stdio on them would put
# debug text down a MIDI cable.
pico_enable_stdio_usb(rt 1)
pico_enable_stdio_uart(rt 0)
pico_add_extra_outputs(rt)
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
