#include "network.h"
#include <string.h>
#include <stdio.h>
#include <errno.h>
#include <stdatomic.h>
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_mac.h"
#include "esp_random.h"
#include "nvs.h"
#include "lwip/sockets.h"
#include "lwip/tcp.h"
#include "mbedtls/md.h"

static SemaphoreHandle_t lock;
static uint8_t credentials[PTV_WIFI_CONFIG_SIZE];
static char device_id[13], address[16]="0.0.0.0";
static int wifi_error, listener=-1, peer=-1;
static uint32_t session_id;
static atomic_bool initialized, configured;
static atomic_bool scan_done, scan_active;
static bool scan_loaded;
static uint16_t scan_count;
static wifi_ap_record_t scan_records[20];
static bool init_attempted;
static esp_err_t init_result=ESP_FAIL;
static unsigned key_version;
static esp_netif_t *netif;
static portMUX_TYPE status_lock=portMUX_INITIALIZER_UNLOCKED;

static void status_error(int error) {
    portENTER_CRITICAL(&status_lock);wifi_error=error;portEXIT_CRITICAL(&status_lock);
}
static void on_wifi(void *arg, esp_event_base_t base, int32_t event, void *data) {
    (void)arg;
    if (base==WIFI_EVENT && event==WIFI_EVENT_SCAN_DONE) {
        scan_count=sizeof(scan_records)/sizeof(scan_records[0]);
        if(esp_wifi_scan_get_ap_records(&scan_count,scan_records)!=ESP_OK)scan_count=0;
        scan_loaded=true;scan_active=false;scan_done=true;
        if(configured)esp_wifi_connect();
    } else if (base==IP_EVENT && event==IP_EVENT_STA_GOT_IP) {
        const ip_event_got_ip_t *ip=data;
        portENTER_CRITICAL(&status_lock);
        snprintf(address,sizeof(address),IPSTR,IP2STR(&ip->ip_info.ip));wifi_error=0;
        portEXIT_CRITICAL(&status_lock);
    } else if (base==WIFI_EVENT && event==WIFI_EVENT_STA_DISCONNECTED) {
        portENTER_CRITICAL(&status_lock);
        strcpy(address,"0.0.0.0");wifi_error=((wifi_event_sta_disconnected_t*)data)->reason;
        portEXIT_CRITICAL(&status_lock);
        if (configured && !scan_active) esp_wifi_connect();
    } else if (base==WIFI_EVENT && event==WIFI_EVENT_STA_START) {
        if (configured && !scan_active) esp_wifi_connect();
    }
}
static bool digest(const void *key, const void *data, size_t size, uint8_t out[32]) {
    return mbedtls_md_hmac(mbedtls_md_info_from_type(MBEDTLS_MD_SHA256),key,32,data,size,out)==0;
}
static bool equal(const uint8_t *a,const uint8_t *b,size_t n) {
    unsigned diff=0;for(size_t i=0;i<n;i++) diff|=a[i]^b[i];return diff==0;
}
static bool exact(int fd, void *data, size_t size) {
    uint8_t *p=data;
    int64_t deadline=esp_timer_get_time()+2000000;
    while(size) {if(esp_timer_get_time()>deadline)return false;int n=recv(fd,p,size,0);if(n<=0)return false;p+=n;size-=n;}
    return true;
}
/* Authenticated discovery reply: nonce + device id + HMAC, no key on the wire. */
static void discovery(void *unused) {
    (void)unused;
    int fd=socket(AF_INET,SOCK_DGRAM,IPPROTO_UDP);
    struct sockaddr_in local={.sin_family=AF_INET,.sin_port=htons(PTV_DISCOVERY_PORT),.sin_addr.s_addr=htonl(INADDR_ANY)};
    if(fd<0 || bind(fd,(struct sockaddr*)&local,sizeof(local))<0) {if(fd>=0)close(fd);vTaskDelete(NULL);return;}
    while(true) {
        uint8_t request[33],reply[64],key[32];
        struct sockaddr_in remote;socklen_t len=sizeof(remote);
        int n=recvfrom(fd,request,sizeof(request),0,(struct sockaddr*)&remote,&len);
        if(n!=32 || memcmp(request,"PTV6FIND",8) || memcmp(request+8,device_id,12))continue;
        xSemaphoreTake(lock,portMAX_DELAY);bool enabled=configured;memcpy(key,credentials+98,32);xSemaphoreGive(lock);
        if(!enabled)continue;
        memcpy(reply,request,32);
        if(digest(key,request,32,reply+32))sendto(fd,reply,64,0,(struct sockaddr*)&remote,len);
        memset(key,0,sizeof(key));
    }
}
static esp_err_t initialize_wifi(void) {
    if(initialized)return ESP_OK;
    esp_err_t e=esp_netif_init();if(e)return e;
    e=esp_event_loop_create_default();if(e && e!=ESP_ERR_INVALID_STATE)return e;
    netif=esp_netif_create_default_wifi_sta();if(!netif)return ESP_ERR_NO_MEM;
    wifi_init_config_t config=WIFI_INIT_CONFIG_DEFAULT();
    e=esp_wifi_init(&config);if(e)return e;
    e=esp_event_handler_register(WIFI_EVENT,ESP_EVENT_ANY_ID,on_wifi,NULL);if(e)return e;
    e=esp_event_handler_register(IP_EVENT,IP_EVENT_STA_GOT_IP,on_wifi,NULL);if(e)return e;
    e=esp_wifi_set_storage(WIFI_STORAGE_RAM);if(e)return e;
    e=esp_wifi_set_mode(WIFI_MODE_STA);if(e)return e;
    /* Power-save naps produce avoidable jitter on continuous PCM/video. */
    e=esp_wifi_set_ps(WIFI_PS_NONE);if(e)return e;
    if(xTaskCreate(discovery,"discovery",3072,NULL,3,NULL)!=pdPASS)return ESP_ERR_NO_MEM;
    initialized=true;return ESP_OK;
}
static esp_err_t start_wifi(void) {
    if(!init_attempted) {init_attempted=true;init_result=initialize_wifi();}
    return init_result; /* A failed partial init is not repeatedly allocated. */
}
static esp_err_t apply_config(void) {
    esp_err_t e=start_wifi();if(e)return e;
    configured=false;esp_wifi_stop();
    wifi_config_t config={0};
    memcpy(config.sta.ssid,credentials+2,credentials[0]);
    memcpy(config.sta.password,credentials+34,credentials[1]);
    config.sta.threshold.authmode=credentials[1]?WIFI_AUTH_WPA2_PSK:WIFI_AUTH_OPEN;
    config.sta.pmf_cfg.capable=true;
    e=esp_wifi_set_config(WIFI_IF_STA,&config);memset(&config,0,sizeof(config));
    if(e)return e;
    configured=true;return esp_wifi_start();
}
void ptv_network_init(void) {
    lock=xSemaphoreCreateMutex();configASSERT(lock);
    uint8_t mac[6];esp_read_mac(mac,ESP_MAC_WIFI_STA);
    snprintf(device_id,sizeof(device_id),"%02x%02x%02x%02x%02x%02x",mac[0],mac[1],mac[2],mac[3],mac[4],mac[5]);
    nvs_handle_t nvs;
    if(nvs_open("pocket_tv",NVS_READONLY,&nvs)!=ESP_OK)return;
    size_t size=sizeof(credentials);
    esp_err_t e=nvs_get_blob(nvs,"wifi",credentials,&size);nvs_close(nvs);
    if(!e && size==sizeof(credentials) && credentials[0]>0 && credentials[0]<=32 && credentials[1]<=63)status_error(apply_config());
}
esp_err_t ptv_network_configure(const uint8_t *data,size_t size) {
    if(size!=sizeof(credentials) || !data[0] || data[0]>32 || data[1]>63 || (data[1] && data[1]<8))return ESP_ERR_INVALID_ARG;
    nvs_handle_t nvs;esp_err_t e=nvs_open("pocket_tv",NVS_READWRITE,&nvs);if(e)return e;
    e=nvs_set_blob(nvs,"wifi",data,size);if(!e)e=nvs_commit(nvs);nvs_close(nvs);if(e)return e;
    xSemaphoreTake(lock,portMAX_DELAY);
    if(peer>=0)shutdown(peer,SHUT_RDWR);
    memcpy(credentials,data,size);key_version++;xSemaphoreGive(lock);
    e=apply_config();status_error(e);return e;
}
esp_err_t ptv_network_forget(void) {
    nvs_handle_t nvs;esp_err_t e=nvs_open("pocket_tv",NVS_READWRITE,&nvs);if(e)return e;
    e=nvs_erase_key(nvs,"wifi");if(e==ESP_ERR_NVS_NOT_FOUND)e=ESP_OK;if(!e)e=nvs_commit(nvs);nvs_close(nvs);
    if(e)return e;
    xSemaphoreTake(lock,portMAX_DELAY);configured=false;key_version++;memset(credentials,0,sizeof(credentials));if(peer>=0)shutdown(peer,SHUT_RDWR);xSemaphoreGive(lock);
    if(initialized)esp_wifi_stop();
    portENTER_CRITICAL(&status_lock);strcpy(address,"0.0.0.0");wifi_error=0;portEXIT_CRITICAL(&status_lock);
    return ESP_OK;
}
void ptv_network_status(char *ip,size_t size,char id[13],int *error) {
    portENTER_CRITICAL(&status_lock);snprintf(ip,size,"%s",address);*error=wifi_error;portEXIT_CRITICAL(&status_lock);
    memcpy(id,device_id,13);
}
uint32_t ptv_network_accept(void) {
    if(!initialized || !configured) {vTaskDelay(pdMS_TO_TICKS(100));return 0;}
    if(listener<0) {
        listener=socket(AF_INET,SOCK_STREAM,IPPROTO_TCP);
        if(listener<0) {vTaskDelay(pdMS_TO_TICKS(100));return 0;}
        struct sockaddr_in local={.sin_family=AF_INET,.sin_port=htons(PTV_TCP_PORT),.sin_addr.s_addr=htonl(INADDR_ANY)};
        int one=1;setsockopt(listener,SOL_SOCKET,SO_REUSEADDR,&one,sizeof(one));
        if(bind(listener,(struct sockaddr*)&local,sizeof(local))<0 || listen(listener,1)<0) {close(listener);listener=-1;vTaskDelay(pdMS_TO_TICKS(100));return 0;}
    }
    int fd=accept(listener,NULL,NULL);if(fd<0)return 0;
    struct timeval timeout={.tv_sec=1};int one=1;
    setsockopt(fd,SOL_SOCKET,SO_RCVTIMEO,&timeout,sizeof(timeout));setsockopt(fd,SOL_SOCKET,SO_SNDTIMEO,&timeout,sizeof(timeout));setsockopt(fd,IPPROTO_TCP,TCP_NODELAY,&one,sizeof(one));
    uint8_t challenge[48],expected[32],response[32],key[32],signed_data[54],proof[35];
    memcpy(challenge,"PTV6",4);memcpy(challenge+4,device_id,12);esp_fill_random(challenge+16,32);
    xSemaphoreTake(lock,portMAX_DELAY);memcpy(key,credentials+98,32);bool enabled=configured;unsigned version=key_version;xSemaphoreGive(lock);
    memcpy(signed_data,"client",6);memcpy(signed_data+6,challenge,48);
    bool ok=enabled && digest(key,signed_data,sizeof(signed_data),expected) && send(fd,challenge,sizeof(challenge),0)==sizeof(challenge) && exact(fd,response,sizeof(response)) && equal(response,expected,32);
    memcpy(signed_data,"server",6);memcpy(proof,"OK\n",3);
    ok=ok && digest(key,signed_data,sizeof(signed_data),proof+3);
    memset(key,0,sizeof(key));
    if(!ok || send(fd,proof,sizeof(proof),0)!=sizeof(proof)) {close(fd);return 0;}
    xSemaphoreTake(lock,portMAX_DELAY);
    if(version!=key_version || !configured) {xSemaphoreGive(lock);close(fd);return 0;}
    peer=fd;if(++session_id==0)session_id++;uint32_t session=session_id;xSemaphoreGive(lock);
    return session;
}
int ptv_network_read(uint32_t session,void *data,size_t size) {
    int fd=peer;if(session!=session_id || fd<0)return -1;
    int n=recv(fd,data,size,0);
    if(n<0 && (errno==EAGAIN || errno==EWOULDBLOCK))return 0;
    return n<=0?-1:n;
}
void ptv_network_send(uint32_t session,const void *data,size_t size) {
    xSemaphoreTake(lock,portMAX_DELAY);
    if(session==session_id && peer>=0) {
        const uint8_t *p=data;
        while(size) {int n=send(peer,p,size,0);if(n<=0){shutdown(peer,SHUT_RDWR);break;}p+=n;size-=n;}
    }
    xSemaphoreGive(lock);
}
void ptv_network_close(uint32_t session) {
    xSemaphoreTake(lock,portMAX_DELAY);
    if(session==session_id && peer>=0){close(peer);peer=-1;}
    xSemaphoreGive(lock);
}

