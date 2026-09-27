/*
 * SPDX-FileCopyrightText: 2021 Espressif Systems (Shanghai) CO LTD
 * SPDX-License-Identifier: Unlicense OR CC0-1.0
 *
 * Overall behavior:
 * 1) On startup, if the button on GPIO7 is pressed:
 *    - Scan BLE for 5 seconds
 *    - Keep the MAC address of the BLE device with the highest RSSI ≥ -80 dBm
 *    - Store this MAC (6 bytes) in NVS under the key "target_mac"
 *    - Restart the ESP32-C3
 *
 * 2) On reboot (GPIO7 released):
 *    - Wait until GPIO7 is released to avoid immediately triggering the relay
 *    - Load the MAC address stored in NVS into the variable target_addr
 *    - Initialize the BLE HID Host (ESP-HID)
 *    - Try to connect to the device whose address is target_addr
 *    - Then, the main loop manages the relay via GPIO7 and the LED on GPIO8
 */

#include <stdio.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "esp_system.h"
#include "esp_log.h"
#include "nvs_flash.h"
#include "nvs.h"

#include "esp_bt.h"
#include "esp_gap_ble_api.h"
#include "esp_bt_main.h"
#include "esp_bt_defs.h"

#include "esp_hidh.h"
#include "esp_hid_gap.h"

#include "driver/gpio.h"
#include "driver/adc.h"
#include "driver/uart.h"

// --- ADC / LDR ---
#define LDR_ADC_CHANNEL      ADC_CHANNEL_0   // GPIO2 = ADC1_CHANNEL_0
#define ADC_WIDTH            ADC_WIDTH_BIT_12
#define ADC_ATTENUATION      ADC_ATTEN_DB_11 // plage ~0–3,6V

// --- UART ---
#define UART_TX_GPIO         GPIO_NUM_21      
#define UART_BAUD_RATE       115200
#define UART_PORT_NUM        UART_NUM_1

#define ADC_SEND_INTERVAL_MS 200              // fréquence d’envoi UART
#define ADC_SAMPLES          20               // nb d’échantillons pour la moyenne
#define ADC_FILTER_ALPHA     0.2f             // facteur du filtre exponentiel
static void adc_uart_init(void);
static void adc_uart_task(void *param);

static void adc_uart_init(void)
{
    // Configuration ADC
    adc1_config_width(ADC_WIDTH);
    adc1_config_channel_atten(LDR_ADC_CHANNEL, ADC_ATTENUATION);

    // Configuration UART
    uart_config_t uart_config = {
        .baud_rate = UART_BAUD_RATE,
        .data_bits = UART_DATA_8_BITS,
        .parity    = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE
    };
    uart_param_config(UART_PORT_NUM, &uart_config);
    uart_set_pin(UART_PORT_NUM, UART_TX_GPIO, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE);
    uart_driver_install(UART_PORT_NUM, 1024, 0, 0, NULL, 0);
}

////////////////////////////////////////////////////////////////////////////////
// Shared Definitions
////////////////////////////////////////////////////////////////////////////////
#define GPIO_POWER           GPIO_NUM_5   // Relay control to the RPi
#define GPIO_CONTROL         GPIO_NUM_6   // Reads RPi "power good" (0 = RPi off)
#define BUTTON_GPIO          GPIO_NUM_7   // Used as "scan button on boot" then as relay button
#define LED_GPIO             GPIO_NUM_8   // HID LED (inverted logic: 0 = on, 1 = off)
#define GPIO_SHUTDOWN        GPIO_NUM_10  // Triggers RPi shutdown signal
#define SHUTDOWN_COUNT       2            // 2 × 500 ms ≈ 1 s

#define SCAN_DURATION_SEC    5
#define RSSI_THRESHOLD       -80          // Minimum RSSI threshold to keep the advertisement

static const char *TAG_SCAN    = "BLE_SCAN";
static const char *TAG         = "powerpi";

static int8_t      best_rssi = RSSI_THRESHOLD;
static esp_bd_addr_t best_bda;

