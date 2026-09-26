#include "osd.h"
#include <assert.h>
#include <stdio.h>
static unsigned ink(unsigned y0,unsigned y1){unsigned n=0;for(unsigned y=y0;y<y1;y++)for(unsigned x=0;x<320;x++)n+=osd_pixel(x,y,0)==0xffff;return n;}
int main(void){
    osd_prepare(320,240,55,false,true,0,false,false,"");
    for(unsigned y=0;y<240;y++)for(unsigned x=0;x<320;x++)assert(osd_pixel(x,y,0x1234)==0x1234);
    osd_prepare(320,240,55,false,false,0,false,false,"");assert(ink(88,152)>10);assert(ink(208,240)==0);
    osd_prepare(320,240,55,true,true,0,false,false,"");assert(ink(88,152)>200);
    osd_prepare(320,240,55,false,true,0,true,false,"");
    assert(osd_pixel(92,224,0)==0xffff);assert(osd_pixel(169,224,0)==0x7bef);
    osd_prepare(320,240,0,false,true,0,true,false,"");assert(osd_pixel(92,224,0)==0x7bef);
    osd_prepare(320,240,100,false,true,0,true,false,"");assert(osd_pixel(225,224,0)==0xffff);
    osd_prepare(320,240,55,false,true,0,false,true,"湖南卫视 CCTV1");assert(ink(208,240)>80);
    osd_prepare(240,320,55,true,false,0,true,true,"\xe4");
    for(unsigned y=0;y<320;y++)for(unsigned x=0;x<240;x++)(void)osd_pixel(x,y,0);
    puts("OSD tests PASS");
    return 0;
}
