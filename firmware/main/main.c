#include <stdio.h>
#include <string.h>
#include <inttypes.h>
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "driver/usb_serial_jtag.h"
#include "esp_heap_caps.h"
#include "esp_lcd_panel_ops.h"
#include "esp_lcd_panel_io.h"
#include "esp_timer.h"
#include "bsp_audio.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "miniz.h"
#include "bsp_display.h"
#include "bsp_button.h"
#include "bsp_pins.h"
#include "player.h"
#include "font.h"

/* Single display owner; RX only fills bounded compressed buffers. */
typedef struct {
    gp_header_t h;
    uint8_t data[GP_MAX_PAYLOAD];
    uint32_t rx_us;
    int error;
} job_t;
static job_t jobs[2];
#define STRIP_ROWS 30
#define MAX_DISPLAY_W 320
static QueueHandle_t free_jobs, ready_jobs, keys;
static SemaphoreHandle_t dma_done;
static uint8_t *strips[2];
static bool dma_pending;
static gp_state_t state;
static uint32_t rendered, bad_packets;
static int64_t last_contact;
static bool connected;
static unsigned display_w=240, display_h=320;
static bool landscape;
static uint8_t frame_indices[BSP_LCD_W*BSP_LCD_H];
static uint8_t frame_palette[512];
static unsigned frame_w, frame_h;
static bool frame_valid;
static uint8_t hint_mask[32][40];
static volatile bool audio_ready;
static uint32_t audio_bytes;
static int64_t next_frame_start;
static uint32_t timing_generation;

/* 512 ms mono PCM FIFO. A separate task feeds I2S while main renders. */
#define AUDIO_CAPACITY 16384
static uint8_t audio_ring[AUDIO_CAPACITY];
static size_t audio_head, audio_tail;
static volatile size_t audio_queued;
static volatile uint32_t audio_played, audio_underruns, audio_write_errors;
static int64_t audio_start_us;
static bool audio_active, audio_paused;
static int64_t audio_pause_us;
static portMUX_TYPE audio_lock=portMUX_INITIALIZER_UNLOCKED;
static nvs_handle_t settings;
static bool settings_ready;
static int settings_error;

static void audio_flush(void) {
    portENTER_CRITICAL(&audio_lock);
    audio_head=audio_tail=audio_queued=0;audio_active=false;
    portEXIT_CRITICAL(&audio_lock);
}
static void audio_pause(bool paused) {
    portENTER_CRITICAL(&audio_lock);
    if (paused && !audio_paused) audio_pause_us=esp_timer_get_time();
    if (!paused && audio_paused) audio_start_us+=esp_timer_get_time()-audio_pause_us;
    audio_paused=paused;
    portEXIT_CRITICAL(&audio_lock);
}
static int audio_enqueue(const uint8_t *data, size_t length, unsigned delay_ms) {
    int error=0;
    portENTER_CRITICAL(&audio_lock);
    if (length>AUDIO_CAPACITY-audio_queued) error=142;
    else {
        if (!audio_active) {audio_start_us=esp_timer_get_time()+(int64_t)delay_ms*1000;audio_active=true;}
        for (size_t i=0;i<length;i++) {audio_ring[audio_tail]=data[i];audio_tail=(audio_tail+1)%AUDIO_CAPACITY;}
        audio_queued+=length;
    }
    portEXIT_CRITICAL(&audio_lock);
    return error;
}
static void audio_worker(void *arg) {
    (void)arg;
    uint8_t pcm[320];
    bool starving=false;
    while (true) {
        size_t length=0;
        bool active;
        portENTER_CRITICAL(&audio_lock);
        active=audio_ready && audio_active && !audio_paused && esp_timer_get_time()>=audio_start_us;
        if (active) {
            length=audio_queued<sizeof(pcm)?audio_queued:sizeof(pcm);
            for (size_t i=0;i<length;i++) {pcm[i]=audio_ring[audio_head];audio_head=(audio_head+1)%AUDIO_CAPACITY;}
            audio_queued-=length;
            if (!length) audio_active=false;
        }
        portEXIT_CRITICAL(&audio_lock);
        if (!active) {starving=false;vTaskDelay(pdMS_TO_TICKS(2));continue;}
        if (!length) {
            if (!starving) audio_underruns++;
            starving=true;
            memset(pcm,0,sizeof(pcm));
        } else {
            starving=false;
            if (length<sizeof(pcm)) memset(pcm+length,0,sizeof(pcm)-length);
        }
        if (bsp_audio_write(pcm,sizeof(pcm))) audio_write_errors++;
        else audio_played+=length;
    }
}
static void load_settings(void) {
    settings_error=nvs_flash_init();
    if (settings_error) return; /* Never erase unrelated NVS on an init error. */
    settings_error=nvs_open("pocket_tv",NVS_READWRITE,&settings);
    if (settings_error) return;
    settings_ready=true;
    uint8_t value=0;
    esp_err_t e=nvs_get_u8(settings,"volume",&value);
    if (!e && value<=100) state.volume=value;
    else if (e!=ESP_ERR_NVS_NOT_FOUND) settings_error=e;
    uint8_t hints=1;
    e=nvs_get_u8(settings,"hints",&hints);
    if (!e && hints<=1) state.show_hints=hints;
    else if (e!=ESP_ERR_NVS_NOT_FOUND) settings_error=e;
}
static int save_setting(const char *key, uint8_t value) {
    if (!settings_ready) return settings_error?settings_error:ESP_FAIL;
    settings_error=nvs_set_u8(settings,key,value);
    if (!settings_error) settings_error=nvs_commit(settings);
    return settings_error;
}