// For HID Host
static bool device_connected    = false;
static esp_bd_addr_t target_addr;   // Loaded from NVS after initial boot
static esp_hidh_dev_t *g_connected_dev = NULL;

// For Relay and Bluetooth
static bool relay_active     = false;
static bool bluetooth_active = false;
static bool button_lockout   = false; // Prevents multiple quick activations

////////////////////////////////////////////////////////////////////////////////
// BLE Scan and NVS Storage Prototypes
////////////////////////////////////////////////////////////////////////////////
static void gap_cb(esp_gap_ble_cb_event_t event, esp_ble_gap_cb_param_t *param);
static void scan_and_save_mac(void);
static bool load_saved_mac(esp_bd_addr_t out_bda);

////////////////////////////////////////////////////////////////////////////////
// HID Host Prototypes
////////////////////////////////////////////////////////////////////////////////
static void init_hid_host(void);
static void connect_hid_device(void);
static void schedule_hid_reconnect(void);
static void hid_reconnect_task(void *arg);
static void hidh_callback(void *handler_args,
                          esp_event_base_t base,
                          int32_t id,
                          void *event_data);

////////////////////////////////////////////////////////////////////////////////
// RelayControl + Button Prototypes
////////////////////////////////////////////////////////////////////////////////
static void configure_relay_gpio(void);
static bool is_button_pressed(void);
static void activate_relay_sequence(void);

////////////////////////////////////////////////////////////////////////////////
// HID LED Prototype
////////////////////////////////////////////////////////////////////////////////
static void update_led_status(void);

////////////////////////////////////////////////////////////////////////////////
// GAP BLE Callback Implementation
////////////////////////////////////////////////////////////////////////////////
static void gap_cb(esp_gap_ble_cb_event_t event, esp_ble_gap_cb_param_t *param)
{
    if (event == ESP_GAP_BLE_SCAN_RESULT_EVT &&
        param->scan_rst.search_evt == ESP_GAP_SEARCH_INQ_RES_EVT) {
        int8_t       rssi = param->scan_rst.rssi;
        esp_bd_addr_t bda;
        memcpy(bda, param->scan_rst.bda, sizeof(esp_bd_addr_t));

        if (rssi >= RSSI_THRESHOLD) {
            ESP_LOGI(TAG_SCAN,
                     "ADV %02x:%02x:%02x:%02x:%02x:%02x   RSSI=%d",
                     bda[0], bda[1], bda[2],
                     bda[3], bda[4], bda[5],
                     rssi);
            if (rssi > best_rssi) {
                best_rssi = rssi;
                memcpy(best_bda, bda, sizeof(esp_bd_addr_t));
            }
        }
    }
}

