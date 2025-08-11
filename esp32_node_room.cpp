#include <Arduino.h>
#include <Wire.h>
#include <WiFi.h>
#include <PubSubClient.h>
#include "DFRobot_ENS160.h"
#include <Adafruit_AHTX0.h>

#define DEVICE_ID 1

#define SCL_PIN 22
#define SDA_PIN 21

#define BUTTON_PIN 14

#define LED_RED_PIN 19
#define LED_GREEN_PIN 18
#define LED_BLUE_PIN 5
#define CH_RED 0
#define CH_GREEN 1
#define CH_BLUE 2
#define PWM_FREQ 5000
#define PWM_BITS 8

// =============== WiFi Configuration ===============
const char *ssid = "SSIoT-02";
const char *password = "SSIoT-02";
// const char *ssid = "K70";
// const char *password = "77777777";
// const char *ssid = "VIETTEL_03012024";
// const char *password = "Wifitang345@";
// const char *ssid = "KomLab_2";
// const char *password = "komedavnlab";

// =============== MQTT Configuration ===============
const char *mqtt_server = "192.168.72.156";
// const char *mqtt_server = "broker.emqx.io";
const int mqtt_port = 1883;
const char *mqtt_topic = "sensors/bedroom";
// const char *mqtt_topic = "sensors/workingroom";

WiFiClient espClient;
PubSubClient mqttClient(espClient);

// =============== Sensor Initialization ===============
DFRobot_ENS160_I2C ens160(&Wire, 0x53); // Default I2C address 0x53
Adafruit_AHTX0 aht;

void setup_wifi()
{
  Serial.println();
  Serial.print("[WiFi] Connecting to ");
  Serial.println(ssid);

  WiFi.begin(ssid, password);
  // Auto reconnect is set true as default
  // To set auto connect off, use the following function
  //  WiFi.setAutoReconnect(false);

  // Will try for about 10 seconds (20x 500ms)
  int tryDelay = 500;
  int numberOfTries = 20;

  // Wait for the WiFi event
  while (true)
  {
    switch (WiFi.status())
    {
    case WL_NO_SSID_AVAIL:
      Serial.println("[WiFi] SSID not found");
      break;
    case WL_CONNECT_FAILED:
      Serial.print("[WiFi] Failed - WiFi not connected! Reason: ");
      return;
      break;
    case WL_CONNECTION_LOST:
      Serial.println("[WiFi] Connection was lost");
      break;
    case WL_SCAN_COMPLETED:
      Serial.println("[WiFi] Scan is completed");
      break;
    case WL_DISCONNECTED:
      Serial.println("[WiFi] WiFi is disconnected");
      break;
    case WL_CONNECTED:
      Serial.println("[WiFi] WiFi is connected!");
      Serial.print("[WiFi] IP address: ");
      Serial.println(WiFi.localIP());
      return;
      break;
    default:
      Serial.print("[WiFi] WiFi Status: ");
      Serial.println(WiFi.status());
      break;
    }
    delay(tryDelay);

    if (numberOfTries <= 0)
    {
      Serial.print("[WiFi] Failed to connect to WiFi!");
      // Use disconnect function to force stop trying to connect
      WiFi.disconnect();
      return;
    }
    else
    {
      numberOfTries--;
    }
  }
  Serial.println("");
  Serial.println("WiFi connected");
  Serial.println("IP Address: " + WiFi.localIP());
}

void reconnect()
{
  while (!mqttClient.connected())
  {
    Serial.print("Connecting to MQTT...");
    String mqttClientId = "ESP32Client-";
    mqttClientId += String(random(0xffff), HEX);

    if (mqttClient.connect(mqttClientId.c_str()))
    {
      Serial.println("Success!");
    }
    else
    {
      Serial.print("\nFailed, rc=");
      Serial.print(mqttClient.state());
      Serial.println("\nTrying again in 5s.");
      delay(5000);
    }
  }
}

void setColor(uint8_t r, uint8_t g, uint8_t b)
{
  ledcWrite(CH_RED, r);
  ledcWrite(CH_GREEN, g);
  ledcWrite(CH_BLUE, b);
}

