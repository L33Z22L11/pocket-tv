#include "player.h"
#include "font.h"
#include <assert.h>
#include <stdio.h>

int main(void) {
    assert(gp_volume_key(true, false) == 3);
    assert(gp_volume_key(false, false) == 4);
    assert(gp_volume_key(true, true) == 4);
    assert(gp_volume_key(false, true) == 3);
    assert(gp_channel_key(true, false) == 0);
    assert(gp_channel_key(false, false) == 1);
    assert(gp_channel_key(true, true) == 0);
    assert(gp_channel_key(false, true) == 1);
    /* Every non-space glyph used in the overlay must have visible pixels. */
    const char *overlay="0123456789/ VOL OK PAUSE PLAY HOLD TO HIDE USB WIFI WAIT + - CHANNEL CLOSE HELP PAUSED LOADING BUFFERING RECONNECTING NO SIGNAL";
    for (const char *c=overlay;*c;c++) {
        if (*c==' ') continue;
        unsigned ink=0;
        for (unsigned r=0;r<12;r++) ink|=font8[(unsigned char)*c-32][r];
        assert(ink);
    }
    gp_state_t s;
    gp_init(&s);
    assert(!gp_key(&s, 0));
    assert(!gp_count(&s, 0));
    assert(gp_count(&s, 17));
    assert(gp_key(&s, 0) && s.index == 16);
    assert(gp_key(&s, 1) && s.index == 0);
    uint32_t old = s.generation;
    s.preview = false;
    assert(gp_key(&s, 2) && !s.playing);
    assert(!gp_can_render(&s, old));
    assert(!gp_can_render(&s, s.generation));
    assert(gp_key(&s, 1) && s.index == 1 && !s.playing);
    assert(gp_can_render(&s, s.generation));
    s.preview = false;
    assert(!gp_can_render(&s, s.generation));
    assert(gp_key(&s, 2) && gp_can_render(&s, s.generation));
    assert(s.volume == 0);
    old = s.generation;
    assert(gp_key(&s, 3) && s.volume == 5 && old == s.generation);
    assert(gp_key(&s, 4) && s.volume == 0);
    assert(gp_key(&s, 4) && s.volume == 0);
    for (int i=0;i<30;i++) assert(gp_key(&s,3));
    assert(s.volume == 100 && s.generation == old);
    old=s.generation;
    bool playing=s.playing;
    assert(!s.show_hints);
    assert(gp_key(&s,5) && s.show_hints && s.generation==old && s.playing==playing);
    assert(gp_key(&s,5) && !s.show_hints && s.generation==old);
    assert(!gp_key(&s,6));
    assert(gp_overlay_pixel(0xffff,false)==0x7bef);
    assert(gp_overlay_pixel(0xf800,false)==0x7800);
    assert(gp_overlay_pixel(0x07e0,false)==0x03e0);
    assert(gp_overlay_pixel(0x001f,false)==0x000f);
    assert(gp_overlay_pixel(0,false)==0);
    assert(gp_overlay_pixel(0,true)==0xffff);
    assert(gp_select(&s, 12) && s.index == 12 && s.preview);
    assert(!gp_select(&s, 17) && s.index == 12);
    assert(gp_crc32("123456789", 9) == 0xcbf43926u);
    gp_header_t h = {.magic=GP_MAGIC, .type=GP_FRAME, .length=GP_MAX_PAYLOAD};
    h.header_crc = gp_crc32(&h, 28);
    assert(gp_header_valid(&h));
    h.length++;
    h.header_crc = gp_crc32(&h, 28);
    assert(!gp_header_valid(&h));
    h.length=32;
    h.header_crc = gp_crc32(&h, 28);
    h.generation++;
    assert(!gp_header_valid(&h));
    h.type=GP_WIFI_CONFIG;h.length=130;
    h.header_crc=gp_crc32(&h,28);assert(gp_header_valid(&h));
    h.length=129;h.header_crc=gp_crc32(&h,28);assert(!gp_header_valid(&h));
    h.type=GP_WIFI_CLEAR;h.length=0;
    h.header_crc=gp_crc32(&h,28);assert(gp_header_valid(&h));
    h.type=GP_WIFI_SCAN;h.header_crc=gp_crc32(&h,28);assert(gp_header_valid(&h));
    h.type=GP_CHANNEL_NAME;h.length=127;h.header_crc=gp_crc32(&h,28);assert(gp_header_valid(&h));
    h.length=128;h.header_crc=gp_crc32(&h,28);assert(!gp_header_valid(&h));
    h.length=0;h.header_crc=gp_crc32(&h,28);assert(!gp_header_valid(&h));
    h.type=GP_NOTICE;h.header_crc=gp_crc32(&h,28);assert(gp_header_valid(&h));
    h.type=GP_WIFI_SCAN_RESULT;h.header_crc=gp_crc32(&h,28);assert(gp_header_valid(&h));
    puts("Player state/protocol: PASS");
}