////////////////////////////////////////////////////////////////////////////////
// BLE Scan, MAC Storage, and Reboot
////////////////////////////////////////////////////////////////////////////////
static void scan_and_save_mac(void)
{
    best_rssi = RSSI_THRESHOLD;
    memset(best_bda, 0, sizeof(best_bda));

    ESP_LOGI(TAG_SCAN,
             "Starting BLE scan (RSSI ≥ %d) for %d s...",
             RSSI_THRESHOLD,
             SCAN_DURATION_SEC);

    esp_ble_scan_params_t params = {
        .scan_type           = BLE_SCAN_TYPE_ACTIVE,
        .own_addr_type       = BLE_ADDR_TYPE_PUBLIC,
        .scan_filter_policy  = BLE_SCAN_FILTER_ALLOW_ALL,
        .scan_interval       = 0x10,
        .scan_window         = 0x10,
        .scan_duplicate      = BLE_SCAN_DUPLICATE_DISABLE
    };
    esp_ble_gap_set_scan_params(&params);
    esp_ble_gap_start_scanning(SCAN_DURATION_SEC);

    vTaskDelay(pdMS_TO_TICKS(SCAN_DURATION_SEC * 1000 + 200));
    esp_ble_gap_stop_scanning();

    if (best_rssi > RSSI_THRESHOLD) {
        ESP_LOGI(TAG_SCAN,
                 "Best BLE device: %02x:%02x:%02x:%02x:%02x:%02x   RSSI=%d",
                 best_bda[0], best_bda[1], best_bda[2],
                 best_bda[3], best_bda[4], best_bda[5],
                 best_rssi);

        nvs_handle_t nvs_handle;
        esp_err_t err = nvs_open("storage", NVS_READWRITE, &nvs_handle);
        if (err == ESP_OK) {
            err = nvs_set_blob(nvs_handle,
                               "target_mac",
                               best_bda,
                               sizeof(best_bda));
            if (err == ESP_OK) {
                nvs_commit(nvs_handle);
                ESP_LOGI(TAG_SCAN, "MAC saved to NVS.");
            } else {
                ESP_LOGE(TAG_SCAN, "NVS write error: %s", esp_err_to_name(err));
            }
            nvs_close(nvs_handle);
        } else {
            ESP_LOGE(TAG_SCAN, "Cannot open NVS: %s", esp_err_to_name(err));
        }
    } else {
        ESP_LOGI(TAG_SCAN,
                 "No BLE device with RSSI ≥ %d detected.",
                 RSSI_THRESHOLD);
    }

    ESP_LOGI(TAG_SCAN, "Restarting ESP32-C3...");
    vTaskDelay(pdMS_TO_TICKS(500));
    esp_restart();
}

////////////////////////////////////////////////////////////////////////////////
// Load MAC from NVS
////////////////////////////////////////////////////////////////////////////////
static bool load_saved_mac(esp_bd_addr_t out_bda)
{
    nvs_handle_t nvs_handle;
    esp_err_t err = nvs_open("storage", NVS_READONLY, &nvs_handle);
    if (err != ESP_OK) {
        ESP_LOGW(TAG_SCAN, "NVS inaccessible: %s", esp_err_to_name(err));
        return false;
    }
    size_t required = sizeof(esp_bd_addr_t);
    err = nvs_get_blob(nvs_handle, "target_mac", out_bda, &required);
    nvs_close(nvs_handle);
    if (err == ESP_OK && required == sizeof(esp_bd_addr_t)) {
        return true;
    }
    ESP_LOGW(TAG_SCAN, "No MAC found or NVS error: %s", esp_err_to_name(err));
    return false;
}

////////////////////////////////////////////////////////////////////////////////
// ADC + UART Task (avec moyenne + filtre)
////////////////////////////////////////////////////////////////////////////////


static void adc_uart_task(void *param) {
    float filtered_value = 0; // valeur filtrée
    bool first_sample = true;

    while (1) {
        // Moyenne sur N échantillons
        uint32_t sum = 0;
        for (int i = 0; i < ADC_SAMPLES; i++) {
            sum += adc1_get_raw(LDR_ADC_CHANNEL);
        }
        int avg_value = sum / ADC_SAMPLES;

        // Filtre exponentiel
        if (first_sample) {
            filtered_value = avg_value;
            first_sample = false;
        } else {
            filtered_value = (ADC_FILTER_ALPHA * avg_value) +
                             ((1.0f - ADC_FILTER_ALPHA) * filtered_value);
        }

        // Envoi UART
        char buffer[16];
        int len = snprintf(buffer, sizeof(buffer), "%d\n", (int)filtered_value);
        uart_write_bytes(UART_PORT_NUM, buffer, len);

        vTaskDelay(pdMS_TO_TICKS(ADC_SEND_INTERVAL_MS));
    }
}


////////////////////////////////////////////////////////////////////////////////
// HID Host BLE / RelayControl: global declarations
////////////////////////////////////////////////////////////////////////////////

// Global variables
//   - device_connected : true if the HID connection is established
//   - target_addr      : MAC address of the HID device to load from NVS
//   - g_connected_dev  : pointer to the opened HID device

