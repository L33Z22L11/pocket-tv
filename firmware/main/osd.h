#pragma once
#include <stdbool.h>
#include <stdint.h>
#define OSD_CENTER_ROWS 64
void osd_prepare(unsigned width,unsigned height,unsigned volume,bool help,bool playing,unsigned notice,bool volume_visible,bool channel_visible,const char *channel);
uint16_t osd_pixel(unsigned x,unsigned y,uint16_t background);
