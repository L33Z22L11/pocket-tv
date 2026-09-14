#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define GP_MAGIC 0x35565450u /* PTV5, little endian */
#define GP_MAX_PAYLOAD 49152u
enum { GP_HELLO=1, GP_FRAME=2, GP_STATUS=3, GP_KEY=4, GP_SINK=5, GP_AUDIO=6, GP_VOLUME=7, GP_LAYOUT=8, GP_STOP=9, GP_SELECT=10 };
typedef struct {
    uint32_t magic, type, sequence, generation, argument, length, crc, header_crc;
} gp_header_t;
typedef struct {
    uint32_t count, index, generation, physical_keys, volume;
    bool playing, preview, show_hints;
} gp_state_t;
uint32_t gp_crc32(const void *data, size_t length);
bool gp_header_valid(const gp_header_t *header);
void gp_init(gp_state_t *state);
bool gp_count(gp_state_t *state, uint32_t count);
bool gp_key(gp_state_t *state, unsigned key);
bool gp_can_render(const gp_state_t *state, uint32_t generation);

unsigned gp_volume_key(bool up, bool landscape);

unsigned gp_channel_key(bool up, bool landscape);
bool gp_select(gp_state_t *state, uint32_t index);

uint16_t gp_overlay_pixel(uint16_t background, bool text_pixel);