// BLE HID Host prototypes
static void init_hid_host(void);
static void connect_hid_device(void);
static void schedule_hid_reconnect(void);
static void hid_reconnect_task(void *arg);
static void hidh_callback(void *handler_args,
                          esp_event_base_t base,
                          int32_t id,
                          void *event_data);

// RelayControl prototypes
static void configure_relay_gpio(void);
static bool is_button_pressed(void);
static void activate_relay_sequence(void);

// LED HID prototype
static void update_led_status(void);

////////////////////////////////////////////////////////////////////////////////
// BLE HID Host implementation
////////////////////////////////////////////////////////////////////////////////

static void init_hid_host(void)
{
    ESP_LOGI(TAG, "Initializing HID Host (mode %d)", HID_HOST_MODE);
    ESP_ERROR_CHECK(esp_hid_gap_init(HID_HOST_MODE));
#if CONFIG_BT_BLE_ENABLED
    ESP_ERROR_CHECK(esp_ble_gattc_register_callback(
                    esp_hidh_gattc_event_handler));
#endif
    esp_hidh_config_t config = {
        .callback         = hidh_callback,
        .event_stack_size = 4096,
        .callback_arg     = NULL,
    };
    ESP_ERROR_CHECK(esp_hidh_init(&config));
    bluetooth_active = true;
}

static void connect_hid_device(void)
{
    ESP_LOGI(TAG, "Connecting to HID BLE: " ESP_BD_ADDR_STR,
             ESP_BD_ADDR_HEX(target_addr));
    g_connected_dev = esp_hidh_dev_open(target_addr,
                                        ESP_HID_TRANSPORT_BLE,
                                        0);
    if (g_connected_dev == NULL) {
        ESP_LOGE(TAG, "Failed to open HID device");
        device_connected = false;
        update_led_status();
        schedule_hid_reconnect();
    } else {
        device_connected = true;
        update_led_status();
        ESP_LOGI(TAG, "HID device successfully opened");
    }
}

static void schedule_hid_reconnect(void)
{
    if (!device_connected) {
        xTaskCreate(&hid_reconnect_task,
                    "hid_reconnect_task",
                    4096,
                    NULL,
                    5,
                    NULL);
    }
}

static void hid_reconnect_task(void *arg)
{
    while (!device_connected) {
        ESP_LOGI(TAG, "Attempting HID reconnection...");
        g_connected_dev = esp_hidh_dev_open(target_addr,
                                            ESP_HID_TRANSPORT_BLE,
                                            0);
        if (g_connected_dev != NULL) {
            ESP_LOGI(TAG, "Reconnection successful");
            device_connected = true;
            update_led_status();
            break;
        }
        vTaskDelay(pdMS_TO_TICKS(5000));
    }
    vTaskDelete(NULL);
}

