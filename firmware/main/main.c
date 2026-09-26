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
#include "osd.h"
#include "network.h"

/* Single display owner; RX only fills bounded compressed buffers. */
typedef struct {
    gp_header_t h;
    uint8_t data[GP_MAX_PAYLOAD];
    uint32_t rx_us, source;
    int error;
} job_t;
/* One shared packet slot: host waits for ACK, so a second 48 KB slot
   added no throughput. USB and TCP receivers share it with backpressure. */
static job_t jobs[1];
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
static uint32_t owner, reply_source;
static char response_extra[256];
static int64_t last_usb;
static unsigned display_w=240, display_h=320;
static bool landscape;
/* Zlib needs a 32 KiB history, not a full 76.8 KB decoded frame.
   Retain only the bottom area used to restore the optional text overlay. */
static uint8_t inflate_window[32768];
static uint8_t overlay_indices[32*MAX_DISPLAY_W];
static uint8_t center_indices[OSD_CENTER_ROWS*MAX_DISPLAY_W];
static unsigned notice=1; /* LOADING, BUFFERING, RECONNECTING, NO SIGNAL */
static int64_t last_frame;
static uint8_t frame_palette[512];
static unsigned frame_w, frame_h;
static bool frame_valid;
static int64_t volume_until,channel_until;
static char channel_name[128]="POCKET TV";
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
    state.show_hints=false; /* Help is transient, never restored from the legacy HUD setting. */
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
    memset(strips[0], 0, display_w*STRIP_ROWS*2);
    for (int y=0; y<display_h; y+=STRIP_ROWS) draw(0,y,display_w,display_h-y<STRIP_ROWS?display_h-y:STRIP_ROWS,strips[0]);
    wait_dma();
}
static void prepare_hints(void) {
    osd_prepare(display_w,display_h,state.volume,state.show_hints,state.playing,notice,
                volume_until!=0,channel_until!=0,channel_name);
}
static void channel_changed(void) {
    snprintf(channel_name,sizeof(channel_name),"CH %03"PRIu32,state.index+1);
    channel_until=INT64_MAX;
}
static uint16_t cached_pixel(unsigned x, unsigned y) {
    if (!frame_valid) return 0;
    unsigned left=(display_w-frame_w)/2, top=(display_h-frame_h)/2;
    if (x<left || x>=left+frame_w || y<top || y>=top+frame_h) return 0;
    unsigned index=overlay_indices[(y-(display_h-32))*display_w+x]*2;
    return ((uint16_t)frame_palette[index]<<8)|frame_palette[index+1];
}
static uint16_t center_pixel(unsigned x, unsigned y) {
    if (!frame_valid) return 0;
    unsigned left=(display_w-frame_w)/2, top=(display_h-frame_h)/2;
    if (x<left || x>=left+frame_w || y<top || y>=top+frame_h) return 0;
    unsigned i=center_indices[(y-(display_h/2-32))*display_w+x]*2;
    return ((uint16_t)frame_palette[i]<<8)|frame_palette[i+1];
}
static void center_ui(void) {
    prepare_hints();wait_dma();
    for(unsigned row=0;row<OSD_CENTER_ROWS;row+=STRIP_ROWS) {
        unsigned rows=OSD_CENTER_ROWS-row<STRIP_ROWS?OSD_CENTER_ROWS-row:STRIP_ROWS;
        for(unsigned r=0;r<rows;r++)for(unsigned x=0;x<display_w;x++) {
            unsigned y=display_h/2-32+row+r;
            uint16_t color=osd_pixel(x,y,center_pixel(x,y));
            unsigned pos=(r*display_w+x)*2;
            strips[0][pos]=color>>8;strips[0][pos+1]=color;
        }
        draw(0,display_h/2-32+row,display_w,rows,strips[0]);wait_dma();
    }
}
static void set_notice(unsigned value) {
    if(notice!=value){notice=value;center_ui();}
}
static void status_ui(void) {
    prepare_hints();
    center_ui();
    /* Reconstruct only the overlay area from the retained indexed frame.
       No readback or additional full RGB framebuffer, even while paused. */
    wait_dma();
    for (unsigned y=display_h-32;y<display_h;y+=STRIP_ROWS) {
        unsigned rows=display_h-y<STRIP_ROWS?display_h-y:STRIP_ROWS;
        uint8_t *out=strips[0];
        for (unsigned row=0;row<rows;row++) for (unsigned x=0;x<display_w;x++) {
            uint16_t color=osd_pixel(x,y+row,cached_pixel(x,y+row));
            size_t pos=(row*display_w+x)*2;
            out[pos]=color>>8;out[pos+1]=color;
        }
        draw(0,y,display_w,rows,out);wait_dma();
    }
}
static void respond(uint32_t seq, const char *kind, int error, uint32_t usec, uint32_t rx_us, bool shown) {
    static char line[1536];
    char ip[16], id[13];int wifi_error;
    ptv_network_status(ip,sizeof(ip),id,&wifi_error);
    int n=snprintf(line,sizeof(line),
        "{\"device_id\":\"%s\",\"wifi_ip\":\"%s\",\"wifi_error\":%d,\"transport\":\"%s\",\"hints\":%s,\"notice\":%u,\"osd_volume\":%s,\"osd_channel\":%s,\"frame_w\":%u,\"frame_h\":%u,\"audio_queued\":%u,\"audio_played\":%"PRIu32",\"audio_underruns\":%"PRIu32",\"audio_write_errors\":%"PRIu32",\"settings_error\":%d,\"protocol\":\"PTV6\",\"volume\":%"PRIu32",\"landscape\":%s,\"audio_bytes\":%"PRIu32",\"kind\":\"%s\",\"seq\":%"PRIu32",\"generation\":%"PRIu32
        ",\"index\":%"PRIu32",\"count\":%"PRIu32",\"playing\":%s,\"preview\":%s"
        ",\"rendered\":%s,\"frames\":%"PRIu32",\"render_us\":%"PRIu32",\"rx_us\":%"PRIu32
        ",\"error\":%d,\"bad_packets\":%"PRIu32",\"physical_keys\":%"PRIu32
        ",\"heap\":%u,\"min_heap\":%u,\"largest\":%u%s}\n",
        id,ip,wifi_error,owner?"wifi":"usb",state.show_hints?"true":"false",notice,volume_until?"true":"false",channel_until?"true":"false",frame_w,frame_h,(unsigned)audio_queued,audio_played,audio_underruns,audio_write_errors,settings_error,
        state.volume,landscape?"true":"false",audio_bytes,kind,seq,state.generation,state.index,state.count,state.playing?"true":"false",
        state.preview?"true":"false",shown?"true":"false",rendered,usec,rx_us,error,
        bad_packets,state.physical_keys,(unsigned)esp_get_free_heap_size(),
        (unsigned)esp_get_minimum_free_heap_size(),
        (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_8BIT),response_extra);
    if (n>0 && n<(int)sizeof(line)) {
        if(reply_source) ptv_network_send(reply_source,line,n);
        else usb_serial_jtag_write_bytes(line,n,pdMS_TO_TICKS(20));
    }
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
        if(key==2){last_frame=esp_timer_get_time();if(!state.playing)set_notice(0);}
        if (key<2) {notice=1;last_frame=esp_timer_get_time();channel_changed();}
        if(key==3 || key==4)volume_until=esp_timer_get_time()+1800000;
        if(key!=5)error=apply_volume();
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
    frame_valid=false;
    if(w!=previous_w || h!=previous_h) {
        clear_screen();status_ui();previous_w=w;previous_h=h;
    }
    memcpy(frame_palette,p+4,sizeof(frame_palette));
    frame_w=w;frame_h=h;
    unsigned left=(display_w-w)/2, top=(display_h-h)/2;
    size_t input_pos=0, pixels=0, strip_pixels=0;
    unsigned strip=0, strip_y=0, x=left, y=top;
    tinfl_init(&decoder);
    while(true) {
        size_t input=job->h.length-516-input_pos, output=sizeof(inflate_window);
        tinfl_status result=tinfl_decompress(&decoder,p+516+input_pos,&input,
            inflate_window,inflate_window,&output,TINFL_FLAG_PARSE_ZLIB_HEADER);
        input_pos+=input;
        if(result<TINFL_STATUS_DONE || pixels+output>w*h || (!input && !output))break;
        for(size_t i=0;i<output;i++,pixels++) {
            uint8_t index=inflate_window[i];
            if(y>=display_h/2-32 && y<display_h/2+32)center_indices[(y-(display_h/2-32))*display_w+x]=index;
            if(y>=display_h-32)overlay_indices[(y-(display_h-32))*display_w+x]=index;
            unsigned pos=index*2;
            uint16_t color=((uint16_t)frame_palette[pos]<<8)|frame_palette[pos+1];
            color=osd_pixel(x,y,color);
            strips[strip][strip_pixels*2]=color>>8;
            strips[strip][strip_pixels*2+1]=color;
            strip_pixels++;
            if (++x==left+w) {x=left;y++;}
            if(strip_pixels==w*STRIP_ROWS || pixels+1==w*h) {
                draw(left,top+strip_y,w,strip_pixels/w,strips[strip]);
                strip_y+=strip_pixels/w;strip_pixels=0;strip^=1;
            }
        }
        if(result==TINFL_STATUS_DONE) {
            if(pixels!=w*h || input_pos!=job->h.length-516)break;
            wait_dma();frame_valid=true;return 0;
        }
        /* The ring has to wrap only at its 32 KiB boundary. All compressed
           input is present, so NEEDS_MORE_INPUT cannot be a valid continuation. */
        if(result!=TINFL_STATUS_HAS_MORE_OUTPUT || output!=sizeof(inflate_window))break;
    }
    clear_screen();status_ui();return 132;
}
static int receive(uint32_t source,void *data,size_t size) {
    return source?ptv_network_read(source,data,size):usb_serial_jtag_read_bytes(data,size,pdMS_TO_TICKS(20));
}
static bool read_exact(uint32_t source,void *data, size_t size) {
    uint8_t *p=data;
    int64_t deadline=esp_timer_get_time()+2000000;
    while (size && esp_timer_get_time()<deadline) {
        int n=receive(source,p,size);
        if(n<0)return false;
        if (n>0) {p+=n;size-=n;}
    }
    return !size;
}
static void receiver(void *arg) {
    bool tcp=(bool)(uintptr_t)arg;
    uint32_t source=0;
    uint8_t window[sizeof(gp_header_t)];
    size_t used=0;
    int64_t activity=esp_timer_get_time();
    while (true) {
        if(tcp && !source) {source=ptv_network_accept();if(!source)continue;activity=esp_timer_get_time();}
        int n=receive(source,window+used,sizeof(window)-used);
        if(n<0) {if(tcp)ptv_network_close(source);source=0;used=0;continue;}
        if(n==0 && tcp && esp_timer_get_time()-activity>5000000) {ptv_network_close(source);source=0;used=0;}
        if (n<=0) continue;
        activity=esp_timer_get_time();
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
        job->h=h;job->source=source;
        int64_t start=esp_timer_get_time();
        job->error=read_exact(source,job->data,h.length)?0:1;
        job->rx_us=esp_timer_get_time()-start;
        if (!job->error && gp_crc32(job->data,h.length)!=h.crc) job->error=2;
        if(h.type==GP_WIFI_CONFIG && source) job->error=153;
        bool broken=tcp && job->error==1;
        xQueueSend(ready_jobs,&job,portMAX_DELAY);
        if(broken) {ptv_network_close(source);source=0;}
    }
}
void app_main(void) {
    gp_init(&state);
    load_settings();
    dma_done=xSemaphoreCreateBinary();
    keys=xQueueCreate(16,sizeof(unsigned));
    free_jobs=xQueueCreate(1,sizeof(job_t*));
    ready_jobs=xQueueCreate(1,sizeof(job_t*));
    configASSERT(dma_done && keys && free_jobs && ready_jobs);
    for (unsigned i=0;i<2;i++) {
        strips[i]=heap_caps_malloc(MAX_DISPLAY_W*STRIP_ROWS*2,MALLOC_CAP_DMA);
        configASSERT(strips[i]);
    }
    job_t *first=&jobs[0];xQueueSend(free_jobs,&first,0);
    usb_serial_jtag_driver_config_t usb={.tx_buffer_size=4096,.rx_buffer_size=4096};
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
    ptv_network_init();
    configASSERT(xTaskCreate(receiver,"tcp_rx",4096,(void*)1,4,NULL)==pdPASS);
    respond(0,"boot",0,0,0,false);
    while (true) {
        unsigned key;
        int64_t now=esp_timer_get_time();
        if((volume_until && now>=volume_until) || (channel_until && now>=channel_until)) {
            if(now>=volume_until)volume_until=0;
            if(now>=channel_until)channel_until=0;
            status_ui();
        }
        if(connected && state.playing && frame_valid && last_frame) {
            int64_t gap=esp_timer_get_time()-last_frame;
            if(gap>15000000 && notice!=3)set_notice(4);
            else if(gap>2500000 && !notice)set_notice(2);
        }
        reply_source=owner;
        while (xQueueReceive(keys,&key,0)==pdTRUE) apply_key(key,true);
        job_t *job;
        if (xQueueReceive(ready_jobs,&job,pdMS_TO_TICKS(5))!=pdTRUE) {
            if (connected && esp_timer_get_time()-last_contact>3000000) {
                connected=false;audio_flush();set_notice(3);
                if (audio_ready) bsp_audio_set_volume(0);
                status_ui();
            }
            continue;
        }
        bool was_connected=connected;
        reply_source=job->source;
        int error=job->error;
        if(!error && job->h.type==GP_HELLO && (!job->h.argument || job->h.argument>10000))error=3;
        if(!error && job->h.type==GP_HELLO) {
            if(job->source && last_usb && esp_timer_get_time()-last_usb<1000000) error=150;
            else owner=job->source;
        }
        if(!error && job->source!=owner && job->h.type!=GP_STATUS && job->h.type!=GP_WIFI_CONFIG && job->h.type!=GP_WIFI_CLEAR && job->h.type!=GP_WIFI_SCAN && job->h.type!=GP_WIFI_SCAN_RESULT)error=151;
        if(!error && job->source==owner) {
            connected=true;last_contact=esp_timer_get_time();
            if(!owner)last_usb=last_contact;
        }
        bool shown=false;
        uint32_t usec=0;
        if (!error) {
            switch(job->h.type) {
            case GP_CHANNEL_NAME:
                if(job->h.generation==state.generation) {
                    memcpy(channel_name,job->data,job->h.length);channel_name[job->h.length]=0;
                    channel_until=state.preview?INT64_MAX:esp_timer_get_time()+4000000;
                    status_ui();
                }
                break;
            case GP_NOTICE:
                if(job->h.argument>4)error=3;
                else if(job->h.generation==state.generation)set_notice(job->h.argument);
                break;
            case GP_WIFI_SCAN:
                error=job->source?153:ptv_network_scan_start();break;
            case GP_WIFI_SCAN_RESULT:
                error=job->source?153:ptv_network_scan_result(job->h.argument,response_extra,sizeof(response_extra));break;
            case GP_WIFI_CONFIG:
                error=job->source?153:ptv_network_configure(job->data,job->h.length);
                memset(job->data,0,job->h.length);break;
            case GP_WIFI_CLEAR:
                error=job->source?153:ptv_network_forget();break;
            case GP_HELLO:
                if (!gp_count(&state,job->h.argument)) error=3;
                audio_flush();audio_pause(!state.playing);
                state.generation++;
                error=apply_volume();
                state.preview=true; /* reconnect while paused must restore the image */
                notice=1;last_frame=esp_timer_get_time();status_ui();
                break;
            case GP_KEY:
                if (job->h.argument>5) error=3;
                else error=apply_key(job->h.argument,false);
                break;
            case GP_VOLUME:
                if (job->h.argument>100) {error=3;break;}
                state.volume=job->h.argument;volume_until=esp_timer_get_time()+1800000;
                if (!state.volume) audio_flush();
                error=apply_volume();
                {int e=save_setting("volume",state.volume);if (!error) error=e;}
                status_ui(); break;
            case GP_SELECT:
                if (!gp_select(&state,job->h.argument)) {error=3;break;}
                audio_flush();notice=1;last_frame=esp_timer_get_time();channel_changed();status_ui();break;
            case GP_STOP:
                connected=false;audio_flush();set_notice(0);
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
                        shown=true;rendered++;state.preview=false;last_frame=esp_timer_get_time();
                        if(channel_until==INT64_MAX)channel_until=last_frame+4000000;
                        if(notice){notice=0;center_ui();}
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
        response_extra[0]=0;
        xQueueSend(free_jobs,&job,portMAX_DELAY);
    }
}