void setup()
{
  pinMode(LED_RED_PIN, OUTPUT);
  pinMode(LED_GREEN_PIN, OUTPUT);
  pinMode(LED_BLUE_PIN, OUTPUT);
  pinMode(BUTTON_PIN, INPUT_PULLUP);
  ledcSetup(CH_RED, PWM_FREQ, PWM_BITS);
  ledcSetup(CH_GREEN, PWM_FREQ, PWM_BITS);
  ledcSetup(CH_BLUE, PWM_FREQ, PWM_BITS);
  ledcAttachPin(LED_RED_PIN, CH_RED);
  ledcAttachPin(LED_GREEN_PIN, CH_GREEN);
  ledcAttachPin(LED_BLUE_PIN, CH_BLUE);

  Serial.begin(115200);

  // Initialize I2C
  Wire.begin(SDA_PIN, SCL_PIN); // SDA = GPIO21, SCL = GPIO22

  // Initialize AHT21 sensor
  if (!aht.begin())
  {
    Serial.println("AHT21 not found! Check connection!");
    while (1)
      ;
  }
  Serial.println("AHT21 initialized successfully");

  // Initialize ENS160 sensor
  while (NO_ERR != ens160.begin())
  {
    Serial.println("ENS160 initialization failed, check connection!");
    delay(3000);
  }
  Serial.println("ENS160 initialized successfully");

  // Set operating mode
  ens160.setPWRMode(ENS160_STANDARD_MODE);
  ens160.setTempAndHum(25.0, 50.0); // Sample values, will be updated later

  setup_wifi();
  mqttClient.setServer(mqtt_server, mqtt_port);
  randomSeed(micros()); // Initialize random seed for random() function
}

unsigned long lastPublishMillis = 0;
const unsigned long publishInterval = 5000; // 5 seconds

void loop()
{
  if (millis() - lastPublishMillis >= publishInterval)
  {
    lastPublishMillis = millis();
    if (!mqttClient.connected())
    {
      reconnect();
    }
    mqttClient.loop();

    // Read data from AHT21
    sensors_event_t humidity, temp;
    aht.getEvent(&humidity, &temp);

    // Update environmental parameters for ENS160
    ens160.setTempAndHum(temp.temperature, humidity.relative_humidity);

    // Read data from ENS160
    uint8_t status = ens160.getENS160Status();
    int aqi = ens160.getAQI();
    int tvoc = ens160.getTVOC();
    int eco2 = ens160.getECO2();

    switch (aqi)
    {
    case 1:
      setColor(0, 0, 255); // Blue for excellent air quality
      break;
    case 2:
      setColor(0, 255, 0); // Green for good air quality
      break;
    case 3:
      setColor(125, 125, 0); // Yellow for moderate air quality
      break;
    case 4:
      setColor(255, 60, 0); // Orange for poor air quality
      break;
    case 5:
      setColor(255, 0, 0); // Red for very poor air quality
      break;
    default:
      setColor(0, 0, 0); // Off for unknown status
      break;
    }

    // Print results to Serial Monitor
    Serial.println("\n======= Sensor Reading =======");
    Serial.println("Device ID: " + String(DEVICE_ID));
    Serial.println("ENS160 Status: " + String(status));
    Serial.println("AQI: " + String(aqi));
    Serial.println("TVOC: " + String(tvoc) + " ppb");
    Serial.println("eCO2: " + String(eco2) + " ppm");
    Serial.printf("Temperature: %.2f°C\n", temp.temperature);
    Serial.printf("Humidity: %.2f%%\n", humidity.relative_humidity);

    // Create JSON string
    String payload = "{";
    // payload += "\"Device ID\":" + String(DEVICE_ID) + ",";
    // payload += "\"ENS160 Status\":" + String(status) + ",";
    // payload += "\"AQI\":" + String(aqi) + ",";
    // payload += "\"TVOC\":" + String(tvoc) + ",";
    // payload += "\"eCO2\":" + String(eco2) + ",";
    // payload += "\"Temperature\":" + String(temp.temperature) + ",";
    // payload += "\"Humidity\":" + String(humidity.relative_humidity);

    payload += "\"tvoc\":" + String(tvoc) + ",";
    payload += "\"temperature\":" + String(temp.temperature) + ",";
    payload += "\"humidity\":" + String(humidity.relative_humidity) + ",";
    payload += "\"eco2\":" + String(eco2) + ",";
    payload += "\"aqi\":" + String(aqi);
    payload += "}";

    // Chỉ gửi dữ liệu lên MQTT mỗi 5 giây

    // Send data to MQTT
    mqttClient.publish(mqtt_topic, payload.c_str());
    Serial.println("Data sent to MQTT: " + payload);
  }
}