static void hidh_callback(void *handler_args,
                          esp_event_base_t base,
                          int32_t id,
                          void *event_data)
{
    esp_hidh_event_t       event = (esp_hidh_event_t)id;
    esp_hidh_event_data_t *param = (esp_hidh_event_data_t *)event_data;

    switch (event) {
        case ESP_HIDH_OPEN_EVENT: {
            if (param->open.status == ESP_OK) {
                device_connected = true;
                update_led_status();
                const uint8_t *bda = esp_hidh_dev_bda_get(
                                        param->open.dev);
                ESP_LOGI(TAG, ESP_BD_ADDR_STR " OPEN: %s",
                         ESP_BD_ADDR_HEX(bda),
                         esp_hidh_dev_name_get(param->open.dev));
                esp_hidh_dev_dump(param->open.dev, stdout);
            } else {
                ESP_LOGE(TAG, "OPEN failed!");
                device_connected = false;
                update_led_status();
            }
            break;
        }
        case ESP_HIDH_BATTERY_EVENT: {
            const uint8_t *bda = esp_hidh_dev_bda_get(
                                        param->battery.dev);
            ESP_LOGI(TAG, ESP_BD_ADDR_STR " BATTERY: %d%%",
                     ESP_BD_ADDR_HEX(bda),
                     param->battery.level);
            break;
        }
        case ESP_HIDH_INPUT_EVENT: {
            ESP_LOGI(TAG, "HID Input received → activating relay sequence");
            if (!relay_active && !button_lockout) {
                button_lockout = true;
                activate_relay_sequence();
            }
            break;
        }
        case ESP_HIDH_FEATURE_EVENT: {
            const uint8_t *bda = esp_hidh_dev_bda_get(
                                        param->feature.dev);
            ESP_LOGI(TAG, ESP_BD_ADDR_STR " FEATURE: %8s, MAP: %2u, ID: %3u, Len: %d",
                     ESP_BD_ADDR_HEX(bda),
                     esp_hid_usage_str(param->feature.usage),
                     param->feature.map_index,
                     param->feature.report_id,
                     param->feature.length);
            ESP_LOG_BUFFER_HEX(TAG, param->feature.data,
                               param->feature.length);
            break;
        }
        case ESP_HIDH_CLOSE_EVENT: {
            device_connected = false;
            update_led_status();
            const uint8_t *bda = esp_hidh_dev_bda_get(
                                        param->close.dev);
            ESP_LOGI(TAG, ESP_BD_ADDR_STR " CLOSE: %s",
                     ESP_BD_ADDR_HEX(bda),
                     esp_hidh_dev_name_get(param->close.dev));
            ESP_LOGI(TAG, "Disconnected, scheduling reconnection");
            schedule_hid_reconnect();
            break;
        }
        default:
            ESP_LOGI(TAG, "Unhandled HID event %d", event);
            break;
    }
}

static void deinit_bluetooth(void)
{
    if (!bluetooth_active) return;
    ESP_LOGW(TAG, "⚠️ Forcing Bluetooth stop without closing HID");
    esp_bt_controller_disable();
    esp_bt_controller_deinit();
    bluetooth_active = false;
    device_connected = false;
    update_led_status();
    ESP_LOGI(TAG, "Bluetooth forcibly stopped");
}

////////////////////////////////////////////////////////////////////////////////
// RelayControl + button (GPIO7) implementation
////////////////////////////////////////////////////////////////////////////////

static void configure_relay_gpio(void)
{
    gpio_config_t io_conf = {0};

    // Relay (GPIO5) as output, initialized to 0
    io_conf.mode         = GPIO_MODE_OUTPUT;
    io_conf.pin_bit_mask = (1ULL << GPIO_POWER);
    io_conf.pull_up_en   = GPIO_PULLUP_DISABLE;
    io_conf.pull_down_en = GPIO_PULLDOWN_DISABLE;
    io_conf.intr_type    = GPIO_INTR_DISABLE;
    gpio_config(&io_conf);
    gpio_set_level(GPIO_POWER, 0);

    // GPIO_CONTROL (GPIO6) as input with pull-down
    io_conf.mode         = GPIO_MODE_INPUT;
    io_conf.pin_bit_mask = (1ULL << GPIO_CONTROL);
    io_conf.pull_down_en = GPIO_PULLDOWN_ENABLE;
    io_conf.pull_up_en   = GPIO_PULLUP_DISABLE;
    io_conf.intr_type    = GPIO_INTR_DISABLE;
    gpio_config(&io_conf);

    // GPIO_BUTTON (GPIO7) as input with pull-up
    io_conf.mode         = GPIO_MODE_INPUT;
    io_conf.pin_bit_mask = (1ULL << BUTTON_GPIO);
    io_conf.pull_down_en = GPIO_PULLDOWN_DISABLE;
    io_conf.pull_up_en   = GPIO_PULLUP_ENABLE;
    io_conf.intr_type    = GPIO_INTR_DISABLE;
    gpio_config(&io_conf);
    
    // GPIO_SHUTDOWN (GPIO9) as output, initialized to 0
    io_conf.mode         = GPIO_MODE_OUTPUT;
    io_conf.pin_bit_mask = (1ULL << GPIO_SHUTDOWN);
    io_conf.pull_up_en   = GPIO_PULLUP_DISABLE;
    io_conf.pull_down_en = GPIO_PULLDOWN_DISABLE;
    io_conf.intr_type    = GPIO_INTR_DISABLE;
    gpio_config(&io_conf);
    gpio_set_level(GPIO_SHUTDOWN, 0);
    
}

