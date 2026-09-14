#include "player.h"
#ifdef ESP_PLATFORM
#include "esp_rom_crc.h"
#endif

uint32_t gp_crc32(const void *data, size_t length) {
#ifdef ESP_PLATFORM
    return esp_rom_crc32_le(0, data, length);
#else
    const uint8_t *bytes = data;
    uint32_t crc = ~0u;
    for (size_t i = 0; i < length; i++) {
        crc ^= bytes[i];
        for (unsigned b = 0; b < 8; b++)
            crc = (crc >> 1) ^ (0xedb88320u & (0u - (crc & 1u)));
    }
    return ~crc;
#endif
}

bool gp_header_valid(const gp_header_t *h) {
    return h->magic == GP_MAGIC && h->type >= GP_HELLO && h->type <= GP_SELECT &&
        h->length <= GP_MAX_PAYLOAD &&
        ((h->type == GP_FRAME || h->type == GP_SINK || h->type == GP_AUDIO) ? h->length > 0 : h->length == 0) &&
        h->header_crc == gp_crc32(h, offsetof(gp_header_t, header_crc));
}

void gp_init(gp_state_t *s) {
    *s = (gp_state_t){.generation=1, .playing=true, .preview=true, .show_hints=true};
}

bool gp_count(gp_state_t *s, uint32_t count) {
    if (!count || count > 10000) return false;
    if (s->count != count) {
        s->count = count;
        s->index %= count;
        s->generation++;
        s->preview = true;
    }
    return true;
}

bool gp_key(gp_state_t *s, unsigned key) {
    if (key == 5) {s->show_hints=!s->show_hints;return true;}
    if (key == 3 || key == 4) {
        if (key == 3 && s->volume < 100) s->volume = s->volume > 95 ? 100 : s->volume + 5;
        if (key == 4 && s->volume > 0) s->volume = s->volume > 5 ? s->volume - 5 : 0;
        return true;
    }
    if (key > 2 || !s->count) return false;
    if (key == 2) s->playing = !s->playing;
    else {
        s->index = (s->index + (key == 0 ? s->count - 1 : 1)) % s->count;
        s->preview = true;
    }
    s->generation++;
    return true;
}

bool gp_can_render(const gp_state_t *s, uint32_t generation) {
    return s->count && s->generation == generation && (s->playing || s->preview);
}

unsigned gp_volume_key(bool up, bool landscape) {
    return up != landscape ? 3 : 4;
}

unsigned gp_channel_key(bool up, bool landscape) {
    return up != landscape ? 0 : 1;
}
bool gp_select(gp_state_t *s, uint32_t index) {
    if (index >= s->count) return false;
    s->index=index;s->generation++;s->preview=true;
    return true;
}

uint16_t gp_overlay_pixel(uint16_t background, bool text_pixel) {
    return text_pixel ? 0xffff : (background & 0xf7deu) >> 1;
}