esp_err_t ptv_network_scan_start(void) {
    esp_err_t e=start_wifi();if(e)return e;
    /* Start STA without configuring credentials. ESP32-C3 scans 2.4 GHz only. */
    scan_active=true;
    e=esp_wifi_start();if(e){scan_active=false;return e;}
    if(configured && strcmp(address,"0.0.0.0")==0)esp_wifi_disconnect();
    scan_done=false;scan_loaded=false;scan_count=0;
    wifi_scan_config_t config={.show_hidden=false,.scan_type=WIFI_SCAN_TYPE_ACTIVE};
    e=esp_wifi_scan_start(&config,false);
    if(e)scan_active=false;
    return e;
}
esp_err_t ptv_network_scan_result(unsigned index,char *json,size_t size) {
    bool done=scan_done;
    if(done && !scan_loaded) {
        scan_count=sizeof(scan_records)/sizeof(scan_records[0]);
        esp_err_t e=esp_wifi_scan_get_ap_records(&scan_count,scan_records);
        if(e){scan_count=0;return e;}
        scan_loaded=true;
    }
    char hex[65]={0};int rssi=0,channel=0,auth=0;
    if(done && index<scan_count) {
        const wifi_ap_record_t *ap=&scan_records[index];
        for(unsigned i=0;i<32 && ap->ssid[i];i++)snprintf(hex+i*2,3,"%02x",ap->ssid[i]);
        rssi=ap->rssi;channel=ap->primary;auth=ap->authmode;
    }
    snprintf(json,size,",\"scan_done\":%s,\"scan_count\":%u,\"scan_ssid_hex\":\"%s\",\"scan_rssi\":%d,\"scan_channel\":%d,\"scan_auth\":%d",
        done?"true":"false",scan_count,hex,rssi,channel,auth);
    return ESP_OK;
}
