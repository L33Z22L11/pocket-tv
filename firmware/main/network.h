#pragma once
#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"
#define PTV_TCP_PORT 5760
#define PTV_DISCOVERY_PORT 5761
/* USB-only provisioning: ssid length, password length, padded ssid[32],
   password[64], random pairing key[32]. No credentials in status messages. */
#define PTV_WIFI_CONFIG_SIZE 130
void ptv_network_init(void);
esp_err_t ptv_network_configure(const uint8_t *data, size_t size);
esp_err_t ptv_network_forget(void);
void ptv_network_status(char *ip, size_t size, char id[13], int *error);
/* Called by main's TCP RX task. Sessions prevent fd reuse from routing old ACKs. */
uint32_t ptv_network_accept(void);
int ptv_network_read(uint32_t session, void *data, size_t size);
void ptv_network_send(uint32_t session, const void *data, size_t size);
void ptv_network_close(uint32_t session);

esp_err_t ptv_network_scan_start(void);
esp_err_t ptv_network_scan_result(unsigned index, char *json, size_t size);