static bool is_button_pressed(void)
{
    if (gpio_get_level(BUTTON_GPIO) == 0) {
        vTaskDelay(pdMS_TO_TICKS(50));
        return gpio_get_level(BUTTON_GPIO) == 0;
    }
    return false;
}

static void activate_relay_sequence(void)
{
    ESP_LOGI(TAG, "Relay ON → RPi powered on");
    gpio_set_level(GPIO_POWER, 1);
    relay_active = true;
    deinit_bluetooth();

    button_lockout = false;

    // A LOW at startup is ignored. GPIO_CONTROL must first be HIGH
    // continuously for 1 second before a later LOW can switch the relay off.
    bool rpi_was_on = false;
    int high_count = 0;
    int low_count = 0;

    while (relay_active) {
        int level = gpio_get_level(GPIO_CONTROL);
        ESP_LOGI(TAG, "GPIO_CONTROL = %d (high_count = %d, low_count = %d, armed = %d)",
                 level, high_count, low_count, rpi_was_on);

        if (!rpi_was_on) {
            low_count = 0;
            if (level == 1) {
                if (++high_count >= 2) {
                    rpi_was_on = true;
                    ESP_LOGI(TAG, "RPi power-good detected → shutdown detection armed");
                }
            } else {
                high_count = 0;
            }
        } else if (level == 0) {
            if (++low_count >= SHUTDOWN_COUNT) {
                ESP_LOGI(TAG, "RPi power-good lost → stopping relay");
                break;
            }
        } else {
            low_count = 0;
        }

        // ✅ Nouvelle condition : second appui bouton
        if (is_button_pressed()) {
            ESP_LOGI(TAG, "Second button press detected → activating GPIO_SHUTDOWN");
            gpio_set_level(GPIO_SHUTDOWN, 1); // Demande de shutdown au RPi
            vTaskDelay(pdMS_TO_TICKS(1000));  // court délai pour s'assurer de l'émission
        }

        vTaskDelay(pdMS_TO_TICKS(500));
    }

    ESP_LOGI(TAG, "Relay OFF → RPi powered off");
    gpio_set_level(GPIO_POWER, 0);
    vTaskDelay(pdMS_TO_TICKS(1000));
    ESP_LOGI(TAG, "Restarting ESP...");
    esp_restart();
}

////////////////////////////////////////////////////////////////////////////////
// HID LED Management (GPIO8)
////////////////////////////////////////////////////////////////////////////////

static void update_led_status(void)
{
    // Inverted logic: 0 = ON, 1 = OFF
    gpio_set_level(LED_GPIO, device_connected ? 0 : 1);
}


////////////////////////////////////////////////////////////////////////////////
// Task dedicated to BLE connection
////////////////////////////////////////////////////////////////////////////////

void connect_hid_task(void *param)
{
    connect_hid_device();  // previously blocking call
    vTaskDelete(NULL);     // delete the task once connection is done
}

////////////////////////////////////////////////////////////////////////////////
// Main function
////////////////////////////////////////////////////////////////////////////////