static bool on_dma(esp_lcd_panel_io_handle_t io, esp_lcd_panel_io_event_data_t *ev, void *ctx) {
    (void)io; (void)ev; (void)ctx;
    BaseType_t wake = pdFALSE;
    xSemaphoreGiveFromISR(dma_done, &wake);
    return wake == pdTRUE;
}
static void wait_dma(void) {
    if (dma_pending) {
        configASSERT(xSemaphoreTake(dma_done, pdMS_TO_TICKS(1000)) == pdTRUE);
        dma_pending = false;
    }
}
static void draw(int x, int y, int w, int h, const void *pixels) {
    wait_dma();
    ESP_ERROR_CHECK(esp_lcd_panel_draw_bitmap(bsp_display_panel(), x, y, x+w, y+h, pixels));
    dma_pending = true;
}
static void clear_screen(void) {
    frame_valid=false;
    wait_dma();
    memset(strips[0], 0, display_w*16*2);
    for (int y=0; y<display_h; y+=16) draw(0,y,display_w,16,strips[0]);
    wait_dma();
}
static void prepare_hints(void) {
    char title[40];
    snprintf(title,sizeof(title),"%02"PRIu32"/%02"PRIu32" vol%"PRIu32" %s %s",
        state.count?state.index+1:0,state.count,state.volume,
        state.playing?"PLAY":"PAUSE",connected?"USB":"WAIT USB");
    const char *lines[2]={title,state.playing?"ok pause  hold to hide":"ok play  hold to hide"};
    memset(hint_mask,0,sizeof(hint_mask));
    for (unsigned line=0;line<2;line++) {
        for (unsigned i=0;lines[line][i] && i<display_w/8;i++) {
            unsigned c=(unsigned char)lines[line][i];
            if (c<32 || c>126) continue;
            for (unsigned row=0;row<12;row++) hint_mask[line*16+row+2][i]=font8[c-32][row];
        }
    }
}
static uint16_t compose_hint(unsigned x, unsigned y, uint16_t color) {
    if (!state.show_hints || y<display_h-32) return color;
    bool glyph=hint_mask[y-(display_h-32)][x/8] & (128>>(x%8));
    return gp_overlay_pixel(color,glyph);
}
static uint16_t cached_pixel(unsigned x, unsigned y) {
    if (!frame_valid) return 0;
    unsigned left=(display_w-frame_w)/2, top=(display_h-frame_h)/2;
    if (x<left || x>=left+frame_w || y<top || y>=top+frame_h) return 0;
    unsigned index=frame_indices[(y-top)*frame_w+x-left]*2;
    return ((uint16_t)frame_palette[index]<<8)|frame_palette[index+1];
}
static void status_ui(void) {
    prepare_hints();
    /* Reconstruct only the overlay area from the retained indexed frame.
       No readback or additional full RGB framebuffer, even while paused. */
    wait_dma();
    for (unsigned y=display_h-32;y<display_h;y+=16) {
        uint8_t *out=strips[0];
        for (unsigned row=0;row<16;row++) for (unsigned x=0;x<display_w;x++) {
            uint16_t color=compose_hint(x,y+row,cached_pixel(x,y+row));
            size_t pos=(row*display_w+x)*2;
            out[pos]=color>>8;out[pos+1]=color;
        }
        draw(0,y,display_w,16,out);wait_dma();
    }
}
static void respond(uint32_t seq, const char *kind, int error, uint32_t usec, uint32_t rx_us, bool shown) {
    char line[896];
    int n=snprintf(line,sizeof(line),
        "{\"hints\":%s,\"frame_w\":%u,\"frame_h\":%u,\"audio_queued\":%u,\"audio_played\":%"PRIu32",\"audio_underruns\":%"PRIu32",\"audio_write_errors\":%"PRIu32",\"settings_error\":%d,\"protocol\":\"PTV5\",\"volume\":%"PRIu32",\"landscape\":%s,\"audio_bytes\":%"PRIu32",\"kind\":\"%s\",\"seq\":%"PRIu32",\"generation\":%"PRIu32
        ",\"index\":%"PRIu32",\"count\":%"PRIu32",\"playing\":%s,\"preview\":%s"
        ",\"rendered\":%s,\"frames\":%"PRIu32",\"render_us\":%"PRIu32",\"rx_us\":%"PRIu32
        ",\"error\":%d,\"bad_packets\":%"PRIu32",\"physical_keys\":%"PRIu32
        ",\"heap\":%u,\"min_heap\":%u,\"largest\":%u}\n",
        state.show_hints?"true":"false",frame_w,frame_h,(unsigned)audio_queued,audio_played,audio_underruns,audio_write_errors,settings_error,
        state.volume,landscape?"true":"false",audio_bytes,kind,seq,state.generation,state.index,state.count,state.playing?"true":"false",
        state.preview?"true":"false",shown?"true":"false",rendered,usec,rx_us,error,
        bad_packets,state.physical_keys,(unsigned)esp_get_free_heap_size(),
        (unsigned)esp_get_minimum_free_heap_size(),
        (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_8BIT));
    if (n>0 && n<(int)sizeof(line)) usb_serial_jtag_write_bytes(line,n,pdMS_TO_TICKS(20));
}
static void key_callback(bsp_btn_t key, bsp_btn_ev_t event, void *ctx) {
    (void)ctx;
    static bool held[3];
    unsigned k;
    if (key == BSP_BTN_OK) {
        if (event == BSP_BTN_CLICK) k=2;
        else if (event == BSP_BTN_LONG) k=5;
        else return;
    } else {
        if (event == BSP_BTN_PRESS) {held[key]=false;return;}
        if (event == BSP_BTN_LONG) {held[key]=true;k=gp_channel_key(key==BSP_BTN_UP,landscape);}
        else if (event == BSP_BTN_RELEASE && !held[key]) k=gp_volume_key(key==BSP_BTN_UP,landscape);
        else return;
    }
    xQueueSend(keys,&k,0);
}
static int apply_volume(void) {
    int error=0;
    if (state.volume && !audio_ready) {
        error=bsp_audio_init();
        if (!error) error=bsp_audio_set_format(16000,16,1);
        if (!error) audio_ready=true;
        else state.volume=0;
    }
    if (audio_ready) {
        bsp_audio_set_volume(connected && state.playing?state.volume:0);
    }
    return error;
}
static int apply_key(unsigned key, bool physical) {
    if (physical) state.physical_keys++;
    int error=0;
    uint32_t old_volume=state.volume;
    if (gp_key(&state,key)) {
        if (key<2 || !state.volume) audio_flush();
        audio_pause(!state.playing);
        if (key<2) clear_screen();
        if (key==5) error=save_setting("hints",state.show_hints);
        else error=apply_volume();
        if (old_volume!=state.volume) {int e=save_setting("volume",state.volume);if (!error) error=e;}
        status_ui();
    }
    respond(0,physical?"key":"injected_key",error,0,0,false);
    return error;
}

