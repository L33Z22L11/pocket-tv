idf_component_register(SRCS "src/bsp_display.c" "src/bsp_button.c" "src/bsp_audio.c" "src/bsp_i2c.c"
 INCLUDE_DIRS "include" REQUIRES driver esp_lcd button esp_adc esp_codec_dev esp_driver_i2c esp_driver_i2s)
