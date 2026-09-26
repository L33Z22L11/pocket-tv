#include "osd.h"
#include "font.h"
#include "channel_font.h"
#include <stdio.h>
#include <string.h>
/* Only text masks live in RAM. Font bitmaps stay in flash. */
static uint8_t mask[96][40];
static unsigned width,height,center_left,center_right,center_top,center_bottom,bottom_left,bottom_right,vol;
static bool center_visible,bottom_visible,volume_visible;
static void pixel(unsigned x,unsigned row){if(x<width && row<96)mask[row][x/8]|=128>>(x%8);}
static void ascii(const char *s,unsigned left,unsigned row){
    for(unsigned i=0;s[i] && left+i*8+8<=width;i++) {
        unsigned c=(unsigned char)s[i];if(c<32 || c>126)continue;
        for(unsigned y=0;y<12;y++)for(unsigned x=0;x<8;x++)
            if(font8[c-32][y]&(128>>x))pixel(left+i*8+x,row+y);
    }
}
static uint32_t utf8(const unsigned char **p){
    unsigned c=*(*p)++;if(c<128)return c;
    unsigned n=(c&0xe0)==0xc0?1:(c&0xf0)==0xe0?2:(c&0xf8)==0xf0?3:0;
    if(!n)return '?';
    uint32_t value=c&((1u<<(6-n))-1);
    for(unsigned i=0;i<n;i++){if((**p&0xc0)!=0x80)return '?';value=(value<<6)|(*(*p)++&63);}
    return value;
}
static unsigned glyph(uint32_t c){
    unsigned lo=0,hi=CHANNEL_GLYPHS;
    while(lo<hi){unsigned mid=(lo+hi)/2;if(channel_codes[mid]<c)lo=mid+1;else hi=mid;}
    return lo<CHANNEL_GLYPHS && channel_codes[lo]==c?lo:CHANNEL_GLYPHS;
}
static unsigned text_width(const char *text){
    const unsigned char *p=(const unsigned char*)text;unsigned result=0;
    while(*p){unsigned g=glyph(utf8(&p));result+=g<CHANNEL_GLYPHS?channel_widths[g]:8;}
    return result;
}
static unsigned text_draw(const char *text,unsigned left,unsigned row){
    const unsigned char *p=(const unsigned char*)text;
    while(*p && left+12<width-12){
        unsigned g=glyph(utf8(&p));
        if(g<CHANNEL_GLYPHS){
            for(unsigned y=0;y<12;y++)for(unsigned x=0;x<12;x++)
                if(channel_bits[g][y]&(0x8000>>x))pixel(left+x,row+y);
            left+=channel_widths[g];
        }else{ascii("-",left,row);left+=8;}
    }
    if(*p && left+8<=width)ascii("-",left,row);
    return left+8;
}
void osd_prepare(unsigned w,unsigned h,unsigned volume,bool help,bool playing,unsigned notice,bool volume_on,bool channel_on,const char *channel){
    width=w;height=h;vol=volume;volume_visible=volume_on;
    memset(mask,0,sizeof(mask));center_visible=help || !playing || notice;
    const char *states[]={"","加载中","缓冲中","重新连接","暂无信号"};
    const char *line=playing?states[notice<=4?notice:4]:"已暂停";
    center_left=(w-(help?216:text_width(line)+24))/2;center_right=w-center_left;
    center_top=help?0:20;center_bottom=help?64:44;
    if(help){
        const char *lines[]={"短按 OK 播放/暂停",w==320?"上键 -  下键 +":"上键 +  下键 -","长按上下键 切换频道","长按 OK 关闭帮助"};
        for(unsigned i=0;i<4;i++)text_draw(lines[i],(w-text_width(lines[i]))/2,4+i*14);
    }else if(center_visible)text_draw(line,(w-text_width(line))/2,26);
    bottom_visible=volume_on || channel_on;
    if(volume_on){
        bottom_left=(w-216)/2;bottom_right=bottom_left+216;
        ascii("VOL",bottom_left+8,74);
        char value[4];snprintf(value,sizeof(value),"%u",volume>100?100:volume);ascii(value,bottom_left+184,74);
    }else if(channel_on){bottom_left=8;bottom_right=text_draw(channel,16,74);}
}
uint16_t osd_pixel(unsigned x,unsigned y,uint16_t color){
    unsigned row;
    if(center_visible && y>=height/2-32+center_top && y<height/2-32+center_bottom && x>=center_left && x<center_right)row=y-(height/2-32);
    else if(bottom_visible && y>=height-28 && y<height-4 && x>=bottom_left && x<bottom_right){
        row=64+y-(height-32);
        if(volume_visible && y>=height-17 && y<height-14 && x>=bottom_left+40 && x<bottom_left+180){
            unsigned bar=(x-bottom_left-40)/7;
            if((x-bottom_left-40)%7<5)return bar*5<vol?0xffff:0x7bef;
        }
    }else return color;
    return (mask[row][x/8]&(128>>(x%8)))?0xffff:(color&0xf7deu)>>1;
}