static int render_indexed(job_t *job) {
    static unsigned previous_w, previous_h;
    static tinfl_decompressor decoder;
    const uint8_t *p=job->data;
    if (job->h.length<517) return 130;
    unsigned w=p[0]|(p[1]<<8), h=p[2]|(p[3]<<8);
    if (!w || !h || w>display_w || h>display_h) return 131;
    size_t input=job->h.length-516, length=w*h;
    frame_valid=false;
    tinfl_init(&decoder);
    tinfl_status result=tinfl_decompress(&decoder,p+516,&input,frame_indices,frame_indices,&length,
        TINFL_FLAG_PARSE_ZLIB_HEADER|TINFL_FLAG_USING_NON_WRAPPING_OUTPUT_BUF);
    if (result!=TINFL_STATUS_DONE || length!=w*h || input!=job->h.length-516) return 132;
    if (w!=previous_w || h!=previous_h) {
        clear_screen();status_ui();previous_w=w;previous_h=h;
    }
    memcpy(frame_palette,p+4,sizeof(frame_palette));
    frame_w=w;frame_h=h;frame_valid=true;
    unsigned strip=0;
    for (unsigned y=0;y<h;y+=STRIP_ROWS) {
        unsigned rows=h-y<STRIP_ROWS?h-y:STRIP_ROWS;
        uint8_t *out=strips[strip];
        for (unsigned row=0;row<rows;row++) for (unsigned x=0;x<w;x++) {
            unsigned pos=frame_indices[(y+row)*w+x]*2;
            uint16_t color=((uint16_t)frame_palette[pos]<<8)|frame_palette[pos+1];
            color=compose_hint((display_w-w)/2+x,(display_h-h)/2+y+row,color);
            size_t output=(row*w+x)*2;
            out[output]=color>>8;out[output+1]=color;
        }
        draw((display_w-w)/2,(display_h-h)/2+y,w,rows,out);
        strip^=1;
    }
    wait_dma();
    return 0;
}
static bool read_exact(void *data, size_t size) {
    uint8_t *p=data;
    int64_t deadline=esp_timer_get_time()+2000000;
    while (size && esp_timer_get_time()<deadline) {
        int n=usb_serial_jtag_read_bytes(p,size,pdMS_TO_TICKS(20));
        if (n>0) {p+=n;size-=n;}
    }
    return !size;
}
static void receiver(void *arg) {
    (void)arg;
    uint8_t window[sizeof(gp_header_t)];
    size_t used=0;
    while (true) {
        int n=usb_serial_jtag_read_bytes(window+used,sizeof(window)-used,pdMS_TO_TICKS(20));
        if (n<=0) continue;
        used+=n;
        if (used<sizeof(window)) continue;
        gp_header_t h;
        memcpy(&h,window,sizeof(h));
        if (!gp_header_valid(&h)) {
            memmove(window,window+1,--used);
            continue;
        }
        used=0;
        job_t *job;
        xQueueReceive(free_jobs,&job,portMAX_DELAY);
        job->h=h;
        int64_t start=esp_timer_get_time();
        job->error=read_exact(job->data,h.length)?0:1;
        job->rx_us=esp_timer_get_time()-start;
        if (!job->error && gp_crc32(job->data,h.length)!=h.crc) job->error=2;
        xQueueSend(ready_jobs,&job,portMAX_DELAY);
    }
}
void app_main(void) {
    gp_init(&state);
    load_settings();
    dma_done=xSemaphoreCreateBinary();
    keys=xQueueCreate(16,sizeof(unsigned));
    free_jobs=xQueueCreate(2,sizeof(job_t*));
    ready_jobs=xQueueCreate(2,sizeof(job_t*));
    configASSERT(dma_done && keys && free_jobs && ready_jobs);
    for (unsigned i=0;i<2;i++) {
        strips[i]=heap_caps_malloc(MAX_DISPLAY_W*STRIP_ROWS*2,MALLOC_CAP_DMA);
        configASSERT(strips[i]);
        job_t *job=&jobs[i]; xQueueSend(free_jobs,&job,0);
    }
    usb_serial_jtag_driver_config_t usb={.tx_buffer_size=4096,.rx_buffer_size=16384};
    ESP_ERROR_CHECK(usb_serial_jtag_driver_install(&usb));
    ESP_ERROR_CHECK(bsp_display_init());
    esp_lcd_panel_io_callbacks_t callbacks={.on_color_trans_done=on_dma};
    ESP_ERROR_CHECK(esp_lcd_panel_io_register_event_callbacks(bsp_display_io(),&callbacks,NULL));
    clear_screen();
    status_ui();
    bsp_display_backlight(80);
    ESP_ERROR_CHECK(bsp_button_init(key_callback,NULL));
    configASSERT(xTaskCreate(receiver,"usb_rx",4096,NULL,6,NULL)==pdPASS);
    configASSERT(xTaskCreate(audio_worker,"audio_tx",3072,NULL,5,NULL)==pdPASS);
    respond(0,"boot",0,0,0,false);
    while (true) {
        unsigned key;
        while (xQueueReceive(keys,&key,0)==pdTRUE) apply_key(key,true);
        job_t *job;
        if (xQueueReceive(ready_jobs,&job,pdMS_TO_TICKS(5))!=pdTRUE) {
            if (connected && esp_timer_get_time()-last_contact>3000000) {
                connected=false;audio_flush();
                if (audio_ready) bsp_audio_set_volume(0);
                status_ui();
            }
            continue;
        }
        bool was_connected=connected;
        connected=true;last_contact=esp_timer_get_time();
        int error=job->error;
        bool shown=false;
        uint32_t usec=0;
        if (!error) {
            switch(job->h.type) {
            case GP_HELLO:
                if (!gp_count(&state,job->h.argument)) error=3;
                audio_flush();audio_pause(!state.playing);
                state.generation++;
                error=apply_volume();
                state.preview=true; /* reconnect while paused must restore the image */
                clear_screen();status_ui();
                break;
            case GP_KEY:
                if (job->h.argument>5) error=3;
                else error=apply_key(job->h.argument,false);
                break;
            case GP_VOLUME:
                if (job->h.argument>100) {error=3;break;}
                state.volume=job->h.argument;
                if (!state.volume) audio_flush();
                error=apply_volume();
                {int e=save_setting("volume",state.volume);if (!error) error=e;}
                status_ui(); break;
            case GP_SELECT:
                if (!gp_select(&state,job->h.argument)) {error=3;break;}
                audio_flush();clear_screen();status_ui();break;
            case GP_STOP:
                connected=false;audio_flush();
                if (audio_ready) bsp_audio_set_volume(0);
                status_ui();break;
            case GP_LAYOUT:
                if (job->h.argument>1) {error=3;break;}
                if (landscape != (bool)job->h.argument) {
                    wait_dma(); landscape=job->h.argument;
                    ESP_ERROR_CHECK(esp_lcd_panel_swap_xy(bsp_display_panel(),landscape));
                    ESP_ERROR_CHECK(esp_lcd_panel_mirror(bsp_display_panel(),landscape,false));
                    display_w=landscape?320:240;display_h=landscape?240:320;
                    clear_screen(); status_ui();
                }
                break;
            case GP_AUDIO:
                if (state.volume>0 && state.playing && job->h.generation==state.generation) {
                    if (job->h.length>2048 || job->h.length%2) {error=140;break;}
                    if (!audio_ready) {error=141;break;}
                    if (job->h.argument>2000) {error=143;break;}
                    error=audio_enqueue(job->data,job->h.length,job->h.argument);
                    if (!error) audio_bytes+=job->h.length;
                }
                break;
            case GP_FRAME:
                if (timing_generation!=state.generation) {
                    next_frame_start=0;
                    timing_generation=state.generation;
                }
                while (gp_can_render(&state,job->h.generation) &&
                       esp_timer_get_time()<next_frame_start) {
                    if (xQueueReceive(keys,&key,pdMS_TO_TICKS(2))==pdTRUE) apply_key(key,true);
                }
                if (gp_can_render(&state,job->h.generation)) {
                    int64_t start=esp_timer_get_time();
                    unsigned codec=job->h.argument&255;
                    unsigned duration_ms=job->h.argument>>8;
                    if (duration_ms>10000) error=134;
                    else error=codec==1?render_indexed(job):133;
                    usec=esp_timer_get_time()-start;
                    if (!error) {
                        shown=true;rendered++;state.preview=false;
                        next_frame_start=start+(int64_t)duration_ms*1000;
                    }
                }
                break;
            default: break;
            }
        }
        if (!was_connected && job->h.type!=GP_HELLO) status_ui();
        if (error) bad_packets++;
        respond(job->h.sequence,"ack",error,usec,job->rx_us,shown);
        xQueueSend(free_jobs,&job,portMAX_DELAY);
    }
}
