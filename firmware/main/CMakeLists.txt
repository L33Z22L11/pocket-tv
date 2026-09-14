idf_component_register(SRCS "main.c" "player.c" INCLUDE_DIRS "."
    REQUIRES bsp esp_driver_usb_serial_jtag esp_timer esp_rom nvs_flash)