void app_main(void)
{
    esp_err_t ret;

    // 1) Initialize NVS
    ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES ||
        ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    // 2) Configure GPIO7 as input with pull-up for BLE scan on boot
    gpio_config_t io_conf_boot = {
        .pin_bit_mask = (1ULL << BUTTON_GPIO),
        .mode         = GPIO_MODE_INPUT,
        .pull_up_en   = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type    = GPIO_INTR_DISABLE,
    };
    gpio_config(&io_conf_boot);

    // 3) If GPIO7 == 0 at boot → BLE scan + NVS + reboot
    if (gpio_get_level(BUTTON_GPIO) == 0) {
        // Initialize BLE controller (not classic BT)
        ESP_ERROR_CHECK(esp_bt_controller_mem_release(ESP_BT_MODE_CLASSIC_BT));
        esp_bt_controller_config_t bt_cfg = BT_CONTROLLER_INIT_CONFIG_DEFAULT();
        ESP_ERROR_CHECK(esp_bt_controller_init(&bt_cfg));
        ESP_ERROR_CHECK(esp_bt_controller_enable(ESP_BT_MODE_BLE));
        ESP_ERROR_CHECK(esp_bluedroid_init());
        ESP_ERROR_CHECK(esp_bluedroid_enable());

        // Register BLE GAP callback
        ESP_ERROR_CHECK(esp_ble_gap_register_callback(gap_cb));

        // Start scan and store MAC, then reboot
        scan_and_save_mac();
        return; // scan_and_save_mac() reboots, does not return
    }

    // 4) Normal reboot: wait for GPIO7 release to avoid triggering relay
    while (gpio_get_level(BUTTON_GPIO) == 0) {
        vTaskDelay(pdMS_TO_TICKS(50));
    }
    vTaskDelay(pdMS_TO_TICKS(200)); // small buffer to avoid bounce

    // 5) Configure HID LED (GPIO8) as output, off by default (1)
    {
        gpio_config_t io_conf_led = {
            .pin_bit_mask = (1ULL << LED_GPIO),
            .mode         = GPIO_MODE_OUTPUT,
            .pull_up_en   = GPIO_PULLUP_DISABLE,
            .pull_down_en = GPIO_PULLDOWN_DISABLE,
            .intr_type    = GPIO_INTR_DISABLE,
        };
        gpio_config(&io_conf_led);
        gpio_set_level(LED_GPIO, 1);  // LED off (inverted logic)
    }

    // 6) Load MAC from NVS
    bool mac_loaded = load_saved_mac(target_addr);
    if (!mac_loaded) {
        ESP_LOGW(TAG, "No MAC in NVS. Entering degraded mode: button can control relay, LED will blink slowly.");
    }
    
    ESP_LOGI(TAG, "Loaded MAC: " ESP_BD_ADDR_STR,
             ESP_BD_ADDR_HEX(target_addr));

    // 7) Configure GPIO5/6/7 for relay/RPi and relay button (GPIO7)
    configure_relay_gpio();

    // 8) Initialize ADC + UART for LDR transmission
    adc_uart_init();
    xTaskCreate(adc_uart_task, "adc_uart_task", 2048, NULL, 5, NULL);

    // 9) Initialize HID Host NimBLE
    init_hid_host();

    // 10) Attempt initial connection to target_addr
    // connect_hid_device();
    xTaskCreate(connect_hid_task, "connect_hid_task", 4096, NULL, 5, NULL);

    // 11) Main loop: button (GPIO7) for relay + LED update
    while (1) {
        // Handle physical button press
        if (is_button_pressed() && !button_lockout) {
            button_lockout = true;
            ESP_LOGI(TAG, "Physical button (GPIO7) pressed");

            if (!mac_loaded) {
                ESP_LOGW(TAG, "No MAC configured → degraded mode: direct relay activation.");
            }

            if (!relay_active) {
                ESP_LOGI(TAG, "→ Activating relay");
                activate_relay_sequence();  // Starts full sequence (relay, wait for GPIO_CONTROL, etc.)
            } else {
                ESP_LOGI(TAG, "→ Relay already active: requesting RPi shutdown via GPIO10");
                gpio_set_level(GPIO_SHUTDOWN, 1);  // Activate GPIO10 (RPi shutdown)
            }
        }

        // LED blinking (state depending on MAC presence)
        if (!mac_loaded) {
            gpio_set_level(LED_GPIO, 0);  // LED on
            vTaskDelay(pdMS_TO_TICKS(300));
            gpio_set_level(LED_GPIO, 1);  // LED off
            vTaskDelay(pdMS_TO_TICKS(1700));
        } else {
            update_led_status();
            vTaskDelay(pdMS_TO_TICKS(500));
        }
    }
}